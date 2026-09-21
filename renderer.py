"""Rendu final : metadonnees de trace -> frames -> FFmpeg.

Le navigateur n'envoie plus d'images. Il envoie la description des traces
(points horodates + pinceau) et ce module les rejoue frame par frame, en
ecrivant directement des pixels bruts RGBA dans l'entree standard de FFmpeg.

Le compositing se fait en float32 avec alpha premultiplie, ce qui permet de
sortir un vrai canal alpha en ProRes 4444 sans passer par des PNG intermediaires.

Trois couches, une seule geometrie
----------------------------------

Le module produit deux sortes de fichiers, qui partagent le meme referentiel --
le canevas de trace -- et la meme origine des temps -- le point IN du media :

- `render`       : le trace. Seul (couche alpha), aplati sur la couleur de fond,
                   ou aplati sur le media place dans le canevas.
- `render_media` : le media seul, place dans ce meme canevas, avec ou sans la
                   couleur de fond autour.

Le placement du media n'est *pas* refait ici a la main : il est confie aux
filtres de FFmpeg (`crop`, `scale`, `pad`), decrits par la meme arithmetique que
`fitBox()` cote navigateur. Les expressions n'utilisent que `iw`/`ih`, donc les
dimensions reellement decodees -- une video portant une rotation dans ses
metadonnees est cadree sur ce que le navigateur affichait, pas sur ce que
ffprobe annonce.
"""

import math
import os
import re
import shutil
import subprocess
import threading

import numpy as np

import brush_engine as be
import colorspace as cs

FFMPEG = os.environ.get("LIVE_NOTES_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
TAIL_MS = 1000.0  # temps conserve apres le dernier trace


class RenderError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Etat d'un trace en cours de rejeu
# --------------------------------------------------------------------------

class _ActiveStroke:
    __slots__ = ("planned", "scratch", "cursor", "bbox")

    def __init__(self, planned, height, width):
        self.planned = planned
        self.scratch = np.zeros((height, width), dtype=np.float32)
        self.cursor = 0
        self.bbox = None

    def grow(self, region):
        if region is None:
            return
        if self.bbox is None:
            self.bbox = list(region)
        else:
            self.bbox[0] = min(self.bbox[0], region[0])
            self.bbox[1] = max(self.bbox[1], region[1])
            self.bbox[2] = min(self.bbox[2], region[2])
            self.bbox[3] = max(self.bbox[3], region[3])


class _Sheet:
    """Une couche de dessin au rejeu.

    Chaque couche s'accumule dans son propre canevas premultiplie, et les
    canevas sont composes dans l'ordre de la liste -- le premier au fond. C'est
    ce qui fait qu'une trace de la couche 1 posee a t = 5 s passe SOUS une
    trace de la couche 2 posee a t = 1 s : sur une toile unique, rejouee
    chronologiquement, elle passerait dessus.

    Une gomme n'attaque que sa propre couche, pour la meme raison qu'elle
    n'attaque que son calque dans n'importe quel outil a calques : sans cela
    les couches ne seraient plus composables separement.
    """

    __slots__ = ("canvas", "pending", "actives", "clears")

    def __init__(self, planned_strokes, clears, height, width):
        self.canvas = np.zeros((height, width, 4), dtype=np.float32)
        self.pending = list(planned_strokes)
        self.actives = []
        self.clears = list(clears)


def payload_sheets(payload):
    """Les couches decrites par une charge utile, du fond vers le premier plan.

    Une charge d'avant les couches -- `strokes` et `clears` a plat -- en decrit
    une seule : elle rend donc exactement comme avant, et les bancs qui
    l'emploient n'ont rien a changer.
    """
    raw = payload.get("layers")
    if isinstance(raw, list) and raw:
        return [{"strokes": sheet.get("strokes") or [], "clears": sheet.get("clears") or []}
                for sheet in raw]
    return [{"strokes": payload.get("strokes") or [], "clears": payload.get("clears") or []}]


def _merge(bbox, region):
    if region is None:
        return bbox
    if bbox is None:
        return list(region)
    bbox[0] = min(bbox[0], region[0])
    bbox[1] = max(bbox[1], region[1])
    bbox[2] = min(bbox[2], region[2])
    bbox[3] = max(bbox[3], region[3])
    return bbox


# --------------------------------------------------------------------------
# Couleur de fond du canevas
# --------------------------------------------------------------------------

_HEX_COLOR = re.compile(r"#?[0-9a-fA-F]{6}\Z")


def parse_background(value):
    """Couleur de fond du canevas -> (r, g, b) float sRGB 0..1.

    Repli sur le blanc, et non sur le noir de `be.parse_color` : le blanc est le
    fond historique de l'outil, et une valeur malformee qui produirait un export
    entierement noir serait un echec autrement plus spectaculaire.

    Cette couleur est le papier : ce sur quoi le trace est aplati, et ce qui
    entoure un media qui n'occupe pas tout le cadre.
    """
    if isinstance(value, str) and _HEX_COLOR.match(value.strip()):
        return be.parse_color(value.strip())
    return (1.0, 1.0, 1.0)


def background_bytes(value):
    """Couleur de fond -> triplet uint8 Rec. 709, tel qu'ecrit dans `out_u8`.

    `out_u8` ne contient pas du sRGB mais des octets deja encodes en Rec. 709
    (voir la conversion finale de `_write_region`). Le pre-remplissage du buffer
    doit donc passer par la meme conversion que les regions recalculees, sinon
    une couture apparait entre les deux des qu'un effacement salit toute l'image.

    Le blanc masquait le probleme : 1.0 est invariant par la conversion, d'ou le
    `255` en dur qui suffisait tant que le fond n'etait pas reglable. Un gris
    moyen, lui, part a 128 et doit etre ecrit a 135.
    """
    rgb = np.asarray(parse_background(value), dtype=np.float64)
    return (cs.srgb_to_rec709(rgb) * 255.0 + 0.5).astype(np.uint8)


def background_hex(value):
    """Couleur de fond -> '#rrggbb' encode en Rec. 709, pour les filtres FFmpeg.

    Le filtre `pad` remplit les cotes d'un media qui n'est pas au format du
    canevas. Cette zone doit etre exactement la meme couleur que celle sur
    laquelle `_write_region` aplatit le trace, sinon les deux moities de la meme
    image de fond ne s'accorderaient pas d'un pixel a l'autre.
    """
    return "#%02x%02x%02x" % tuple(int(channel) for channel in background_bytes(value))


# --------------------------------------------------------------------------
# Le media de fond, place dans le canevas
# --------------------------------------------------------------------------

# Ce qui passe sous le trace.
#   media - le media cadre dans le canevas, aplati sur la couleur de fond
#   solid - la couleur de fond seule
#   alpha - rien : le trace sort sur du transparent (ProRes 4444 seulement)
FLATTEN_MEDIA, FLATTEN_SOLID, FLATTEN_ALPHA = "media", "solid", "alpha"

TRANSPARENT = "#00000000"

DEFAULT_FIT = {"mode": "contain", "zoom": 1.0, "posX": 0.5, "posY": 0.5}


def normalize_fit(fit):
    """Cadrage du media, borne comme cote navigateur et cote session."""
    if not isinstance(fit, dict):
        return dict(DEFAULT_FIT)

    def fraction(key):
        try:
            return min(1.0, max(0.0, float(fit.get(key, 0.5))))
        except (TypeError, ValueError):
            return 0.5

    try:
        zoom = float(fit.get("zoom", 1.0))
    except (TypeError, ValueError):
        zoom = 1.0
    return {
        "mode": "crop" if fit.get("mode") == "crop" else "contain",
        "zoom": min(4.0, max(1.0, zoom)),
        "posX": fraction("posX"),
        "posY": fraction("posY"),
    }


def normalize_media(media):
    """Description du media de fond attendue par le rendu, ou None.

    `in`/`out` sont les points poses dans le lecteur, en secondes depuis le
    debut du media. Ce sont eux qui portent la synchronisation : le trace a ete
    dessine a partir du point IN, donc t = 0 du trace vaut `in` dans le media.

    Un media `audio` n'a rien a montrer sous le trace, mais il a tout a y faire
    entendre : il entre ici comme les autres, et c'est aux appelants de ne lui
    demander que sa piste son (voir `is_visual`).
    """
    if not isinstance(media, dict):
        return None
    path = media.get("path")
    kind = media.get("kind")
    if not path or kind not in ("video", "image", "audio"):
        return None

    def seconds(key, fallback):
        try:
            value = float(media.get(key))
        except (TypeError, ValueError):
            return fallback
        return value if value >= 0.0 and value == value else fallback

    def whole(key):
        try:
            return max(0, int(media.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    duration = max(0.0, seconds("duration", 0.0))
    start = seconds("in", 0.0)
    end = seconds("out", 0.0)
    if duration:
        start = min(start, duration)
        end = min(end, duration) if end else duration
    if end <= start:
        end = duration if duration > start else 0.0
    return {
        "path": path,
        "kind": kind,
        "duration": duration,
        "in": start,
        "out": end,
        "fit": normalize_fit(media.get("fit")),
        # Un media audio a forcement une piste son : c'est ce qu'il est.
        "hasAudio": bool(media.get("hasAudio")) or kind == "audio",
        "audioRate": whole("audioRate"),
        "audioBits": whole("audioBits"),
        "audioCodec": media.get("audioCodec") or "",
    }


def is_visual(media):
    """Ce media a-t-il quelque chose a montrer sous le trace ?"""
    return media is not None and media["kind"] != "audio"


def media_input(media, fps=None):
    """Options d'entree FFmpeg du media.

    Le decoupage se fait a l'entree (`-ss` / `-to`) : on ne decode que la portion
    jouee sous le trace, et les timestamps repartent de zero -- soit exactement
    l'origine des temps du trace.

    Une image n'a pas de duree propre : elle est bouclee, et c'est la sortie qui
    decide ou s'arreter. `fps` ne sert qu'a elle.
    """
    if media["kind"] == "image":
        return ["-loop", "1", "-framerate", str(fps or 25), "-i", media["path"]]
    args = []
    if media["in"] > 0.0:
        args += ["-ss", "%.6f" % media["in"]]
    if media["out"] > media["in"]:
        args += ["-to", "%.6f" % media["out"]]
    return args + ["-i", media["path"]]


def media_filters(media, out_w, out_h, fps, pad_color, freeze):
    """Chaine de filtres qui amene le media a la geometrie du canevas.

    Miroir exact de `fitBox()` cote navigateur :

    - `contain` fait entrer le media en entier, la couleur de fond (ou la
      transparence) occupant l'espace restant ;
    - `crop` retient la plus grande region au format du canevas qui tienne dans
      le media, divisee par le zoom et promenee par `posX`/`posY` sur le jeu
      restant, puis l'etire au canevas.

    Tout est exprime en `iw`/`ih` : les dimensions annoncees par ffprobe ne sont
    pas celles qu'on voit quand le fichier porte une rotation, et c'est bien
    l'image affichee qui a ete annotee.
    """
    fit = media["fit"]
    steps = ["fps=%d" % fps, "format=rgba"]
    if fit["mode"] == "crop":
        aspect = out_w / float(out_h)
        # `ow`/`oh` designent ici la region retenue : la position est le
        # deplacement du cadre dans le jeu que le zoom a libere.
        steps.append(
            "crop=w='min(iw\\,ih*%.6f)/%.6f':h='min(ih\\,iw/%.6f)/%.6f'"
            ":x='(iw-ow)*%.6f':y='(ih-oh)*%.6f'"
            % (aspect, fit["zoom"], aspect, fit["zoom"], fit["posX"], fit["posY"]))
        steps.append("scale=%d:%d:flags=bicubic" % (out_w, out_h))
    else:
        steps.append("scale=%d:%d:force_original_aspect_ratio=decrease:flags=bicubic"
                     % (out_w, out_h))
        steps.append("pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=%s" % (out_w, out_h, pad_color))
    steps.append("setsar=1")
    if freeze and media["kind"] != "image":
        # Le trace peut durer plus longtemps que la portion jouee (lecture qui
        # a bufferise, point OUT atteint avant la fin du geste). L'apercu doit
        # alors montrer la fin du trace sur une image figee plutot que du noir.
        steps.append("tpad=stop=-1:stop_mode=clone")
    return ",".join(steps)


def timecode(seconds, fps):
    """Secondes -> HH:MM:SS:FF non drop-frame, comme le lecteur l'affiche.

    Sert a donner la meme premiere image a toutes les couches d'un export pro :
    le point IN devient le timecode de depart des trois fichiers, donc les poser
    dans un montage suffit a les aligner.
    """
    frames = max(0, int(round(max(0.0, seconds) * fps)))
    return "%02d:%02d:%02d:%02d" % (frames // (3600 * fps), frames // (60 * fps) % 60,
                                    frames // fps % 60, frames % fps)


# --------------------------------------------------------------------------
# Compositing
# --------------------------------------------------------------------------

def _apply_layer(target, alpha, color, opacity, eraser):
    """Compose un calque alpha sur une region RGBA premultipliee (in-place)."""
    a = alpha if opacity >= 1.0 else alpha * opacity
    inv = (1.0 - a)[..., None]
    if eraser:
        target *= inv
    else:
        target *= inv
        target[..., 0] += color[0] * a
        target[..., 1] += color[1] * a
        target[..., 2] += color[2] * a
        target[..., 3] += a


def _stack_sheet(target, source):
    """Compose une couche premultipliee sur une autre (source-over, in-place)."""
    inv = (1.0 - source[..., 3])[..., None]
    target *= inv
    target += source


def _write_region(sheets, bbox, out_u8, alpha_mode, background):
    """Recalcule out_u8 sur la region donnee en empilant toutes les couches.

    Chaque couche est prise dans son etat courant, ses traces en cours d'ecriture
    comprises : une trace qui n'est pas encore terminee doit deja passer sous la
    couche du dessus, pas par-dessus la pile.
    """
    y0, y1, x0, x1 = bbox
    region = None
    for sheet in sheets:
        layer = sheet.canvas[y0:y1, x0:x1].copy()
        for stroke in sheet.actives:
            planned = stroke.planned
            _apply_layer(layer, stroke.scratch[y0:y1, x0:x1], planned.color,
                         planned.opacity, planned.eraser)
        if region is None:
            region = layer
        else:
            _stack_sheet(region, layer)

    np.clip(region, 0.0, 1.0, out=region)
    dest = out_u8[y0:y1, x0:x1]
    if alpha_mode:
        a = region[..., 3]
        safe = np.maximum(a, 1e-5)[..., None]
        rgb = np.clip(region[..., :3] / safe, 0.0, 1.0)
        # Conversion en toute derniere etape, sur la couleur demultipliee : le
        # compositing reste en sRGB (identique au canvas de preview), seul
        # l'encodage du fichier passe en Rec. 709. L'alpha n'est pas converti.
        dest[..., :3] = (cs.srgb_to_rec709(rgb) * 255.0 + 0.5).astype(np.uint8)
        dest[..., 3] = (a * 255.0 + 0.5).astype(np.uint8)
    else:
        # Aplatissement sur la couleur de fond du canevas : la gomme revele ce
        # fond, pas la transparence.
        opaque = np.clip(region[..., :3] + (1.0 - region[..., 3])[..., None] * background,
                         0.0, 1.0)
        dest[..., :3] = (cs.srgb_to_rec709(opaque) * 255.0 + 0.5).astype(np.uint8)
        dest[..., 3] = 255


# --------------------------------------------------------------------------
# FFmpeg
# --------------------------------------------------------------------------

# Sans out_color_matrix explicite, swscale choisit sa matrice d'apres la taille
# de l'image et retombe sur BT.601 en dessous de la HD, ce qui contredirait
# l'etiquette bt709 posee sur le fichier.
TO_VIDEO_RANGE = "scale=in_range=full:out_range=tv:out_color_matrix=bt709"


def _video_codec(codec, alpha_mode):
    if codec == "h264":
        # `-bf 0` : aucune image B, donc aucun reordonnancement.
        #
        # x264 en produit deux par defaut, ce qui fait sortir les deux premiers
        # paquets avec un DTS negatif (-2 images). Le conteneur MP4 rattrape
        # avec une liste d'edition, et un lecteur qui l'honore affiche la bonne
        # duree ; beaucoup de logiciels de montage l'ignorent et demarrent a la
        # premiere image decodee. L'apercu arrivait alors *deux images avant*
        # les couches ProRes, qui n'ont pas d'images B -- et debutait par des
        # images de pre-roll que le decodeur rend figees ou noires. C'est le
        # decalage constate en posant les trois couches dans un montage.
        #
        # Le cout est negligeable pour ce que fait ce fichier : c'est une copie
        # de visee, pas un master de diffusion, et l'alignement image a image
        # avec les deux autres couches vaut bien quelques pourcents de debit.
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-bf", "0", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    cmd = ["-c:v", "prores_ks", "-profile:v", "4444", "-vendor", "apl0"]
    if alpha_mode:
        return cmd + ["-pix_fmt", "yuva444p10le", "-alpha_bits", "16"]
    return cmd + ["-pix_fmt", "yuv444p10le"]


def _audio_codec(codec):
    """Piste son du media, reprise telle quelle des qu'il en a une.

    Elle ne sert pas au montage -- il a le rush d'origine -- mais a verifier
    d'un coup d'oreille que la couche tombe la ou on croit.
    """
    return ["-c:a", "aac", "-b:a", "192k"] if codec == "h264" else ["-c:a", "pcm_s16le"]


# PCM que le conteneur WAV porte nativement. Un PCM gros-boutiste (vieux AIFF)
# n'en fait pas partie : il est reencode dans la variante petit-boutiste de meme
# definition, ce qui ne perd rien.
WAV_PCM = {"pcm_u8", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le"}


def wav_codec(media):
    """Format PCM du WAV livre, choisi d'apres la source.

    Un ProRes se livre en ProRes ; un son se livre a la definition ou il a ete
    travaille. Une source PCM ressort donc dans sa propre variante -- ce qui est
    bit a bit la meme chose -- et une source lossless (FLAC, ALAC) garde ses 24
    bits. Seul un codec avec perte retombe sur 16 bits : il n'a pas de
    definition propre, et lui en inventer une ne restituerait rien.

    Le reencodage n'est pas evite meme quand la copie serait possible : `-c:a
    copy` coupe au paquet, donc jusqu'a une vingtaine de millisecondes a cote du
    point IN. Sur une couche dont la raison d'etre est la synchronisation,
    c'est le seul detail qui ne se rattrape pas.
    """
    source = media.get("audioCodec") or ""
    if source in WAV_PCM:
        return source
    return "pcm_s24le" if media.get("audioBits", 0) >= 24 else "pcm_s16le"


def build_audio_command(path, media, timecode_value=None):
    """Commande d'encodage de la couche son : le media, coupe aux bornes IN/OUT.

    Un WAV n'a pas de piste de timecode. Le `bext` des Broadcast Wave, lui,
    porte un `time_reference` en echantillons depuis minuit : c'est la meme
    information que le timecode des couches video, dans la seule forme qu'un
    fichier son sache transporter.
    """
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-progress", "pipe:1", "-nostdin"]
    cmd += media_input(media)
    cmd += ["-vn", "-map", "0:a:0", "-c:a", wav_codec(media)]
    rate = media.get("audioRate") or 0
    if rate:
        cmd += ["-write_bext", "1",
                "-metadata", "time_reference=%d" % round(media["in"] * rate)]
    if timecode_value:
        # Sans piste video, FFmpeg n'a nulle part ou poser un timecode ; on le
        # garde comme metadonnee, lisible dans n'importe quel outil.
        cmd += ["-metadata", "timecode=%s" % timecode_value]
    cmd.append(path)
    return cmd


def _tagging(timecode_value):
    # Etiquetage du fichier. `bt709` en color_trc vaut gamma 2.4 a l'affichage :
    # H.273 n'a pas de code "gamma 2.4 pur", et un flux Rec. 709 se decode selon
    # BT.1886. Voir la note en tete de colorspace.py.
    cmd = [
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-color_range", "tv",
    ]
    if timecode_value:
        cmd += ["-timecode", timecode_value]
    return cmd


def build_ffmpeg_command(path, width, height, fps, codec, alpha_mode,
                         media=None, background=None, frames=None, timecode_value=None):
    """Commande d'encodage du trace, eventuellement pose sur le media.

    Sans media, l'entree brute part directement a l'encodeur : c'est le chemin
    historique, inchange. Avec un media visuel, il devient la seconde entree
    d'un `overlay`, et le trace arrive alors en alpha droit -- l'aplatissement
    se fait dans le graphe de filtres, pas dans numpy.

    Un media audio n'a rien a montrer : le trace est aplati sur le fond comme
    s'il n'y avait pas de media, et le fichier n'herite que de sa piste son.

    Un seul processus FFmpeg dans tous les cas : passer par un fichier
    intermediaire de trace couterait un encodage et un decodage entiers, pour
    exactement le meme resultat.
    """
    # Les pixels arrivent deja encodes en Rec. 709 gamma 2.4 (voir _write_region
    # et colorspace.py). FFmpeg n'a donc aucune conversion de transfert a faire :
    # il convertit RGB->YCbCr avec la matrice BT.709 et etiquette le flux.
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    if media is not None:
        cmd += media_input(media, fps)
    cmd += [
        "-f", "rawvideo",
        "-pixel_format", "rgba",
        "-video_size", "%dx%d" % (width, height),
        "-framerate", str(fps),
        "-i", "-",
    ]

    if is_visual(media):
        chain = media_filters(media, width, height, fps,
                              background_hex(background), freeze=True)
        # `shortest=1` : le fond est infini (image bouclee ou derniere image
        # clonee), c'est donc le trace qui decide de la fin.
        cmd += ["-filter_complex",
                "[0:v]%s[base];[base][1:v]overlay=0:0:format=auto:shortest=1,%s[flat]"
                % (chain, TO_VIDEO_RANGE),
                "-map", "[flat]"]
    else:
        cmd += ["-vf", TO_VIDEO_RANGE]
        # Deux entrees et pas de graphe complexe : sans cartographie explicite,
        # FFmpeg n'en retiendrait qu'une.
        if media is not None:
            cmd += ["-map", "1:v:0"]

    if media is not None and media["hasAudio"]:
        cmd += ["-map", "0:a:0?"] + _audio_codec(codec)
    elif media is not None:
        cmd += ["-an"]

    cmd += _video_codec(codec, alpha_mode)
    if frames:
        # Borne l'encodage a la mesure du trace : le fond, lui, ne s'arrete
        # jamais de lui-meme. `-t` fait la meme chose pour la piste son.
        cmd += ["-frames:v", str(int(frames)), "-t", "%.6f" % (frames / float(fps))]
    cmd += _tagging(timecode_value)
    cmd.append(path)
    return cmd


def build_media_command(path, media, width, height, fps, codec, alpha_mode,
                        background=None, frames=None, timecode_value=None):
    """Commande d'encodage du media seul, cadre dans le canevas.

    `alpha_mode` decide de ce qui entoure un media qui n'occupe pas tout le
    cadre : la couleur de fond, ou du vide. Le vide est ce qu'on veut quand la
    couche part dans un montage -- un rush vertical dans un canevas horizontal
    ressort alors centre, a son format, sans fond a decouper.

    Le gel de la derniere image (`freeze`) vaut ici comme pour l'apercu : la
    couche couvre toute la duree pendant laquelle le rush etait a l'ecran, y
    compris le depassement du trace sur une image arretee.
    """
    pad = TRANSPARENT if alpha_mode else background_hex(background)
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-progress", "pipe:1", "-nostdin"]
    cmd += media_input(media, fps)
    cmd += ["-filter_complex",
            "[0:v]%s,%s[out]" % (media_filters(media, width, height, fps, pad, freeze=True),
                                 TO_VIDEO_RANGE),
            "-map", "[out]"]
    if media["hasAudio"]:
        cmd += ["-map", "0:a:0?"] + _audio_codec(codec)
    else:
        cmd += ["-an"]
    cmd += _video_codec(codec, alpha_mode)
    if frames:
        cmd += ["-frames:v", str(int(frames)), "-t", "%.6f" % (frames / float(fps))]
    cmd += _tagging(timecode_value)
    cmd.append(path)
    return cmd


def _drain(pipe, sink):
    try:
        for line in iter(pipe.readline, b""):
            sink.append(line.decode("utf-8", "replace").rstrip())
            del sink[:-40]
    finally:
        pipe.close()


# --------------------------------------------------------------------------
# Rendu
# --------------------------------------------------------------------------

def flatten_mode(payload, media):
    """Ce qui passe sous le trace, une fois confronte au reste de la demande.

    La forme historique du payload (`alpha` booleen) reste comprise : elle dit
    la meme chose que « couche alpha » ou « fond uni », et les bancs de tests
    l'utilisent encore.
    """
    mode = payload.get("flatten")
    if mode not in (FLATTEN_MEDIA, FLATTEN_SOLID, FLATTEN_ALPHA):
        mode = FLATTEN_ALPHA if payload.get("alpha") else FLATTEN_SOLID
    if mode == FLATTEN_MEDIA and media is None:
        # Media absent, libere, ou choisi dans le navigateur : le fond uni est
        # le repli honnete, plutot qu'un export noir.
        mode = FLATTEN_SOLID
    # Un media audio reste FLATTEN_MEDIA : rien ne passe sous le trace, mais sa
    # piste son suit le fichier. C'est `is_visual` qui fait la difference.
    if mode == FLATTEN_ALPHA and payload.get("codec") == "h264":
        mode = FLATTEN_SOLID
    return mode


def frame_count(payload, fps):
    """Nombre d'images de la couche trace, queue comprise.

    Une seule mesure pour l'apercu et pour le trace : deux calculs differents
    donneraient deux fichiers d'une image d'ecart, et la superposition en post
    ne tomberait plus juste.
    """
    last = 0.0
    for sheet in payload_sheets(payload):
        for stroke in sheet["strokes"]:
            points = stroke.get("points") or []
            if points:
                try:
                    last = max(last, float(points[-1].get("t") or 0.0))
                except (AttributeError, TypeError, ValueError):
                    pass
    duration_ms = max(float(payload.get("durationMs") or 0.0), last) + TAIL_MS
    return max(1, int(math.ceil(duration_ms * fps / 1000.0)))


def _output_size(payload, codec):
    src_w = int(payload["width"])
    src_h = int(payload["height"])
    out_w = int(payload.get("outWidth") or src_w)
    out_h = int(payload.get("outHeight") or src_h)
    if codec == "h264":
        out_w -= out_w % 2
        out_h -= out_h % 2
    if out_w < 2 or out_h < 2:
        raise RenderError("Resolution de sortie invalide.")
    return src_w, src_h, out_w, out_h


def render(payload, output_path, progress=None, cancelled=None):
    """Rejoue les traces et encode la video. Retourne le chemin du fichier.

    payload : {
      'width', 'height'            taille de la zone de dessin (referentiel des traces)
      'outWidth', 'outHeight'      resolution de sortie (optionnel)
      'fps', 'codec'
      'flatten'                    'media' | 'solid' | 'alpha' (defaut : d'apres 'alpha')
      'background'                 couleur de fond '#rrggbb' (optionnel, blanc)
      'media'                      media de fond (voir normalize_media), optionnel
      'durationMs'                 duree enregistree (optionnel)
      'strokes': [{'brush', 'seed', 'points'}]
      'clears': [ms, ...]          instants ou la toile est remise a zero
    }
    """
    fps = int(payload.get("fps") or 30)
    codec = payload.get("codec") or "prores4444"
    src_w, src_h, out_w, out_h = _output_size(payload, codec)
    background = np.asarray(parse_background(payload.get("background")), dtype=np.float32)

    source = normalize_media(payload.get("media"))
    mode = flatten_mode(payload, source)
    media = source if mode == FLATTEN_MEDIA else None
    # Ce qui sort du fichier a un alpha : le trace seul. Ce que la boucle ecrit
    # dans le tuyau en a un des qu'il reste quelque chose a composer en aval --
    # sans quoi l'`overlay` recouvrirait le media d'un aplat opaque. Un media
    # audio ne compose rien : le trace est aplati ici, comme sur un fond uni.
    alpha_mode = mode == FLATTEN_ALPHA
    pipe_alpha = alpha_mode or is_visual(media)
    stamp = payload.get("timecode") or (timecode(source["in"], fps) if source else None)

    scale = out_w / float(src_w)

    caches = {}
    sheets = []
    for raw in payload_sheets(payload):
        planned_strokes = []
        for stroke in raw["strokes"]:
            brush = be.normalize(stroke.get("brush", {}))
            key = _brush_key(brush)
            cache = caches.get(key)
            if cache is None:
                cache = caches[key] = be.StampCache(brush)
            planned = be.plan_stroke(stroke, scale=scale, cache=cache)
            if planned.count:
                planned_strokes.append(planned)
        # Une couche vide -- jamais dessinee, ou masquee donc jamais envoyee --
        # n'a pas de canevas a elle : a 4K ce serait 132 Mo pour rien.
        if not planned_strokes:
            continue
        planned_strokes.sort(key=lambda s: s.times[0])
        sheets.append(_Sheet(planned_strokes,
                             sorted(float(value) for value in raw["clears"]),
                             out_h, out_w))

    if not sheets:
        raise RenderError("Aucun trace a exporter.")

    total_frames = frame_count(payload, fps)

    # Etat initial du buffer de sortie : seules les regions "sales" sont
    # recalculees ensuite, le reste doit donc deja etre correct.
    out_u8 = np.zeros((out_h, out_w, 4), dtype=np.uint8)
    if not pipe_alpha:
        out_u8[..., :3] = background_bytes(payload.get("background"))
        out_u8[..., 3] = 255

    cmd = build_ffmpeg_command(output_path, out_w, out_h, fps, codec, alpha_mode,
                               media=media, background=payload.get("background"),
                               frames=total_frames, timecode_value=stamp)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RenderError("FFmpeg est introuvable. Installez-le et ajoutez-le au PATH.")

    errors = []
    watcher = threading.Thread(target=_drain, args=(proc.stderr, errors), daemon=True)
    watcher.start()

    stdin = proc.stdin

    try:
        for frame_index in range(total_frames):
            if cancelled is not None and cancelled():
                raise RenderError("Export annule.")

            frame_time = (frame_index + 1) * 1000.0 / fps
            dirty = None

            for sheet in sheets:
                # "Effacer" pendant l'enregistrement : la couche repart a zero,
                # mais les traces deja poses restent dans les metadonnees (ils
                # ont ete rejoues avant cet instant). Les autres couches ne
                # bougent pas : un effacement n'attaque que la sienne.
                while sheet.clears and sheet.clears[0] <= frame_time:
                    sheet.clears.pop(0)
                    sheet.canvas[...] = 0.0
                    for stroke in sheet.actives:
                        stroke.scratch[...] = 0.0
                        stroke.bbox = None
                    dirty = _merge(dirty, (0, out_h, 0, out_w))

                while sheet.pending and sheet.pending[0].times[0] <= frame_time:
                    sheet.actives.append(_ActiveStroke(sheet.pending.pop(0), out_h, out_w))

                finished = []
                for stroke in sheet.actives:
                    planned = stroke.planned
                    cache = planned.cache
                    index = stroke.cursor
                    while index < planned.count and planned.times[index] <= frame_time:
                        x = float(planned.xs[index])
                        y = float(planned.ys[index])
                        ix, iy = math.floor(x), math.floor(y)
                        stamp = cache.get(float(planned.sizes[index]),
                                          float(planned.angles[index]), x - ix, y - iy)
                        alpha = float(planned.alphas[index])
                        scaled = stamp if alpha >= 1.0 else stamp * alpha
                        half = stamp.shape[0] // 2
                        region = be.stamp_onto(stroke.scratch, scaled, ix - half, iy - half)
                        stroke.grow(region)
                        dirty = _merge(dirty, region)
                        index += 1
                    stroke.cursor = index
                    if index >= planned.count:
                        finished.append(stroke)

                for stroke in finished:
                    sheet.actives.remove(stroke)
                    _commit(sheet.canvas, stroke)

            if dirty is not None:
                _write_region(sheets, dirty, out_u8, pipe_alpha, background)

            stdin.write(out_u8.tobytes())

            if progress is not None:
                progress(frame_index + 1, total_frames)

        stdin.close()
        code = proc.wait()
        if code != 0:
            raise RenderError("FFmpeg a echoue (code %s): %s" % (code, " / ".join(errors[-4:])))
    except BrokenPipeError:
        proc.wait()
        raise RenderError("FFmpeg s'est interrompu : %s" % " / ".join(errors[-4:]))
    except BaseException:
        try:
            if proc.poll() is None:
                proc.kill()
        finally:
            proc.wait()
        raise
    finally:
        watcher.join(timeout=1.0)

    return output_path


def media_extension(media):
    """Extension de la couche « media » : un son ne se livre pas en .mov."""
    return "wav" if media and media.get("kind") == "audio" else "mov"


def render_media(payload, output_path, progress=None, cancelled=None):
    """Encode la couche « media » : le media seul, cadre dans le canevas.

    Aucun trace ici, donc aucune image a fabriquer : FFmpeg lit, cadre et
    encode tout seul, et l'on se contente de suivre sa progression.

    La duree est celle pendant laquelle le rush etait a l'ecran : les bornes
    IN/OUT, et, si le trace les a depassees, le gel de la derniere image
    jusqu'a la fin du trace. La couche couvre donc exactement ce que l'apercu
    montre -- image figee comprise -- ce qui est la condition pour que les
    reposer l'une sur l'autre redonne l'apercu jusqu'a la derniere image.

    Les bornes restent un plancher : un trace arrete avant le point OUT ne
    raccourcit pas la couche. Ce qui a ete joue reste disponible au montage.

    Un media audio donne une couche audio : un WAV coupe aux memes bornes, a la
    definition de la source. Il n'y a rien a cadrer, mais tout a synchroniser --
    et une queue de silence n'aurait rien a montrer.
    """
    fps = int(payload.get("fps") or 30)
    codec = payload.get("codec") or "prores4444"

    media = normalize_media(payload.get("media"))
    if media is None:
        raise RenderError("Aucun media de fond a exporter.")

    stamp = payload.get("timecode") or timecode(media["in"], fps)
    if media["kind"] == "audio":
        span = media["out"] - media["in"]
        frames = max(1, int(round(span * fps))) if span > 0 else frame_count(payload, fps)
        cmd = build_audio_command(output_path, media, timecode_value=stamp)
        return _run_ffmpeg(cmd, frames, progress, cancelled)

    _, _, out_w, out_h = _output_size(payload, codec)
    alpha_mode = bool(payload.get("mediaAlpha")) and codec != "h264"
    span = media["out"] - media["in"]
    if media["kind"] == "image" or span <= 0.0:
        # Une image n'a pas de bornes -- et une video dont personne ne sait dire
        # la duree n'en a pas de mesurables : la couche tient alors toute la
        # duree du trace, ce qui est toujours le repli le plus sur.
        frames = frame_count(payload, fps)
    else:
        # Le rush est reste a l'ecran de son point IN jusqu'a la fin du trace :
        # les bornes donnent le plancher, le trace l'etirement eventuel.
        frames = max(1, int(round(span * fps)), frame_count(payload, fps))

    cmd = build_media_command(output_path, media, out_w, out_h, fps, codec, alpha_mode,
                              background=payload.get("background"), frames=frames,
                              timecode_value=stamp)
    return _run_ffmpeg(cmd, frames, progress, cancelled)


def _run_ffmpeg(cmd, frames, progress, cancelled):
    """Lance un encodage que FFmpeg mene seul, en suivant sa progression.

    `-progress pipe:1` egrene les images encodees ; pour une couche son, ou il
    n'y en a pas, la barre reste a zero jusqu'a la fin -- un WAV de quelques
    secondes s'ecrit trop vite pour que cela se voie.
    """
    output_path = cmd[-1]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RenderError("FFmpeg est introuvable. Installez-le et ajoutez-le au PATH.")

    errors = []
    watcher = threading.Thread(target=_drain, args=(proc.stderr, errors), daemon=True)
    watcher.start()

    try:
        for raw in iter(proc.stdout.readline, b""):
            if cancelled is not None and cancelled():
                proc.kill()
                proc.wait()
                raise RenderError("Export annule.")
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("frame=") and progress is not None:
                try:
                    progress(min(frames, int(line[6:])), frames)
                except ValueError:
                    pass
    except BaseException:
        try:
            if proc.poll() is None:
                proc.kill()
        finally:
            proc.wait()
        raise
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass

    code = proc.wait()
    watcher.join(timeout=1.0)
    if code != 0:
        raise RenderError("FFmpeg a echoue (code %s): %s" % (code, " / ".join(errors[-4:])))
    if progress is not None:
        progress(frames, frames)
    return output_path


def _commit(canvas, stroke):
    """Fusionne definitivement un trace termine dans le canvas."""
    if stroke.bbox is None:
        return
    y0, y1, x0, x1 = stroke.bbox
    planned = stroke.planned
    _apply_layer(canvas[y0:y1, x0:x1], stroke.scratch[y0:y1, x0:x1],
                 planned.color, planned.opacity, planned.eraser)


def _brush_key(brush):
    """Deux pinceaux partagent un cache s'ils produisent les memes empreintes."""
    return (brush["shape"], round(brush["hardness"], 4), round(brush["aspect"], 4),
            brush.get("texture") or "")
