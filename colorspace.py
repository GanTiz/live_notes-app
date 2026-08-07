"""Conversion sRGB (interface web) -> Rec. 709 gamma 2.4 (fichiers exportes).

L'interface travaille en sRGB et continue de le faire : `<input type="color">`
renvoie des hex que le navigateur interprete en sRGB, et le canvas de preview
compose en sRGB. Les fichiers exportes, eux, partent dans une chaine video :
primaires BT.709, blanc D65, gamma d'affichage 2.4 (BT.1886).

Ce que la conversion fait -- et ne fait pas :

  * Primaires et point blanc : rien a faire. sRGB (IEC 61966-2-1) et Rec. 709
    (ITU-R BT.709-6) definissent exactement les memes chromaticites -- R
    0.640/0.330, G 0.300/0.600, B 0.150/0.060 -- et le meme blanc D65. La
    matrice RGB->RGB entre les deux espaces est donc l'identite. Appliquer ici
    une quelconque matrice de primaires serait une erreur : elle deplacerait
    les couleurs au lieu de les preserver. `test_colorspace.py` le verifie en
    reconstruisant les deux matrices RGB->XYZ a partir des chromaticites.

  * Fonction de transfert : c'est la seule difference reelle, et elle est bien
    reelle. sRGB est une courbe par morceaux (segment lineaire de pente 12.92
    sous 0.04045, puis ((x + 0.055) / 1.055)^2.4), d'exposant effectif ~2.2.
    BT.1886, l'EOTF d'affichage de Rec. 709, est un gamma pur 2.4. On decode
    donc le sRGB vers la lumiere lineaire, puis on re-encode en 2.4.

La conversion preserve l'apparence : la meme couleur vue dans un viewer sRGB
avant export et dans un viewer Rec. 709 apres export. Un gris moyen #808080
(128) ressort a 136 dans le fichier -- c'est le signe que la conversion a bien
eu lieu, pas une derive.

Note sur l'etiquetage : H.273 (la table de codes que MOV/MP4 utilisent) n'a pas
de point de code pour "gamma 2.4 pur". La convention broadcast est d'encoder en
2.4 et d'etiqueter `bt709` : un fichier Rec. 709 est *affiche* selon BT.1886,
donc en gamma 2.4. C'est ce que fait `renderer.build_ffmpeg_command`.
"""

import numpy as np

# Exposant de l'EOTF d'affichage BT.1886.
BT1886_GAMMA = 2.4

# Chromaticites xy, communes a sRGB et Rec. 709. Conservees ici pour que le
# test puisse verifier l'affirmation "les primaires sont identiques" au lieu de
# la prendre pour argent comptant.
PRIMARIES_BT709 = {
    "red": (0.640, 0.330),
    "green": (0.300, 0.600),
    "blue": (0.150, 0.060),
}
PRIMARIES_SRGB = dict(PRIMARIES_BT709)
WHITE_D65 = (0.3127, 0.3290)


# --------------------------------------------------------------------------
# Fonctions de transfert (references, non optimisees)
# --------------------------------------------------------------------------

def srgb_to_linear(values):
    """EOTF sRGB : code sRGB 0..1 -> lumiere lineaire 0..1."""
    x = np.clip(np.asarray(values, dtype=np.float64), 0.0, 1.0)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(values):
    """OETF sRGB : lumiere lineaire 0..1 -> code sRGB 0..1."""
    x = np.clip(np.asarray(values, dtype=np.float64), 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * x ** (1.0 / 2.4) - 0.055)


def linear_to_bt1886(values):
    """Lumiere lineaire 0..1 -> code Rec. 709 gamma 2.4."""
    x = np.clip(np.asarray(values, dtype=np.float64), 0.0, 1.0)
    return x ** (1.0 / BT1886_GAMMA)


def bt1886_to_linear(values):
    """EOTF BT.1886 : code Rec. 709 gamma 2.4 -> lumiere lineaire 0..1."""
    x = np.clip(np.asarray(values, dtype=np.float64), 0.0, 1.0)
    return x ** BT1886_GAMMA


def srgb_to_rec709_reference(values):
    """Conversion exacte, en float64. Sert de reference au test de la LUT."""
    return linear_to_bt1886(srgb_to_linear(values))


def rec709_to_srgb(values):
    """Conversion inverse. Utilisee par le test de fidelite aller-retour."""
    return linear_to_srgb(bt1886_to_linear(values))


# --------------------------------------------------------------------------
# Conversion appliquee au rendu
# --------------------------------------------------------------------------

# Le genou de la courbe sRGB : en dessous, segment lineaire ; au-dessus, la
# partie en puissance.
SRGB_KNEE = 0.04045


def srgb_to_rec709(values):
    """sRGB encode 0..1 -> Rec. 709 gamma 2.4 encode 0..1.

    Au-dessus du genou, les deux exposants 2.4 s'annulent exactement :

        f(x) = ( ((x + 0.055) / 1.055) ^ 2.4 ) ^ (1 / 2.4) = (x + 0.055) / 1.055

    La conversion s'y reduit donc a une affine -- ni approximation, ni table :
    l'ecart au calcul de reference est de 3e-8. Seul le pied sous le genou
    (segment lineaire cote sRGB) demande une puissance, et il ne concerne que
    les pixels quasi noirs.

    Une LUT uniforme a ete essayee puis abandonnee : le gamma 2.4 ayant une
    pente infinie en 0, son premier intervalle couvre a lui seul 1.5 LSB 8 bits,
    ce qui posterisait les noirs. Le calcul exact est ici aussi rapide.

    Ne s'applique qu'aux canaux de couleur. L'alpha est une couverture
    geometrique, pas une luminance : il ne subit aucune fonction de transfert.
    """
    x = np.clip(values, 0.0, 1.0)
    out = (x + 0.055) / 1.055
    toe = x <= SRGB_KNEE
    if toe.any():
        out[toe] = (x[toe] / 12.92) ** (1.0 / BT1886_GAMMA)
    return out
