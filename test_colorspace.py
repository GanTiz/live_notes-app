"""Verification de la chaine couleur sRGB -> Rec. 709 gamma 2.4.

Lancer : py test_colorspace.py   (ou pytest test_colorspace.py)

Les tests marques [ffmpeg] encodent et redecodent de vrais fichiers : ils
verifient non seulement l'etiquetage, mais aussi que la matrice reellement
utilisee par swscale correspond a l'etiquette posee -- le piege classique d'un
fichier tague bt709 et encode en bt601.
"""

import json
import os
import subprocess
import sys
import tempfile

import numpy as np

import colorspace as cs
import renderer

TMP = tempfile.mkdtemp(prefix="live_notes_color_")


# --------------------------------------------------------------------------
# 1. Primaires : sRGB et Rec. 709 sont-ils vraiment identiques ?
# --------------------------------------------------------------------------

def rgb_to_xyz_matrix(primaries, white_xy):
    """Matrice RGB->XYZ reconstruite a partir des chromaticites (Wyszecki)."""
    def xyz(xy):
        x, y = xy
        return np.array([x / y, 1.0, (1.0 - x - y) / y])

    m = np.column_stack([xyz(primaries[k]) for k in ("red", "green", "blue")])
    scale = np.linalg.solve(m, xyz(white_xy))
    return m * scale


def test_primaries_are_identical():
    """La matrice de conversion des primaires sRGB -> Rec. 709 est l'identite."""
    m_srgb = rgb_to_xyz_matrix(cs.PRIMARIES_SRGB, cs.WHITE_D65)
    m_709 = rgb_to_xyz_matrix(cs.PRIMARIES_BT709, cs.WHITE_D65)
    transform = np.linalg.inv(m_709) @ m_srgb
    deviation = np.abs(transform - np.eye(3)).max()
    assert deviation < 1e-12, "primaires divergentes : %.3e" % deviation
    return "ecart max a l'identite : %.2e (matrice de primaires = identite)" % deviation


def test_transfer_functions_differ():
    """sRGB n'est pas un gamma 2.4 : c'est ce qui justifie la conversion."""
    grid = np.linspace(0.0, 1.0, 256)
    pure_24 = grid ** cs.BT1886_GAMMA
    srgb_linear = cs.srgb_to_linear(grid)
    gap = np.abs(srgb_linear - pure_24).max()
    assert gap > 0.02, "les deux courbes coincident, conversion inutile ?"
    # Reference : sRGB a un exposant effectif ~2.2, pas 2.4.
    mid = float(cs.srgb_to_linear(0.5))
    assert 0.21 < mid < 0.22
    return "ecart max des EOTF : %.4f ; sRGB(0.5) = %.5f (gamma 2.4 -> %.5f)" % (
        gap, mid, 0.5 ** 2.4)


# --------------------------------------------------------------------------
# 2. Conversion numerique
# --------------------------------------------------------------------------

def test_fast_path_matches_reference():
    """Le raccourci affine doit coller a la conversion de reference partout.

    Le pied sous le genou sRGB est echantillonne finement : c'est la que le
    gamma 2.4 est le plus raide, et donc la que toute approximation deraille.
    """
    grid = np.concatenate([
        np.linspace(0.0, cs.SRGB_KNEE, 50001),
        np.linspace(cs.SRGB_KNEE, 1.0, 50001),
    ]).astype(np.float32)
    error = np.abs(cs.srgb_to_rec709(grid) - cs.srgb_to_rec709_reference(grid)).max()
    lsb = 1.0 / 255.0
    assert error < lsb / 100.0, "raccourci imprecis : %.3e (LSB = %.3e)" % (error, lsb)
    return "erreur max : %.1e, soit 1/%d de LSB 8 bits" % (error, int(lsb / error))


def test_round_trip_is_lossless():
    """sRGB -> Rec. 709 -> sRGB doit rendre la valeur d'origine."""
    codes = np.arange(256, dtype=np.float64) / 255.0
    back = cs.rec709_to_srgb(cs.srgb_to_rec709_reference(codes))
    error = np.abs(back - codes).max()
    assert error < 1e-9
    return "aller-retour analytique exact (erreur max %.1e)" % error


def independent_reference(code8):
    """Conversion reecrite en Python pur, sans numpy ni le module teste.

    Sert de controle croise : si `colorspace` et cette fonction s'accordent,
    l'erreur devrait etre presente dans les deux, ecrites separement.
    """
    x = code8 / 255.0
    linear = x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
    return round(linear ** (1.0 / 2.4) * 255.0)


def test_known_values():
    """Controle croise sur les 256 codes 8 bits, plus deux ancres evidentes."""
    got = {}
    for code in range(256):
        value = float(cs.srgb_to_rec709(np.float64([code / 255.0]))[0])
        got[code] = int(round(value * 255.0))
        expected = independent_reference(code)
        assert got[code] == expected, \
            "sRGB %d -> %d, reference independante %d" % (code, got[code], expected)

    # Les extremes ne doivent pas bouger : noir et blanc sont communs aux deux
    # espaces, une derive ici signalerait une erreur de mise a l'echelle.
    assert got[0] == 0 and got[255] == 255, (got[0], got[255])
    return "256/256 codes concordants ; " + ", ".join(
        "%d->%d" % (c, got[c]) for c in (0, 64, 128, 192, 255))


# --------------------------------------------------------------------------
# 3. Bout en bout : encodage reel + relecture
# --------------------------------------------------------------------------

def make_payload(color, codec, alpha, size=320, background=None):
    """Un trace epais et opaque d'une couleur donnee, au centre de l'image.

    `background` reste absent du payload par defaut : les autres tests exercent
    ainsi le repli sur le blanc, c'est-a-dire le comportement historique.
    """
    brush = {
        "shape": "round", "size": 160, "opacity": 1.0, "flow": 1.0,
        "hardness": 1.0, "spacing": 0.05, "aspect": 1.0, "angle": 0.0,
        "follow": 0.0, "eraser": False, "color": color,
        "jitter": {"position": 0.0, "size": 0.0, "angle": 0.0, "opacity": 0.0},
        "pressure": {"size": 0.0, "opacity": 0.0},
    }
    points = [{"x": 60 + i * 20, "y": size / 2, "t": i * 10.0, "p": 1.0}
              for i in range(11)]
    payload = {
        "width": size, "height": size, "fps": 25, "alpha": alpha,
        "codec": codec, "durationMs": 200.0,
        "strokes": [{"brush": brush, "seed": 12345, "points": points}],
        "clears": [],
    }
    if background is not None:
        payload["background"] = background
    return payload


def probe(path):
    out = subprocess.check_output([
        "ffprobe", "-hide_banner", "-loglevel", "error", "-select_streams", "v:0",
        "-show_entries", "stream=color_primaries,color_transfer,color_space,color_range,pix_fmt",
        "-of", "json", path,
    ])
    return json.loads(out)["streams"][0]


def decode_center(path, pix_fmt, size=320):
    """Redecode la premiere frame et retourne le pixel central.

    On laisse ffmpeg choisir sa conversion YCbCr->RGB d'apres les etiquettes du
    fichier : c'est ce que fera un NLE, et c'est ce qui doit etre valide.
    """
    channels = 4 if pix_fmt == "rgba" else 3
    raw = subprocess.check_output([
        renderer.FFMPEG, "-hide_banner", "-loglevel", "error",
        "-i", path, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", pix_fmt, "-",
    ])
    frame = np.frombuffer(raw, dtype=np.uint8).reshape(size, size, channels)
    return frame[size // 2, size // 2].astype(int)


def render_case(name, color, codec, alpha):
    payload = make_payload(color, codec, alpha)
    path = os.path.join(TMP, "%s.%s" % (name, "mp4" if codec == "h264" else "mov"))
    renderer.render(payload, path)
    return path


def test_tagging_prores():
    """[ffmpeg] Le ProRes exporte doit etre tague bt709 / bt709 / bt709.

    Note sur le pix_fmt : on alimente l'encodeur en yuva444p10le, mais ProRes
    4444 est un format 12 bits, donc le decodeur annonce yuva444p12le. On
    verifie ce qui compte vraiment -- 4:4:4 sans sous-echantillonnage chroma, et
    la presence du plan alpha -- plutot que la profondeur d'entree.
    """
    with_alpha = probe(render_case("tag_prores_a", "#808080", "prores4444", True))
    without = probe(render_case("tag_prores_na", "#808080", "prores4444", False))

    for info in (with_alpha, without):
        assert info["color_primaries"] == "bt709", info
        assert info["color_transfer"] == "bt709", info
        assert info["color_space"] == "bt709", info

    assert with_alpha["pix_fmt"].startswith("yuva444"), with_alpha
    assert without["pix_fmt"].startswith("yuv444"), without
    assert not without["pix_fmt"].startswith("yuva"), \
        "plan alpha present alors que l'export est opaque : %s" % without
    return "prores primaries=%s trc=%s space=%s ; pix_fmt alpha=%s / opaque=%s" % (
        with_alpha["color_primaries"], with_alpha["color_transfer"],
        with_alpha["color_space"], with_alpha["pix_fmt"], without["pix_fmt"])


def test_tagging_h264():
    """[ffmpeg] Le H.264 de previz doit porter le meme etiquetage."""
    path = render_case("tag_h264", "#808080", "h264", False)
    info = probe(path)
    assert info["color_primaries"] == "bt709", info
    assert info["color_transfer"] == "bt709", info
    assert info["color_space"] == "bt709", info
    return "h264 %s : primaries=%s trc=%s space=%s range=%s" % (
        info["pix_fmt"], info["color_primaries"], info["color_transfer"],
        info["color_space"], info.get("color_range"))


def test_pipeline_fidelity_prores():
    """[ffmpeg] Fidelite complete : hex sRGB -> fichier -> retour au hex.

    Verifie deux choses d'un coup :
      - le fichier contient bien la valeur encodee en gamma 2.4 (128 -> 136) ;
      - en appliquant l'EOTF Rec. 709 puis l'OETF sRGB, on retombe sur 128,
        donc la couleur affichee est preservee.
    """
    report = []
    for hexa, srgb_code in (("#808080", 128), ("#404040", 64), ("#ff0000", 255)):
        path = render_case("fid_%s" % hexa.strip("#"), hexa, "prores4444", True)
        pixel = decode_center(path, "rgba")
        channel = 0 if hexa == "#ff0000" else 1  # rouge pur : on lit R
        stored = pixel[channel]

        expected_stored = round(float(cs.srgb_to_rec709(
            np.float32([srgb_code / 255.0]))[0]) * 255.0)
        assert abs(stored - expected_stored) <= 2, \
            "%s : fichier=%d, attendu=%d" % (hexa, stored, expected_stored)

        recovered = round(float(cs.rec709_to_srgb(stored / 255.0)) * 255.0)
        assert abs(recovered - srgb_code) <= 2, \
            "%s : retour sRGB=%d, attendu=%d" % (hexa, recovered, srgb_code)

        assert pixel[3] >= 253, "%s : alpha=%d, trace non opaque" % (hexa, pixel[3])
        report.append("%s sRGB %d -> fichier %d (attendu %d) -> retour %d"
                      % (hexa, srgb_code, stored, expected_stored, recovered))
    return " | ".join(report)


def test_matrix_matches_tag():
    """[ffmpeg] Un rouge sature detecte une matrice BT.601 masquee en bt709.

    Sous la HD, swscale retomberait sur BT.601 sans consigne explicite. Le rouge
    pur est le patch le plus sensible : l'erreur de matrice y depasse largement
    le bruit de compression.
    """
    path = render_case("matrix", "#ff0000", "prores4444", False)
    pixel = decode_center(path, "rgb24")
    error = np.abs(pixel - np.array([255, 0, 0])).max()
    assert error <= 3, "rouge decode %s : matrice incoherente avec l'etiquette" % pixel
    return "rouge pur redecode a %s (ecart max %d)" % (tuple(pixel), error)


def decode_all(path, pix_fmt, size=320):
    channels = 4 if pix_fmt == "rgba" else 3
    raw = subprocess.check_output([
        renderer.FFMPEG, "-hide_banner", "-loglevel", "error",
        "-i", path, "-f", "rawvideo", "-pix_fmt", pix_fmt, "-",
    ])
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, size, size, channels)


def test_no_seam_on_untouched_background():
    """[ffmpeg] Le fond non recalcule doit egaler le fond reconverti.

    `renderer.render` pre-remplit le buffer de sortie en blanc et ne recalcule
    ensuite que les regions sales. Un effacement salit toute l'image : le fond
    repasse alors par la conversion. Si celle-ci ne laissait pas le blanc
    invariant, une couture apparaitrait a cet instant precis -- un bord visible
    entre pixels convertis et pixels d'origine.
    """
    payload = make_payload("#808080", "prores4444", False)
    payload["clears"] = [200.0]
    path = os.path.join(TMP, "seam.mov")
    renderer.render(payload, path)

    frames = decode_all(path, "rgb24")
    before = frames[0][2, 2].astype(int)      # fond jamais salit
    after = frames[-1][2, 2].astype(int)      # fond repasse par la conversion
    assert np.abs(before - after).max() <= 1, \
        "couture : fond %s avant effacement, %s apres" % (tuple(before), tuple(after))
    assert np.abs(before - 255).max() <= 2, "fond non blanc : %s" % (tuple(before),)
    return "fond stable a l'effacement : %s -> %s (blanc invariant)" % (
        tuple(before), tuple(after))


def test_background_colour_is_encoded():
    """[ffmpeg] Le fond choisi sort encode en Rec. 709, et sans couture.

    Meme piege que le test precedent, mais sur une couleur qui n'est *pas*
    invariante par la conversion. Le blanc masquait le probleme : 255 reste 255.
    Un fond #808080 pre-rempli a 128 au lieu de 135 traverserait le rendu sans
    la moindre erreur et laisserait une couture invisible en revue de code,
    flagrante a l'ecran des qu'un effacement salit toute l'image.
    """
    expected = int(renderer.background_bytes("#808080")[0])
    payload = make_payload("#ff0000", "prores4444", False, background="#808080")
    payload["clears"] = [200.0]
    path = os.path.join(TMP, "background.mov")
    renderer.render(payload, path)

    frames = decode_all(path, "rgb24")
    before = frames[0][2, 2].astype(int)       # fond pre-rempli, jamais recalcule
    after = frames[-1][2, 2].astype(int)       # fond repasse par _write_region
    assert np.abs(before - after).max() <= 1, \
        "couture : fond %s avant effacement, %s apres" % (tuple(before), tuple(after))
    assert np.abs(before - expected).max() <= 2, \
        "fond %s, attendu ~%d" % (tuple(before), expected)
    return "#808080 -> fond %s (attendu %d ; 128 trahirait l'absence de conversion)" % (
        tuple(before), expected)


def test_alpha_untouched():
    """[ffmpeg] L'alpha est une couverture : aucune fonction de transfert."""
    payload = make_payload("#ffffff", "prores4444", True)
    payload["strokes"][0]["brush"]["opacity"] = 0.5
    path = os.path.join(TMP, "alpha.mov")
    renderer.render(payload, path)
    pixel = decode_center(path, "rgba")
    assert abs(int(pixel[3]) - 128) <= 3, "alpha=%d, attendu ~128" % pixel[3]
    return "opacite 0.5 -> alpha %d (non converti ; converti aurait donne ~%d)" % (
        pixel[3], round(float(cs.srgb_to_rec709(np.float32([0.5]))[0]) * 255))


# --------------------------------------------------------------------------

TESTS = [
    test_primaries_are_identical,
    test_transfer_functions_differ,
    test_fast_path_matches_reference,
    test_round_trip_is_lossless,
    test_known_values,
    test_tagging_prores,
    test_tagging_h264,
    test_pipeline_fidelity_prores,
    test_matrix_matches_tag,
    test_no_seam_on_untouched_background,
    test_background_colour_is_encoded,
    test_alpha_untouched,
]


def main():
    failures = 0
    for test in TESTS:
        try:
            detail = test()
            print("  OK   %-32s %s" % (test.__name__, detail or ""))
        except Exception as exc:  # noqa: BLE001 - rapport de test
            failures += 1
            print("  FAIL %-32s %s: %s" % (test.__name__, type(exc).__name__, exc))
    print("\n%d/%d tests passes" % (len(TESTS) - failures, len(TESTS)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
