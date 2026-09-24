"""live_notes — serveur local.

Le navigateur ne rasterise plus rien pour l'export : il transmet la description
des traces (points horodates + pinceau) et le rendu final est entierement
calcule ici, puis pousse dans FFmpeg (voir `renderer.py`).

Le serveur ecoute aussi sur le reseau local, pour le mode tablette : une
tablette rejoint la session en cours, prend la main sur le dessin, et le poste
la regarde travailler en direct (voir `session.py`). Les routes qui touchent au
disque -- selecteurs de fichiers, export -- restent reservees au poste par
`@pc_only`, meme quand le serveur est ouvert sur le reseau.
"""

import datetime
import io
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import traceback
import uuid
from functools import wraps

from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from flask_sock import Sock

import brush_engine as be
import nativedialog
import paths
import pdfdoc
import proxy
import renderer
import session as sessions

# Port d'ecoute par defaut. Ce n'est deliberement plus 5000, le port
# historique de Flask : depuis macOS Monterey, le recepteur AirPlay l'occupe
# des l'ouverture de session, le serveur ne peut pas s'y lier et
# l'application n'affiche qu'une page blanche. 7341 n'est attribue par l'IANA
# a aucun service et ne croise aucun port de developpement usuel.
DEFAULT_PORT = 7341

# Combien de ports consecutifs essayer avant de laisser le systeme en attribuer
# un au hasard. Suffisant pour plusieurs instances ouvertes en meme temps.
PORT_ATTEMPTS = 20

HERE = paths.resource_dir()
EXPORT_DIR = paths.default_export_dir()
USER_PRESETS_PATH = os.path.join(paths.user_data_dir(), "user_presets.json")
LAST_MEDIA_DIR_PATH = os.path.join(paths.user_data_dir(), "last_media_dir.txt")

app = Flask(__name__, root_path=HERE, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024
# Sans cela, une connexion WebSocket ouverte pendant des heures finirait par
# etre coupee par le lecteur de trames de simple-websocket.
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}

sock = Sock(app)

SESSION = sessions.Session()
HUB = sessions.Hub()

_jobs = {}
_jobs_lock = threading.Lock()
_presets_lock = threading.Lock()


# --------------------------------------------------------------------------
# Frontiere poste / reseau
# --------------------------------------------------------------------------

def _from_pc():
    return sessions.is_loopback(request.remote_addr)


def _authorized():
    """Le poste (boucle locale) ou un client porteur du jeton de session."""
    if _from_pc():
        return True
    token = request.args.get("k") or request.headers.get("X-Live-Notes-Token")
    return SESSION.accept(token)


def pc_only(view):
    """Reserve une route a la machine qui heberge le serveur.

    Ouvrir le serveur au reseau local rend joignables des routes qui ecrivent
    sur le disque ou ouvrent une fenetre sur l'ecran du poste. La tablette n'en
    a aucun besoin : elle dessine, elle ne configure ni n'exporte.
    """
    @wraps(view)
    def guard(*args, **kwargs):
        if not _from_pc():
            return jsonify({"error": "Route reservee au poste de travail."}), 403
        return view(*args, **kwargs)
    return guard


def broadcast_state():
    HUB.send(dict(SESSION.snapshot(), t="state"))


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.route("/tablet")
def tablet():
    """Meme page que le poste.

    Le mode est deduit du chemin cote client (voir `static/remote.js`) : c'est
    ce qui garantit que la tablette dessine avec exactement le meme moteur de
    pinceaux, donc que ce qui apparait sous le stylet est bien ce que le poste
    affiche et ce que l'export produira.
    """
    return send_from_directory(HERE, "index.html")


@app.route("/favicon.png")
def favicon():
    # Reference par index.html (<link rel="icon">). Genere par
    # packaging/make_icon.py et embarque a cote d'index.html dans les builds
    # figes (voir packaging/live_notes.spec) ; absent en dev tant que le
    # script n'a pas ete lance localement.
    return send_from_directory(HERE, "favicon.png")


# --------------------------------------------------------------------------
# Bibliotheque de pinceaux
# --------------------------------------------------------------------------

def _read_user_presets():
    if not os.path.exists(USER_PRESETS_PATH):
        return []
    try:
        with open(USER_PRESETS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_user_presets(presets):
    tmp = USER_PRESETS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(presets, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, USER_PRESETS_PATH)


def _read_last_media_dir():
    """Le dernier dossier ou l'utilisateur.ice a choisi un media, ou None.

    Un dossier qui n'existe plus (rush deplace, disque externe retire) n'est
    pas propose tel quel : le selecteur s'ouvrirait sur du vide sans rien
    expliquer, alors que le repli sur la bibliotheque Videos reste un repere
    connu."""
    try:
        with open(LAST_MEDIA_DIR_PATH, "r", encoding="utf-8") as fh:
            directory = fh.read().strip()
    except OSError:
        return None
    return directory if directory and os.path.isdir(directory) else None


def _write_last_media_dir(directory):
    tmp = LAST_MEDIA_DIR_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(directory)
    os.replace(tmp, LAST_MEDIA_DIR_PATH)


def _media_pick_initial_dir():
    """Dossier propose par defaut au selecteur de media.

    Le dernier dossier utilise si on en a un encore valide ; sinon le dossier
    live_notes de la bibliotheque Videos/Films -- le seul repere qui ait un
    sens avant le tout premier choix."""
    return _read_last_media_dir() or EXPORT_DIR


@app.route("/api/brushes")
def api_brushes():
    library = be.load_library()
    return jsonify({
        "defaults": library["defaults"],
        "presets": library["presets"],
        "userPresets": _read_user_presets(),
    })


@app.route("/api/presets", methods=["POST"])
def api_save_preset():
    preset = request.get_json(silent=True) or {}
    name = (preset.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Nom de preset manquant."}), 400

    preset = dict(preset)
    preset["name"] = name
    preset.setdefault("id", "user-%s" % uuid.uuid4().hex[:8])

    with _presets_lock:
        presets = _read_user_presets()
        presets = [p for p in presets if p.get("id") != preset["id"]]
        presets.append(preset)
        _write_user_presets(presets)

    return jsonify({"preset": preset, "userPresets": presets})


@app.route("/api/presets/<preset_id>", methods=["DELETE"])
def api_delete_preset(preset_id):
    with _presets_lock:
        presets = [p for p in _read_user_presets() if p.get("id") != preset_id]
        _write_user_presets(presets)
    return jsonify({"userPresets": presets})


# --------------------------------------------------------------------------
# Session partagee et appairage de la tablette
# --------------------------------------------------------------------------

def _pairing_payload():
    host = sessions.public_host_from(request.host)
    token = SESSION.token
    if not host or not token:
        return None
    port = (request.host.split(":")[-1] if ":" in request.host
            else str(DEFAULT_PORT))
    url = "http://%s:%s/tablet#%s" % (host, port, token)
    return {"url": url, "host": host, "token": token,
            "addresses": sessions.lan_addresses()}


@app.route("/api/session")
def api_session():
    if not _authorized():
        return jsonify({"error": "Session non appairee."}), 403
    payload = SESSION.snapshot()
    payload["tablets"] = HUB.count("tablet")
    if _from_pc():
        payload["pairing"] = _pairing_payload()
    return jsonify(payload)


@app.route("/api/session/tablet", methods=["POST"])
@pc_only
def api_session_tablet():
    """Pilote le mode tablette. Quatre actions, et une distinction qui compte.

    - `arm`  : arme l'appairage (QR code) ;
    - `take` : le poste reprend le stylet, **sans couper le lien** — la tablette
      reste connectee et suit ce que fait le poste ;
    - `give` : le poste rend le stylet a la tablette ;
    - `end`  : rompt la session — jeton invalide, tablette deconnectee, ecran
      « session terminee » de son cote.

    `take` et `end` etaient autrefois la meme chose, et c'etait le defaut : une
    tablette ejectee gardait sa video en lecture pendant que le poste avait mis
    en pause, faute de canal pour l'apprendre.
    """
    payload = request.get_json(silent=True) or {}
    action = payload.get("action")
    if not action:
        # Forme historique : {"enabled": true|false}.
        action = "arm" if payload.get("enabled") else "end"

    if action == "arm":
        SESSION.arm()
        # Le seuil de debit ne s'applique qu'a partir d'ici : un rush trop
        # lourd pour le Wi-Fi etait servi tel quel au poste, il lui faut
        # maintenant une copie allegee. Sans cette reevaluation, la tablette
        # heriterait du fichier d'origine.
        _reevaluate_media_for_tablet()
    elif action == "take":
        SESSION.take()
    elif action == "give":
        SESSION.give()
    elif action == "end":
        SESSION.end()
        # Prevenir puis couper : l'ordre compte, un canal ferme avant le
        # message laisserait la tablette sans explication a l'ecran.
        HUB.send({"t": "ended", "reason": "La session a été terminée depuis le poste."},
                 to="tablet")
        HUB.drop("tablet")
    else:
        return jsonify({"error": "Action inconnue : %s" % action}), 400

    broadcast_state()
    result = SESSION.snapshot()
    result["pairing"] = _pairing_payload()
    return jsonify(result)


@app.route("/api/session/qr.svg")
@pc_only
def api_session_qr():
    pairing = _pairing_payload()
    if not pairing:
        return jsonify({"error": "Aucune adresse reseau exploitable sur ce poste."}), 409
    return Response(sessions.qr_svg(pairing["url"]), mimetype="image/svg+xml")


@app.route("/api/session/config", methods=["POST"])
@pc_only
def api_session_config():
    """Le poste publie le format de travail ; la tablette s'y conforme.

    Les traces voyagent en coordonnees canevas : tant que les deux cotes
    partagent la meme taille de zone de dessin, un point vaut le meme point de
    part et d'autre, quelle que soit la taille reelle des ecrans.
    """
    config = SESSION.set_config(request.get_json(silent=True) or {})
    broadcast_state()
    return jsonify({"config": config})


# --------------------------------------------------------------------------
# Media : selection, preparation du proxy, service
# --------------------------------------------------------------------------

_media_lock = threading.Lock()
_media_cancel = threading.Event()

# PDF choisi mais dont la page n'est pas encore arretee. Un seul emplacement :
# choisir un autre fichier remplace le precedent, et rien ne survit au
# redemarrage -- c'est un sas, pas un etat a conserver.
_pending_pdf = None

# Definition des apercus de la fenetre de choix de page. Sans rapport avec la
# rasterisation finale, qui suit le canevas : ici on ne fait que montrer de
# quelle page il s'agit, dans une vignette de quelques centaines de pixels.
PDF_PREVIEW_EDGE = 1000

@app.route("/api/media/pick", methods=["POST"])
@pc_only
def api_media_pick():
    """Selecteur de fichier natif, cote serveur.

    Un <input type=file> ne donne au navigateur qu'un blob anonyme : impossible
    d'en transcoder un proxy, puisque Python n'a jamais le chemin du fichier.
    Le detour par une boite de dialogue native est ce qui rend possible la
    prise en charge de n'importe quel codec. Le detail de l'ouverture -- et ce
    qu'elle doit a la compilation -- est dans nativedialog.py.
    """
    try:
        path = nativedialog.ask_open_file(_media_pick_initial_dir())
    except (nativedialog.PickerError, OSError, subprocess.SubprocessError) as exc:
        return jsonify({"error": "Selecteur de fichier indisponible (%s)." % exc}), 500

    if not path:
        return jsonify({"cancelled": True})

    # Retenu pour le prochain choix -- avant meme de savoir si c'est un PDF ou
    # non, puisque c'est le dossier parcouru qui compte, pas le type retenu.
    _write_last_media_dir(os.path.dirname(path))

    # Un PDF ne s'ouvre pas tout de suite : il faut d'abord savoir quelle page.
    # On decrit le document, on le met de cote, et le client revient par
    # /api/media/open une fois la page choisie.
    if pdfdoc.is_pdf(path):
        try:
            described = pdfdoc.describe(path)
        except pdfdoc.PdfError as exc:
            media = SESSION.set_media(dict(_media_error(path, str(exc))))
            broadcast_state()
            return jsonify({"media": media}), 400
        with _media_lock:
            globals()["_pending_pdf"] = path
        return jsonify({"pdf": {"name": os.path.basename(path),
                                "pageCount": described["pageCount"],
                                "page": 1}})

    return _open_media(path)


@app.route("/api/media/open", methods=["POST"])
@pc_only
def api_media_open():
    payload = request.get_json(silent=True) or {}
    path = (payload.get("path") or "").strip()
    if path:
        path = os.path.abspath(os.path.expanduser(path))
    else:
        # Choix d'une page : le chemin du PDF est deja connu du serveur, le
        # client ne designe que le numero. Rien d'arbitraire ne transite, et le
        # navigateur n'a jamais eu besoin de connaitre le chemin.
        path = _pdf_in_play()
    if not path:
        return jsonify({"error": "Chemin de media manquant."}), 400
    return _open_media(path, page=payload.get("page"))


@app.route("/api/pdf/page/<int:page>.png")
@pc_only
def api_pdf_page(page):
    """Apercu d'une page, pour la fenetre de choix.

    Deux chemins seulement sont rendus : le PDF en attente de choix, et la
    source du media courant si c'en est un. Aucun chemin n'est accepte en
    entree -- ce n'est pas une route de rendu generique, et rien ne doit
    permettre de faire rasteriser un fichier arbitraire du disque.
    """
    path = _pdf_in_play()
    if not path:
        return jsonify({"error": "Aucun PDF en cours."}), 409
    stream = io.BytesIO()
    try:
        pdfdoc.render(path, page, PDF_PREVIEW_EDGE, stream)
    except pdfdoc.PdfError as exc:
        return jsonify({"error": str(exc)}), 400
    stream.seek(0)
    return send_file(stream, mimetype="image/png")


def _pdf_in_play():
    """Le PDF en attente de choix, ou celui du media affiche."""
    pending = globals().get("_pending_pdf")
    if pending and pdfdoc.is_pdf(pending):
        return pending
    source = SESSION.snapshot()["media"].get("sourcePath")
    return source if source and pdfdoc.is_pdf(source) else None


@app.route("/api/media/reopen", methods=["POST"])
@pc_only
def api_media_reopen():
    """Refabrique le proxy d'un media libere apres un export."""
    source = SESSION.snapshot()["media"].get("sourcePath")
    if not source:
        return jsonify({"error": "Aucun media a rouvrir."}), 409
    return _open_media(source, page=_current_page())


@app.route("/api/media", methods=["DELETE"])
@pc_only
def api_media_clear():
    _media_cancel.set()
    media = SESSION.clear_media()
    broadcast_state()
    return jsonify({"media": media})


@app.route("/api/media/cancel", methods=["POST"])
@pc_only
def api_media_cancel():
    """Interrompt une preparation de proxy en cours.

    Transcoder un rush long prend plusieurs minutes, pendant lesquelles
    l'interface est bloquee par le voile de progression : s'etre trompe de
    fichier condamnait a attendre la fin. Le drapeau d'annulation existait
    deja -- `proxy.build` le consulte a chaque image -- mais rien ne permettait
    de le lever depuis l'ecran.

    Le rush reste en place, servi tel quel. Le retirer aurait ete plus simple,
    mais renvoyait a l'ecran de choix quelqu'un qui voulait seulement se passer
    de la copie de lecture : la presomption d'illisibilite peut se tromper, et
    c'est justement le sens de « Servir l'original ». Si le navigateur ne le
    decode pas, l'image restera noire -- avec le bouton « Transcoder ce rush »
    a portee pour revenir en arriere.
    """
    media = SESSION.snapshot()["media"]
    if media.get("state") != "preparing":
        return jsonify({"media": media, "cancelled": False})
    source = media.get("sourcePath")
    _media_cancel.set()
    if not source:
        media = SESSION.clear_media()
        broadcast_state()
        return jsonify({"media": media, "cancelled": True})
    response = _open_media(source, bypass=True, page=media.get("page"))
    return response


def _canvas_edge():
    """Bord long de rasterisation d'une page : celui du canevas de trace.

    Contrairement a un rush, dont le proxy n'est qu'une visee degradee, la page
    rasterisee est la seule version qui existe -- la plafonner a `MAX_EDGE`
    rendrait le texte mou des qu'on recadre.
    """
    config = SESSION.snapshot()["config"]
    return pdfdoc.target_edge(config.get("width"), config.get("height"))


def _current_page():
    """Page du media courant, pour les rappels qui ne connaissent qu'un chemin.

    `reopen`, `force`, `bypass` et le passage en mode tablette rouvrent le
    media a partir de son seul chemin : sans cela, ils retomberaient tous
    silencieusement sur la page 1.
    """
    return SESSION.snapshot()["media"].get("page")


def _open_media(path, force=False, bypass=False, page=None):
    """Analyse le media, decide s'il faut un proxy, et lance la preparation.

    Le mode de session entre dans la decision : le seuil de debit ne vaut que
    pour la liaison de la tablette (voir proxy.plan). Un rush H.264 lourd est
    donc servi tel quel tant qu'on travaille au poste, et prepare au moment ou
    le mode tablette est arme.

    `force` et `bypass` sont les deux corrections manuelles de cette decision,
    l'une dans chaque sens. Elles voyagent jusqu'au client dans l'etat du media
    pour qu'il sache ne pas defaire ce qu'on vient de lui demander -- le repli
    automatique se tait apres un `bypass`.

    `page` ne concerne que les PDF : c'est la page a rasteriser.
    """
    with _media_lock:
        # Une preparation deja en cours devient caduque des qu'un autre rush
        # est choisi : on la coupe avant de publier le nouvel etat.
        _media_cancel.set()
        cancel = threading.Event()
        globals()["_media_cancel"] = cancel

        for_tablet = SESSION.snapshot()["mode"] != sessions.SOLO
        try:
            info = proxy.probe(path, page=page, long_edge=_canvas_edge())
            needed, reason, dimensions = proxy.plan(
                info, for_tablet=for_tablet, force=force, bypass=bypass)
        except proxy.ProxyError as exc:
            media = SESSION.set_media(dict(_media_error(path, str(exc))))
            broadcast_state()
            return jsonify({"media": media}), 400

        # Un proxy deja fabrique pour cette source evite de refaire plusieurs
        # minutes de transcodage : c'est toute la raison d'etre du cache.
        cached = proxy.find_cached(info, forced=force) if needed else None

        media_id = uuid.uuid4().hex[:12]
        media = SESSION.set_media({
            "id": media_id,
            "state": "ready" if (not needed or cached) else "preparing",
            "kind": info["kind"],
            "name": info["name"],
            "url": _media_url(media_id),
            "progress": 100.0 if (not needed or cached) else 0.0,
            "error": None,
            "proxied": needed,
            "reason": reason,
            "cached": bool(cached),
            "bypassed": bool(bypass and not force),
            "width": info["width"],
            "height": info["height"],
            "duration": info["duration"],
            "fps": info["fps"],
            "hasAudio": info["hasAudio"],
            "audioCodec": info["audioCodec"],
            "audioRate": info["audioRate"],
            "audioBits": info["audioBits"],
            "bitrate": info["bitrate"],
            "pixFmt": info["pixFmt"],
            "sourcePath": path,
            "sourceCodec": info["videoCodec"] or info["audioCodec"],
            "servedPath": cached or (None if needed else path),
            "page": info.get("pdfPage"),
            "pageCount": info.get("pdfPageCount") or 0,
            "documentName": info.get("pdfName"),
        })
        # Le sas a joue son role : la page est ouverte, et c'est desormais le
        # media courant qui porte le PDF. Le garder ferait repondre l'apercu
        # sur l'ancien document apres un changement de media.
        globals()["_pending_pdf"] = None
        broadcast_state()

        if needed and not cached:
            worker = threading.Thread(target=_build_proxy,
                                      args=(media_id, info, dimensions, reason, force, cancel),
                                      daemon=True)
            worker.start()

    return jsonify({"media": media})


def _build_proxy(media_id, info, dimensions, reason, forced, cancel):
    last = [0.0]

    def progress(percent):
        # Le transcodage emet une mise a jour par image encodee : les diffuser
        # toutes noierait le canal sous des messages sans interet.
        if percent - last[0] < 1.0 and percent < 100.0:
            return
        last[0] = percent
        if SESSION.update_media(_for=media_id, progress=round(percent, 1)):
            broadcast_state()

    try:
        produced = proxy.build(info, dimensions, progress=progress, cancelled=cancel.is_set)
    except proxy.ProxyError as exc:
        if not cancel.is_set():
            SESSION.update_media(_for=media_id, state="error", error=str(exc), progress=0.0)
            broadcast_state()
        return

    if cancel.is_set():
        return
    # Inscrit au journal avant d'etre publie : meme si le media change dans la
    # seconde, le travail reste retrouvable au lieu de dormir anonymement dans
    # le dossier de cache.
    proxy.register(info, produced, reason, forced=forced)
    if SESSION.update_media(_for=media_id, state="ready", progress=100.0,
                            servedPath=produced) is None:
        return
    broadcast_state()


def _reevaluate_media_for_tablet():
    """Rouvre le media courant maintenant que la tablette entre en jeu.

    Sans effet quand il est deja servi par un proxy, ou quand ses
    caracteristiques passent le seuil reseau : `_open_media` refait simplement
    le meme constat, et le cache lui rend le meme fichier.
    """
    media = SESSION.snapshot()["media"]
    source = media.get("sourcePath")
    if not source or media.get("state") == "error":
        return
    try:
        # « Servir l'original » est un choix explicite : l'arrivee de la tablette
        # n'a pas a le defaire dans le dos de qui vient de le prendre.
        _open_media(source, bypass=bool(media.get("bypassed")),
                    page=media.get("page"))
    except Exception:      # noqa: BLE001 - l'appairage ne doit pas echouer pour ca
        traceback.print_exc()


def _media_url(media_id):
    return "/api/media/file/%s" % media_id


def _media_error(path, message):
    empty = dict(SESSION.snapshot()["media"])
    empty.update({
        "id": None, "state": "error", "error": message,
        "name": os.path.basename(path), "sourcePath": path,
        "url": None, "progress": 0.0, "kind": None,
    })
    return empty


# --------------------------------------------------------------------------
# Gestion du cache de proxys
# --------------------------------------------------------------------------

@app.route("/api/proxies")
@pc_only
def api_proxies():
    listing = proxy.entries()
    current = SESSION.snapshot()["media"]
    served = os.path.basename(current.get("servedPath") or "")
    for item in listing:
        # Marque le proxy du rush affiche : le supprimer coupe l'image en
        # cours, l'interface doit pouvoir le dire avant.
        item["inUse"] = item["file"] == served
    return jsonify({
        "entries": listing,
        "total": sum(item["size"] for item in listing),
        "directory": proxy.cache_dir(),
        "current": {
            "name": current.get("name") or "",
            "proxied": bool(current.get("proxied")),
            "hasSource": bool(current.get("sourcePath")),
            # Y a-t-il un proxy a *reprendre*, ou un transcodage a *lancer* ?
            # L'interface propose « servir le proxy » dans le premier cas et
            # « transcoder ce rush » dans le second : ce ne sont pas la meme
            # attente.
            "hasProxy": proxy.cached_for_source(current.get("sourcePath")),
        },
    })


@app.route("/api/proxies/<path:file_name>", methods=["DELETE"])
@pc_only
def api_proxy_delete(file_name):
    served = SESSION.snapshot()["media"].get("servedPath") or ""
    try:
        gone = proxy.remove(file_name)
    except proxy.ProxyError as exc:
        return jsonify({"error": str(exc)}), 400
    if gone and os.path.basename(served) == file_name:
        # Le lecteur des clients pointe sur un fichier qui n'existe plus.
        SESSION.update_media(state="released", url=None, servedPath=None, progress=0.0)
        broadcast_state()
    return jsonify({"deleted": gone, "total": proxy.total_size()})


@app.route("/api/proxies", methods=["DELETE"])
@pc_only
def api_proxies_purge():
    served = SESSION.snapshot()["media"].get("servedPath") or ""
    removed, kept = proxy.purge()
    if served and not os.path.exists(served):
        SESSION.update_media(state="released", url=None, servedPath=None, progress=0.0)
        broadcast_state()
    return jsonify({"removed": removed, "kept": kept, "total": proxy.total_size()})


@app.route("/api/proxies/force", methods=["POST"])
@pc_only
def api_proxy_force():
    """Fabrique une copie de lecture du rush courant, meme s'il paraissait bon.

    Le recours quand l'aperçu reste noir : certains navigateurs annoncent un
    codec qu'ils ne decodent pas vraiment (HEVC sans support materiel), et
    aucune analyse de fichier ne peut le deviner a leur place.
    """
    source = SESSION.snapshot()["media"].get("sourcePath")
    if not source:
        return jsonify({"error": "Aucun media ouvert."}), 409
    return _open_media(source, force=True, page=_current_page())


@app.route("/api/proxies/bypass", methods=["POST"])
@pc_only
def api_proxy_bypass():
    """Sert le rush courant tel quel, malgre l'analyse.

    Le pendant exact de `force`. L'analyse ne fait que presumer de ce qu'un
    navigateur decode : elle se trompe forcement quelque part, et se tromper
    dans le sens « je transcode un fichier que la machine lisait tres bien »
    coute des minutes d'attente pour rien. Ce bouton rend la main sur ce point
    a qui, lui, voit l'image.
    """
    source = SESSION.snapshot()["media"].get("sourcePath")
    if not source:
        return jsonify({"error": "Aucun media ouvert."}), 409
    return _open_media(source, bypass=True, page=_current_page())


@app.route("/api/media/file/<media_id>")
def api_media_file(media_id):
    """Sert le media aux deux clients.

    `conditional=True` active les requetes Range : c'est ce qui permet a la
    tablette de se deplacer dans le rush sans le telecharger en entier.
    """
    if not _authorized():
        return jsonify({"error": "Session non appairee."}), 403

    media = SESSION.snapshot()["media"]
    if media.get("id") != media_id:
        return jsonify({"error": "Media expire."}), 404
    served = media.get("servedPath")
    if media.get("state") != "ready" or not served or not os.path.exists(served):
        return jsonify({"error": "Media indisponible."}), 409

    # Le type MIME suit le fichier *servi*, pas la source : un ProRes nomme
    # « rush.mov » est envoye en video/mp4 parce que c'est un proxy H.264, et
    # annoncer autre chose ferait echouer la lecture cote client.
    guessed = mimetypes.guess_type(served)[0] or "application/octet-stream"
    return send_file(served, mimetype=guessed, conditional=True)


# --------------------------------------------------------------------------
# Relais temps reel
# --------------------------------------------------------------------------

# Messages qui ne traversent pas : le serveur y repond lui-meme.
_LOCAL_MESSAGES = {"ping"}


@sock.route("/ws")
def ws_relay(connection):
    role = request.args.get("role")
    if role not in ("pc", "tablet"):
        connection.send(json.dumps({"t": "denied", "reason": "Role inconnu."}))
        return

    token = request.args.get("token")
    if role == "tablet" and not SESSION.accept(token):
        connection.send(json.dumps({
            "t": "denied",
            "reason": "Session fermee ou lien perime — relancez l'appairage depuis le poste.",
        }))
        return
    if role == "pc" and not _from_pc():
        connection.send(json.dumps({"t": "denied", "reason": "Role poste reserve a la machine hote."}))
        return

    peer_id = HUB.add(role, connection)
    if role == "tablet":
        SESSION.tablet_joined()

    connection.send(json.dumps(dict(SESSION.snapshot(), t="state")))
    broadcast_state()
    HUB.send({"t": "peers", "roles": HUB.roles(), "tablets": HUB.count("tablet")})

    try:
        while True:
            raw = connection.receive()
            if raw is None:
                break
            _relay(role, raw, peer_id, connection)
    except Exception:      # noqa: BLE001 - coupure reseau : on nettoie et on sort
        pass
    finally:
        HUB.remove(peer_id)
        if role == "tablet" and HUB.count("tablet") == 0:
            SESSION.tablet_left()
            broadcast_state()
        HUB.send({"t": "peers", "roles": HUB.roles(), "tablets": HUB.count("tablet")})


def _relay(role, raw, peer_id, connection):
    """Fait suivre un message a l'autre cote.

    Le serveur n'interprete pas le dessin : il ne lit le type que pour
    reconnaitre les quelques messages qui lui sont adresses. Tout le reste est
    retransmis tel quel, sans etre re-serialise -- c'est ce qui garde le relais
    a quelques dixiemes de milliseconde.
    """
    kind = _peek_type(raw)
    if kind in _LOCAL_MESSAGES:
        if kind == "ping":
            try:
                message = json.loads(raw)
            except ValueError:
                return
            connection.send(json.dumps({"t": "pong", "ts": message.get("ts")}))
        return
    HUB.send(raw, to="pc" if role == "tablet" else "tablet")


def _peek_type(raw):
    """Type du message sans deserialiser tout le corps.

    Un lot de points de trace peut peser quelques kilooctets ; en parser le
    JSON complet a chaque relais pour lire un seul champ serait du travail pur
    perte, repete des dizaines de fois par seconde.
    """
    head = raw[:64] if isinstance(raw, str) else ""
    match = re.search(r'"t"\s*:\s*"([a-z.]+)"', head)
    return match.group(1) if match else ""


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

class Step:
    """Une couche a rendre : un fichier, un moteur, une charge utile.

    L'export « pro » en aligne trois (apercu, media, trace) ; les autres modes
    n'en ont qu'une. Un seul chemin de code dans les deux cas -- la progression,
    l'annulation et le menage ne se demandent pas combien il y a de fichiers.
    """

    __slots__ = ("label", "path", "render", "payload")

    def __init__(self, label, path, render, payload):
        self.label = label
        self.path = path
        self.render = render
        self.payload = payload


class Job:
    def __init__(self, job_id, steps, folder=None):
        self.id = job_id
        self.steps = steps
        self.folder = folder
        self.index = 0
        self.state = "running"
        self.frame = 0
        self.total = 0
        self.error = None
        self.produced = []
        self.cancel = threading.Event()

    @property
    def path(self):
        """Ce que l'export a livre : le dossier s'il y en a un, sinon le fichier."""
        return self.folder or self.steps[0].path

    def snapshot(self):
        inside = (self.frame / self.total * 100.0) if self.total else 0.0
        # Les couches sont comptees a poids egal. C'est faux au sens strict --
        # le media s'encode plus vite que le trace -- mais une barre qui avance
        # regulierement en dit plus long qu'une ponderation savante.
        percent = (self.index * 100.0 + inside) / len(self.steps)
        current = self.steps[min(self.index, len(self.steps) - 1)]
        return {
            "jobId": self.id,
            "state": self.state,
            "frame": self.frame,
            "totalFrames": self.total,
            "percent": round(min(100.0, percent), 1),
            "step": min(self.index + 1, len(self.steps)),
            "steps": len(self.steps),
            "label": current.label,
            "filepath": self.path if self.state == "done" else None,
            "files": list(self.produced) if self.state == "done" else [],
            "error": self.error,
        }


# --------------------------------------------------------------------------
# Destination du fichier
# --------------------------------------------------------------------------

_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def _safe_filename(name, extension):
    """Nom sur mesure -> nom de fichier valide, extension imposee par le codec."""
    # Les separateurs sont neutralises avant le basename : sinon « a/b » ne
    # garderait que « b » au lieu de devenir « a_b ».
    name = _INVALID_NAME.sub("_", (name or "").strip())
    name = os.path.basename(name)
    if name.lower().endswith("." + extension):
        name = name[: -(len(extension) + 1)]
    name = name.strip().rstrip(". ")
    if not name:
        name = "livenote_%s" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return "%s.%s" % (name, extension)


def _safe_dirname(name):
    """Nom sur mesure -> nom de dossier valide, pour l'export pro."""
    name = _INVALID_NAME.sub("_", (name or "").strip())
    name = os.path.basename(name).strip().rstrip(". ")
    # Le nom du media garde son extension quand il arrive tel quel : « rush.mov »
    # ferait un dossier a l'air de fichier, et trois fichiers nommes
    # « rush.mov_trace.mov ». On ne retire qu'un suffixe qui ressemble a une
    # extension : « doc.pdf (page 3) » doit garder sa page.
    name = re.sub(r"\.[A-Za-z0-9]{1,5}\Z", "", name).strip().rstrip(". ") or name
    if not name:
        name = "livenote_%s" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return name


def _unique_path(directory, filename):
    """Ne jamais ecraser un rendu deja la : on suffixe _2, _3, ..."""
    base, ext = os.path.splitext(filename)
    path = os.path.join(directory, filename)
    index = 2
    while os.path.exists(path):
        path = os.path.join(directory, "%s_%d%s" % (base, index, ext))
        index += 1
    return path


def _unique_dir(directory, name):
    """Meme regle pour un dossier d'export pro, sans toucher a son nom."""
    path = os.path.join(directory, name)
    index = 2
    while os.path.exists(path):
        path = os.path.join(directory, "%s_%d" % (name, index))
        index += 1
    return path


@app.route("/api/export/defaults")
def api_export_defaults():
    return jsonify({"directory": EXPORT_DIR})


@app.route("/api/browse", methods=["POST"])
@pc_only
def api_browse():
    payload = request.get_json(silent=True) or {}
    initial = payload.get("directory") or EXPORT_DIR
    try:
        directory = nativedialog.ask_directory(initial)
    except (nativedialog.PickerError, OSError, subprocess.SubprocessError) as exc:
        return jsonify({"error": "Selecteur de dossier indisponible (%s)." % exc}), 500
    return jsonify({"directory": directory})


@app.route("/api/project/save", methods=["POST"])
@pc_only
def api_project_save():
    """Sauvegarde du projet via un dialogue d'enregistrement natif.

    Cote client, un `<a download>` sur un blob ne marche qu'avec Chrome :
    WKWebView (le build macOS) ignore l'attribut `download` et navigue vers
    le blob, remplacant l'ecran de l'application par le JSON. Comme pour le
    choix du media et du dossier d'export, on passe donc par une boite de
    dialogue native ; le chemin final vient d'elle, jamais du client -- un
    `<input type=file>` ne donnerait de toute facon qu'un blob, et le serveur
    ne doit jamais accepter un chemin venu de la page.
    """
    payload = request.get_json(silent=True) or {}
    content = payload.get("content")
    if not isinstance(content, str) or not content:
        return jsonify({"error": "Contenu de projet manquant."}), 400
    suggested = os.path.basename(str(payload.get("name") or "live_notes.lvn"))
    if not suggested.lower().endswith(".lvn"):
        suggested += ".lvn"
    try:
        path = nativedialog.ask_save_path(EXPORT_DIR, suggested)
    except (nativedialog.PickerError, OSError, subprocess.SubprocessError) as exc:
        return jsonify({"error": "Dialogue d'enregistrement indisponible (%s)." % exc}), 500
    if not path:
        return jsonify({"cancelled": True})
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as exc:
        return jsonify({"error": "Ecriture du projet impossible (%s)." % exc}), 500
    return jsonify({"path": path})


def _export_media(payload):
    """Le media de fond tel que l'export doit le voir, ou None.

    Le chemin vient de la session, jamais de la requete : le poste seul sait ou
    le fichier se trouve, et rien ne doit permettre de faire encoder un fichier
    arbitraire du disque en passant par la page.

    C'est la **source** qui est rendue, pas la copie de lecture -- un tracage
    fait sur un proxy 1280 px ressort au format du canevas. Seule exception, la
    page de PDF : rasterisee, elle n'existe que sous la forme servie.

    Un media audio en fait partie : il n'a rien a montrer sous le trace, mais
    sa piste son est une couche a part entiere.
    """
    media = SESSION.snapshot()["media"]
    if media.get("kind") not in ("video", "image", "audio"):
        return None

    served = media.get("servedPath")
    source = media.get("sourcePath")
    path = served if media.get("pageCount") else source
    for candidate in (path, source, served):
        if candidate and os.path.exists(candidate):
            path = candidate
            break
    else:
        return None

    fit = payload.get("mediaFit")
    if not isinstance(fit, dict):
        fit = SESSION.snapshot()["config"].get("mediaFit")
    return {
        "path": path,
        "kind": media.get("kind"),
        "duration": media.get("duration") or 0.0,
        # Les bornes viennent du lecteur : c'est ce qui a ete joue sous le trace.
        "in": payload.get("mediaIn") or 0.0,
        "out": payload.get("mediaOut") or 0.0,
        "fit": fit,
        "hasAudio": bool(media.get("hasAudio")),
        "audioCodec": media.get("audioCodec") or "",
        "audioRate": media.get("audioRate") or 0,
        "audioBits": media.get("audioBits") or 0,
        "name": media.get("name") or "",
    }


def _export_plan(payload, directory, media):
    """(couches a rendre, dossier cree). Leve ValueError si la demande n'a pas de sens.

    L'export pro depose trois fichiers dans un sous-dossier : l'apercu aplati,
    le media cadre, et le trace en alpha. Les trois partagent le meme canevas,
    la meme cadence, la meme premiere image -- les reunir en post revient a les
    empiler, sans rien recaler.

    Un media qui n'est que du son suit exactement le meme plan : la couche
    « media » y devient un WAV coupe aux memes bornes, et l'apercu emporte la
    bande son sur le fond uni.
    """
    mode = payload.get("mode")
    if mode not in ("preview", "prores", "pro"):
        # Forme historique : le codec disait tout.
        mode = "preview" if payload.get("codec") == "h264" else "prores"

    if mode != "pro":
        codec = "h264" if mode == "preview" else "prores4444"
        filename = _safe_filename(payload.get("filename"), "mp4" if codec == "h264" else "mov")
        step = Step("Rendu", _unique_path(directory, filename), renderer.render,
                    dict(payload, codec=codec, media=media))
        return [step], None

    if media is None:
        raise ValueError("L'export pro demande un média de fond : il en constitue une des trois couches.")

    wanted = payload.get("folder") or payload.get("filename")
    # Nom impose : on le respecte tel quel, horodatage compris ou non -- c'est
    # celui qui exporte qui sait comment il range. Sinon, le nom du rush et
    # l'heure : le premier dit de quel rush il s'agit, la seconde distingue
    # deux essais du meme.
    base = _safe_dirname(wanted) if wanted else "%s_%s" % (
        _safe_dirname(media.get("name") or "livenote"),
        datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    folder = _unique_dir(directory, base)
    os.makedirs(folder)
    name = os.path.basename(folder)

    def inside(suffix, extension):
        return os.path.join(folder, "%s_%s.%s" % (name, suffix, extension))

    layer = "Son" if media.get("kind") == "audio" else "Média"
    steps = [
        Step("Aperçu", inside("preview", "mp4"), renderer.render,
             dict(payload, codec="h264", flatten="media", media=media)),
        Step(layer, inside("media", renderer.media_extension(media)), renderer.render_media,
             dict(payload, codec="prores4444", media=media)),
    ]

    # Une couche de trace par fichier, au lieu d'une seule aplatie. La question
    # ne se pose qu'au-dela d'une couche : un projet a une seule couche garde le
    # nom sans numero, et le meme dossier a trois fichiers qu'avant.
    sheets = [sheet for sheet in renderer.payload_sheets(payload) if sheet["strokes"]]
    if not (payload.get("splitLayers") and len(sheets) > 1):
        steps.append(Step("Tracé", inside("trace", "mov"), renderer.render,
                          dict(payload, codec="prores4444", flatten="alpha", media=media)))
        return steps, folder

    # Toutes les couches gardent la duree de l'ensemble : c'est ce qui permet de
    # les reposer au montage sans rien recaler. Une couche dont le dernier trait
    # tombe tot donnerait sinon un fichier plus court que les autres. Elles sont
    # numerotees du fond vers le premier plan, dans l'ordre ou elles s'empilent.
    span = renderer.total_duration_ms(payload)
    for index, sheet in enumerate(sheets, start=1):
        steps.append(Step("Tracé %d" % index, inside("trace_%d" % index, "mov"),
                          renderer.render,
                          dict(payload, codec="prores4444", flatten="alpha", media=media,
                               layers=[sheet], durationMs=span)))
    return steps, folder


@app.route("/api/export", methods=["POST"])
@pc_only
def api_export():
    payload = request.get_json(silent=True) or {}
    # Les couches, ou -- charge d'avant les couches -- les traces a plat.
    if not any(sheet["strokes"] for sheet in renderer.payload_sheets(payload)):
        return jsonify({"error": "Aucun trace a exporter."}), 400

    raw_directory = (payload.get("directory") or "").strip()
    directory = os.path.abspath(os.path.expanduser(raw_directory)) if raw_directory else EXPORT_DIR
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        return jsonify({"error": "Dossier de destination inutilisable : %s" % exc}), 400

    try:
        steps, folder = _export_plan(payload, directory, _export_media(payload))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OSError as exc:
        return jsonify({"error": "Dossier d'export impossible a creer : %s" % exc}), 400

    job = Job(uuid.uuid4().hex[:12], steps, folder)
    with _jobs_lock:
        _jobs[job.id] = job

    thread = threading.Thread(target=_run_job, args=(job,), daemon=True)
    thread.start()
    return jsonify(job.snapshot())


def _run_job(job):
    def progress(frame, total):
        job.frame = frame
        job.total = total

    try:
        for index, step in enumerate(job.steps):
            job.index = index
            job.frame = job.total = 0
            step.render(step.payload, step.path, progress=progress,
                        cancelled=job.cancel.is_set)
            job.produced.append(step.path)
        job.index = len(job.steps) - 1
        job.frame = job.total
        job.state = "done"
    except renderer.RenderError as exc:
        job.state = "cancelled" if job.cancel.is_set() else "error"
        job.error = str(exc)
        _cleanup(job)
    except Exception as exc:  # noqa: BLE001 - remonte l'erreur au front-end
        traceback.print_exc()
        job.state = "error"
        job.error = "%s: %s" % (type(exc).__name__, exc)
        _cleanup(job)


def _cleanup(job):
    """Un export interrompu ne laisse pas de couche orpheline.

    Livrer un dossier ou l'apercu existe mais pas le trace serait pire que ne
    rien livrer : on croirait l'export termine.
    """
    for step in job.steps:
        try:
            if os.path.exists(step.path):
                os.remove(step.path)
        except OSError:
            pass
    if job.folder:
        try:
            os.rmdir(job.folder)
        except OSError:      # noqa: PERF203 - dossier non vide : on n'y touche pas
            pass


@app.route("/api/export/<job_id>")
@pc_only
def api_export_status(job_id):
    job = _jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Job introuvable."}), 404
    return jsonify(job.snapshot())


@app.route("/api/export/<job_id>/cancel", methods=["POST"])
@pc_only
def api_export_cancel(job_id):
    job = _jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Job introuvable."}), 404
    job.cancel.set()
    return jsonify(job.snapshot())


def listen_host():
    """Interface d'ecoute.

    Ouvert au reseau local par defaut : c'est ce qui rend le mode tablette
    possible sans configuration. Les routes sensibles restent verrouillees sur
    la boucle locale (voir `pc_only`). LIVE_NOTES_HOST=127.0.0.1 rend le
    serveur strictement local pour qui n'en veut pas.
    """
    return os.environ.get("LIVE_NOTES_HOST") or "0.0.0.0"


def _port_is_free(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # Werkzeug pose SO_REUSEADDR avant de se lier : sans lui ici, un port
        # laisse en TIME_WAIT par un redemarrage passerait pour occupe alors
        # que le serveur, lui, s'y lierait sans probleme.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def choose_port(host=None):
    """Un port libre, en partant de celui qu'on prefere.

    Se lier a un port deja pris ne produit pas d'erreur visible pour
    l'utilisateur.ice : la fenetre s'ouvre sur une page blanche, sans rien
    indiquer. C'est ce qui arrivait systematiquement sur macOS, ou le
    recepteur AirPlay occupe 5000. On verifie donc avant de demarrer, et on
    se decale s'il le faut -- le port reel se propage partout, l'URL de la
    fenetre comme le QR code d'appairage (construit depuis l'entete Host).
    """
    host = host or listen_host()
    requested = os.environ.get("LIVE_NOTES_PORT")
    preferred = int(requested) if requested else DEFAULT_PORT

    candidates = [preferred]
    candidates += [port for port in range(DEFAULT_PORT, DEFAULT_PORT + PORT_ATTEMPTS)
                   if port != preferred]
    for candidate in candidates:
        if _port_is_free(host, candidate):
            return candidate

    # Tout est pris : plutot qu'echouer, on laisse le systeme trancher.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


def main():
    # Le cache de proxys survit deliberement aux redemarrages : refaire
    # plusieurs minutes de transcodage parce qu'on a ferme la fenetre serait
    # absurde. Le menage se fait a la main, depuis le panneau de gestion.
    cached = proxy.entries()

    host = listen_host()
    port = choose_port(host)

    print("=" * 62)
    print("live_notes — serveur local")
    print("=" * 62)
    print("FFmpeg   : %s" % renderer.FFMPEG)
    print("Exports  : %s" % EXPORT_DIR)
    if cached:
        print("Proxys   : %d en cache, %.0f Mo (%s)"
              % (len(cached), sum(item["size"] for item in cached) / 1e6,
                 proxy.cache_dir()))
    print("Interface: http://127.0.0.1:%d" % port)
    if host == "0.0.0.0":
        for address in sessions.lan_addresses():
            print("Tablette : http://%s:%d  (mode tablette)" % (address, port))
    else:
        print("Reseau   : desactive (LIVE_NOTES_HOST=%s)" % host)
    print("=" * 62)
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
