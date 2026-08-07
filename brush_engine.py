"""Moteur de pinceaux — cote Python (rendu final).

Ce module est le pendant exact de `static/brushes.js` (preview live dans le
navigateur). Les deux implementations partagent :
  - le meme format de pinceau (voir `brushes.json`)
  - le meme generateur pseudo-aleatoire (mulberry32, seede par trace)
  - le meme algorithme de repartition des empreintes le long du trace

Un trace n'est donc pas rejoue "a peu pres" a l'export : il est recalcule a
l'identique, mais a la resolution et au FPS demandes.

Principe : chaque pinceau est un tampon (stamp) applique le long du chemin tous
les `spacing * taille` pixels. Les variations (jitter, pression) sont tirees du
RNG seede, ce qui rend le rendu deterministe et reproductible.
"""

import base64
import io
import json
import math
import os
import re

import numpy as np
from PIL import Image

import paths

BASE_RES = 128  # resolution du masque de reference de chaque pinceau
ANGLE_STEP = 5.0  # quantification des angles pour le cache d'empreintes
SUBPIXEL_STEPS = 2  # quantification sous-pixel (1/2 px) pour le cache


# --------------------------------------------------------------------------
# RNG — portage exact de mulberry32 (identique a la version JS)
# --------------------------------------------------------------------------

class Rng:
    """mulberry32. Produit la meme sequence que son equivalent JavaScript."""

    __slots__ = ("a",)

    def __init__(self, seed):
        self.a = int(seed) & 0xFFFFFFFF

    def next(self):
        self.a = (self.a + 0x6D2B79F5) & 0xFFFFFFFF
        a = self.a
        t = ((a ^ (a >> 15)) * (1 | a)) & 0xFFFFFFFF
        t = (((t + (((t ^ (t >> 7)) * (61 | t)) & 0xFFFFFFFF)) & 0xFFFFFFFF) ^ t) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296.0

    def sym(self):
        """Tirage symetrique dans [-1, 1]."""
        return self.next() * 2.0 - 1.0


# --------------------------------------------------------------------------
# Chargement / normalisation des pinceaux
# --------------------------------------------------------------------------

_LIBRARY_PATH = os.path.join(paths.resource_dir(), "brushes.json")


def load_library():
    with open(_LIBRARY_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


_LIBRARY = load_library()
_DEFAULTS = _LIBRARY["defaults"]


def normalize(brush):
    """Complete un pinceau partiel avec les valeurs par defaut."""
    out = dict(_DEFAULTS)
    out["jitter"] = dict(_DEFAULTS["jitter"])
    out["pressure"] = dict(_DEFAULTS["pressure"])
    for key, value in (brush or {}).items():
        if key in ("jitter", "pressure") and isinstance(value, dict):
            out[key].update(value)
        else:
            out[key] = value

    out["size"] = max(1.0, float(out["size"]))
    out["opacity"] = _clamp(float(out["opacity"]), 0.0, 1.0)
    out["flow"] = _clamp(float(out["flow"]), 0.01, 1.0)
    out["hardness"] = _clamp(float(out["hardness"]), 0.0, 1.0)
    out["spacing"] = _clamp(float(out["spacing"]), 0.01, 2.0)
    out["aspect"] = _clamp(float(out["aspect"]), 0.02, 1.0)
    out["angle"] = float(out["angle"])
    out["follow"] = _clamp(float(out["follow"]), 0.0, 1.0)
    out["eraser"] = bool(out["eraser"])
    return out


def _clamp(value, lo, hi):
    return lo if value < lo else (hi if value > hi else value)


def parse_color(value):
    """'#rrggbb' -> (r, g, b) en float 0..1."""
    text = (value or "#000000").strip()
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", text)
    if not match:
        return (0.0, 0.0, 0.0)
    hexa = match.group(1)
    return tuple(int(hexa[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def shape_seed(brush):
    """Graine stable derivee de la forme (et non de l'instance de trace).

    La durete n'entre volontairement pas dans la cle. Elle y figurait, et le
    moindre changement de durete retirait au sort la disposition des taches
    d'un pinceau « Taches » ou « Grain » : au reglage, la brosse changeait de
    dessin au lieu de s'adoucir. La durete doit moduler le fondu des taches,
    pas les redistribuer -- elle agit plus bas, sur l'enveloppe.

    Toute modification de cette cle doit etre reportee a l'identique dans
    static/brushes.js (`shapeSeed`) : l'apercu du navigateur et le rendu
    d'export doivent produire exactement la meme forme.
    """
    key = "%s|%.4f" % (brush["shape"], brush["aspect"])
    h = 2166136261
    for ch in key:
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return h


# --------------------------------------------------------------------------
# Masques de reference (BASE_RES x BASE_RES, float 0..1)
# --------------------------------------------------------------------------

def _radial_distance(res):
    axis = (np.arange(res, dtype=np.float32) + 0.5) / res * 2.0 - 1.0
    yy, xx = np.meshgrid(axis, axis, indexing="ij")
    return np.sqrt(xx * xx + yy * yy), xx, yy


def _smoothstep(edge0, edge1, x):
    if edge1 <= edge0:
        return (x <= edge0).astype(np.float32)
    t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return (t * t * (3.0 - 2.0 * t)).astype(np.float32)


# Adoucissement minimum des bords, en pixels de sortie. En dessous, la bordure
# tombe sous la taille du pixel et crenelle (visible surtout sur le surligneur,
# dont la durete est proche de 1).
MIN_EDGE_PX = 1.25


def _min_soft(res):
    """Adoucissement plancher, exprime dans le repere normalise [-1, 1]."""
    return min(0.5, 2.0 * MIN_EDGE_PX / max(res, 1))


def _disc(dist, hardness, res=BASE_RES):
    inner = _clamp(hardness, 0.0, 1.0 - _min_soft(res))
    return 1.0 - _smoothstep(inner, 1.0, dist)


def _blob(dist_sq_grid, cx, cy, radius, softness, xx, yy):
    d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / max(radius, 1e-4)
    return 1.0 - _smoothstep(max(0.0, 1.0 - softness), 1.0, d)


ANALYTIC_SHAPES = ("round", "rect")


def build_base_mask(brush, res=BASE_RES):
    """Construit le masque du pinceau a la resolution demandee.

    Les formes analytiques (rond, biseau) sont generees directement a la taille
    du tampon : les bords sont alors adoucis en fonction du nombre de pixels
    reellement disponibles. Les autres formes sont generees a `BASE_RES` puis
    reduites par `StampCache`.
    """
    shape = brush["shape"]
    dist, xx, yy = _radial_distance(res)

    if shape == "texture" and brush.get("texture"):
        return _mask_from_texture(brush["texture"], res)

    if shape == "rect":
        half_h = brush["aspect"]
        soft = max(_min_soft(res), (1.0 - brush["hardness"]) * 0.5)
        mx = 1.0 - _smoothstep(1.0 - soft, 1.0, np.abs(xx))
        my = 1.0 - _smoothstep(max(0.0, half_h - soft), half_h, np.abs(yy))
        return (mx * my).astype(np.float32)

    if shape == "speckle":
        rng = Rng(shape_seed(brush))
        mask = np.zeros((res, res), dtype=np.float32)
        for _ in range(16):
            angle = rng.next() * math.tau
            radius = math.sqrt(rng.next()) * 0.62
            cx, cy = math.cos(angle) * radius, math.sin(angle) * radius
            blob_r = 0.12 + rng.next() * 0.2
            alpha = 0.55 + rng.next() * 0.45
            np.maximum(mask, _blob(None, cx, cy, blob_r, 0.7, xx, yy) * alpha, out=mask)
        return (mask * _disc(dist, brush["hardness"] * 0.6)).astype(np.float32)

    if shape == "grain":
        rng = Rng(shape_seed(brush))
        mask = np.zeros((res, res), dtype=np.float32)
        for _ in range(140):
            angle = rng.next() * math.tau
            radius = math.sqrt(rng.next()) * 0.85
            cx, cy = math.cos(angle) * radius, math.sin(angle) * radius
            dot_r = 0.03 + rng.next() * 0.05
            alpha = 0.45 + rng.next() * 0.55
            np.maximum(mask, _blob(None, cx, cy, dot_r, 0.9, xx, yy) * alpha, out=mask)
        return (mask * _disc(dist, 0.35)).astype(np.float32)

    # "round" par defaut
    return _disc(dist, brush["hardness"], res).astype(np.float32)


def _mask_from_texture(data_url, res):
    """Extrait un masque alpha d'une texture PNG fournie en data URL."""
    payload = data_url.split(",", 1)[-1]
    img = Image.open(io.BytesIO(base64.b64decode(payload)))
    img = img.convert("RGBA").resize((res, res), Image.LANCZOS)
    arr = np.asarray(img, dtype=np.float32) / 255.0
    alpha = arr[..., 3]
    if float(alpha.max()) < 0.01:
        # Texture opaque : on utilise la luminance inversee comme masque.
        lum = arr[..., :3].mean(axis=2)
        alpha = 1.0 - lum
    return np.ascontiguousarray(alpha, dtype=np.float32)


def extent_factor(brush):
    """Marge necessaire pour qu'une rotation ne rogne pas l'empreinte."""
    shape = brush["shape"]
    if shape == "rect":
        return math.sqrt(1.0 + brush["aspect"] ** 2)
    if shape == "texture":
        return math.sqrt(2.0)
    return 1.0  # formes inscrites dans le disque


# --------------------------------------------------------------------------
# Empreintes (stamps) : masque de reference transforme (echelle/rotation/offset)
# --------------------------------------------------------------------------

def _to_image(mask):
    return Image.fromarray((np.clip(mask, 0, 1) * 255.0 + 0.5).astype(np.uint8), mode="L")


class StampCache:
    """Cache d'empreintes rasterisees, indexe par (taille, angle, sous-pixel)."""

    def __init__(self, brush):
        self.brush = brush
        self.extent = extent_factor(brush)
        self.analytic = brush["shape"] in ANALYTIC_SHAPES
        self.base_img = None if self.analytic else _to_image(build_base_mask(brush, BASE_RES))
        self._levels = {}
        self._cache = {}

    def _level(self, size):
        """Masque pre-calcule a la taille exacte du tampon.

        Indispensable pour la qualite des bords : `Image.transform` ne moyenne
        pas les pixels quand il reduit. Passer de 128 px a 20 px en une seule
        transformation affine sous-echantillonne et crenelle. On reduit donc
        d'abord en LANCZOS (qui, lui, moyenne), et la transformation affine ne
        fait plus que tourner et decaler a l'echelle 1.
        """
        image = self._levels.get(size)
        if image is None:
            if self.analytic:
                image = _to_image(build_base_mask(self.brush, size))
            elif size <= BASE_RES:
                image = self.base_img.resize((size, size), Image.LANCZOS)
            else:
                image = self.base_img.resize((size, size), Image.BICUBIC)
            self._levels[size] = image
        return image

    def get(self, size, angle_deg, dx, dy):
        size_q = max(1, int(round(size)))
        angle_q = round(angle_deg / ANGLE_STEP) * ANGLE_STEP % 360.0
        dx_q = round(dx * SUBPIXEL_STEPS) / SUBPIXEL_STEPS
        dy_q = round(dy * SUBPIXEL_STEPS) / SUBPIXEL_STEPS
        key = (size_q, angle_q, dx_q, dy_q)
        stamp = self._cache.get(key)
        if stamp is None:
            stamp = self._render(size_q, angle_q, dx_q, dy_q)
            self._cache[key] = stamp
        return stamp

    def _render(self, size, angle_deg, dx, dy):
        # Taille paire : le centre de l'empreinte tombe pile sur out_size/2,
        # ce qui permet un placement entier + un decalage sous-pixel explicite.
        out_size = 2 * (int(math.ceil(size * self.extent / 2.0)) + 1)
        source = self._level(size)
        cx = out_size / 2.0 + dx
        cy = out_size / 2.0 + dy
        theta = math.radians(angle_deg)
        cos_t, sin_t = math.cos(theta), math.sin(theta)

        # Transformation inverse (rotation + decalage, echelle 1) :
        # pixel de sortie -> pixel du masque deja mis a l'echelle.
        a, b = cos_t, sin_t
        d, e = -sin_t, cos_t
        c = size / 2.0 - (a * cx + b * cy)
        f = size / 2.0 - (d * cx + e * cy)

        img = source.transform(
            (out_size, out_size), Image.AFFINE, (a, b, c, d, e, f), resample=Image.BILINEAR
        )
        return np.asarray(img, dtype=np.float32) / 255.0


# --------------------------------------------------------------------------
# Planification d'un trace : chemin -> liste d'empreintes horodatees
# --------------------------------------------------------------------------

class PlannedStroke:
    """Empreintes d'un trace, pretes a etre consommees dans l'ordre temporel."""

    __slots__ = ("brush", "color", "opacity", "eraser", "cache",
                 "xs", "ys", "sizes", "angles", "alphas", "times", "count")

    def __init__(self, brush, cache, xs, ys, sizes, angles, alphas, times):
        self.brush = brush
        self.cache = cache
        self.color = parse_color(brush["color"])
        self.opacity = brush["opacity"]
        self.eraser = brush["eraser"]
        self.xs = np.asarray(xs, dtype=np.float32)
        self.ys = np.asarray(ys, dtype=np.float32)
        self.sizes = np.asarray(sizes, dtype=np.float32)
        self.angles = np.asarray(angles, dtype=np.float32)
        self.alphas = np.asarray(alphas, dtype=np.float32)
        self.times = np.asarray(times, dtype=np.float64)
        self.count = len(self.xs)

    @property
    def end_time(self):
        return float(self.times[-1]) if self.count else 0.0


def plan_stroke(stroke, scale=1.0, cache=None):
    """Transforme un trace enregistre en sequence d'empreintes.

    `stroke` : {'brush': {...}, 'seed': int, 'points': [{'x','y','t','p'}, ...]}
    `scale`  : facteur d'echelle (export a une resolution differente).
    """
    brush = normalize(stroke.get("brush", {}))
    points = stroke.get("points") or []
    if cache is None:
        cache = StampCache(brush)
    if not points:
        return PlannedStroke(brush, cache, [], [], [], [], [], [])

    rng = Rng(stroke.get("seed", 1))
    jitter = brush["jitter"]
    pressure = brush["pressure"]
    base_size = brush["size"] * scale
    step = max(0.5, brush["spacing"] * base_size)

    xs, ys, sizes, angles, alphas, times = [], [], [], [], [], []

    def emit(x, y, t, p, direction):
        size = base_size * (1.0 - pressure["size"] + pressure["size"] * p)
        if jitter["size"]:
            size *= max(0.05, 1.0 + rng.sym() * jitter["size"])
        angle = brush["angle"] + direction * brush["follow"]
        if jitter["angle"]:
            angle += rng.sym() * jitter["angle"]
        alpha = brush["flow"] * (1.0 - pressure["opacity"] + pressure["opacity"] * p)
        if jitter["opacity"]:
            alpha *= max(0.0, 1.0 - rng.next() * jitter["opacity"])
        px, py = x, y
        if jitter["position"]:
            radius = jitter["position"] * size
            px += rng.sym() * radius
            py += rng.sym() * radius
        xs.append(px)
        ys.append(py)
        sizes.append(max(1.0, size))
        angles.append(angle)
        alphas.append(_clamp(alpha, 0.0, 1.0))
        times.append(t)

    first = points[0]
    emit(first["x"] * scale, first["y"] * scale, first.get("t", 0.0), _pressure_of(first), 0.0)

    carry = 0.0
    for i in range(1, len(points)):
        p0, p1 = points[i - 1], points[i]
        x0, y0 = p0["x"] * scale, p0["y"] * scale
        x1, y1 = p1["x"] * scale, p1["y"] * scale
        dx, dy = x1 - x0, y1 - y0
        seg = math.hypot(dx, dy)
        if seg < 1e-6:
            continue
        direction = math.degrees(math.atan2(dy, dx))
        t0, t1 = p0.get("t", 0.0), p1.get("t", 0.0)
        pr0, pr1 = _pressure_of(p0), _pressure_of(p1)

        travelled = step - carry
        while travelled <= seg:
            u = travelled / seg
            emit(x0 + dx * u, y0 + dy * u, t0 + (t1 - t0) * u, pr0 + (pr1 - pr0) * u, direction)
            travelled += step
        carry = seg - (travelled - step)

    return PlannedStroke(brush, cache, xs, ys, sizes, angles, alphas, times)


def _pressure_of(point):
    value = point.get("p", 1.0)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 1.0
    return _clamp(value, 0.0, 1.0)


# --------------------------------------------------------------------------
# Application d'une empreinte sur un calque alpha
# --------------------------------------------------------------------------

def stamp_onto(layer, stamp, x0, y0):
    """Compose une empreinte (alpha-over) sur un calque alpha float32.

    `x0`/`y0` sont les coordonnees entieres du coin superieur gauche.
    Retourne la region modifiee (y0, y1, x0, x1) ou None si hors-champ.
    """
    h, w = stamp.shape
    height, width = layer.shape
    x1, y1 = x0 + w, y0 + h

    sx0, sy0 = max(0, -x0), max(0, -y0)
    dx0, dy0 = max(0, x0), max(0, y0)
    dx1, dy1 = min(width, x1), min(height, y1)
    if dx1 <= dx0 or dy1 <= dy0:
        return None

    src = stamp[sy0:sy0 + (dy1 - dy0), sx0:sx0 + (dx1 - dx0)]
    dst = layer[dy0:dy1, dx0:dx1]
    # alpha-over : dst = src + dst * (1 - src)
    np.add(src, dst * (1.0 - src), out=dst)
    return (dy0, dy1, dx0, dx1)
