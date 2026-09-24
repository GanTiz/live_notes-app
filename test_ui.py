"""Verification de l'interface dans un vrai navigateur.

Lancer : py test_ui.py

Les trois autres bancs s'arretent au bord du navigateur : ils verifient ce que
le serveur repond, jamais ce que la page en fait. C'est une frontiere genante,
parce que les pannes les plus deroutantes vivent de l'autre cote -- une
exception dans un `then` laisse une fenetre vide, et le serveur, lui, a
repondu 200.

Le banc pilote donc Chrome sans interface par le Chrome DevTools Protocol, et
verifie trois choses :

1. le parcours nominal aboutit -- QR d'appairage genere, et **aucune exception
   JavaScript** ;
2. le cadrage decide sur le poste part bien vers la tablette (c'est la seule
   chose qui garantit que les deux ecrans annotent la meme image) ;
3. une panne se dit a l'ecran. Serveur arrete et ecran non reconnu comme le
   poste sont deux situations differentes, longtemps confondues sous un meme
   « Session indisponible. » qui n'apprenait rien.

Aucune dependance nouvelle : `simple_websocket` arrive avec `flask-sock`, et
le navigateur est celui de la machine. Sans Chrome installe, le banc le dit et
ne pretend pas avoir verifie quoi que ce soit.

Le banc ne demande aucun codec proprietaire. Ses rushes sont en VP9, que tout
navigateur decode ; le format des copies de lecture est demande au serveur
d'apres ce que le navigateur trouve annonce. Un Chromium nu -- celui de
Playwright, ceux des paquets Linux, ceux des integrations continues -- verifie
donc exactement la meme chose qu'un Chrome complet. `LIVE_NOTES_PROXY_FORMAT`
reste respecte quand on veut examiner un format precis ; c'est le seul cas ou
un test peut encore s'abstenir faute de decodeur.
"""

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import simple_websocket

import renderer
import session as sessions

TMP = tempfile.mkdtemp(prefix="live_notes_ui_")
REPO = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- navigateur

def find_chrome():
    """Chrome ou Edge, selon ce que la machine a sous la main."""
    override = os.environ.get("LIVE_NOTES_CHROME")
    if override:
        return override if os.path.exists(override) else None

    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Browser:
    """Le strict necessaire du protocole : naviguer, cliquer, lire, ecouter."""

    def __init__(self, chrome, profile):
        self.port = free_port()
        flags = [
            chrome, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions",
            # Le banc n'a pas de doigt : sans cela `play()` est refuse, et tout
            # ce qui se verifie *pendant* une lecture -- l'arret sur le point
            # OUT, un trace enregistre -- ne pourrait jamais l'etre.
            "--autoplay-policy=no-user-gesture-required",
            # Une fenetre sans surface visible voit ses minuteries ralenties : le
            # decompte de trois secondes du REC en durerait sept.
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--remote-debugging-port=%d" % self.port,
            "--user-data-dir=" + profile,
        ]
        # Une machine peut avoir besoin d'un drapeau de plus (`--no-sandbox` sous
        # root dans un conteneur, par exemple) : c'est de sa configuration qu'il
        # releve, pas du banc.
        flags += shlex.split(os.environ.get("LIVE_NOTES_CHROME_FLAGS", ""))
        self.proc = subprocess.Popen(flags + ["about:blank"],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)

        target = None
        for _ in range(60):
            time.sleep(0.3)
            try:
                raw = urllib.request.urlopen(
                    "http://127.0.0.1:%d/json" % self.port, timeout=2).read()
            except Exception:
                continue
            for page in json.loads(raw):
                if page.get("type") == "page" and page.get("webSocketDebuggerUrl"):
                    target = page["webSocketDebuggerUrl"]
                    break
            if target:
                break
        if not target:
            raise RuntimeError("navigateur injoignable sur le port de debogage")

        self.ws = simple_websocket.Client(target)
        self.seq = 0
        self.events = []
        self.send("Runtime.enable")
        self.send("Page.enable")
        self.send("DOM.enable")
        self.pump(0.4)

    def send(self, method, **params):
        self.seq += 1
        self.ws.send(json.dumps(
            {"id": self.seq, "method": method, "params": params}))
        return self.seq

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            try:
                raw = self.ws.receive(timeout=max(0.05, end - time.time()))
            except Exception:
                return
            if raw is None:
                return
            self.events.append(json.loads(raw))

    def call(self, method, wait=1.5, **params):
        ident = self.send(method, **params)
        self.pump(wait)
        for item in self.events:
            if item.get("id") == ident:
                return item.get("result", {})
        return {}

    def js(self, expression, wait=1.0):
        got = self.call("Runtime.evaluate", wait=wait,
                        expression=expression, returnByValue=True)
        return got.get("result", {}).get("value")

    def goto(self, url):
        self.send("Page.navigate", url=url)
        self.pump(3.5)

    def click(self, element_id):
        self.call("Runtime.evaluate", wait=1.0, userGesture=True, returnByValue=True,
                  expression="document.getElementById('%s').click()" % element_id)

    def input(self, method, **params):
        """Un evenement d'entree reel, sans attendre la reponse.

        Un trace se joue a la cadence du stylet : attendre l'accuse de chaque
        point etirerait le geste bien au-dela de ce qu'il doit durer.
        """
        self.send(method, **params)

    def text(self, element_id):
        return self.js("document.getElementById('%s').textContent" % element_id)

    def display(self, element_id):
        return self.js(
            "getComputedStyle(document.getElementById('%s')).display" % element_id)

    def attach_file(self, element_id, path):
        doc = self.call("DOM.getDocument", depth=-1)
        root = doc.get("root", {}).get("nodeId")
        node = self.call("DOM.querySelector", nodeId=root,
                         selector="#" + element_id)
        self.call("DOM.setFileInputFiles",
                  nodeId=node.get("nodeId"), files=[path])

    def wait_for(self, expression, timeout=6.0, step=0.4):
        """Attend qu'une expression devienne vraie plutot que de dormir.

        `step` est la duree d'ecoute de chaque tour. La valeur par defaut suffit
        a attendre un etat qui, une fois acquis, ne repart pas. Guetter un
        instant qui passe -- une lecture qui vient de demarrer, et qui ne durera
        qu'une seconde ou deux -- demande un pas plus court.
        """
        end = time.time() + timeout
        while time.time() < end:
            if self.js(expression, wait=step):
                return True
            if step >= 0.3:
                self.pump(0.3)
        return False

    def exceptions(self):
        out = []
        for item in self.events:
            if item.get("method") != "Runtime.exceptionThrown":
                continue
            detail = item["params"]["exceptionDetails"]
            out.append(detail.get("exception", {}).get("description")
                       or detail.get("text"))
        return out

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
        self.proc.terminate()


# -------------------------------------------------------------------- serveur

class Server:
    def __init__(self, proxy_format):
        self.port = free_port()
        # Le format des copies de lecture est celui que le navigateur du banc
        # sait decoder : servir un proxy H.264 a un Chromium qui n'a pas ce
        # codec ne prouverait rien de l'application, et c'est pourtant le
        # lecteur -- pas le codec -- que les tests concernes examinent.
        self.proxy_format = proxy_format
        env = dict(os.environ, LIVE_NOTES_PORT=str(self.port),
                   LIVE_NOTES_HOST="0.0.0.0",
                   LIVE_NOTES_PROXY_FORMAT=proxy_format)
        self.proc = subprocess.Popen([sys.executable, "app.py"], cwd=REPO,
                                     env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        for _ in range(60):
            time.sleep(0.4)
            if self.get("/api/session") is not None:
                return
        raise RuntimeError("le serveur n'a pas demarre")

    @property
    def url(self):
        return "http://127.0.0.1:%d/" % self.port

    def get(self, route, host="127.0.0.1"):
        try:
            raw = urllib.request.urlopen(
                "http://%s:%d%s" % (host, self.port, route), timeout=3).read()
            return json.loads(raw)
        except Exception:
            return None

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def make_pdf(name):
    """Un vrai PDF de deux pages, fabrique avec Pillow (deja dependance)."""
    from PIL import Image
    path = os.path.join(TMP, name)
    pages = [Image.new("RGB", (600, 850), (245, 245, 245)),
             Image.new("RGB", (850, 600), (210, 225, 245))]
    pages[0].save(path, "PDF", save_all=True, append_images=pages[1:])
    return path


def make_rush(name, size, seconds=2, rate=25):
    """Un vrai fichier, lisible par le navigateur : le banc ne simule rien.

    VP9 dans un WebM, et non H.264 : les builds « Chromium » nus n'ont pas les
    codecs proprietaires (voir `h264_available`), et c'est justement le lecteur
    -- timecode, bornes IN/OUT, tete de lecture, cadrage -- qu'on ne pourrait
    alors jamais verifier. Aucun de ces tests ne porte sur le codec : ils
    portent sur ce que l'application fait d'un rush qui s'affiche. Dependre
    d'un format que la moitie des navigateurs refusent, c'etait donc renoncer a
    les verifier la ou ils sont le plus utiles.

    `rate` sert a fabriquer un rush qui n'est *pas* a la cadence du projet :
    c'est le cas de travail ordinaire (un rush 24 dans un projet 25) et il ne
    doit pas passer inapercu.
    """
    path = os.path.join(TMP, name)
    subprocess.run([renderer.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i",
                    "testsrc=size=%s:rate=%d:duration=%d" % (size, rate, seconds),
                    "-c:v", "libvpx-vp9", "-b:v", "400k", "-pix_fmt", "yuv420p",
                    path], check=True)
    return path


# ---------------------------------------------------------------------- tests

def test_the_workspace_opens_without_a_single_exception(ctx):
    page = ctx["page"]
    page.goto(ctx["server"].url)
    assert page.js("!!document.getElementById('btn-create')"), "page non chargee"
    page.click("btn-create")
    assert page.wait_for(
        "getComputedStyle(document.getElementById('setup-screen')).display === 'none'"
    ), "l'ecran de configuration ne s'est pas efface"
    errors = page.exceptions()
    assert not errors, "exceptions : %s" % errors
    return "espace cree, 0 exception"


def test_pairing_shows_a_qr_code(ctx):
    page = ctx["page"]
    page.click("btn-tablet")
    assert page.wait_for(
        "document.getElementById('tablet-qr').childElementCount > 0"
    ), "aucun QR code n'a ete insere"
    url = page.text("tablet-url")
    assert url and "/tablet#" in url, "URL d'appairage absente : %r" % url
    page.click("tablet-close")
    return url.split("//")[-1]


def test_the_qr_image_is_really_served(ctx):
    served = ctx["page"].js(
        "fetch('/api/session/qr.svg').then(r => r.ok && r.status)", wait=0.2)
    ctx["page"].pump(1.0)
    # La valeur d'une promesse ne revient pas par `returnByValue` : on
    # interroge le serveur, qui est l'interesse.
    raw = urllib.request.urlopen(
        "http://127.0.0.1:%d/api/session/qr.svg" % ctx["server"].port, timeout=3)
    body = raw.read()
    assert raw.status == 200, raw.status
    assert body.startswith(b"<svg"), body[:40]
    return "%d octets de SVG" % len(body)


def test_a_mismatched_ratio_asks_how_to_place_the_media(ctx):
    page = ctx["page"]
    page.attach_file("media-input", ctx["rush43"])
    assert page.wait_for(
        "getComputedStyle(document.getElementById('crop-screen')).display === 'flex'"
    ), "la fenetre de cadrage ne s'est pas ouverte"
    return "640x480 sur un canevas 16:9 : la question est posee"


def test_cropping_travels_to_the_tablet(ctx):
    # Le coeur du mode tablette : sans cette publication, le poste recadre et
    # la tablette montre autre chose.
    page = ctx["page"]
    # La fenetre de cadrage est celle que le test precedent a ouverte : piloter
    # ses controles a vide ne prouverait rien et laisserait une exception
    # derriere soi.
    assert page.js(
        "getComputedStyle(document.getElementById('crop-screen')).display === 'flex'"
    ), "la fenetre de cadrage n'est pas ouverte : il n'y a rien a valider"
    page.js("var m = document.getElementById('crop-mode');"
            "m.value = 'crop'; m.dispatchEvent(new Event('change'));")
    page.click("crop-apply")
    page.pump(1.5)
    config = ctx["server"].get("/api/session")["config"]
    assert config["mediaFit"]["mode"] == "crop", config["mediaFit"]
    return "mediaFit.mode = crop publie dans la session"


def test_releasing_the_media_resets_the_published_cropping(ctx):
    page = ctx["page"]
    page.click("tp-clear")
    page.pump(1.5)
    config = ctx["server"].get("/api/session")["config"]
    assert config["mediaFit"]["mode"] == "contain", config["mediaFit"]
    return "retrait du rush -> cadrage remis a contain"


def test_the_background_travels_too(ctx):
    page = ctx["page"]
    page.js("var c = document.getElementById('bg-color');"
            "c.value = '#808080'; c.dispatchEvent(new Event('input'));")
    page.pump(1.5)
    config = ctx["server"].get("/api/session")["config"]
    assert config["background"] == "#808080", config
    return "fond #808080 publie"


def test_a_pdf_page_becomes_an_annotatable_image(ctx):
    """Le coeur de la demande : une page de PDF vaut n'importe quel media.

    Le selecteur natif ne s'automatise pas, donc le banc entre par la route que
    le navigateur appelle lui-meme apres le choix de la page. Ce qui compte est
    la suite : la page rasterisee doit s'installer comme une image et poser la
    question du cadrage, exactement comme un rush au mauvais format.
    """
    page = ctx["page"]
    page.js("window.__pdf = null;"
            "fetch('/api/media/open', {method: 'POST',"
            " headers: {'Content-Type': 'application/json'},"
            " body: JSON.stringify({path: %s, page: 2})})"
            ".then(function (r) { return r.json(); })"
            ".then(function (d) { window.__pdf = d; });"
            % json.dumps(ctx["pdf"].replace("\\", "/")))
    assert page.wait_for("window.__pdf && window.__pdf.media", timeout=10.0), \
        "la route n'a pas repondu"

    assert page.wait_for(
        "document.getElementById('bg-media').tagName === 'IMG'", timeout=20.0
    ), "la page n'est pas devenue une image"
    assert page.wait_for(
        "getComputedStyle(document.getElementById('crop-screen')).display === 'flex'",
        timeout=10.0
    ), "le cadrage n'a pas ete propose pour la page de PDF"

    media = ctx["server"].get("/api/session")["media"]
    assert media["kind"] == "image", media["kind"]
    assert media["page"] == 2, media["page"]
    return "page 2/%d rasterisee en %dx%d, cadrage propose" % (
        media["pageCount"], media["width"], media["height"])


def test_arming_the_tablet_does_not_reask_a_settled_cropping(ctx):
    """Armer le mode tablette ne doit pas redemander un cadrage deja decide.

    Armer republie le rush courant sous un nouvel identifiant : le seuil de
    debit ne vaut que pour la liaison de la tablette, donc le media est
    reevalue (voir app._reevaluate_media_for_tablet). Cote page, ce nouvel
    identifiant reinstalle le media -- et la fenetre de cadrage se rouvrait,
    proposant de refaire un choix deja fait, par-dessus l'interface.
    """
    page = ctx["page"]
    # On part de la fenetre laissee ouverte par le test precedent : la valider
    # est ce qui « decide » le cadrage.
    page.click("crop-apply")
    assert page.wait_for(
        "getComputedStyle(document.getElementById('crop-screen')).display === 'none'"
    ), "le cadrage ne s'est pas referme"

    before = ctx["server"].get("/api/session")["media"]["id"]
    page.js("fetch('/api/session/tablet', {method: 'POST',"
            " headers: {'Content-Type': 'application/json'},"
            " body: JSON.stringify({action: 'arm'})});")

    # Attendre le nouvel identifiant, pas une duree : c'est lui qui declenche
    # la reinstallation, donc c'est apres lui que la question se pose.
    deadline = time.time() + 15.0
    after = before
    while time.time() < deadline and after == before:
        time.sleep(0.3)
        media = ctx["server"].get("/api/session")["media"]
        after = media["id"] if media else after
    assert after != before, "le media n'a pas ete reevalue : le test ne prouve rien"

    page.pump(1.5)
    assert page.js(
        "getComputedStyle(document.getElementById('crop-screen')).display === 'none'"
    ), "la fenetre de cadrage s'est rouverte sur un cadrage deja decide"
    return "media republie (%s -> %s), cadrage non redemande" % (before[:6], after[:6])


def test_the_cropped_image_covers_the_whole_stage(ctx):
    """En recadrage, la scene *est* le cadre : l'image doit la couvrir.

    La scene prend le ratio du canevas et l'image glisse dedans, rognee par
    son `overflow: hidden` -- il n'y a plus de cadre blanc a mesurer. Ce qui
    doit tenir, c'est que l'image deborde de la scene sur les quatre cotes,
    jamais l'inverse : un cote qui rentre laisserait apparaitre le fond de la
    scene, c'est-a-dire un bord vide dans le canevas exporte.
    """
    page = ctx["page"]
    page.click("tp-fit")
    assert page.wait_for(
        "getComputedStyle(document.getElementById('crop-screen')).display === 'flex'"
    ), "la fenetre de cadrage ne s'est pas rouverte"
    page.js("var m = document.getElementById('crop-mode');"
            "m.value = 'crop'; m.dispatchEvent(new Event('change'));")
    page.pump(0.6)

    measured = page.js(
        "(function () {"
        "  var stage = document.getElementById('crop-stage');"
        "  var image = stage.querySelector('img, video');"
        "  if (!image) return 'pas d image';"
        "  var s = stage.getBoundingClientRect(), i = image.getBoundingClientRect();"
        # La scene a une bordure : l'image, en position absolue, se place dans
        # sa boite de contenu -- c'est elle qu'il faut mesurer.
        "  var left = s.left + stage.clientLeft, top = s.top + stage.clientTop;"
        "  return [i.left - left, i.top - top,"
        "          left + stage.clientWidth - i.right,"
        "          top + stage.clientHeight - i.bottom].join(',');"
        "})()")
    page.click("crop-cancel")
    assert measured != "pas d image", "le mode recadrage ne s'est pas applique"
    # Une demi-tolerance de pixel : les rectangles sont en sous-pixels.
    gaps = [float(value) for value in measured.split(",")]
    assert max(gaps) <= 0.5, \
        "l'image ne couvre pas la scene (jeux gauche/haut/droite/bas : %s)" % measured
    return "image couvrante (debords de %s px)" % measured


def test_the_page_button_appears_only_for_a_pdf(ctx):
    """« Page… » n'a de sens que sur un document qui en a plusieurs."""
    page = ctx["page"]
    page.click("crop-cancel")
    page.pump(0.5)
    assert page.display("tp-page") != "none", "le bouton Page… devrait etre visible"

    page.attach_file("media-input", ctx["rush43"])
    assert page.wait_for(
        "document.getElementById('tp-page').style.display === 'none'", timeout=10.0
    ), "le bouton Page… survit a un media sans pages"
    return "visible sur le PDF, masque sur un rush"


# -------------------------------------------------- lecteur : bornes IN/OUT

def open_on_server(ctx, path):
    """Ouvre un media par le serveur et attend qu'il soit a l'ecran.

    Le chemin passe par `/api/media/open`, comme apres le selecteur natif :
    c'est le seul chemin qui donne au serveur un vrai fichier, donc le seul
    ou le rush peut ensuite etre republie sous un autre identifiant (proxy).
    """
    page = ctx["page"]
    page.js("window.__opened = null;"
            "fetch('/api/media/open', {method: 'POST',"
            " headers: {'Content-Type': 'application/json'},"
            " body: JSON.stringify({path: %s})})"
            ".then(function (r) { return r.json(); })"
            ".then(function (d) { window.__opened = d; });"
            % json.dumps(path.replace("\\", "/")))
    assert page.wait_for("window.__opened && window.__opened.media", timeout=20.0), \
        "la route n'a pas repondu"
    assert page.wait_for("window.MediaTransport.isTimed()"
                         " && window.MediaTransport.duration() > 0", timeout=20.0), \
        "le rush ne s'est pas ouvert dans le lecteur"


def bounds(page):
    return [float(value) for value in page.js(
        "window.MediaTransport.inPoint() + ',' + window.MediaTransport.outPoint()"
    ).split(",")]


def test_the_bounds_land_on_the_frame_grid(ctx):
    """Une borne designe une image, pas un instant entre deux.

    Posee a la souris elle tombait n'importe ou, et chacun l'arrondissait de
    son cote -- le poste, la tablette, puis le moteur de rendu. Une image
    d'ecart suffit a ce que le trace ne retombe plus en face du rush.
    """
    page = ctx["page"]
    open_on_server(ctx, ctx["rush_open"])
    page.js("window.MediaTransport.setRange(0.317, 1.443);")
    page.pump(0.4)
    start, end = bounds(page)
    assert abs(start * 25 - round(start * 25)) < 1e-6, start
    assert abs(end * 25 - round(end * 25)) < 1e-6, end
    return "0.317 / 1.443 s calees sur les images %d / %d" % (
        round(start * 25), round(end * 25))


def test_the_playhead_parks_on_the_last_frame_in_range(ctx):
    """La fin d'un trace ne doit pas figer une image hors plage.

    Se garer sur le point OUT lui-meme affichait la premiere image *apres* la
    plage annotee : le fameux decalage de +1 ou +2 images en bout de prise.
    """
    page = ctx["page"]
    page.js("window.MediaTransport.setRange(0.4, 1.2);"
            "window.MediaTransport.parkAtOut();")
    assert page.wait_for(
        "Math.abs(document.getElementById('bg-media').currentTime - 1.18) < 0.03",
        timeout=5.0
    ), ("tete de lecture a %s au lieu de la derniere image de la plage (1.16-1.20)"
        % page.js("document.getElementById('bg-media').currentTime + ''"))
    return "garee sur la derniere image de [0.4 s ; 1.2 s["


def test_the_bounds_survive_a_proxy_swap(ctx):
    """Le meme rush reservi sous un autre identifiant garde ses bornes.

    Preparer une copie de lecture republie le *meme* rush sous un nouvel
    identifiant : le lecteur est reinstalle, et il repartait sur la plage
    entiere -- les bornes posees a la main disparaissaient au moment ou l'on
    armait le mode tablette.
    """
    # La copie est transcodee dans le format de proxy du serveur, choisi au
    # demarrage pour que ce navigateur-ci sache le decoder (voir `Server`). Il
    # n'y a plus qu'un cas ou la copie n'arriverait pas a l'ecran : un format
    # impose a la main que le navigateur refuse -- et l'echec parlerait alors du
    # navigateur, pas des bornes.
    if ctx["server"].proxy_format == "h264" and not ctx["h264"]:
        raise Skipped("LIVE_NOTES_PROXY_FORMAT=h264 impose a un navigateur qui"
                      " n'a pas ce codec : la copie de lecture ne decoderait pas")

    page = ctx["page"]
    page.js("window.MediaTransport.setRange(0.4, 1.2);")
    page.pump(0.4)
    before = ctx["server"].get("/api/session")["media"]["id"]

    page.js("fetch('/api/proxies/force', {method: 'POST'});")
    deadline = time.time() + 40.0
    after = before
    while time.time() < deadline and after == before:
        time.sleep(0.3)
        media = ctx["server"].get("/api/session")["media"] or {}
        after = media.get("id") or after
    assert after != before, "le rush n'a pas ete republie : le test ne prouve rien"
    assert page.wait_for("window.MediaTransport.duration() > 0", timeout=40.0), \
        "la copie de lecture ne s'est pas ouverte"

    page.pump(1.0)
    start, end = bounds(page)
    assert abs(start - 0.4) < 0.021 and abs(end - 1.2) < 0.021, (start, end)
    return "bornes 0.4 / 1.2 s conservees (%s -> %s)" % (before[:6], after[:6])


def test_another_rush_does_not_inherit_the_bounds(ctx):
    """Les bornes d'un rush ne decrivent rien chez le suivant.

    Les conserver d'un rush a l'autre -- le prix a payer si l'on confond
    « le meme rush sous un autre URL » avec « un autre rush » -- decoupait le
    nouveau au hasard, quelque part au milieu.
    """
    page = ctx["page"]
    open_on_server(ctx, ctx["rush_open_long"])
    page.pump(1.0)
    start, end = bounds(page)
    assert abs(start) < 1e-6, "le point IN a suivi le rush precedent : %s" % start
    assert end > 2.5, "le point OUT a suivi le rush precedent : %s" % end
    return "plage entiere sur le nouveau rush (0 -> %.2f s)" % end


def test_pairing_closes_itself_once_the_tablet_has_the_stylus(ctx):
    """Le QR code s'efface quand il a servi.

    Une tablette qui rejoint prend la main (voir session.tablet_joined) :
    l'appairage a abouti, le code n'a plus rien a dire, et le poste devrait
    montrer le canevas plutot qu'une fenetre a refermer a la main.
    """
    addresses = sessions.lan_addresses()
    if not addresses:
        raise Skipped("aucune adresse LAN sur cette machine")

    page = ctx["page"]
    tablet = ctx["lan_page"]
    page.click("btn-tablet")
    assert page.wait_for(
        "getComputedStyle(document.getElementById('tablet-screen')).display === 'flex'"
    ), "la fenetre d'appairage ne s'est pas ouverte"

    state = ctx["server"].get("/api/session")
    token = (state.get("pairing") or {}).get("token")
    assert token, "pas de jeton d'appairage : le test ne prouverait rien"

    tablet.goto("http://%s:%d/tablet#%s"
                % (addresses[0], ctx["server"].port, token))

    # Attendre que le serveur constate la bascule, pas une duree : c'est elle
    # que le poste doit suivre.
    deadline = time.time() + 15.0
    joined = False
    while time.time() < deadline and not joined:
        time.sleep(0.3)
        current = ctx["server"].get("/api/session") or {}
        joined = current.get("mode") == "live" and current.get("controller") == "tablet"
    assert joined, "la tablette n'a pas pris la main : le test ne prouve rien"

    assert page.wait_for(
        "getComputedStyle(document.getElementById('tablet-screen')).display === 'none'",
        timeout=10.0
    ), "la fenetre du QR code est restee ouverte alors que la tablette dessine"

    # Rouvrir l'appairage doit rester possible -- c'est par la qu'on reprend le
    # stylet. Fermer d'office a chaque etat publie l'aurait rendu inatteignable.
    page.click("btn-tablet")
    page.pump(1.2)
    assert page.js(
        "getComputedStyle(document.getElementById('tablet-screen')).display === 'flex'"
    ), "l'appairage ne se rouvre plus une fois la tablette connectee"
    page.click("tablet-close")

    tablet.goto("about:blank")
    page.js("fetch('/api/session/tablet', {method: 'POST',"
            " headers: {'Content-Type': 'application/json'},"
            " body: JSON.stringify({action: 'end'})});")
    page.pump(1.2)
    return "tablette connectee -> QR referme, reouverture toujours possible"


# ------------------------------------------------ lecteur : arret sur le OUT

FPS = 25


def type_timecode(page, field_id, value):
    """Saisit un timecode dans un champ du transport, comme au clavier.

    Passer par le champ plutot que par `setRange` compte : c'est la seule facon
    de verifier que ce que l'on tape, ce que l'outil retient et ce qu'il redit
    sont bien la meme image.
    """
    page.js(
        "(function () {"
        "  var field = document.getElementById(%s);"
        "  field.value = %s;"
        "  field.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));"
        "})()" % (json.dumps(field_id), json.dumps(value)))
    page.pump(0.3)


def shown_frame(page):
    """Numero de l'image reellement presentee par le lecteur.

    L'image d'un instant est celle dont l'intervalle le contient : une division
    entiere, jamais un arrondi -- c'est justement l'arrondi qui faisait dire au
    timecode une image et a l'ecran une autre.
    """
    value = page.js("(function () {"
                    "  var node = document.getElementById('bg-media');"
                    "  if (!node || typeof node.currentTime !== 'number') return -1;"
                    "  var at = window.__realTime ? window.__realTime() : node.currentTime;"
                    "  return Math.floor(at * %d + 1e-6);"
                    "})()" % FPS)
    return int(value) if value is not None else -1


def watch_presented_frames(page):
    """Note chaque image que le lecteur presente reellement a l'ecran.

    `requestVideoFrameCallback` est le seul temoin d'une image fantome : elle ne
    dure qu'une image, et `currentTime` -- qui est justement ce qui retarde --
    ne la voit pas passer.
    """
    page.js("window.__frames = [];"
            "(function () {"
            "  var node = document.getElementById('bg-media');"
            "  if (!node || !node.requestVideoFrameCallback) return;"
            "  function next(now, meta) {"
            "    window.__frames.push(meta.mediaTime);"
            "    node.requestVideoFrameCallback(next);"
            "  }"
            "  node.requestVideoFrameCallback(next);"
            "})()")


def last_presented_frame(page):
    value = page.js("window.__frames && window.__frames.length"
                    " ? Math.floor(Math.max.apply(null, window.__frames) * %d + 1e-6)"
                    " : -1" % FPS)
    return int(value) if value is not None else -1


def lag_the_player_clock(page, seconds=0.06):
    """Fait retarder `currentTime` sur ce qui est reellement a l'ecran.

    C'est la situation d'une tablette : le rush arrive par le reseau, le
    decodeur avance par a-coups et rattrape en rafale, et l'horloge du lecteur
    ne dit plus quelle image le compositeur vient de presenter. Surveiller la
    fin de plage a cette horloge-la revient donc a s'arreter trop tard -- une
    image de trop passe a l'ecran avant le retour en arriere.

    Le banc ne peut pas provoquer un hoquet reseau a la milliseconde pres ; il
    reproduit son effet, qui est exactement celui-la. `window.__realTime` garde
    la verite pour les mesures.
    """
    page.js("(function () {"
            "  var node = document.getElementById('bg-media');"
            "  var real = Object.getOwnPropertyDescriptor("
            "    HTMLMediaElement.prototype, 'currentTime');"
            "  window.__realTime = function () { return real.get.call(node); };"
            "  Object.defineProperty(node, 'currentTime', {"
            "    configurable: true,"
            "    get: function () {"
            "      var at = real.get.call(this);"
            "      return this.paused ? at : Math.max(0, at - %f);"
            "    },"
            "    set: function (value) { real.set.call(this, value); }"
            "  });"
            "})()" % seconds)


def unlag_the_player_clock(page):
    page.js("(function () {"
            "  var node = document.getElementById('bg-media');"
            "  if (node) delete node.currentTime;"
            "  window.__realTime = null;"
            "})()")


def test_the_out_point_names_the_image_it_freezes(ctx):
    """Le point OUT designe une image, et c'est celle-la qui reste a l'ecran.

    Au montage, le point de sortie se pose sur la *derniere* image du plan, pas
    sur la premiere du plan suivant. La borne etait pourtant tenue pour une fin
    exclusive : le lecteur se garait une image avant celle qu'on lui avait
    designee. Et le timecode affiche, arrondi a l'image la plus proche au lieu
    d'etre lu sur celle qui est presentee, pouvait annoncer l'une pendant que
    l'ecran montrait l'autre -- d'ou l'impression d'une borne « instable ».
    """
    page = ctx["page"]
    open_on_server(ctx, ctx["rush_open_long"])
    type_timecode(page, "tp-in-tc", "00:00:00:10")
    type_timecode(page, "tp-out-tc", "00:00:01:05")

    redit = page.js("document.getElementById('tp-out-tc').value")
    assert redit == "00:00:01:05", "le champ redit %s au lieu du timecode pose" % redit

    page.js("window.MediaTransport.parkAtOut();")
    assert page.wait_for("Math.floor(document.getElementById('bg-media')"
                         ".currentTime * %d + 1e-6) === 30" % FPS, timeout=6.0), \
        "image figee %d au lieu de l'image 30 (00:00:01:05)" % shown_frame(page)

    lu = page.js("document.getElementById('tp-current').textContent")
    assert lu == "00:00:01:05", \
        "l'ecran montre l'image 30 et le timecode annonce %s" % lu

    # Une image de plus doit changer l'image *et* le timecode, du meme pas :
    # c'est ce que l'on verifie a la main quand on doute de la borne.
    page.click("tp-frame-fwd")
    page.pump(0.8)
    assert shown_frame(page) == 31, \
        "une image en avant mene a l'image %d, pas a la 31" % shown_frame(page)
    lu = page.js("document.getElementById('tp-current').textContent")
    assert lu == "00:00:01:06", "une image en avant annonce %s" % lu
    return "OUT 00:00:01:05 -> image 30 figee, timecode en face"


def test_a_late_player_clock_never_shows_the_image_after_the_out(ctx):
    """L'image fantome : celle d'apres le point OUT, montree une image durant.

    La fin de plage etait guettee sur `currentTime`. Or cette horloge retarde
    sur ce que le compositeur affiche des que le decodeur avance par a-coups --
    ce que fait un lecteur de tablette servi par le reseau. On s'arretait donc
    apres avoir presente l'image *suivante*, puis on revenait en arriere : une
    image fantome, suivie de la bonne. Il faut lire l'image presentee, pas
    l'heure qu'il est.
    """
    page = ctx["page"]
    open_on_server(ctx, ctx["rush_open_long"])
    type_timecode(page, "tp-in-tc", "00:00:00:10")
    type_timecode(page, "tp-out-tc", "00:00:01:20")

    lag_the_player_clock(page)
    try:
        watch_presented_frames(page)
        page.click("btn-rec")
        assert page.wait_for("document.body.classList.contains('recording')",
                             timeout=10.0, step=0.2), "l'enregistrement n'a pas demarre"
        assert page.wait_for("!document.body.classList.contains('recording')",
                             timeout=20.0, step=0.2), "l'enregistrement ne s'est pas arrete"
        page.pump(1.2)

        presented = last_presented_frame(page)
        assert presented != -1, "aucune image presentee : le test ne prouve rien"
        assert presented <= 45, \
            ("l'image %d a ete presentee alors que la plage s'arrete a la 45 :"
             " image fantome" % presented)
        assert page.wait_for("Math.floor(window.__realTime() * %d + 1e-6) === 45" % FPS,
                             timeout=6.0), \
            "image figee %d au lieu de la 45" % shown_frame(page)
    finally:
        unlag_the_player_clock(page)
    return "horloge en retard d'une image et demie : rien au-dela de l'image 45"


def test_a_rush_off_the_project_cadence_says_so(ctx):
    """Un rush 24 dans un projet 25 : les images des deux ne coincident pas.

    Les bornes se calent sur la grille d'images du *projet*. Un rush qui n'a pas
    cette cadence n'a pas d'image a ces instants-la : le point OUT tombe au
    milieu d'une image du rush, celle qui se fige n'est pas celle qu'on a
    designee, et rien ne le dit. La cadence du projet est un choix de depart --
    encore faut-il savoir qu'il est a refaire.
    """
    page = ctx["page"]
    open_on_server(ctx, ctx["rush_24"])
    assert page.wait_for(
        "getComputedStyle(document.getElementById('tp-cadence')).display !== 'none'",
        timeout=10.0), "aucune alerte sur un rush 24 dans un projet 25"
    said = page.text("tp-cadence")
    assert "24" in said and "25" in said, \
        "l'alerte ne dit pas les deux cadences : %s" % said

    # Et elle se tait des que le rush est a la cadence du projet.
    open_on_server(ctx, ctx["rush_open_long"])
    assert page.wait_for(
        "getComputedStyle(document.getElementById('tp-cadence')).display === 'none'",
        timeout=10.0), "l'alerte reste affichee sur un rush a la bonne cadence"
    return "rush 24 signale, rush 25 silencieux"


# -------------------------------------- tablette : le trace deborde le OUT

def tablet_takes_the_stylus(ctx):
    """Ouvre une vraie tablette sur la session et attend qu'elle ait le stylet."""
    addresses = sessions.lan_addresses()
    if not addresses:
        raise Skipped("aucune adresse LAN sur cette machine")

    page = ctx["page"]
    tablet = ctx["lan_page"]
    page.click("btn-tablet")
    assert page.wait_for(
        "getComputedStyle(document.getElementById('tablet-screen')).display === 'flex'"
    ), "la fenetre d'appairage ne s'est pas ouverte"

    state = ctx["server"].get("/api/session") or {}
    token = (state.get("pairing") or {}).get("token")
    assert token, "pas de jeton d'appairage : le test ne prouverait rien"

    tablet.goto("http://%s:%d/tablet#%s"
                % (addresses[0], ctx["server"].port, token))

    deadline = time.time() + 20.0
    while time.time() < deadline:
        current = ctx["server"].get("/api/session") or {}
        if current.get("mode") == "live" and current.get("controller") == "tablet":
            break
        time.sleep(0.3)
    else:
        raise AssertionError("la tablette n'a pas pris la main")

    page.click("tablet-close")
    return tablet


def end_tablet_session(ctx, tablet):
    tablet.goto("about:blank")
    ctx["page"].js("fetch('/api/session/tablet', {method: 'POST',"
                   " headers: {'Content-Type': 'application/json'},"
                   " body: JSON.stringify({action: 'end'})});")
    ctx["page"].pump(1.2)


def draw_across(tablet, seconds):
    """Un trait continu, du doigt, pendant `seconds` -- sans jamais le lever.

    Les evenements sont ceux du navigateur, pas des appels a l'API de dessin :
    c'est la chaine complete que l'on veut, capture comprise.
    """
    box = tablet.js("(function () {"
                    "  var r = document.getElementById('drawing-canvas')"
                    "    .getBoundingClientRect();"
                    "  return [r.left, r.top, r.width, r.height].join(',');"
                    "})()")
    left, top, width, height = [float(value) for value in box.split(",")]
    y = top + height * 0.5
    start_x = left + width * 0.25
    travel = width * 0.5

    tablet.input("Input.dispatchMouseEvent", type="mousePressed", x=start_x, y=y,
                 button="left", buttons=1, clickCount=1)
    began = time.time()
    while True:
        gone = time.time() - began
        if gone >= seconds:
            break
        tablet.input("Input.dispatchMouseEvent", type="mouseMoved",
                     x=start_x + travel * (gone / seconds), y=y,
                     button="left", buttons=1)
        time.sleep(0.03)
    tablet.input("Input.dispatchMouseEvent", type="mouseReleased",
                 x=start_x + travel, y=y, button="left", buttons=0, clickCount=1)
    tablet.pump(0.8)


def captured_export(page):
    """Ce que le poste enverrait au moteur de rendu, intercepte avant le depart.

    C'est la seule mesure qui compte : le trace tel qu'il sera exporte, et non
    ce que l'ecran a bien voulu afficher au passage.
    """
    # `alert()` bloque le moteur de rendu tant que personne ne la referme, et le
    # banc n'a pas de main pour cela : plus aucune evaluation ne repondrait.
    page.js("window.__alerts = [];"
            "window.__realAlert = window.__realAlert || window.alert;"
            "window.alert = function (text) { window.__alerts.push(text); };")
    page.js("window.__exported = null;"
            "window.__realFetch = window.__realFetch || window.fetch;"
            "window.fetch = function (url, options) {"
            "  if (String(url).indexOf('/api/export') === 0 && options && options.body) {"
            "    window.__exported = JSON.parse(options.body);"
            "    return Promise.resolve(new Response('{\"error\": \"banc\"}',"
            "      {status: 200, headers: {'Content-Type': 'application/json'}}));"
            "  }"
            "  return window.__realFetch.apply(window, arguments);"
            "};")
    page.click("btn-export")
    page.pump(0.5)
    page.click("export-go")
    page.pump(1.5)
    summary = page.js(
        "(function () {"
        "  var data = window.__exported;"
        "  if (!data) return '';"
        "  var gap = 0, last = 0, first = null;"
        # La charge porte des couches ; une charge a plat reste lisible, c'est
        # celle que produisaient les versions d'avant.
        "  var strokes = [];"
        "  (data.layers || [{strokes: data.strokes || []}]).forEach(function (sheet) {"
        "    strokes = strokes.concat(sheet.strokes || []);"
        "  });"
        "  strokes.forEach(function (item) {"
        # Le trou se mesure *dans* un trace, entre deux points consecutifs : le
        # silence qui precede le premier point n'en est pas un.
        "    var previous = null;"
        "    item.points.forEach(function (point) {"
        "      if (first === null) first = point.t;"
        "      if (previous !== null && point.t - previous > gap) gap = point.t - previous;"
        "      previous = point.t;"
        "      if (point.t > last) last = point.t;"
        "    });"
        "  });"
        "  return [strokes.length, first === null ? -1 : first, last, gap,"
        "          data.durationMs].join(',');"
        "})()")
    page.js("window.fetch = window.__realFetch;"
            "window.alert = window.__realAlert;"
            "document.getElementById('render-overlay').style.display = 'none';")
    assert summary, (
        "l'export n'a pas ete declenche : rien a mesurer (bouton %s, fenetre %s,"
        " etat « %s »)" % (
            "inactif" if page.js("document.getElementById('btn-export').disabled")
            else "actif",
            page.display("export-screen"), page.text("status")))
    parts = summary.split(",")
    return {"strokes": int(parts[0]), "first": float(parts[1]),
            "last": float(parts[2]), "gap": float(parts[3]),
            "duration": float(parts[4])}


def test_a_stroke_across_the_out_point_reaches_the_pc_whole(ctx):
    """Le scenario complet : tablette, bornes IN/OUT, trait qui deborde le OUT.

    Trois choses se jouent au moment precis ou le rush atteint sa derniere
    image, et les trois se voyaient :

    1. le lecteur de la tablette presentait l'image *suivante* pendant une
       image -- l'« image fantome » -- avant de revenir en arriere sur la bonne ;
    2. l'image qui restait figee n'etait pas celle du point OUT ;
    3. la fin d'enregistrement scellait, chez le poste, le trace encore en
       cours : la suite du meme geste -- celle qui se dessine sur l'image figee,
       et que l'on veut exporter -- n'arrivait jamais. Sur la tablette, le trait
       restait pourtant continu : la coupure ne se voyait qu'a l'arrivee.
    """
    page = ctx["page"]
    open_on_server(ctx, ctx["rush_open_long"])
    type_timecode(page, "tp-in-tc", "00:00:00:10")
    type_timecode(page, "tp-out-tc", "00:00:02:12")

    tablet = tablet_takes_the_stylus(ctx)
    try:
        assert tablet.wait_for("!!window.MediaTransport && window.MediaTransport.isTimed()"
                               " && window.MediaTransport.duration() > 0", timeout=30.0), \
            "le rush n'est pas arrive sur la tablette"
        assert tablet.wait_for(
            "document.getElementById('tp-out-tc').value === '00:00:02:12'", timeout=15.0), \
            ("les bornes du poste ne sont pas arrivees sur la tablette : OUT a %s"
             % tablet.js("document.getElementById('tp-out-tc').value"))

        span = float(tablet.js("(window.MediaTransport.outPoint()"
                               " - window.MediaTransport.inPoint()) + ''"))
        start = float(tablet.js("window.MediaTransport.inPoint() + ''"))

        # Ce qui est reellement presente a l'ecran, image par image.
        watch_presented_frames(tablet)

        tablet.click("btn-rec")
        assert tablet.wait_for(
            "(function () {"
            "  var node = document.getElementById('bg-media');"
            "  return !!node && !node.paused && node.currentTime >= %f;"
            "})()" % (start + 0.04), timeout=15.0, step=0.12), \
            "la lecture n'a pas demarre sur la tablette apres le decompte"

        # Le geste enjambe le point OUT et continue franchement au-dela : c'est
        # cette part-la, tracee sur l'image figee, qui disparaissait.
        draw_across(tablet, span + 1.2)

        assert tablet.wait_for("!document.body.classList.contains('recording')",
                               timeout=10.0), \
            "l'enregistrement ne s'est pas arrete sur la tablette"

        presented = last_presented_frame(tablet)
        assert presented != -1, "aucune image presentee : le test ne prouve rien"
        assert presented <= 62, \
            ("l'image %d a ete presentee alors que la plage s'arrete a la 62 :"
             " image fantome" % presented)

        assert tablet.wait_for("Math.floor(document.getElementById('bg-media')"
                               ".currentTime * %d + 1e-6) === 62" % FPS, timeout=8.0), \
            "la tablette fige l'image %d au lieu de la 62" % shown_frame(tablet)

        # Le poste reprend la main, puis exporte : c'est le parcours decrit.
        page.js("fetch('/api/session/tablet', {method: 'POST',"
                " headers: {'Content-Type': 'application/json'},"
                " body: JSON.stringify({action: 'take'})});")
        assert page.wait_for("!document.body.classList.contains('viewing')",
                             timeout=10.0), "le poste n'a pas repris la main"
        page.pump(0.8)

        got = captured_export(page)
    finally:
        end_tablet_session(ctx, tablet)

    assert got["strokes"] == 1, \
        ("%d traces recus pour un seul geste : le trait a ete coupe"
         % got["strokes"])
    beyond = got["last"] - span * 1000.0
    assert beyond > 400.0, \
        ("le trace s'arrete %.0f ms apres le point OUT : la part dessinee sur"
         " l'image figee n'est pas arrivee (dernier point a %.0f ms, plage de"
         " %.0f ms)" % (beyond, got["last"], span * 1000.0))
    assert got["gap"] < 400.0, \
        "un trou de %.0f ms au milieu du trace recu" % got["gap"]
    assert got["duration"] >= got["last"] - 1.0, \
        ("la duree exportee (%.0f ms) coupe le trace (%.0f ms)"
         % (got["duration"], got["last"]))
    return ("trait continu de %.0f ms recu entier, %.0f ms au-dela du OUT ;"
            " aucune image au-dela de la 62" % (got["last"], beyond))


# ------------------------------------------------------ couches & historique

def canvas_ink(page):
    """Combien d'encre le canevas porte, toutes couches composees.

    La mesure passe par une reduction a 160 x 90 : lire les huit millions de
    composantes d'un canevas HD depasse le budget d'une evaluation, et on
    cherche une difference franche entre « le trait est la » et « il n'y est
    plus », pas une empreinte exacte.
    """
    return int(page.js("""(function () {
      var node = document.getElementById('drawing-canvas');
      var small = document.createElement('canvas');
      small.width = 160; small.height = 90;
      var ctx = small.getContext('2d');
      ctx.drawImage(node, 0, 0, small.width, small.height);
      var data = ctx.getImageData(0, 0, small.width, small.height).data;
      var count = 0;
      for (var i = 3; i < data.length; i += 4) if (data[i] > 8) count += 1;
      return count;
    })()""", wait=2.5))


def draw_stroke(page, y_ratio, from_x=0.25, to_x=0.65):
    """Un trait pose puis leve : une trace, au sens du projet."""
    box = page.js("(function () {"
                  "  var r = document.getElementById('drawing-canvas')"
                  "    .getBoundingClientRect();"
                  "  return [r.left, r.top, r.width, r.height].join(',');"
                  "})()")
    left, top, width, height = [float(value) for value in box.split(",")]
    y = top + height * y_ratio
    x0 = left + width * from_x
    x1 = left + width * to_x

    page.input("Input.dispatchMouseEvent", type="mousePressed", x=x0, y=y,
               button="left", buttons=1, clickCount=1)
    for step in range(1, 9):
        page.input("Input.dispatchMouseEvent", type="mouseMoved",
                   x=x0 + (x1 - x0) * step / 8.0, y=y, button="left", buttons=1)
    page.input("Input.dispatchMouseEvent", type="mouseReleased", x=x1, y=y,
               button="left", buttons=0, clickCount=1)
    page.pump(0.35)


def exported_layers(page):
    """Le nombre de traces par couche, tel que l'export les recevrait."""
    # `alert()` bloque le moteur de rendu tant que personne ne la referme, et le
    # banc n'a pas de main pour cela : plus aucune evaluation ne repondrait --
    # ni ici, ni dans aucun banc suivant.
    page.js("window.__alerts = [];"
            "window.__realAlert = window.__realAlert || window.alert;"
            "window.alert = function (text) { window.__alerts.push(text); };")
    page.js("window.__exported = null;"
            "window.__realFetch = window.__realFetch || window.fetch;"
            "window.fetch = function (url, options) {"
            "  if (String(url).indexOf('/api/export') === 0 && options && options.body) {"
            "    window.__exported = JSON.parse(options.body);"
            "    return Promise.resolve(new Response('{\"error\": \"banc\"}',"
            "      {status: 200, headers: {'Content-Type': 'application/json'}}));"
            "  }"
            "  return window.__realFetch.apply(window, arguments);"
            "};")
    page.click("btn-export")
    page.pump(0.4)
    page.click("export-go")
    page.pump(1.0)
    raw = page.js("(function () {"
                  "  var data = window.__exported;"
                  "  if (!data || !data.layers) return '';"
                  "  return data.layers.map(function (sheet) {"
                  "    return sheet.strokes.length;"
                  "  }).join(',');"
                  "})()")
    page.js("window.fetch = window.__realFetch;"
            "window.alert = window.__realAlert;")
    assert raw, ("l'export n'a pas ete declenche : rien a mesurer (etat « %s »)"
                 % page.text("status"))
    return [int(value) for value in raw.split(",")]


def fresh_layer(page):
    """Une couche vide et active, pour partir de quelque chose de connu.

    L'onglet est ouvert au passage : la liste ne se redessine pas quand elle
    n'est pas a l'ecran, et plusieurs bancs lisent ses lignes.
    """
    page.click("tab-layers")
    page.click("btn-layer-add")
    page.pump(0.3)


def test_undo_takes_the_stroke_off_the_canvas_and_out_of_the_payload(ctx):
    """Annuler retire la trace des pixels ET des metadonnees.

    Un masquage aurait la meme allure a l'ecran et ressortirait a l'export :
    c'est la charge utile qui fait foi.
    """
    page = ctx["page"]
    fresh_layer(page)
    draw_stroke(page, 0.35)
    one = canvas_ink(page)
    draw_stroke(page, 0.62)
    two = canvas_ink(page)
    assert two > one, "le second trait n'a pas ete pose (%d puis %d)" % (one, two)

    page.click("btn-undo")
    page.pump(0.5)
    undone = canvas_ink(page)
    assert undone < two - (two - one) * 0.5, \
        "le trait annule est toujours a l'ecran (%d, attendu ~%d)" % (undone, one)
    assert exported_layers(page)[-1] == 1, "la trace annulee est encore dans la charge"

    page.click("btn-redo")
    page.pump(0.5)
    again = canvas_ink(page)
    assert again > undone, "le trait retabli n'est pas revenu a l'ecran"
    assert exported_layers(page)[-1] == 2, "la trace retablie manque dans la charge"

    # Une nouvelle trace referme l'avenir : plus rien a retablir.
    page.click("btn-undo")
    page.pump(0.4)
    draw_stroke(page, 0.5)
    assert page.js("document.getElementById('btn-redo').disabled + ''") == "true", \
        "la pile de retablissement a survecu a une nouvelle trace"
    return "annulation et retablissement, a l'ecran comme dans la charge"


def test_recording_only_wipes_the_active_layer(ctx):
    """REC repart de zero sur la couche active, et sur elle seule.

    C'est tout l'objet des couches : jusqu'ici, appuyer sur REC appelait
    clearAll() et coutait la prise entiere.
    """
    page = ctx["page"]
    fresh_layer(page)
    draw_stroke(page, 0.3)
    page.click("btn-layer-new")       # verrouille, et pose une couche par-dessus
    page.pump(0.4)
    draw_stroke(page, 0.7)
    before = exported_layers(page)
    assert before[-2:] == [1, 1], "les deux couches n'ont pas une trace chacune : %s" % before

    page.click("btn-rec")
    assert page.wait_for("document.body.classList.contains('recording')", timeout=12.0), \
        "l'enregistrement n'a pas demarre"
    page.click("btn-stop")
    assert page.wait_for("!document.body.classList.contains('recording')", timeout=8.0), \
        "l'enregistrement ne s'est pas arrete"
    page.pump(0.5)

    after = exported_layers(page)
    assert after[-2] == 1, "la couche verrouillee a ete emportee par le REC : %s" % after
    assert after[-1] == 0, "la couche active n'a pas ete videe par le REC : %s" % after
    return "REC vide la couche active, la verrouillee est intacte"


def write_project(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    return path


def test_a_v1_project_opens_as_a_single_layer(ctx):
    """Un projet d'avant les couches s'ouvre sans rien perdre.

    Et un projet a couches se relit avec les siennes. Les deux passent par le
    meme chemin de chargement : c'est lui qu'on met a l'epreuve.
    """
    page = ctx["page"]
    brush = {"shape": "round", "size": 18, "color": "#111111", "opacity": 1.0,
             "flow": 1.0, "hardness": 0.8, "spacing": 0.1}

    def trace(offset):
        return {"brush": brush, "seed": 3, "points": [
            {"x": 200 + index * 20, "y": 300 + offset, "t": index * 30.0, "p": 0.8}
            for index in range(12)]}

    config = {"width": 1920, "height": 1080, "fps": 25, "alpha": True,
              "background": "#ffffff"}

    old = write_project(os.path.join(TMP, "ancien.lvn"), {
        "format": "live_notes", "version": 1, "config": config,
        "strokes": [trace(0), trace(60)], "clears": [], "durationMs": 900,
        "media": None, "inPoint": None, "outPoint": None})

    page.attach_file("project-input", old)
    assert page.wait_for("getComputedStyle(document.getElementById('project-loading'))"
                         ".display === 'none'", timeout=20.0), \
        "le chargement du projet v1 ne s'est pas termine"
    page.pump(0.6)
    assert exported_layers(page) == [2], \
        "un projet v1 devrait donner une seule couche de deux traces"
    assert page.js("document.getElementById('tab-layers-count').textContent") == "1", \
        "l'onglet n'annonce pas une seule couche"

    recent = write_project(os.path.join(TMP, "couches.lvn"), {
        "format": "live_notes", "version": 2, "config": config,
        "layers": [
            {"id": 1, "name": "Fond", "visible": True,
             "strokes": [trace(0)], "clears": [], "durationMs": 400},
            {"id": 2, "name": "Ajouts", "visible": True,
             "strokes": [trace(60), trace(120)], "clears": [], "durationMs": 900},
        ],
        "activeLayer": 2, "durationMs": 900,
        "media": None, "inPoint": None, "outPoint": None})

    page.attach_file("project-input", recent)
    assert page.wait_for("getComputedStyle(document.getElementById('project-loading'))"
                         ".display === 'none'", timeout=20.0), \
        "le chargement du projet v2 ne s'est pas termine"
    page.pump(0.6)
    assert exported_layers(page) == [1, 2], "les couches du projet v2 ne sont pas revenues"
    assert page.js("document.getElementById('tab-layers-count').textContent") == "2", \
        "l'onglet n'annonce pas deux couches"

    # Une version future se reconnait comme telle, au lieu de passer pour un
    # fichier casse -- l'un demande une mise a jour, l'autre un autre fichier.
    future = write_project(os.path.join(TMP, "futur.lvn"), {
        "format": "live_notes", "version": 99, "config": config, "layers": []})
    page.attach_file("project-input", future)
    page.pump(0.8)
    assert "plus récente" in page.text("status"), \
        "un projet trop recent n'est pas annonce comme tel : %r" % page.text("status")
    return "v1 en une couche, v2 avec les siennes, version future annoncee"


def test_a_hidden_layer_leaves_the_payload(ctx):
    """L'oeil retire la couche de l'ecran ET du fichier livre : une seule
    notion, pas deux cases qui pourraient se contredire."""
    page = ctx["page"]
    fresh_layer(page)
    draw_stroke(page, 0.4)
    page.click("btn-layer-new")
    page.pump(0.4)
    draw_stroke(page, 0.75)
    with_both = canvas_ink(page)
    counts = exported_layers(page)
    assert counts[-2:] == [1, 1], "deux couches d'une trace attendues : %s" % counts

    # La couche active ne se masque pas : on ne dessine pas a l'aveugle.
    page.js("(function () {"
            "  document.querySelector('#layer-list .layer-row button').click();"
            "})()")
    page.pump(0.4)
    assert canvas_ink(page) == with_both, "la couche active a ete masquee"
    assert "avant de masquer" in page.text("status"), \
        "masquer la couche active n'est pas refuse : %r" % page.text("status")

    # L'oeil de la couche verrouillee, juste dessous dans la liste.
    page.js("(function () {"
            "  document.querySelectorAll('#layer-list .layer-row')[1]"
            "    .querySelector('button').click();"
            "})()")
    page.pump(0.5)
    # Lu avant tout export : ouvrir la fenetre de rendu reecrit la ligne d'etat.
    said = page.text("status")
    hidden_ink = canvas_ink(page)
    assert hidden_ink < with_both, "la couche masquee est toujours a l'ecran"
    assert "masqu" in said, "rien ne dit qu'une couche est masquee : %r" % said
    after = exported_layers(page)
    assert len(after) == len(counts) - 1, \
        "la couche masquee est encore dans la charge d'export : %s" % after
    assert after[-1] == 1, "ce n'est pas la bonne couche qui est partie : %s" % after
    assert "masqu" in page.js("document.getElementById('export-layers-note').textContent"), \
        "la fenetre d'export ne previent pas qu'une couche est masquee"

    page.js("(function () {"
            "  document.querySelectorAll('#layer-list .layer-row')[1]"
            "    .querySelector('button').click();"
            "})()")
    page.pump(0.4)
    return "une couche masquee quitte l'ecran et la charge ; l'active ne se masque pas"


def stage_width(page):
    """Largeur reelle du cadre a l'ecran : la mesure du zoom, vue du dehors."""
    return float(page.js("document.getElementById('media-container')"
                         ".getBoundingClientRect().width + ''"))


def test_a_pinch_zooms_the_canvas_and_never_draws(ctx):
    """Deux doigts qui s'ecartent zooment, et n'ecrivent rien.

    Le pincement du pave tactile arrive deja cuit sur un poste -- le navigateur
    le presente comme une molette avec `ctrlKey`. Sur une tablette, rien ne
    l'annonce : il faut le suivre doigt par doigt. Et deux doigts veulent
    naviguer : le trait que le premier avait commence est abandonne, jamais
    enregistre.
    """
    page = ctx["page"]
    page.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
    try:
        fresh_layer(page)
        page.js("document.getElementById('zoom-fit').click();")
        page.pump(0.4)
        before = stage_width(page)

        box = page.js("(function () {"
                      "  var r = document.getElementById('drawing-canvas')"
                      "    .getBoundingClientRect();"
                      "  return [r.left + r.width / 2, r.top + r.height / 2].join(',');"
                      "})()")
        cx, cy = [float(value) for value in box.split(",")]

        # Un doigt se pose et glisse : sans le second, ce serait un trait.
        page.input("Input.dispatchTouchEvent", type="touchStart",
                   touchPoints=[{"x": cx - 30, "y": cy, "id": 1}])
        page.input("Input.dispatchTouchEvent", type="touchMove",
                   touchPoints=[{"x": cx - 40, "y": cy, "id": 1}])
        # Le second arrive : le trait est abandonne, le pincement commence.
        page.input("Input.dispatchTouchEvent", type="touchStart",
                   touchPoints=[{"x": cx - 40, "y": cy, "id": 1},
                                {"x": cx + 40, "y": cy, "id": 2}])
        for step in range(1, 7):
            spread = 40 + step * 24
            page.input("Input.dispatchTouchEvent", type="touchMove",
                       touchPoints=[{"x": cx - spread, "y": cy, "id": 1},
                                    {"x": cx + spread, "y": cy, "id": 2}])
        page.input("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
        page.pump(0.5)

        after = stage_width(page)
        assert after > before * 1.15, \
            "le pincement n'a pas zoome (%.0f px puis %.0f px)" % (before, after)
        assert exported_layers(page)[-1] == 0, \
            "le trait commence avant le second doigt a ete enregistre"

        page.js("document.getElementById('zoom-fit').click();")
        page.pump(0.3)
    finally:
        page.call("Emulation.setTouchEmulationEnabled", enabled=False)
    return "pincement : %.0f px -> %.0f px, aucune trace posee" % (before, after)


def tap_space(page):
    page.input("Input.dispatchKeyEvent", type="keyDown", code="Space", key=" ",
               windowsVirtualKeyCode=32, nativeVirtualKeyCode=32)
    page.input("Input.dispatchKeyEvent", type="keyUp", code="Space", key=" ",
               windowsVirtualKeyCode=32, nativeVirtualKeyCode=32)
    page.pump(0.6)


def test_space_still_plays_and_pauses(ctx):
    """Espace reste la lecture / pause du bus, malgre le mode main.

    Espace maintenu + clic passe en mode main : la bascule du lecteur se joue
    donc au relachement, et seulement si personne ne s'en est servi entre-temps.
    Un appui simple doit continuer de jouer et de mettre en pause.
    """
    page = ctx["page"]
    open_on_server(ctx, ctx["rush_open_long"])
    assert page.wait_for("!!window.MediaTransport && window.MediaTransport.isTimed()",
                         timeout=25.0), "le rush n'est pas pret"
    assert page.js("document.getElementById('bg-media').paused + ''") == "true", \
        "le rush joue deja : l'appui ne prouverait rien"

    tap_space(page)
    assert page.wait_for("!document.getElementById('bg-media').paused", timeout=6.0), \
        "un appui sur Espace n'a pas lance la lecture"

    tap_space(page)
    assert page.wait_for("document.getElementById('bg-media').paused", timeout=6.0), \
        "un second appui sur Espace n'a pas mis en pause"
    return "Espace joue puis met en pause"


def exported_payload(page):
    """La charge d'export entiere, interceptee avant le depart."""
    page.js("window.__alerts = [];"
            "window.__realAlert = window.__realAlert || window.alert;"
            "window.alert = function (text) { window.__alerts.push(text); };")
    page.js("window.__exported = null;"
            "window.__realFetch = window.__realFetch || window.fetch;"
            "window.fetch = function (url, options) {"
            "  if (String(url).indexOf('/api/export') === 0 && options && options.body) {"
            "    window.__exported = JSON.parse(options.body);"
            "    return Promise.resolve(new Response('{\"error\": \"banc\"}',"
            "      {status: 200, headers: {'Content-Type': 'application/json'}}));"
            "  }"
            "  return window.__realFetch.apply(window, arguments);"
            "};")
    page.click("export-go")
    page.pump(1.0)
    raw = page.js("JSON.stringify(window.__exported)")
    page.js("window.fetch = window.__realFetch; window.alert = window.__realAlert;")
    assert raw and raw != "null", "l'export n'a pas ete declenche"
    return json.loads(raw)


def test_splitting_the_trace_layers_is_offered_only_beyond_one(ctx):
    """La case ne s'offre qu'au-dela d'une couche, et le nom des fichiers suit.

    Sur un projet a une seule couche la question ne se pose pas : pas de case,
    pas de numero, exactement le dossier a trois fichiers d'avant.
    """
    page = ctx["page"]
    open_on_server(ctx, ctx["rush_open_long"])
    page.pump(0.8)

    # Une seule couche qui porte quelque chose : on masque tout le reste.
    fresh_layer(page)
    draw_stroke(page, 0.4)
    page.js("""(function () {
      var rows = document.querySelectorAll('#layer-list .layer-row');
      for (var i = 1; i < rows.length; i++) {
        var row = rows[i];
        if (row.className.indexOf('is-hidden') < 0) row.querySelector('button').click();
      }
    })()""")
    page.pump(0.6)

    page.click("btn-export")
    page.pump(0.6)
    page.js("document.getElementById('export-mode').value = 'pro';"
            "document.getElementById('export-mode').dispatchEvent(new Event('change'));")
    page.pump(0.5)
    assert page.js("getComputedStyle(document.getElementById('export-mode')"
                   ".options[2]).display") != "none", "l'export pro n'est pas propose"
    assert page.js("document.getElementById('export-mode').options[2].disabled + ''") == "false", \
        ("l'export pro est grise alors qu'un media est ouvert : %s"
         % page.text("export-pro-note"))
    assert page.display("export-split-row") == "none", \
        "la case de scission s'offre sur un projet a une seule couche"
    files = page.text("export-files")
    assert "_trace.mov" in files and "_trace_1" not in files, files

    # Deuxieme couche : la case apparait, et les noms se numerotent.
    page.js("document.getElementById('export-cancel').click();")
    page.pump(0.4)
    page.click("btn-layer-new")
    page.pump(0.4)
    draw_stroke(page, 0.7)
    page.click("btn-export")
    page.pump(0.6)
    page.js("document.getElementById('export-mode').value = 'pro';"
            "document.getElementById('export-mode').dispatchEvent(new Event('change'));")
    page.pump(0.5)
    assert page.display("export-split-row") != "none", \
        "la case de scission manque sur un projet a deux couches"

    page.js("var box = document.getElementById('export-split');"
            "box.checked = true; box.dispatchEvent(new Event('change'));")
    page.pump(0.5)
    files = page.text("export-files")
    assert "_trace_1.mov" in files and "_trace_2.mov" in files and "_trace.mov" not in files, files

    sent = exported_payload(page)
    assert sent.get("splitLayers") is True, "la charge ne demande pas la scission"
    assert len(sent.get("layers") or []) == 2, sent.get("layers")
    return "case absente a une couche, presente et suivie a deux"


def test_the_tablet_buttons_are_not_deaf_to_the_finger(ctx):
    """Les deux boutons isoles de l'interface tablette doivent recevoir le doigt.

    #tablet-ui est transparent aux evenements pour qu'un trace commence a cote
    d'une bulle sans l'activer. Les elements cliquables doivent donc reprendre
    la main un par un -- les bulles le faisaient, les deux boutons isoles non :
    l'appui les traversait et posait un point sur le canevas.
    """
    page = ctx["page"]
    unreachable = page.js(
        "(function () {"
        "  document.body.classList.add('tablet');"
        "  var ids = ['tablet-return', 'tablet-fullscreen'];"
        "  var deaf = ids.filter(function (id) {"
        "    var node = document.getElementById(id);"
        "    return !node || getComputedStyle(node).pointerEvents === 'none';"
        "  });"
        "  document.body.classList.remove('tablet');"
        "  return deaf.join(',');"
        "})()")
    assert unreachable == "", "boutons sourds au doigt : %s" % unreachable
    return "PC et plein ecran recoivent les evenements"


def test_the_toolbar_holds_on_one_row(ctx):
    """La barre d'outils ne doit pas se couper en deux.

    Avec des libelles en toutes lettres, les sept boutons d'action passaient a
    la ligne des qu'on descendait sous ~1500 px : c'est la hauteur du plan de
    travail qui y passait, sur les ecrans qui en ont le moins. Le test mesure
    la rangee, pas l'apparence -- une icone qui grossirait ou un bouton ajoute
    referaient le probleme sans qu'on s'en apercoive.
    """
    page = ctx["page"]
    verdicts = []
    for width, height in ((1280, 800), (1440, 900), (1920, 1080)):
        page.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                  deviceScaleFactor=1, mobile=False)
        page.pump(0.5)
        raw = page.js("""(function(){
          var groupe = document.querySelector('.toolbar-group');
          var actions = document.querySelector('.toolbar-actions');
          return JSON.stringify({
            meme_rangee: Math.abs(groupe.getBoundingClientRect().top
                                  - actions.getBoundingClientRect().top) < 30,
            deborde: document.documentElement.scrollWidth
                     > document.documentElement.clientWidth
          });
        })()""")
        state = json.loads(raw)
        assert state["meme_rangee"], "barre coupee en deux a %dx%d" % (width, height)
        assert not state["deborde"], "debordement horizontal a %dx%d" % (width, height)
        verdicts.append("%d ok" % width)
    page.call("Emulation.clearDeviceMetricsOverride")
    page.pump(0.4)
    return ", ".join(verdicts)


def test_the_tool_buttons_keep_their_icon(ctx):
    """Changer le libelle d'un bouton ne doit pas emporter son icone.

    Les boutons portent une icone SVG *et* une legende : ecrire dans leur
    `textContent` remplacerait les deux par du texte. L'enregistrement et la
    previsualisation renomment les leurs en cours de route -- c'est la que le
    piege se referme.
    """
    page = ctx["page"]
    missing = page.js("""(function(){
      var ids = ['btn-tablet','btn-clear','btn-rec','btn-stop','btn-preview',
                 'btn-export','btn-save-project','btn-load-project'];
      return ids.filter(function(id){
        var n = document.getElementById(id);
        return !n || !n.querySelector('svg') || !n.querySelector('span');
      }).join(',');
    })()""")
    assert missing == "", "boutons sans icone ou sans legende : %s" % missing

    # Le renommage de la previsualisation passe par la legende seule.
    page.js("(function(){ var b = document.getElementById('btn-preview');"
            " b.querySelector('span').textContent = 'Arrêter'; })()")
    still = page.js("!!document.getElementById('btn-preview').querySelector('svg')")
    assert still, "l'icone a disparu au renommage"
    return "8 boutons : icone + legende conservees"


def test_nothing_threw_along_the_way(ctx):
    # Le test qui compte le plus : tout ce qui precede peut « marcher » en
    # apparence pendant qu'une exception laisse un bout d'interface mort.
    errors = ctx["page"].exceptions()
    assert not errors, "exceptions : %s" % errors
    return "0 exception sur tout le parcours"


def test_a_lan_screen_is_told_it_is_not_the_pc(ctx):
    # Les routes d'appairage sont reservees a la boucle locale. Ouvrir
    # l'interface par une adresse reseau donnait une fenetre vide.
    addresses = sessions.lan_addresses()
    if not addresses:
        return "ignore : aucune adresse LAN sur cette machine"
    page = ctx["lan_page"]
    page.goto("http://%s:%d/" % (addresses[0], ctx["server"].port))
    page.click("btn-create")
    page.click("btn-tablet")
    assert page.wait_for(
        "document.getElementById('tablet-state').textContent.indexOf('127.0.0.1') >= 0"
    ), "message obtenu : %r" % page.text("tablet-state")
    assert page.js("document.getElementById('tablet-qr').childElementCount") == 0
    return "403 explique, sans QR trompeur"


def test_a_stopped_server_says_so(ctx):
    """La panne signalee : l'interface reste a l'ecran et parait vivante."""
    server = Server(ctx["server"].proxy_format)
    page = Browser(ctx["chrome"], os.path.join(TMP, "profil_arret"))
    try:
        page.goto(server.url)
        page.click("btn-create")
        page.wait_for(
            "getComputedStyle(document.getElementById('setup-screen')).display === 'none'")
        server.stop()
        time.sleep(1.0)
        page.click("btn-tablet")
        assert page.wait_for(
            "document.getElementById('tablet-state').textContent.indexOf('injoignable') >= 0",
            timeout=10.0
        ), "message obtenu : %r" % page.text("tablet-state")
        return "« serveur injoignable » plutot qu'un carre blanc"
    finally:
        page.close()
        server.stop()


TESTS = [
    test_the_workspace_opens_without_a_single_exception,
    test_pairing_shows_a_qr_code,
    test_the_qr_image_is_really_served,
    test_a_mismatched_ratio_asks_how_to_place_the_media,
    test_cropping_travels_to_the_tablet,
    test_releasing_the_media_resets_the_published_cropping,
    test_the_background_travels_too,
    test_a_pdf_page_becomes_an_annotatable_image,
    test_arming_the_tablet_does_not_reask_a_settled_cropping,
    test_the_cropped_image_covers_the_whole_stage,
    test_the_page_button_appears_only_for_a_pdf,
    test_the_bounds_land_on_the_frame_grid,
    test_the_playhead_parks_on_the_last_frame_in_range,
    test_the_bounds_survive_a_proxy_swap,
    test_another_rush_does_not_inherit_the_bounds,
    test_pairing_closes_itself_once_the_tablet_has_the_stylus,
    test_the_out_point_names_the_image_it_freezes,
    test_a_late_player_clock_never_shows_the_image_after_the_out,
    test_a_rush_off_the_project_cadence_says_so,
    test_a_stroke_across_the_out_point_reaches_the_pc_whole,
    test_undo_takes_the_stroke_off_the_canvas_and_out_of_the_payload,
    test_recording_only_wipes_the_active_layer,
    test_a_hidden_layer_leaves_the_payload,
    test_a_v1_project_opens_as_a_single_layer,
    test_a_pinch_zooms_the_canvas_and_never_draws,
    test_space_still_plays_and_pauses,
    test_splitting_the_trace_layers_is_offered_only_beyond_one,
    test_the_tablet_buttons_are_not_deaf_to_the_finger,
    test_the_toolbar_holds_on_one_row,
    test_the_tool_buttons_keep_their_icon,
    test_nothing_threw_along_the_way,
    test_a_lan_screen_is_told_it_is_not_the_pc,
    test_a_stopped_server_says_so,
]


class Skipped(Exception):
    """Le banc ne peut pas se prononcer -- et le dit, plutot que d'echouer."""


def h264_available(page):
    """Chromium n'embarque pas toujours H.264.

    Les builds « Chromium » nus (celui de Playwright, la plupart des paquets
    Linux) sont livres sans codecs proprietaires, la ou Chrome et Edge les ont.
    Un rush en H.264 n'y decode pas : `videoWidth` reste a zero, `onMediaReady`
    ne part jamais, et tout ce qui depend d'un media a l'ecran tombe.

    Le banc ne demande plus ce codec a personne -- ses rushes sont en VP9, que
    tout navigateur decode. Reste la seule chose qu'il ne fabrique pas lui-meme :
    le format des copies de lecture, decide par le serveur. La reponse sert donc
    a le choisir, une fois, au demarrage.
    """
    return bool(page.js(
        "!!document.createElement('video')"
        ".canPlayType('video/mp4; codecs=\"avc1.42E01E\"')"))


def main():
    chrome = find_chrome()
    if not chrome:
        print("Interface — IGNORE : aucun Chrome ni Edge trouve sur cette machine.")
        print("  Rien n'a ete verifie. LIVE_NOTES_CHROME=<chemin> pour en designer un.")
        return 0

    print("Interface — %s" % os.path.basename(chrome))
    server = None
    page = None
    lan_page = None
    failures = 0
    skipped = 0
    chosen = TESTS
    try:
        # Les navigateurs d'abord : c'est ce que celui-ci sait decoder qui
        # decide du format des copies de lecture, donc de l'environnement du
        # serveur. Un format impose a la main est respecte tel quel -- c'est
        # alors le codec lui-meme que l'on vient verifier.
        page = Browser(chrome, os.path.join(TMP, "profil"))
        lan_page = Browser(chrome, os.path.join(TMP, "profil_lan"))
        h264 = h264_available(page)
        proxy_format = (os.environ.get("LIVE_NOTES_PROXY_FORMAT")
                        or ("h264" if h264 else "vp9"))
        if not h264 and proxy_format != "h264":
            print("  (ce navigateur n'a pas H.264 : les copies de lecture sont"
                  " demandees en %s.)" % proxy_format)
        elif not h264:
            print("  (ce navigateur n'a pas H.264, mais LIVE_NOTES_PROXY_FORMAT"
                  " impose ce format : la copie de lecture ne s'y verifiera pas.)")

        server = Server(proxy_format)
        ctx = {"server": server, "page": page, "lan_page": lan_page,
               "chrome": chrome, "h264": h264,
               "rush43": make_rush("rush43.webm", "640x480"),
               "rush_open": make_rush("rush_libre.webm", "640x360"),
               "rush_open_long": make_rush("rush_libre_long.webm", "640x360", 4),
               "rush_24": make_rush("rush_24.webm", "640x360", 4, rate=24),
               "pdf": make_pdf("document.pdf")}

        # `py test_ui.py <bout de nom>` ne rejoue qu'une partie du banc. La
        # creation de l'espace de travail reste en tete : tout le reste s'y
        # appuie, et un banc qui « passe » sans elle ne prouverait rien.
        wanted = sys.argv[1] if len(sys.argv) > 1 else ""
        chosen = [test for test in TESTS
                  if not wanted or wanted in test.__name__ or test is TESTS[0]]

        for test in chosen:
            try:
                detail = test(ctx)
                print("  OK   %-52s %s" % (test.__name__, detail or ""))
            except Skipped as reason:
                skipped += 1
                print("  SKIP %-52s %s" % (test.__name__, reason))
            except Exception as exc:  # noqa: BLE001 - rapport de test
                failures += 1
                print("  FAIL %-52s %s: %s"
                      % (test.__name__, type(exc).__name__, exc))
    finally:
        for closeable in (page, lan_page):
            if closeable:
                closeable.close()
        if server:
            server.stop()
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n%d/%d tests passes%s"
          % (len(chosen) - failures - skipped, len(chosen),
             (" (%d ignores)" % skipped) if skipped else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
