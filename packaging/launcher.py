"""Point d'entree du build desktop (PyInstaller).

Sert app.py dans une fenetre native plutot que dans un onglet du navigateur
par defaut de l'utilisateur.ice. Ce fichier importe app.py tel quel : aucune
modification du code applicatif n'est necessaire pour ce lanceur en lui-meme
(la resolution des chemins figes est geree par paths.py, partagee avec
app.py/brush_engine.py).

Bibliotheque de fenetrage differente selon l'OS :
- macOS : pywebview (WKWebView, via le pont pyobjc). Stable une fois fige.
- Windows : lancement direct de Chrome ou Edge (le premier trouve) en mode
  "app" (sans barre d'adresse) via un sous-processus (voir
  _find_windows_chromium_browser/_open_windows_app_window plus bas).
  pywebview sur Windows pilote WebView2 via pythonnet/clr, qui casse de
  facon connue et non resolue une fois fige par PyInstaller — "Failed to
  resolve Python.Runtime.Loader.Initialize" (voir issues r0x0r/pywebview
  #1215, #1292, #1638). Le paquet tiers flaskwebgui, qui fait la meme chose,
  a ete essaye puis abandonne : sa version resolue par pip sous Python <3.12
  contient elle-meme un SyntaxError (f-string imbriquee, syntaxe reservee a
  3.12+, cf. son propre `Requires-Python >=3.12` depuis la 1.1.9) — verifie
  par un build local sur cette machine (Python 3.11). Autant garder le
  controle avec quelques lignes sans dependance supplementaire. Edge n'est
  pas garanti present (desinstallable), d'ou la recherche de Chrome aussi.

Le chemin FFmpeg doit etre fixe AVANT l'import de app/renderer : renderer.py
lit LIVE_NOTES_FFMPEG au chargement du module, pas a l'appel.
"""

import os
import sys
import threading

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if not getattr(sys, "frozen", False):
    # Execution locale via `python packaging/launcher.py` (hors PyInstaller) :
    # il faut ajouter la racine du depot au path pour trouver app.py/paths.py.
    sys.path.insert(0, REPO_ROOT)

import nativedialog  # noqa: E402
import paths  # noqa: E402

# Avant tout le reste : une application figee re-executee pour ouvrir un
# selecteur de fichier ne doit surtout pas demarrer un serveur. Cet appel ne
# rend la main que si la ligne de commande ne demande pas de selecteur (voir
# nativedialog.py, qui explique pourquoi ce detour est necessaire).
nativedialog.run_picker_if_requested()


def _bundled_binary(name):
    """Chemin d'un binaire vendorise, embarque a cote des autres assets
    (voir packaging/live_notes.spec) et donc trouvable via le meme
    paths.resource_dir() qu'utilisent app.py/brush_engine.py."""
    if getattr(sys, "frozen", False):
        base = paths.resource_dir()
    else:
        base = os.path.join(REPO_ROOT, "packaging", "vendor", "ffmpeg")
    candidate = os.path.join(base, name + (".exe" if sys.platform == "win32" else ""))
    return candidate if os.path.exists(candidate) else None


_ffmpeg = _bundled_binary("ffmpeg")
if _ffmpeg:
    os.environ.setdefault("LIVE_NOTES_FFMPEG", _ffmpeg)

# ffprobe sert a analyser un rush avant d'en preparer un proxy de lecture
# (voir proxy.py). proxy.py le chercherait de toute facon a cote de ffmpeg,
# mais le designer explicitement evite de dependre de cette deduction.
_ffprobe = _bundled_binary("ffprobe")
if _ffprobe:
    os.environ.setdefault("LIVE_NOTES_FFPROBE", _ffprobe)

import app as live_notes_app  # noqa: E402


def _run_server(port):
    # Ecoute sur le reseau local, pas seulement sur la boucle : c'est ce qui
    # permet a une tablette de rejoindre la session. Les routes qui touchent au
    # disque restent verrouillees sur la machine hote (voir `pc_only` dans
    # app.py), et LIVE_NOTES_HOST=127.0.0.1 referme le serveur pour qui n'a pas
    # l'usage du mode tablette.
    live_notes_app.app.run(host=live_notes_app.listen_host(), port=port,
                           debug=False, threaded=True, use_reloader=False)


def _find_windows_chromium_browser():
    """Chemin de chrome.exe ou msedge.exe (le premier trouve) via la cle App
    Paths du registre : fiable quel que soit l'emplacement d'installation
    (contrairement a un chemin en dur type Program Files). Chrome cherche
    en premier : Edge, bien que livre par defaut avec Windows, reste
    desinstallable — pas une garantie fiable a lui seul."""
    import winreg

    for name in ("chrome.exe", "msedge.exe"):
        key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}"
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    path, _ = winreg.QueryValueEx(key, None)
                    if path and os.path.exists(path):
                        return path
            except OSError:
                continue
    return None


def _wait_for_server(url, timeout=10):
    """Attend que le serveur Flask reponde avant d'ouvrir la fenetre : sans
    ca, le sous-processus navigateur (ou pywebview) demarre plus vite que
    app.run() ne finit de se lier au port, et affiche une erreur de connexion
    au premier chargement au lieu de l'application."""
    import time
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return
        except Exception:
            time.sleep(0.1)


def _open_windows_app_window(url):
    """Ouvre `url` dans une fenetre Chrome/Edge sans barre d'adresse ni
    onglets (mode "app", utilise aussi par les PWA installees). Bloque
    jusqu'a la fermeture de la fenetre. Repli sur le navigateur par defaut
    si aucun des deux n'est installe.

    --user-data-dir pointe vers un profil dedie (pas celui de
    l'utilisateur.ice) : sans ca, si le navigateur tourne deja (cas
    courant), Chromium detecte l'instance unique existante, lui delegue
    simplement l'ouverture et quitte aussitot — le sous-processus qu'on
    lance ne represente alors plus la fenetre reelle, et `proc.wait()`
    revient immediatement au lieu d'attendre sa fermeture."""
    import subprocess

    browser = _find_windows_chromium_browser()
    if not browser:
        import webbrowser

        webbrowser.open(url)
        return

    profile_dir = os.path.join(paths.user_data_dir(), "app-browser-profile")
    proc = subprocess.Popen([
        browser,
        f"--app={url}",
        "--window-size=1440,900",
        f"--user-data-dir={profile_dir}",
    ])
    proc.wait()


def main():
    # Le port est arrete ici, avant de demarrer quoi que ce soit : la fenetre
    # doit ouvrir l'URL reelle. Sur macOS le port 5000 est pris par le
    # recepteur AirPlay, et la fenetre restait blanche (voir
    # live_notes_app.choose_port).
    port = live_notes_app.choose_port()
    threading.Thread(target=_run_server, args=(port,), daemon=True).start()
    url = "http://127.0.0.1:%d" % port
    _wait_for_server(url)

    if sys.platform == "win32":
        _open_windows_app_window(url)
    else:
        import webview

        webview.create_window(
            "live_notes",
            url,
            width=1440,
            height=900,
            min_size=(1024, 700),
        )
        webview.start()


if __name__ == "__main__":
    main()
