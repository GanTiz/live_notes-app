"""Pages de PDF : un document -> l'image d'une de ses pages.

Un PDF n'est pas un media au sens de `proxy.py` : ffprobe ne sait pas le
demuxer, et aucun navigateur n'affiche un PDF dans un `<img>`. Mais le probleme
qu'il pose est exactement celui que `proxy.py` resout deja pour un ProRes --
« ce fichier est illisible tel quel, fabrique-en une copie de lecture » -- a
une difference pres : un PDF a des pages, et il faut donc savoir laquelle.

Ce module ne fait que rasteriser. C'est `proxy.py` qui decide quand, ou ecrire
le fichier et comment le mettre en cache ; c'est `app.py` qui fait circuler le
numero de page. Separer ainsi evite de melanger le monde ffprobe et le monde
PDF dans un meme jeu de branches.

## Pourquoi PDFium et pas PyMuPDF

PyMuPDF est le choix courant, et il est sous **AGPL**. Or `pdfdoc` est importe
dans le process de l'application, donc *lie* a elle -- contrairement a FFmpeg,
qui est invoque en sous-processus et dont `THIRD_PARTY_LICENSES.md` prend soin
de preciser qu'il n'est pas lie. Une dependance AGPL contaminerait donc
l'application distribuee.

pypdfium2 expose PDFium (le moteur PDF de Chrome) sous Apache-2.0/BSD-3-Clause,
avec la bibliotheque native embarquee dans la roue Python : rien a installer
sur la machine, contrairement a poppler qu'exigerait pdf2image.

## Definition de rasterisation

Elle est decidee par l'appelant, jamais ici. Un PDF n'a pas de definition
propre -- une page est une description vectorielle en points typographiques --
et le bon choix depend du canevas de trace, que ce module ne connait pas.
"""

import math
import os

# Octets de tete d'un PDF. Le selecteur natif n'ayant aucun filtre d'extension
# (voir `_MEDIA_PICKER` dans app.py), un fichier mal nomme arriverait jusqu'a
# PDFium ; autant le refuser proprement avant.
_MAGIC = b"%PDF-"

# Bornes de la definition de rasterisation. Le plancher evite qu'un canevas
# modeste rende du texte mou ; le plafond borne la memoire, un canevas 8K
# n'ayant aucune raison de produire une image de reference demesuree.
MIN_EDGE = 1280
MAX_EDGE = 4096


class PdfError(RuntimeError):
    """Meme role que `proxy.ProxyError` : une panne racontable a l'ecran."""


def _pdfium():
    """Import paresseux.

    `test_proxy.py`, `test_session.py` et `test_colorspace.py` n'ont rien a
    voir avec les PDF : ajouter une dependance ne doit pas les empecher de
    tourner sur une machine qui ne l'a pas installee.
    """
    try:
        import pypdfium2
    except ImportError:
        raise PdfError(
            "Lecture des PDF indisponible : la bibliotheque pypdfium2 n'est pas "
            "installee (pip install -r requirements.txt)."
        )
    return pypdfium2


def is_pdf(path):
    """Vrai si le fichier est reellement un PDF, extension mise a part."""
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as handle:
            return handle.read(len(_MAGIC)) == _MAGIC
    except OSError:
        return False


def target_edge(width, height):
    """Bord long de rasterisation pour un canevas donne, borne."""
    edge = max(int(width or 0), int(height or 0))
    return max(MIN_EDGE, min(MAX_EDGE, edge or MIN_EDGE))


def _open(path):
    pdfium = _pdfium()
    try:
        return pdfium.PdfDocument(path)
    except Exception as exc:  # noqa: BLE001 - PdfiumError et surprises diverses
        raise PdfError("PDF illisible : %s" % _reason(exc))


def _reason(exc):
    """Message de PDFium ramene a quelque chose d'affichable."""
    text = str(exc) or type(exc).__name__
    if "password" in text.lower() or "encrypt" in text.lower():
        return "document protege par mot de passe"
    return text


def describe(path):
    """-> {'pageCount': n, 'pages': [(largeur_pt, hauteur_pt), ...]}.

    Les tailles sont retournees page par page : rien n'oblige un PDF a etre
    homogene, et melanger une couverture A4 et un plan A3 est courant.
    """
    document = _open(path)
    try:
        pages = []
        for index in range(len(document)):
            page = document[index]
            try:
                width, height = page.get_size()
            finally:
                page.close()
            pages.append((float(width), float(height)))
        if not pages:
            raise PdfError("PDF sans aucune page.")
        return {"pageCount": len(pages), "pages": pages}
    finally:
        document.close()


def page_size(path, page, long_edge):
    """Dimensions en pixels qu'aurait la page rendue, sans la rendre."""
    info = describe(path)
    width_pt, height_pt = info["pages"][clamp_page(page, info["pageCount"]) - 1]
    return _pixels(width_pt, height_pt, long_edge)


def clamp_page(page, page_count):
    """Numero de page utilisable, en base 1.

    Un repli plutot qu'une erreur : le fichier a pu etre remplace entre le
    choix de la page et son ouverture, et retomber sur la premiere page est
    plus utile que refuser d'afficher quoi que ce soit.
    """
    try:
        page = int(page)
    except (TypeError, ValueError):
        return 1
    if page < 1 or page > page_count:
        return 1
    return page


def pixels(width_pt, height_pt, long_edge):
    """Dimensions en pixels d'une page deja mesuree, sans rouvrir le document."""
    return _pixels(width_pt, height_pt, long_edge)


def _pixels(width_pt, height_pt, long_edge):
    # `ceil`, et non `round` : c'est la convention de PDFium, et `page_size`
    # doit annoncer exactement ce que `render` produira. Un pixel d'ecart
    # suffirait a faire mentir les dimensions publiees vers la tablette.
    scale = float(long_edge) / max(width_pt, height_pt)
    return (max(1, int(math.ceil(width_pt * scale))),
            max(1, int(math.ceil(height_pt * scale))))


def render(path, page, long_edge, destination):
    """Rasterise une page en PNG. -> (largeur, hauteur) en pixels.

    L'echelle se deduit de la taille reelle de la page et non d'une resolution
    fixe en DPI : une page A0 et une page A6 doivent ressortir au meme bord
    long, sans quoi le meme canevas donnerait des references incomparables.
    """
    document = _open(path)
    try:
        count = len(document)
        index = clamp_page(page, count) - 1
        sheet = document[index]
        try:
            width_pt, height_pt = sheet.get_size()
            scale = float(long_edge) / max(width_pt, height_pt)
            try:
                bitmap = sheet.render(scale=scale)
            except Exception as exc:  # noqa: BLE001 - remonte lisiblement
                raise PdfError("Rendu de la page %d impossible : %s"
                               % (index + 1, _reason(exc)))
            image = bitmap.to_pil()
        finally:
            sheet.close()

        # Le fond d'un PDF est transparent la ou rien n'est peint. Sur le
        # canevas, cela laisserait voir la couleur de fond a travers la page au
        # lieu du papier blanc attendu : on aplatit donc sur du blanc.
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            from PIL import Image as _Image
            flat = _Image.new("RGB", image.size, (255, 255, 255))
            flat.paste(image, mask=image.split()[-1])
            image = flat
        elif image.mode != "RGB":
            image = image.convert("RGB")

        image.save(destination, "PNG")
        return image.size
    finally:
        document.close()
