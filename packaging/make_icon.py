"""Genere l'icone de l'application, aux formats attendus par chaque OS.

L'icone est dessinee par code plutot que commitee comme image : meme
logique que le binaire FFmpeg, rien de binaire dans le depot et un
resultat reproductible. Sortie dans packaging/vendor/icons/ (ignore par
git), consomme par packaging/live_notes.spec et packaging/installer.iss.

Motif : un trace de pinceau a largeur variable — le geste que l'appli
enregistre — pose sur le gris neutre 50 % qui sert de reference
colorimetrique a l'interface, avec la pastille rouge de l'enregistrement.
Palette reprise telle quelle de index.html.

Usage : python packaging/make_icon.py
"""

import os
import sys

from PIL import Image, ImageDraw

SIZE = 1024          # cote de l'icone maitresse
SUPERSAMPLE = 4      # trace en 4x puis reduit : anticrenelage propre
W = SIZE * SUPERSAMPLE

GREY = (128, 128, 128)      # --workspace #808080
REC = (182, 51, 43)         # --rec #b6332b
NEAR_BLACK = (19, 19, 19)   # --text #131313

# Tailles embarquees dans le .ico Windows (explorateur, barre des taches,
# alt-tab) ; le .icns macOS est genere par Pillow a partir de la maitresse.
ICO_SIZES = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "icons")


def _quadratic(p0, p1, p2, steps=400):
    pts = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        x = u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0]
        y = u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]
        pts.append((x, y))
    return pts


def _brush_stroke(draw, pts, w_start, w_end, color):
    """Trace a largeur decroissante : disques poses le long de la courbe,
    comme le moteur de brush de l'appli (attaque appuyee, sortie effilee)."""
    n = len(pts)
    for i, (x, y) in enumerate(pts):
        t = i / max(1, n - 1)
        r = (w_start + (w_end - w_start) * t) / 2
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color)


def render():
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Fond arrondi facon squircle macOS (rayon ~22 %), neutre sur Windows.
    draw.rounded_rectangle([0, 0, W - 1, W - 1], radius=int(W * 0.22), fill=GREY)

    stroke = _quadratic(
        (W * 0.16, W * 0.72), (W * 0.44, W * 0.26), (W * 0.86, W * 0.52)
    )
    _brush_stroke(draw, stroke, W * 0.17, W * 0.03, NEAR_BLACK)

    r = W * 0.115
    cx, cy = W * 0.735, W * 0.265
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=REC)

    return img.resize((SIZE, SIZE), Image.LANCZOS)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    icon = render()

    png_path = os.path.join(OUT_DIR, "icon.png")
    icon.save(png_path)

    ico_path = os.path.join(OUT_DIR, "icon.ico")
    icon.save(ico_path, sizes=ICO_SIZES)

    icns_path = os.path.join(OUT_DIR, "icon.icns")
    icon.save(icns_path)

    favicon_path = os.path.join(OUT_DIR, "favicon.png")
    favicon = icon.resize((32, 32), Image.LANCZOS)
    favicon.save(favicon_path)

    for path in (png_path, ico_path, icns_path, favicon_path):
        print(f"{path} ({os.path.getsize(path)} octets)")


if __name__ == "__main__":
    sys.exit(main())
