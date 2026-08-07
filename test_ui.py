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
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import simple_websocket

import proxy
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
        self.proc = subprocess.Popen([
            chrome, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions",
            "--remote-debugging-port=%d" % self.port,
            "--user-data-dir=" + profile, "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
        self.js("document.getElementById('%s').click()" % element_id)

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

    def wait_for(self, expression, timeout=6.0):
        """Attend qu'une expression devienne vraie plutot que de dormir."""
        end = time.time() + timeout
        while time.time() < end:
            if self.js(expression, wait=0.4):
                return True
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
    def __init__(self):
        self.port = free_port()
        env = dict(os.environ, LIVE_NOTES_PORT=str(self.port),
                   LIVE_NOTES_HOST="0.0.0.0")
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


def make_rush(name, size):
    """Un vrai fichier, lisible par le navigateur : le banc ne simule rien."""
    path = os.path.join(TMP, name)
    subprocess.run([renderer.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i",
                    "testsrc=size=%s:rate=25:duration=2" % size,
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", path], check=True)
    return path


def make_open_rush(name, size, seconds=2):
    """Meme chose, dans un codec que *tout* navigateur decode.

    Les builds « Chromium » nus n'ont pas H.264 (voir `h264_available`), et
    c'est justement le lecteur -- timecode, bornes IN/OUT, tete de lecture --
    qu'on ne pourrait alors jamais verifier sur ce banc. VP9 dans un WebM est
    libre : il decode partout, et le transport ne fait aucune difference entre
    les deux.
    """
    path = os.path.join(TMP, name)
    subprocess.run([renderer.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i",
                    "testsrc=size=%s:rate=25:duration=%d" % (size, seconds),
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
    if not ctx["h264"]:
        raise Skipped("navigateur sans H.264 : le rush ne decode pas")
    page.attach_file("media-input", ctx["rush43"])
    assert page.wait_for(
        "getComputedStyle(document.getElementById('crop-screen')).display === 'flex'"
    ), "la fenetre de cadrage ne s'est pas ouverte"
    return "640x480 sur un canevas 16:9 : la question est posee"


def test_cropping_travels_to_the_tablet(ctx):
    # Le coeur du mode tablette : sans cette publication, le poste recadre et
    # la tablette montre autre chose.
    page = ctx["page"]
    # Sans la fenetre de cadrage ouverte par le test precedent, il n'y a rien
    # a valider : piloter ses controles a vide ne prouverait rien et laisserait
    # une exception derriere soi.
    if not ctx["h264"]:
        raise Skipped("navigateur sans H.264 : pas de fenetre de cadrage ouverte")
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
    # La copie est transcodee dans le format de proxy du serveur : un banc dont
    # le navigateur n'a pas ce codec ne verrait jamais le nouveau rush arriver a
    # l'ecran, et l'echec parlerait du navigateur, pas des bornes.
    if not ctx["h264"] and proxy.PROXY_FORMAT == "h264":
        raise Skipped("navigateur sans H.264 : la copie de lecture ne decoderait"
                      " pas (LIVE_NOTES_PROXY_FORMAT=vp9 leve la limite)")

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
    server = Server()
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
    Linux) sont livres sans codecs proprietaires, la ou Chrome et Edge les
    ont. Un rush en H.264 n'y decode pas : `videoWidth` reste a zero,
    `onMediaReady` ne part jamais, et tout ce qui depend d'un media a l'ecran
    tombe. C'est une lacune du navigateur, pas de l'application -- la faire
    passer pour un echec enverrait chercher au mauvais endroit.
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
    try:
        server = Server()
        page = Browser(chrome, os.path.join(TMP, "profil"))
        lan_page = Browser(chrome, os.path.join(TMP, "profil_lan"))
        ctx = {"server": server, "page": page, "lan_page": lan_page,
               "chrome": chrome, "rush43": make_rush("rush43.mp4", "640x480"),
               "rush_open": make_open_rush("rush_libre.webm", "640x360"),
               "rush_open_long": make_open_rush("rush_libre_long.webm", "640x360", 4),
               "pdf": make_pdf("document.pdf")}

        ctx["h264"] = h264_available(page)
        if not ctx["h264"]:
            print("  (ce navigateur n'a pas H.264 : les tests qui exigent un")
            print("   rush video seront IGNORES, pas comptes comme des echecs.)")

        for test in TESTS:
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
          % (len(TESTS) - failures - skipped, len(TESTS),
             (" (%d ignores)" % skipped) if skipped else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
