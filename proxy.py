"""Proxys de lecture : n'importe quel media -> quelque chose qu'un navigateur sait lire.

Le probleme que ce module resout est celui d'un rush de montage. Un ProRes, un
DNxHD, un MXF, un .mov en 4:4:4 12 bits : aucun navigateur ne sait les decoder.
Ni Safari sur l'iPad, ni Chrome sur le poste de travail. Tant que le media etait
choisi par un <input type=file>, l'application heritait simplement de cette
limite.

On transcode donc a la demande une copie legere -- H.264 / AAC en MP4 faststart,
bord long ramene a `MAX_EDGE` -- et c'est elle que les clients lisent. Deux
consequences utiles :

- le meme fichier alimente le poste et la tablette, sans se demander lequel des
  deux sait decoder quoi ;
- le proxy peut etre franchement plus leger que la source, puisqu'il ne sert
  qu'a viser : l'export final ne le regarde jamais.

Ce dernier point est le fond de l'affaire. Le media entre bien dans le rendu
depuis l'export multi-couches (voir l'en-tete de `renderer.py`), mais c'est
toujours la **source** qui y est encodee, jamais la copie de lecture -- seule
une page de PDF fait exception, puisque rasterisee elle n'existe que sous sa
forme servie. Le proxy reste donc un pur artefact de visee, et sa qualite n'a
aucun effet sur le fichier livre : un tracage fait sur un proxy 1280 px ressort
en 4K sans perte.

Un media deja lisible tel quel (H.264 raisonnablement dimensionne, JPEG, MP3)
ne passe pas par la case transcodage : `plan()` le signale et il est servi
directement depuis son emplacement d'origine.

Le cache est **persistant et sous controle de l'utilisateur.ice**. Rien n'est
efface automatiquement -- ni en quittant l'application, ni a l'export : rouvrir
un rush deja prepare doit etre instantane, et un transcodage represente parfois
plusieurs minutes qu'il serait absurde de refaire. Le menage se fait a la main
depuis le panneau de gestion (voir `entries`, `remove`, `purge`).
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid

import paths
import pdfdoc
import renderer

# Bord long du proxy video. 1280 px couvre tres large : l'ecran d'un iPad Pro
# affiche ~1180 px de large, et le proxy ne sert qu'a viser.
MAX_EDGE = 1280

# Au-dela de ce debit, un rush sature la liaison Wi-Fi de la tablette. Seuil
# purement reseau : le poste, lui, lit le fichier depuis son propre disque et
# n'a aucune raison de s'en soucier. C'est pourquoi ce critere ne declenche un
# transcodage qu'en mode tablette (voir `plan`).
MAX_BITRATE = 30_000_000

# Ce qu'un navigateur decode vraiment. Le nom du codec ne suffit pas : un
# H.264 en 4:2:2 ou 10 bits (courant en livraison intermediaire, parfois
# choisi justement pour eviter le ProRes) porte le meme `codec_name` qu'un
# H.264 ordinaire et ne se lit nulle part sur le web. Un fichier hors de cette
# liste doit etre transcode meme si son codec parait bon.
BROWSER_PIX_FMTS = {"yuv420p", "yuvj420p"}

# Codec des proxys video. H.264 par defaut : c'est le seul que decodent a coup
# sur Safari sur iPad, Chrome, Edge et les navigateurs des tablettes Android.
#
# VP9/WebM existe pour les navigateurs compiles sans codec proprietaire -- le
# Chromium empaquete par certaines distributions Linux, notamment, ne lit pas
# le H.264. Sur ces machines, un proxy H.264 donne une image noire sans le
# moindre message d'erreur : LIVE_NOTES_PROXY_FORMAT=vp9 les rattrape.
PROXY_FORMAT = (os.environ.get("LIVE_NOTES_PROXY_FORMAT") or "h264").strip().lower()

_VIDEO_FORMATS = {
    "h264": {
        "extension": ".mp4",
        "video": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                  "-profile:v", "high", "-pix_fmt", "yuv420p"],
        "audio": ["-c:a", "aac", "-b:a", "128k", "-ac", "2"],
        "flags": ["-movflags", "+faststart"],
    },
    "vp9": {
        "extension": ".webm",
        # `-row-mt 1 -cpu-used 5` : sans cela libvpx-vp9 encode a une vitesse
        # qui rendrait la preparation d'un rush plus longue que le rush.
        "video": ["-c:v", "libvpx-vp9", "-crf", "34", "-b:v", "0",
                  "-row-mt", "1", "-cpu-used", "5", "-pix_fmt", "yuv420p"],
        "audio": ["-c:a", "libopus", "-b:a", "128k"],
        "flags": [],
    },
}


# Codecs qu'un navigateur decode nativement.
#
# `hevc` y figure sur decision explicite : Safari sur iPad le lit, et les
# navigateurs de bureau recents aussi lorsque la machine dispose du decodage
# materiel. Ce n'est pas garanti partout -- un poste sans ce support affiche
# une image noire sans message d'erreur. D'ou le bouton « transcoder ce rush »
# du panneau de gestion (voir app.api_proxy_force) : il permet de forcer une
# copie lisible sans avoir a deviner a l'avance ce que chaque machine sait
# faire.
WEB_VIDEO = {"h264", "hevc", "vp8", "vp9", "av1"}

# Le PCM d'un WAV se lit partout, et dans toutes les profondeurs courantes --
# 8, 16, 24, 32 bits entiers comme le flottant 32 bits. N'y avoir mis que
# `pcm_s16le` etait une erreur de recopie : elle envoyait au transcodeur des
# bandes-son parfaitement lisibles, au seul motif qu'elles etaient gravees en
# 24 bits, ce qui est pourtant la livraison normale d'une salle de montage.
# Restent dehors les formats qu'aucun navigateur ne prend : le 64 bits
# flottant, et les PCM gros-boutistes des vieux AIFF.
WEB_AUDIO = {
    "aac", "mp3", "opus", "vorbis", "flac", "alac",
    "pcm_u8", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le",
}
WEB_IMAGE = {"mjpeg", "png", "gif", "webp", "bmp"}
WEB_CONTAINERS = ("mp4", "mov", "m4a", "webm", "matroska", "ogg", "mp3", "wav",
                  "aiff", "flac")


def _video_format():
    return _VIDEO_FORMATS.get(PROXY_FORMAT, _VIDEO_FORMATS["h264"])


def _playable_video():
    """Codecs consideres comme lisibles tels quels.

    En mode VP9, le H.264 sort de la liste : si l'on a bascule sur VP9, c'est
    precisement parce que le navigateur cible ne sait pas decoder le H.264, et
    lui servir un rush H.264 sans le transcoder donnerait la meme image noire
    que le proxy qu'on cherchait a eviter. Meme raisonnement cote audio.
    """
    if PROXY_FORMAT == "vp9":
        return {"vp8", "vp9", "av1"}, {"opus", "vorbis"}
    return WEB_VIDEO, WEB_AUDIO

_IMAGE_FORMATS = {
    "image2", "png_pipe", "jpeg_pipe", "mjpeg", "tiff_pipe", "webp_pipe",
    "bmp_pipe", "gif", "psd_pipe", "dpx_pipe", "exr_pipe", "jpegls_pipe",
}


def _ffprobe():
    """ffprobe vit toujours a cote de ffmpeg : on cherche d'abord la ou
    `renderer` a trouve le binaire, avant de retomber sur le PATH."""
    override = os.environ.get("LIVE_NOTES_FFPROBE")
    if override:
        return override
    directory = os.path.dirname(renderer.FFMPEG)
    if directory:
        suffix = ".exe" if os.name == "nt" else ""
        candidate = os.path.join(directory, "ffprobe" + suffix)
        if os.path.exists(candidate):
            return candidate
    return shutil.which("ffprobe") or "ffprobe"


class ProxyError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Analyse
# --------------------------------------------------------------------------

def probe(path, page=None, long_edge=None):
    """Decrit un media : nature, dimensions, duree, codecs.

    Leve ProxyError si le fichier est illisible -- c'est le seul moment ou
    l'on peut le dire proprement a l'utilisateur.ice, avant d'avoir promis
    quoi que ce soit a la tablette.

    `page` et `long_edge` ne concernent que les PDF : quelle page decrire, et a
    quelle definition. Ils sont ignores pour tout le reste.
    """
    if not os.path.isfile(path):
        raise ProxyError("Fichier introuvable : %s" % path)

    # ffprobe ne sait pas demuxer un PDF : sans cette derivation, le fichier
    # echouerait ici avec un message de codec qui n'apprendrait rien.
    if pdfdoc.is_pdf(path):
        return _probe_pdf(path, page, long_edge)

    cmd = [_ffprobe(), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                                **_no_window())
    except FileNotFoundError:
        raise ProxyError("ffprobe est introuvable. Installez FFmpeg et ajoutez-le au PATH.")
    except subprocess.SubprocessError as exc:
        raise ProxyError("Analyse du media impossible : %s" % exc)

    if result.returncode != 0:
        raise ProxyError("Media illisible : %s" % (result.stderr.strip().splitlines() or ["erreur inconnue"])[-1])

    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise ProxyError("Reponse d'ffprobe illisible.")

    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    # Une pochette d'album est encodee comme un flux video d'une seule image :
    # sans ce filtre, un MP3 illustre passerait pour une video.
    video = next((s for s in streams
                  if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    format_name = (fmt.get("format_name") or "").lower()
    duration = _float(fmt.get("duration")) or (_float(video.get("duration")) if video else 0.0)

    info = {
        "path": path,
        "name": os.path.basename(path),
        "size": _int(fmt.get("size")) or _file_size(path),
        "container": format_name,
        "duration": duration,
        "width": _int(video.get("width")) if video else 0,
        "height": _int(video.get("height")) if video else 0,
        "videoCodec": (video or {}).get("codec_name") or "",
        "audioCodec": (audio or {}).get("codec_name") or "",
        "pixFmt": (video or {}).get("pix_fmt") or "",
        "profile": (video or {}).get("profile") or "",
        "bitrate": _bitrate(fmt, video, path),
        "fps": _fps(video) if video else 0.0,
        "hasAudio": audio is not None,
        # Servent a l'export d'une couche son : le WAV livre garde la
        # definition de la source plutot que de la ramener d'office a 16 bits,
        # et sa cadence d'echantillonnage porte le point IN (voir renderer).
        "audioRate": _int((audio or {}).get("sample_rate")),
        "audioBits": _audio_bits(audio),
    }
    info["kind"] = _kind(info, video, audio, format_name)
    if info["kind"] == "image":
        # Une image n'a pas de duree exploitable : ffprobe en invente une
        # (1/25 s) que le transport n'aurait aucune raison d'afficher.
        info["duration"] = 0.0
    return info


def _probe_pdf(path, page, long_edge):
    """Decrit une page de PDF sous la meme forme qu'un media ordinaire.

    `kind` vaut `"image"` et non `"pdf"` : la copie de lecture *sera* une
    image, et tout l'aval -- transport, cadrage, publication vers la tablette --
    peut alors la traiter comme n'importe quelle autre sans rien connaitre des
    PDF. Seules deux cles supplementaires trahissent l'origine, et elles ne
    servent qu'a `plan`, `build` et `_signature`.
    """
    try:
        described = pdfdoc.describe(path)
    except pdfdoc.PdfError as exc:
        raise ProxyError(str(exc))

    edge = pdfdoc.target_edge(long_edge, 0)
    number = pdfdoc.clamp_page(page, described["pageCount"])
    width_pt, height_pt = described["pages"][number - 1]
    width, height = pdfdoc.pixels(width_pt, height_pt, edge)

    return {
        "path": path,
        # La page fait partie du nom affiche : sans elle, le bandeau media et
        # le panneau de proxys montreraient deux entrees identiques pour deux
        # pages differentes du meme document.
        "name": "%s (page %d)" % (os.path.basename(path), number),
        # Le nom nu, sans la page : la fenetre de choix parle du document, pas
        # de la page qu'on est en train de quitter.
        "pdfName": os.path.basename(path),
        "size": _file_size(path),
        "container": "pdf",
        "duration": 0.0,
        "width": width,
        "height": height,
        "videoCodec": "pdf",
        "audioCodec": "",
        "pixFmt": "",
        "profile": "",
        "bitrate": 0,
        "fps": 0.0,
        "hasAudio": False,
        "audioRate": 0,
        "audioBits": 0,
        "kind": "image",
        "pdfPage": number,
        "pdfPageCount": described["pageCount"],
        "pdfEdge": edge,
    }


def _kind(info, video, audio, format_name):
    if video is None:
        return "audio" if audio is not None else "unknown"
    frames = _int(video.get("nb_frames"))
    base = format_name.split(",")[0]
    looks_still = base in _IMAGE_FORMATS or format_name in _IMAGE_FORMATS
    if looks_still and (frames or 1) <= 1:
        return "image"
    # Un flux d'une seule image dans un conteneur video reste une image fixe.
    if frames == 1 and info["duration"] <= 0.5 and audio is None:
        return "image"
    return "video"


# --------------------------------------------------------------------------
# Decision
# --------------------------------------------------------------------------

def plan(info, for_tablet=False, force=False, bypass=False):
    """Faut-il transcoder, et vers quoi ? Retourne (needed, reason, dimensions).

    Deux familles de raisons, qu'il faut distinguer :

    - **illisible** (codec, conteneur, chroma, profondeur) : le navigateur ne
      sait pas decoder le fichier, ni celui de la tablette ni celui du poste.
      On transcode toujours, meme pour un usage local.
    - **trop lourd pour le reseau** (debit) : le fichier se lit parfaitement,
      mais il ne passera pas dans un Wi-Fi. Le poste le lit depuis son disque
      et n'est pas concerne -- ce critere n'agit donc qu'en mode tablette,
      `for_tablet=True`.

    Tout cela reste une **presomption**. Aucune analyse de fichier ne dit ce
    qu'un navigateur donne decode reellement : la reponse depend de la version,
    du systeme, et de la presence d'un decodeur materiel. D'ou deux echappatoires
    symetriques, qui sont le vrai dernier mot :

    - `force=True` transcode inconditionnellement, quand un fichier repute bon
      s'affiche noir (HEVC sans decodage materiel, typiquement) ;
    - `bypass=True` sert la source telle quelle, quand la presomption
      d'illisibilite s'avere fausse sur cette machine-la.
    """
    kind = info["kind"]
    if kind == "unknown":
        raise ProxyError("Aucun flux audio ou video exploitable dans ce fichier.")

    # Un PDF n'a pas de version « servie telle quelle » : aucun navigateur ne
    # l'affiche dans un <img>. La rasterisation n'est donc jamais facultative,
    # et `bypass` n'a pas de sens ici -- d'ou ce retour avant lui.
    #
    # Les dimensions sont celles calculees par `_probe_pdf`, pas `_target_size` :
    # le plafond de 1280 px vaut pour une visee degradee, alors que la page
    # rasterisee est la seule version qui existe.
    if info.get("pdfPageCount"):
        return (True, "page %d d'un PDF rasterisee" % info["pdfPage"],
                (info["width"], info["height"]))

    if bypass and not force:
        return False, "", (info["width"], info["height"])

    playable_video, playable_audio = _playable_video()
    container_ok = any(token in info["container"] for token in WEB_CONTAINERS)

    if kind == "image":
        target = _target_size(info["width"], info["height"])
        if force:
            return True, "conversion demandee manuellement", target
        if info["videoCodec"] in WEB_IMAGE:
            return False, "", (info["width"], info["height"])
        return True, "format d'image non lisible par un navigateur (%s)" % info["videoCodec"], target

    if kind == "audio":
        if force:
            return True, "conversion demandee manuellement", (0, 0)
        if container_ok and info["audioCodec"] in playable_audio:
            return False, "", (0, 0)
        return True, "codec audio non lisible par un navigateur (%s)" % info["audioCodec"], (0, 0)

    target = _target_size(info["width"], info["height"])
    if force:
        return True, "conversion demandee manuellement", target

    # --- Illisible : vrai quel que soit le mode -----------------------------
    if info["videoCodec"] not in playable_video:
        return True, "codec video non lisible par un navigateur (%s)" % info["videoCodec"], target
    if not container_ok:
        return True, "conteneur non lisible par un navigateur (%s)" % info["container"], target
    if info["hasAudio"] and info["audioCodec"] not in playable_audio:
        # Seule facon de garder le son : le fichier livre est unique, on ne peut
        # pas reencoder la seule piste audio sans repasser par le conteneur. La
        # liste des PCM lisibles ayant ete corrigee, ce cas ne concerne plus
        # guere que l'AC-3 et consorts ; « Servir l'original » l'annule pour qui
        # prefere l'image muette a l'attente.
        return True, "piste audio non lisible par un navigateur (%s)" % info["audioCodec"], target
    if info["pixFmt"] and info["pixFmt"] not in BROWSER_PIX_FMTS:
        # Un 4:2:2 ou du 10 bits porte pourtant un `codec_name` irreprochable.
        return True, "chroma ou profondeur non lisible par un navigateur (%s)" % info["pixFmt"], target

    # --- Lisible, mais trop lourd pour la liaison de la tablette ------------
    if for_tablet and info["bitrate"] > MAX_BITRATE:
        return True, "debit de %.0f Mbps ramene pour le reseau" % (info["bitrate"] / 1e6), target

    return False, "", (info["width"], info["height"])


def _target_size(width, height):
    """Bord long ramene a MAX_EDGE, dimensions paires (impose par yuv420p)."""
    if not width or not height:
        return (0, 0)
    longest = max(width, height)
    if longest <= MAX_EDGE:
        scale = 1.0
    else:
        scale = MAX_EDGE / float(longest)
    out_w = max(2, int(round(width * scale)))
    out_h = max(2, int(round(height * scale)))
    return (out_w - out_w % 2, out_h - out_h % 2)


# --------------------------------------------------------------------------
# Cache sur disque
# --------------------------------------------------------------------------

_cache_lock = threading.RLock()

# Journal du cache. Sans lui, le dossier ne contiendrait que des « proxy_a1b2.mp4 »
# anonymes : impossible de dire de quel rush ils viennent, ni de reutiliser
# celui qu'on a deja fabrique. C'est ce fichier qui rend le cache persistant
# utile plutot qu'encombrant.
INDEX_NAME = "index.json"


def cache_dir():
    directory = paths.proxy_cache_dir()
    os.makedirs(directory, exist_ok=True)
    return directory


def _index_path():
    return os.path.join(cache_dir(), INDEX_NAME)


def _read_index():
    try:
        with open(_index_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_index(entries):
    target = _index_path()
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(entries, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, target)


def _signature(info, forced=False):
    """Identifie une source *et son etat*.

    La date de modification et la taille en font partie : re-exporter un rush
    sous le meme nom depuis le montage doit invalider le proxy, sinon on
    continuerait d'annoter une version perimee sans s'en apercevoir.

    Pour un PDF, la page et la definition de rasterisation en font partie au
    meme titre. Sans elles, demander la page 3 ressortirait le PNG en cache de
    la page 1, et changer de format de canevas garderait une rasterisation
    calculee pour l'ancien.
    """
    try:
        stat = os.stat(info["path"])
        stamp = "%d:%d" % (int(stat.st_mtime), stat.st_size)
    except OSError:
        stamp = "?"
    parts = [os.path.abspath(info["path"]), stamp, PROXY_FORMAT,
             str(MAX_EDGE), "force" if forced else "auto"]
    if info.get("pdfPageCount"):
        parts.append("pdf:%s@%s" % (info.get("pdfPage"), info.get("pdfEdge")))
    return "|".join(parts)


def find_cached(info, forced=False):
    """Proxy deja fabrique pour cette source, ou None.

    Les deux variantes de signature sont consultees, et non la seule demandee.
    « force » et « auto » ne decrivent pas le fichier produit mais la raison de
    le produire : a source et format identiques, `plan` vise les memes
    dimensions dans les deux cas, donc le fichier serait refabrique a
    l'identique. Ne chercher que sa propre variante relancait plusieurs minutes
    de transcodage pour rien -- ce qui arrivait des qu'on passait par « Servir
    l'original » avant de redemander un transcodage.
    """
    wanted = (_signature(info, forced), _signature(info, not forced))
    with _cache_lock:
        for entry in _read_index():
            if entry.get("signature") not in wanted:
                continue
            path = os.path.join(cache_dir(), entry.get("file") or "")
            if os.path.isfile(path):
                return path
            # Fichier efface a la main hors de l'application : on oublie
            # l'entree plutot que de promettre un proxy qui n'existe plus.
            forget(entry.get("file"))
            return None
    return None


def cached_for_source(path):
    """Un proxy utilisable existe-t-il deja pour ce fichier, tel qu'il est ?

    Sert a l'interface, qui doit savoir s'il y a un proxy a *reprendre* ou un
    transcodage a *lancer* -- ce ne sont pas la meme attente, et proposer le
    second quand le premier suffit fait craindre des minutes pour rien.

    Le test porte sur le prefixe de signature (chemin, date, taille, format,
    definition) : ce qui suit ne distingue que la raison du transcodage et la
    page d'un PDF, deux choses qui ne changent pas le fait qu'une copie de
    lecture est disponible.
    """
    if not path:
        return False
    try:
        stat = os.stat(path)
    except OSError:
        return False
    prefix = "%s|%d:%d|%s|%s|" % (os.path.abspath(path), int(stat.st_mtime),
                                  stat.st_size, PROXY_FORMAT, MAX_EDGE)
    with _cache_lock:
        for entry in _read_index():
            if not str(entry.get("signature") or "").startswith(prefix):
                continue
            if os.path.isfile(os.path.join(cache_dir(), entry.get("file") or "")):
                return True
    return False


def register(info, produced, reason, forced=False):
    entry = {
        "file": os.path.basename(produced),
        "signature": _signature(info, forced),
        "source": os.path.abspath(info["path"]),
        "name": info["name"],
        "kind": info["kind"],
        "reason": reason,
        "forced": bool(forced),
        "created": time.time(),
    }
    with _cache_lock:
        entries = [item for item in _read_index() if item.get("file") != entry["file"]]
        entries.append(entry)
        _write_index(entries)
    return entry


def entries():
    """Contenu reel du cache, pour le panneau de gestion.

    On repart du disque, pas du journal : un fichier supprime a la main hors de
    l'application ne doit pas continuer a compter dans le total affiche.
    """
    directory = paths.proxy_cache_dir()
    if not os.path.isdir(directory):
        return []

    with _cache_lock:
        known = {item.get("file"): item for item in _read_index()}
        listing = []
        for name in sorted(os.listdir(directory)):
            if name == INDEX_NAME or name.endswith(".tmp"):
                continue
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            item = known.get(name) or {}
            listing.append({
                "file": name,
                "size": _file_size(path),
                "name": item.get("name") or name,
                "source": item.get("source"),
                "kind": item.get("kind"),
                "reason": item.get("reason") or "",
                "forced": bool(item.get("forced")),
                "created": item.get("created") or os.path.getmtime(path),
            })
    listing.sort(key=lambda item: item["created"], reverse=True)
    return listing


def forget(file_name):
    """Retire une entree du journal sans toucher au fichier."""
    if not file_name:
        return
    with _cache_lock:
        entries_left = [item for item in _read_index() if item.get("file") != file_name]
        _write_index(entries_left)


def remove(file_name):
    """Supprime un proxy precis. Retourne True s'il a disparu.

    Refuse tout nom qui sortirait du dossier de cache : ces noms viennent
    d'une requete HTTP, et rien n'empeche d'y glisser « ../ » pour viser un
    fichier de l'utilisateur.ice.
    """
    directory = cache_dir()
    target = os.path.abspath(os.path.join(directory, file_name))
    if os.path.dirname(target) != os.path.abspath(directory) or os.path.basename(target) == INDEX_NAME:
        raise ProxyError("Nom de fichier invalide.")
    try:
        if os.path.isfile(target):
            os.remove(target)
    except OSError as exc:
        raise ProxyError("Suppression impossible : %s" % exc)
    forget(os.path.basename(target))
    return not os.path.exists(target)


def purge():
    """Vide tout le cache. Retourne (supprimes, restants).

    Declenche uniquement a la demande, depuis le panneau de gestion. Un
    fichier encore ouvert par le lecteur d'un navigateur resiste sous
    Windows -- on l'ignore et il repartira au prochain passage, plutot que de
    faire echouer l'operation entiere.
    """
    directory = paths.proxy_cache_dir()
    removed, kept = 0, 0
    with _cache_lock:
        if not os.path.isdir(directory):
            return removed, kept
        for entry in os.listdir(directory):
            if entry == INDEX_NAME:
                continue
            target = os.path.join(directory, entry)
            try:
                if os.path.isfile(target):
                    os.remove(target)
                else:
                    shutil.rmtree(target)
                removed += 1
            except OSError:
                kept += 1
        _write_index([])
    return removed, kept


def total_size():
    return sum(item["size"] for item in entries())


# --------------------------------------------------------------------------
# Transcodage
# --------------------------------------------------------------------------

_PROGRESS = re.compile(r"^(out_time_ms|out_time_us|progress)=(.*)$")


def build(info, dimensions, progress=None, cancelled=None):
    """Fabrique le proxy et retourne son chemin.

    `progress(percent)` est rappele pendant l'encodage, `cancelled()` permet de
    l'interrompre (changement de media avant la fin, par exemple).
    """
    kind = info["kind"]
    extension = {"video": _video_format()["extension"],
                 "audio": ".ogg" if PROXY_FORMAT == "vp9" else ".m4a",
                 "image": ".png"}[kind]
    destination = os.path.join(cache_dir(), "proxy_%s%s" % (uuid.uuid4().hex[:10], extension))

    # Une page de PDF ne passe pas par FFmpeg : elle se rasterise directement.
    # Le reste du cycle est inchange -- meme dossier de cache, meme journal,
    # meme panneau de gestion -- ce qui rend la page supprimable comme un rush.
    if info.get("pdfPageCount"):
        try:
            pdfdoc.render(info["path"], info["pdfPage"],
                          info.get("pdfEdge") or pdfdoc.MIN_EDGE, destination)
        except pdfdoc.PdfError as exc:
            _forget(destination)
            raise ProxyError(str(exc))
        if progress is not None:
            progress(100.0)
        return destination

    cmd = _command(info, dimensions, destination)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                **_no_window())
    except FileNotFoundError:
        raise ProxyError("FFmpeg est introuvable. Installez-le et ajoutez-le au PATH.")

    errors = []
    watcher = threading.Thread(target=_drain, args=(proc.stderr, errors), daemon=True)
    watcher.start()

    duration = max(info.get("duration") or 0.0, 0.001)
    try:
        for raw in iter(proc.stdout.readline, b""):
            if cancelled is not None and cancelled():
                proc.kill()
                proc.wait()
                _forget(destination)
                raise ProxyError("Preparation annulee.")
            match = _PROGRESS.match(raw.decode("utf-8", "replace").strip())
            if not match or progress is None:
                continue
            key, value = match.group(1), match.group(2)
            if key == "progress" and value == "end":
                progress(100.0)
            elif key in ("out_time_ms", "out_time_us"):
                seconds = _int(value) / 1e6
                progress(max(0.0, min(99.0, seconds / duration * 100.0)))
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass

    code = proc.wait()
    watcher.join(timeout=1.0)
    if code != 0:
        _forget(destination)
        raise ProxyError("Transcodage impossible : %s" % " / ".join(errors[-3:]))
    if not os.path.exists(destination) or os.path.getsize(destination) == 0:
        _forget(destination)
        raise ProxyError("Le transcodage n'a produit aucun fichier.")
    if progress is not None:
        progress(100.0)
    return destination


def _command(info, dimensions, destination):
    ffmpeg = renderer.FFMPEG
    source = info["path"]
    kind = info["kind"]

    if kind == "image":
        return [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-progress", "pipe:1", "-nostdin",
                "-i", source, "-frames:v", "1",
                "-vf", "scale=%d:%d:flags=lanczos" % dimensions if dimensions[0] else "null",
                destination]

    if kind == "audio":
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-progress", "pipe:1", "-nostdin", "-i", source, "-vn"]
        if PROXY_FORMAT == "vp9":
            cmd += ["-c:a", "libopus", "-b:a", "160k"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]
        cmd.append(destination)
        return cmd

    fps = info.get("fps") or 25.0
    # GOP d'une seconde : le scrubbing sur la tablette repositionne l'image
    # presque instantanement, ce qui compte bien plus ici que la taille du
    # fichier -- un GOP long oblige le decodeur a remonter jusqu'a 10 s en
    # arriere a chaque deplacement du curseur.
    gop = max(1, int(round(fps)))
    recipe = _video_format()

    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-progress", "pipe:1", "-nostdin",
           "-i", source, "-map", "0:v:0"]
    cmd += recipe["video"]
    cmd += ["-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0"]
    if dimensions[0]:
        cmd += ["-vf", "scale=%d:%d:flags=bicubic" % dimensions]
    if info.get("hasAudio"):
        cmd += ["-map", "0:a:0?"] + recipe["audio"]
    else:
        cmd += ["-an"]
    cmd += recipe["flags"]
    cmd.append(destination)
    return cmd


def _drain(pipe, sink):
    try:
        for line in iter(pipe.readline, b""):
            sink.append(line.decode("utf-8", "replace").rstrip())
            del sink[:-20]
    finally:
        pipe.close()


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------

def _no_window():
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _forget(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _bitrate(fmt, video, path):
    """Debit global du fichier, en bits par seconde.

    Le debit du conteneur est celui qui compte : c'est lui qui transite sur le
    reseau, pistes audio comprises. Quand il est absent -- courant sur du
    ProRes ou un MXF, ou l'entete ne le declare pas -- on le reconstitue depuis
    la taille et la duree, ce qui reste juste a quelques pourcents.
    """
    declared = _int(fmt.get("bit_rate")) or _int((video or {}).get("bit_rate"))
    if declared:
        return declared
    duration = _float(fmt.get("duration"))
    size = _int(fmt.get("size")) or _file_size(path)
    return int(size * 8 / duration) if duration > 0 and size else 0


def _audio_bits(stream):
    """Definition d'une piste son, en bits par echantillon. 0 si indecidable.

    `bits_per_raw_sample` est renseigne par les formats qui ont une definition
    reelle -- PCM, FLAC, ALAC. Un codec avec perte n'en a pas : il se decode en
    flottant, et sa « definition » est une question sans objet. On retombe alors
    sur le format d'echantillon du decodeur, ce qui suffit a distinguer ce qui
    merite 24 bits de ce qui n'a rien a y gagner.
    """
    if not stream:
        return 0
    declared = _int(stream.get("bits_per_raw_sample"))
    if declared:
        return declared
    sample_fmt = (stream.get("sample_fmt") or "").rstrip("p")
    return {"u8": 8, "s16": 16, "s32": 32, "s64": 64}.get(sample_fmt, 0)


def _fps(stream):
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key) or ""
        if "/" in raw:
            num, den = raw.split("/", 1)
            num, den = _float(num), _float(den)
            if den:
                return num / den
    return 0.0


