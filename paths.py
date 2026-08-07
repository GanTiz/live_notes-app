"""Resolution des chemins applicatifs.

Centralise ici pour que app.py et brush_engine.py partagent la meme
logique, et pour qu'elle reste correcte a la fois en execution normale
(`python app.py`) et une fois le code fige par PyInstaller (packaging
Windows/macOS) :

- en execution normale, `__file__` suffit et pointe vers la racine du
  depot ;
- fige en "onedir" (ou onefile), `__file__` ne pointe plus vers un
  dossier fiable : PyInstaller expose `sys._MEIPASS`, qui lui reste
  correct dans les deux modes, pour retrouver les assets embarques.
"""

import os
import sys
import tempfile


def resource_dir():
    """Dossier des assets embarques en lecture seule (index.html,
    static/, brushes.json, binaire FFmpeg vendorise)."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def user_data_dir():
    """Dossier ecrivable et stable pour les donnees utilisateur.ice
    persistantes (presets), independant de l'endroit ou l'utilisateur.ice
    pose le .exe/le dossier de l'app, et du dossier temporaire volatile
    d'un build onefile (recree a chaque lancement)."""
    if not getattr(sys, "frozen", False):
        # Mode developpement : comportement inchange, a cote du code source.
        return os.path.dirname(os.path.abspath(__file__))

    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")

    directory = os.path.join(base, "live_notes")
    os.makedirs(directory, exist_ok=True)
    return directory


def proxy_cache_dir():
    """Dossier des proxys de lecture (voir proxy.py).

    Volontairement dans le temporaire du systeme, et pas a cote des presets :
    ces fichiers sont des artefacts de visee, pesants et jetables. Ils sont
    effaces au lancement de chaque export et au demarrage du serveur ; les
    poser dans le temporaire garantit que meme un arret brutal ne laisse rien
    d'irrecuperable dans les donnees de l'utilisateur.ice."""
    return os.path.join(tempfile.gettempdir(), "live_notes-proxies")


def default_export_dir():
    """Dossier d'export propose par defaut : la bibliotheque Videos/Movies
    de l'utilisateur.ice, sous-dossier "live_notes" pour ne pas melanger
    les rendus avec le reste de la bibliotheque. Reste modifiable a chaque
    export via le selecteur de dossier natif (`/api/browse`)."""
    videos = _videos_library() or os.path.expanduser("~")
    return os.path.join(videos, "live_notes")


def _videos_library():
    if sys.platform == "win32":
        found = _windows_known_folder_videos()
        return found or os.path.join(os.path.expanduser("~"), "Videos")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Movies")
    # Linux : pas une cible de packaging, repli simple pour le dev.
    return os.path.join(os.path.expanduser("~"), "Videos")


def _windows_known_folder_videos():
    """Interroge l'API Windows (FOLDERID_Videos) plutot que de supposer
    "~/Videos" : gere la localisation (dossier affiche "Videos" mais
    nomme differemment selon la langue) et le cas ou l'utilisateur.ice a
    deplace sa bibliotheque vers un autre disque."""
    try:
        import ctypes
        import uuid

        folderid_videos = uuid.UUID("{18989B1D-99B5-455B-841C-AB7C74E4DDFC}")
        buf = ctypes.c_wchar_p()
        guid = ctypes.create_string_buffer(folderid_videos.bytes_le, 16)
        hresult = ctypes.windll.shell32.SHGetKnownFolderPath(guid, 0, 0, ctypes.byref(buf))
        if hresult != 0 or not buf.value:
            return None
        path = buf.value
        ctypes.windll.ole32.CoTaskMemFree(buf)
        return path
    except Exception:
        return None
