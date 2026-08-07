# -*- mode: python ; coding: utf-8 -*-
"""Spec PyInstaller — build "onedir" (dossier portable), pas "onefile".

Choix delibere : un build onefile s'auto-extrait dans un dossier temporaire
different a chaque lancement, ce qui aurait ete plus contraignant pour les
donnees persistantes (voir paths.py, qui gere neanmoins les deux cas via
sys.frozen + un dossier de donnees utilisateur.ice standard). Le onedir
evite aussi les faux positifs antivirus frequents avec les exe
auto-extractibles onefile.

Resolution des chemins (assets embarques, presets, dossier d'export par
defaut) : voir paths.py, partage par app.py/brush_engine.py/ce spec.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

# `__file__` n'est pas defini dans l'espace d'execution d'un fichier .spec ;
# PyInstaller y injecte a la place `SPECPATH` (dossier contenant ce .spec).
REPO_ROOT = Path(SPECPATH).resolve().parent  # noqa: F821
VENDOR_FFMPEG = REPO_ROOT / "packaging" / "vendor" / "ffmpeg"
EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
FFMPEG_BIN = VENDOR_FFMPEG / ("ffmpeg" + EXE_SUFFIX)
# ffprobe accompagne ffmpeg : proxy.py analyse chaque rush avant de decider
# s'il faut en preparer une copie lisible par un navigateur.
FFPROBE_BIN = VENDOR_FFMPEG / ("ffprobe" + EXE_SUFFIX)

# Icones generees par packaging/make_icon.py (non commitees). Absentes,
# le build reste possible avec l'icone systeme par defaut.
VENDOR_ICONS = REPO_ROOT / "packaging" / "vendor" / "icons"
ICON_FILE = VENDOR_ICONS / ("icon.ico" if sys.platform == "win32" else "icon.icns")
ICON = str(ICON_FILE) if ICON_FILE.exists() else None

datas = [
    (str(REPO_ROOT / "index.html"), "."),
    (str(REPO_ROOT / "static"), "static"),
    (str(REPO_ROOT / "brushes.json"), "."),
]

# Favicon web (genere par packaging/make_icon.py). Absent, la navigation
# en mode app Chrome/Edge sur Windows affichera le globe par defaut.
if VENDOR_ICONS.exists():
    favicon = VENDOR_ICONS / "favicon.png"
    if favicon.exists():
        datas.append((str(favicon), "."))

binaries = []
for binary in (FFMPEG_BIN, FFPROBE_BIN):
    if binary.exists():
        binaries.append((str(binary), "."))

# pdfium (rasterisation des PDF, voir pdfdoc.py) n'est pas telecharge comme
# FFmpeg : il arrive dans la roue pypdfium2, et l'analyse statique de
# PyInstaller ne voit pas une bibliotheque chargee par ctypes.
#
# La destination reste `pypdfium2_raw` et non `.` comme pour FFmpeg : le
# chargeur de pypdfium2 cherche sa bibliotheque dans le dossier de son propre
# paquet. L'aplatir a la racine la rendrait introuvable.
binaries += collect_dynamic_libs("pypdfium2_raw")

a = Analysis(
    [str(REPO_ROOT / "packaging" / "launcher.py")],
    pathex=[str(REPO_ROOT)],
    binaries=binaries,
    datas=datas,
    # `qrcode` n'est importe qu'au moment d'afficher le code d'appairage
    # (session.qr_svg), et `pypdfium2` qu'a l'ouverture d'un PDF (import
    # paresseux dans pdfdoc, pour que les bancs sans rapport tournent sans la
    # dependance) : un import a l'interieur d'une fonction echappe a l'analyse
    # statique de PyInstaller, il faut donc les declarer.
    hiddenimports=["qrcode", "pypdfium2", "pypdfium2_raw"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="live_notes",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="live_notes",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="live_notes.app",
        icon=ICON,
        bundle_identifier="com.hoko-productions.live-notes",
    )
