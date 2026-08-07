"""Session partagee : etat, appairage, et relais temps reel entre les clients.

Le mode tablette repose sur une idee simple : la tablette n'envoie pas une
image, elle envoie le *geste*. Points horodates, pression, pinceau -- exactement
la description que `brush_engine.py` et `static/brushes.js` savent deja rejouer
a l'identique de chaque cote. Le poste rejoue ce flux dans son propre canevas au
fur et a mesure, ce qui donne trois choses d'un coup :

- l'ecran du poste montre le trace en direct, sans encoder ni streamer d'image ;
- le poste detient le trace complet a la fin, donc l'export ne change pas d'un
  iota (`renderer.py` recoit le meme JSON qu'en solo) ;
- la bande passante reste negligeable, quelques centaines d'octets par seconde
  de dessin, la ou un flux video couterait des megabits et ajouterait sa propre
  latence.

Le relais lui-meme est volontairement bete : le serveur ne modelise pas le
dessin, il fait suivre. Seul l'etat de session (qui controle quoi, ou en est le
media) est tenu ici, parce que les deux clients doivent s'accorder dessus.

Repartition des roles, imposee par l'utilisateur.ice et verifiee cote serveur
(voir `pc_only` dans app.py) : le poste garde la configuration, le choix du
rush et l'export ; la tablette dessine et pilote la lecture.
"""

import json
import os
import secrets
import socket
import threading

# Etats possibles d'une session.
#   solo    - fonctionnement historique, le poste fait tout
#   pairing - mode tablette arme, QR code affiche, personne connecte
#   live    - la tablette est connectee ; `controller` dit qui tient le stylet
SOLO, PAIRING, LIVE = "solo", "pairing", "live"

# Qui a la main. Deux notions distinctes, et c'est tout l'interet de les
# separer : `mode` decrit le *lien* (une tablette est-elle la ?), `controller`
# decrit le *controle* (qui dessine ?). Rendre la main au poste ne doit pas
# couper le lien -- sinon la tablette continue de lire la video dans son coin
# pendant que le poste a mis en pause, et les deux ecrans divergent.
PC, TABLET = "pc", "tablet"


class Session:
    """Etat partage par le poste et la tablette. Une seule instance par serveur."""

    def __init__(self):
        self._lock = threading.RLock()
        self.token = None
        self.mode = SOLO
        self.controller = PC
        # `background` et `mediaFit` decrivent ce que le canevas montre sous le
        # trace : la couleur du papier, et la facon dont le rush y est place.
        # Ils voyagent avec le format parce que la tablette doit voir le meme
        # fond et le meme cadrage que le poste -- sinon on annote une image qui
        # n'est pas a la place ou l'autre ecran l'affiche.
        self.config = {
            "width": 1920, "height": 1080, "fps": 25, "alpha": True,
            "background": "#ffffff",
            "mediaFit": {"mode": "contain", "zoom": 1.0, "posX": 0.5, "posY": 0.5},
            # Bornes IN/OUT du rush : elles disent quand le trace commence et
            # finit. Deux ecrans qui ne les partagent pas annotent le meme rush
            # sur deux minutages differents, et rien ne retombe en face a
            # l'export. `None` tant qu'aucun media n'a de duree.
            "inPoint": None, "outPoint": None,
        }
        self.media = _empty_media()
        self.revision = 0

    # ------------------------------------------------------------ appairage

    def arm(self):
        """Arme le mode tablette et retourne le jeton d'appairage."""
        with self._lock:
            if self.mode == SOLO or not self.token:
                self.token = secrets.token_urlsafe(9)
            self.mode = PAIRING
            self._touch()
            return self.token

    def end(self):
        """Rompt la session : plus de tablette, jeton invalide, retour au solo.

        C'est le seul chemin qui ferme le lien. Reprendre la main (`take`) ne
        passe pas par la : le poste redevient maitre du stylet, mais la tablette
        reste connectee et suit ce qu'il fait.
        """
        with self._lock:
            self.mode = SOLO
            self.controller = PC
            self.token = None
            self._touch()

    # Nom historique de `end()`, conserve pour les appelants existants.
    disarm = end

    def take(self):
        """Le poste reprend le stylet ; la tablette reste connectee, en suiveuse."""
        with self._lock:
            self.controller = PC
            self._touch()
            return self.controller

    def give(self):
        """Le poste rend le stylet a la tablette."""
        with self._lock:
            if self.mode == LIVE:
                self.controller = TABLET
                self._touch()
            return self.controller

    def accept(self, token):
        """Un client se presente avec un jeton. Vrai s'il est admis."""
        with self._lock:
            return bool(self.token) and secrets.compare_digest(str(token), self.token)

    def tablet_joined(self):
        """Une tablette arrive : elle prend la main, c'est ce pour quoi on l'a
        appairee. Le poste la lui reprendra explicitement s'il le souhaite."""
        with self._lock:
            if self.mode == PAIRING:
                self.mode = LIVE
                self.controller = TABLET
                self._touch()

    def tablet_left(self):
        with self._lock:
            if self.mode == LIVE:
                self.mode = PAIRING
                # Personne au bout du fil : le stylet revient au poste, sinon
                # plus rien ne serait dessinable nulle part.
                self.controller = PC
                self._touch()

    # -------------------------------------------------------- configuration

    def set_config(self, config):
        with self._lock:
            for key in ("width", "height", "fps"):
                if config.get(key):
                    self.config[key] = int(config[key])
            if "alpha" in config:
                self.config["alpha"] = bool(config["alpha"])
            if config.get("background"):
                self.config["background"] = str(config["background"])[:32]
            if isinstance(config.get("mediaFit"), dict):
                self.config["mediaFit"] = _clean_media_fit(config["mediaFit"])
            self._update_range(config)
            self._touch()
            return dict(self.config)

    def _update_range(self, config):
        """Bornes IN/OUT, refusees plutot que corrigees si elles sont absurdes.

        Une borne de sortie avant l'entree, ou un temps negatif, decrirait un
        intervalle vide : le rush ne jouerait pas du tout chez celui qui les
        recoit, sans que rien ne l'explique. Mieux vaut garder les precedentes.
        """
        if "inPoint" not in config and "outPoint" not in config:
            return
        try:
            start = float(config.get("inPoint"))
            end = float(config.get("outPoint"))
        except (TypeError, ValueError):
            return
        if start < 0 or end <= start:
            return
        self.config["inPoint"] = start
        self.config["outPoint"] = end

    # ----------------------------------------------------------------- media

    def set_media(self, media):
        """Publie un media, complete des valeurs par defaut manquantes.

        Le remplissage compte : `_empty_media` decrit la forme sur laquelle les
        clients s'appuient, et un appelant qui omet une cle publierait sinon un
        media auquel elle manque -- avec un `undefined` cote navigateur au lieu
        d'une valeur neutre.
        """
        with self._lock:
            self.media = dict(_empty_media(), **media)
            self._touch()
            return dict(self.media)

    def update_media(self, **fields):
        """Met a jour le media *courant* seulement.

        Le garde-fou `id` compte : preparer un rush prend du temps, et
        l'utilisateur.ice peut en choisir un autre entre-temps. Sans lui, la
        progression du transcodage abandonne viendrait ecraser l'etat du
        nouveau media.
        """
        media_id = fields.pop("_for", None)
        with self._lock:
            if media_id is not None and self.media.get("id") != media_id:
                return None
            self.media.update(fields)
            self._touch()
            return dict(self.media)

    def clear_media(self):
        with self._lock:
            self.media = _empty_media()
            self._touch()
            return dict(self.media)

    # ------------------------------------------------------------- lecture

    def _touch(self):
        self.revision += 1

    def snapshot(self):
        with self._lock:
            return {
                "mode": self.mode,
                "controller": self.controller,
                "revision": self.revision,
                "paired": self.mode == LIVE,
                "config": dict(self.config),
                "media": dict(self.media),
            }


def _clean_media_fit(fit):
    """Cadrage du rush, borne avant d'etre rediffuse.

    Le poste est la seule source de ces valeurs, mais elles ressortent telles
    quelles vers la tablette : un zoom nul ou une position hors bornes y
    donnerait une geometrie que le navigateur ne sait pas dessiner, et l'ecran
    resterait vide sans rien signaler.
    """
    def _fraction(key):
        try:
            return min(1.0, max(0.0, float(fit.get(key, 0.5))))
        except (TypeError, ValueError):
            return 0.5

    try:
        zoom = float(fit.get("zoom", 1.0))
    except (TypeError, ValueError):
        zoom = 1.0
    return {
        "mode": "crop" if fit.get("mode") == "crop" else "contain",
        "zoom": min(4.0, max(1.0, zoom)),
        "posX": _fraction("posX"),
        "posY": _fraction("posY"),
    }


def _empty_media():
    return {
        "id": None,
        "state": "empty",     # empty | preparing | ready | error
        "kind": None,         # video | audio | image
        "name": "",
        "url": None,
        "progress": 0.0,
        "error": None,
        "proxied": False,
        "reason": "",
        "width": 0,
        "height": 0,
        "duration": 0.0,
        "fps": 0.0,
        # Servent a l'export : une couche « media » reprend la piste son du rush
        # quand il en a une -- ce qui permet de verifier a l'oreille qu'elle
        # tombe au bon endroit dans le montage -- et un media qui n'est *que*
        # du son devient a lui seul une couche WAV, livree a la definition et a
        # la cadence d'echantillonnage de la source.
        "hasAudio": False,
        "audioCodec": "",
        "audioRate": 0,
        "audioBits": 0,
        "sourcePath": None,
        # PDF seulement. La page fait partie de l'identite du media au meme
        # titre que le chemin : rouvrir le meme fichier a une autre page est un
        # autre media, avec un autre proxy et d'autres dimensions.
        "page": None,         # numero en base 1, None hors PDF
        "pageCount": 0,       # 0 hors PDF
        "documentName": None, # nom du PDF sans le suffixe de page
    }


# --------------------------------------------------------------------------
# Relais temps reel
# --------------------------------------------------------------------------

class Hub:
    """Aiguillage des messages WebSocket entre le poste et la tablette.

    Volontairement sans file d'attente ni historique : un point de trace qui
    n'a pas pu partir n'a plus d'interet une seconde plus tard, et faire
    patienter tout le monde derriere un client lent transformerait un aleas
    reseau en blocage general. Un envoi qui echoue retire simplement le pair.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._peers = {}          # id -> (role, connection)
        self._next_id = 1

    def add(self, role, connection):
        with self._lock:
            peer_id = self._next_id
            self._next_id += 1
            self._peers[peer_id] = (role, connection)
            return peer_id

    def remove(self, peer_id):
        with self._lock:
            self._peers.pop(peer_id, None)

    def roles(self):
        with self._lock:
            return sorted({role for role, _ in self._peers.values()})

    def count(self, role=None):
        with self._lock:
            return sum(1 for item_role, _ in self._peers.values()
                       if role is None or item_role == role)

    def drop(self, role):
        """Ferme les connexions d'un role.

        Utilise quand le poste reprend la main : prevenir la tablette ne suffit
        pas, il faut aussi couper le canal. Une connexion laissee ouverte
        continuerait de repondre aux pings, et la tablette croirait la session
        toujours vivante.
        """
        with self._lock:
            doomed = [(peer_id, connection)
                      for peer_id, (item_role, connection) in self._peers.items()
                      if item_role == role]
            for peer_id, _ in doomed:
                self._peers.pop(peer_id, None)
        for _, connection in doomed:
            try:
                connection.close()
            except Exception:      # noqa: BLE001 - deja tombee, rien a faire
                pass
        return len(doomed)

    def send(self, message, to=None, exclude=None):
        """Diffuse un message. `to` limite a un role, `exclude` saute un pair."""
        payload = message if isinstance(message, str) else json.dumps(message)
        with self._lock:
            targets = [(peer_id, connection)
                       for peer_id, (role, connection) in self._peers.items()
                       if (to is None or role == to) and peer_id != exclude]
        dead = []
        for peer_id, connection in targets:
            try:
                connection.send(payload)
            except Exception:      # noqa: BLE001 - pair injoignable, on l'oublie
                dead.append(peer_id)
        if dead:
            with self._lock:
                for peer_id in dead:
                    self._peers.pop(peer_id, None)


# --------------------------------------------------------------------------
# Adresses reseau et QR code
# --------------------------------------------------------------------------

def lan_addresses():
    """Adresses IPv4 par lesquelles la tablette peut joindre ce poste.

    La premiere est celle de l'interface reellement utilisee pour sortir --
    c'est presque toujours la bonne, y compris quand la machine a aussi un
    Docker, un VPN ou un adaptateur virtuel qui repondent a l'appel.
    """
    found = []

    primary = _primary_address()
    if primary:
        found.append(primary)

    try:
        for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = entry[4][0]
            if _usable(address) and address not in found:
                found.append(address)
    except OSError:
        pass

    return found


def _primary_address():
    """Aucun paquet n'est emis : un socket UDP « connecte » se contente de
    demander au systeme quelle interface il choisirait pour cette destination."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("198.51.100.1", 53))   # TEST-NET-2, jamais routee
        address = probe.getsockname()[0]
        return address if _usable(address) else None
    except OSError:
        return None
    finally:
        probe.close()


def _usable(address):
    if not address or address.startswith("127.") or address.startswith("169.254."):
        return False
    return ":" not in address


def qr_svg(text, quiet_zone=2):
    """QR code en SVG, sans dependance de rendu.

    Un SVG plutot qu'un PNG parce qu'il reste net a n'importe quelle taille --
    et un QR flou est un QR qui ne se scanne pas.
    """
    import qrcode

    code = qrcode.QRCode(border=quiet_zone, error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.add_data(text)
    code.make(fit=True)
    matrix = code.get_matrix()
    size = len(matrix)

    # Un seul <path> plutot qu'un millier de <rect> : le SVG passe de ~80 ko a
    # quelques ko, ce qui compte pour un contenu insere en ligne dans la page.
    parts = []
    for y, row in enumerate(matrix):
        x = 0
        while x < size:
            if not row[x]:
                x += 1
                continue
            start = x
            while x < size and row[x]:
                x += 1
            parts.append("M%d %dh%dv1h-%dz" % (start, y, x - start, x - start))

    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
        'shape-rendering="crispEdges" role="img" aria-label="QR code d\'appairage">'
        '<rect width="%d" height="%d" fill="#ffffff"/>'
        '<path fill="#131313" d="%s"/></svg>'
    ) % (size, size, size, size, "".join(parts))


# --------------------------------------------------------------------------

def is_loopback(address):
    """Le client est-il sur la machine du serveur ?

    Sert a reserver au poste les routes qui touchent au disque (selecteurs de
    fichiers, export). Une fois le serveur ouvert sur le reseau local, cette
    frontiere est ce qui empeche un autre appareil du reseau de declencher un
    export ou d'ouvrir une boite de dialogue sur l'ecran du poste.
    """
    if not address:
        return False
    if address in ("127.0.0.1", "::1", "localhost"):
        return True
    if address.startswith("127."):
        return True
    # Adresse IPv4 presentee sous forme mappee IPv6 par certaines piles reseau.
    if address.startswith("::ffff:"):
        return is_loopback(address[7:])
    return False


def public_host_from(request_host):
    """Hote a inscrire dans l'URL d'appairage.

    On garde ce que le navigateur du poste a effectivement tape quand c'est
    deja une adresse reseau : si l'utilisateur.ice est passe par un nom mDNS
    (« macbook.local »), le reproduire evite de le remplacer par une IP qui
    changera au prochain bail DHCP.
    """
    host = (request_host or "").split(":")[0]
    if host and not is_loopback(host) and host != "0.0.0.0":
        return host
    addresses = lan_addresses()
    return addresses[0] if addresses else None


def env_flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
