"""Verification des trois couches d'export.

Lancer : py test_export.py

Ce que ce banc protege tient en une phrase : **les couches doivent se
rempiler**. Un export pro n'a d'interet que si reposer le trace sur le media,
dans un montage, redonne exactement l'apercu -- meme cadre, meme cadence, meme
premiere image. Une couche decalee d'une image, un media cadre autrement que ce
que montrait le canevas, un fond aplati d'une teinte a cote, et le travail est
a refaire a la main.

Le banc encode donc de vrais fichiers avec FFmpeg, les redecode, et recompose
lui-meme le trace sur le media pour le comparer a l'apercu. Rien n'est simule :
c'est le meme chemin que celui de l'application.

La tolerance de comparaison n'est pas du confort : l'apercu est un H.264, qui
n'est pas conservatif. On verifie que l'ecart reste celui d'une compression --
quelques unites en moyenne -- et non celui d'un decalage, qui se verrait
immediatement sur toute la surface.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import numpy as np

import app as application
import renderer

TMP = tempfile.mkdtemp(prefix="live_notes_export_")

WIDTH, HEIGHT, FPS = 640, 360, 25
IN_POINT, OUT_POINT = 1.0, 3.0
BACKGROUND = "#808080"

# Duree du trace : 1.6 s de points, plus la seconde de queue de `renderer`.
STROKE_MS = 1600.0
TRACE_FRAMES = int((STROKE_MS + renderer.TAIL_MS) * FPS / 1000.0)
MEDIA_FRAMES = int((OUT_POINT - IN_POINT) * FPS)


# ------------------------------------------------------------------ materiel

def make_rush(name, size, duration=4.0, audio=True):
    """Un vrai rush, au format qu'on veut, avec ou sans son."""
    path = os.path.join(TMP, name)
    cmd = [renderer.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc=size=%s:rate=25:duration=%s" % (size, duration)]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=%s" % duration,
                "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", path]
    subprocess.run(cmd, check=True)
    return path


def make_pulse(name, duration=4.0):
    """Un rush ou chaque image est un aplat different.

    Instrument de mesure, pas rush realiste : sur un aplat, le H.264 ne perd
    rien, et deux images voisines different de 37 unites. Comparer l'apercu a
    la pile « media + trace » y devient sans appel -- un decalage d'une seule
    image se voit d'un facteur trente sur l'ecart moyen, la ou une mire ordinaire
    laisserait la question ouverte.
    """
    path = os.path.join(TMP, name)
    subprocess.run([
        renderer.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=360x640:r=25:d=%s" % duration,
        "-vf", "geq=r='mod(N*37\\,256)':g='mod(N*37+85\\,256)':b='mod(N*37+170\\,256)'",
        "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p", path], check=True)
    return path


def make_sound(name, codec, rate=48000, duration=4.0):
    """Une bande son seule : pas de flux video du tout."""
    path = os.path.join(TMP, name)
    subprocess.run([
        renderer.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=%s:sample_rate=%d" % (duration, rate),
        "-c:a", codec, path], check=True)
    return path


def payload(**overrides):
    """Charge utile d'export, telle que la page l'envoie."""
    points = [{"x": 60 + index * 6, "y": 180, "t": index * 40.0, "p": 0.7}
              for index in range(40)]
    brush = {"shape": "round", "size": 20, "color": "#ff2020", "opacity": 1.0,
             "flow": 1.0, "hardness": 0.8, "spacing": 0.1}
    base = {
        "width": WIDTH, "height": HEIGHT, "outWidth": WIDTH, "outHeight": HEIGHT,
        "fps": FPS, "background": BACKGROUND, "durationMs": STROKE_MS,
        "strokes": [{"brush": brush, "seed": 1, "points": points}], "clears": [],
        "mediaFit": {"mode": "contain", "zoom": 1.0, "posX": 0.5, "posY": 0.5},
        "mediaIn": IN_POINT, "mediaOut": OUT_POINT,
    }
    base.update(overrides)
    return base


def media(path, kind="video", fit=None, has_audio=True, duration=4.0):
    return {"path": path, "kind": kind, "duration": duration,
            "in": IN_POINT, "out": OUT_POINT, "hasAudio": has_audio,
            "fit": fit or {"mode": "contain", "zoom": 1.0, "posX": 0.5, "posY": 0.5}}


# ------------------------------------------------------------------- lecture

def probe(path):
    raw = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,codec_name,width,height,nb_frames,pix_fmt"
        ":stream_tags=timecode", "-of", "json", path])
    streams = json.loads(raw)["streams"]
    return {stream["codec_type"]: stream for stream in streams}


def read(path):
    """Toutes les images d'un fichier, en RGBA."""
    raw = subprocess.check_output([
        renderer.FFMPEG, "-v", "error", "-i", path,
        "-f", "rawvideo", "-pix_fmt", "rgba", "-"])
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, HEIGHT, WIDTH, 4).astype(np.float32)


def render(name, **overrides):
    path = os.path.join(TMP, name)
    renderer.render(payload(**overrides), path)
    return path


# --------------------------------------------------------------------- tests

def test_the_stroke_alone_keeps_its_alpha():
    """Sans rien dessous, le trace sort sur du transparent."""
    frames = read(render("alpha.mov", codec="prores4444", flatten="alpha",
                         media=media(RUSH)))
    assert frames[0, 5, 5, 3] == 0, frames[0, 5, 5]
    assert frames[-1, ..., 3].max() > 200, frames[-1, ..., 3].max()
    return "coin transparent, trace opaque"


def test_a_solid_background_ignores_the_media():
    """« Fond uni » aplatit sur la couleur du canevas, media ouvert ou non."""
    frames = read(render("uni.mov", codec="prores4444", flatten="solid",
                         media=media(RUSH)))
    expected = int(renderer.background_bytes(BACKGROUND)[0])
    corner = frames[0, 5, 5]
    assert abs(corner[0] - expected) <= 1, (corner, expected)
    assert corner[3] == 255, corner
    return "#808080 -> %d partout, media ecarte" % expected


def test_the_media_is_flattened_under_the_stroke():
    """Le media entre reellement dans l'image, et le fond garde sa couleur."""
    frames = read(render("apercu.mp4", codec="h264", flatten="media",
                         media=media(RUSH)))
    expected = int(renderer.background_bytes(BACKGROUND)[0])
    # Rush vertical dans un canevas horizontal : les cotes sont le fond, le
    # centre est le media.
    side = frames[0, HEIGHT // 2, 3]
    middle = frames[0, HEIGHT // 2, WIDTH // 2]
    assert abs(side[0] - expected) <= 2, (side, expected)
    assert abs(int(middle[0]) - int(middle[2])) > 20, middle
    return "cotes a %d, media au centre" % expected


def test_a_missing_media_falls_back_to_the_solid_background():
    """Media absent : on aplatit sur le fond plutot que de sortir du noir."""
    assert renderer.flatten_mode({"flatten": "media"}, None) == "solid"
    frames = read(render("sans_media.mov", codec="prores4444", flatten="media"))
    expected = int(renderer.background_bytes(BACKGROUND)[0])
    assert abs(frames[0, 5, 5, 0] - expected) <= 1, frames[0, 5, 5]
    return "repli sur le fond uni"


def test_h264_never_carries_an_alpha_channel():
    assert renderer.flatten_mode({"flatten": "alpha", "codec": "h264"}, None) == "solid"
    info = probe(render("h264_alpha.mp4", codec="h264", flatten="alpha", media=media(RUSH)))
    assert info["video"]["pix_fmt"] == "yuv420p", info["video"]
    return "demande d'alpha en H.264 -> fond uni"


def test_contain_shows_the_whole_media():
    """« Ratio d'origine » : le rush vertical entre entier, fond sur les cotes."""
    path = os.path.join(TMP, "contain.mov")
    renderer.render_media(payload(media=media(RUSH)), path)
    frames = read(path)
    expected = int(renderer.background_bytes(BACKGROUND)[0])
    assert abs(frames[0, HEIGHT // 2, 3, 0] - expected) <= 1, frames[0, HEIGHT // 2, 3]
    # Le media occupe 360*(360/640) = 202 px de large, centre.
    assert frames[0, HEIGHT // 2, WIDTH // 2, 3] == 255
    return "media entier, %d px de fond de chaque cote" % ((WIDTH - 203) // 2)


def test_crop_fills_the_frame():
    """« Recadrer » : plus un pixel de fond, le media couvre tout le canevas."""
    path = os.path.join(TMP, "crop.mov")
    renderer.render_media(
        payload(media=media(RUSH, fit={"mode": "crop", "zoom": 1.0,
                                       "posX": 0.5, "posY": 0.5})), path)
    frames = read(path)
    expected = int(renderer.background_bytes(BACKGROUND)[0])
    corners = [frames[0, 3, 3], frames[0, 3, -3], frames[0, -3, 3], frames[0, -3, -3]]
    assert all(abs(corner[0] - expected) > 3 or abs(corner[2] - expected) > 3
               for corner in corners), corners
    return "4 coins occupes par le media"


def test_the_media_layer_can_keep_an_empty_alpha_around_it():
    """Le vide autour du media plutot que le fond : la couche part au montage."""
    path = os.path.join(TMP, "media_alpha.mov")
    renderer.render_media(payload(media=media(RUSH), mediaAlpha=True), path)
    frames = read(path)
    assert frames[0, HEIGHT // 2, 3, 3] == 0, frames[0, HEIGHT // 2, 3]
    assert frames[0, HEIGHT // 2, WIDTH // 2, 3] == 255, frames[0, HEIGHT // 2, WIDTH // 2]
    return "cotes transparents, media opaque"


def test_the_media_layer_covers_the_whole_time_the_rush_was_on_screen():
    """La couche media dure aussi longtemps que le rush etait affiche.

    Le trace dure 2.6 s ici, la portion jouee 2 s : le rush est reste a
    l'ecran tout du long, les 0.6 s excedentaires sur une image arretee. La
    couche les emporte, sans quoi le montage perdrait la fin du geste des
    qu'on le repose sur elle.
    """
    path = os.path.join(TMP, "bornes.mov")
    renderer.render_media(payload(media=media(RUSH)), path)
    frames = read(path)
    assert len(frames) == TRACE_FRAMES, len(frames)
    # Les images qui suivent le point OUT sont la derniere, clonee.
    assert np.abs(frames[-1] - frames[MEDIA_FRAMES - 1]).max() == 0, "gel non identique"
    assert np.abs(frames[MEDIA_FRAMES - 1] - frames[MEDIA_FRAMES - 2]).max() > 0, "deja fige"
    return "%d images jouees + %d figees" % (MEDIA_FRAMES, TRACE_FRAMES - MEDIA_FRAMES)


def test_the_in_out_span_is_a_floor_not_a_ceiling():
    """Un trace arrete avant le point OUT ne raccourcit pas la couche.

    Ce qui a ete joue reste disponible au montage : c'est le rush, pas le
    geste, qui decide de la longueur minimale de sa propre couche.
    """
    path = os.path.join(TMP, "trace_court.mov")
    # Un trace de 0.2 s + la queue d'une seconde, sous 2 s de rush.
    short = payload(media=media(RUSH), durationMs=200.0)
    short["strokes"][0]["points"] = short["strokes"][0]["points"][:5]
    renderer.render_media(short, path)
    assert len(read(path)) == MEDIA_FRAMES, len(read(path))
    return "%d images, la mesure des bornes" % MEDIA_FRAMES


def test_a_longer_stroke_freezes_the_media_instead_of_going_black():
    """L'apercu ne tombe pas dans le noir quand le media est epuise."""
    frames = read(render("gel.mp4", codec="h264", flatten="media", media=media(RUSH)))
    # Hors de la zone du trace, la derniere image doit ressembler a la derniere
    # image du media, pas a du noir.
    late = frames[-1, 40, WIDTH // 2, :3]
    frozen = frames[MEDIA_FRAMES - 1, 40, WIDTH // 2, :3]
    assert np.abs(late - frozen).max() < 24, (late, frozen)
    assert late.max() > 30, late
    return "derniere image figee, ecart %.0f" % np.abs(late - frozen).max()


def test_every_layer_starts_on_the_in_point_timecode():
    """Le point IN est la reference de synchronisation, donc le timecode."""
    assert renderer.timecode(12 + 4 / 25.0, 25) == "00:00:12:04"
    stamps = set()
    for name, kind in (("tc_trace.mov", "trace"), ("tc_apercu.mp4", "apercu")):
        codec = "h264" if name.endswith(".mp4") else "prores4444"
        flatten = "media" if kind == "apercu" else "alpha"
        stamps.add(probe(render(name, codec=codec, flatten=flatten,
                                media=media(RUSH)))["video"]["tags"]["timecode"])
    path = os.path.join(TMP, "tc_media.mov")
    renderer.render_media(payload(media=media(RUSH)), path)
    stamps.add(probe(path)["video"]["tags"]["timecode"])
    assert stamps == {"00:00:01:00"}, stamps
    return "trois couches a %s" % stamps.pop()


def test_the_layers_stack_back_into_the_preview():
    """La promesse de l'export pro : media + trace = apercu.

    On recompose ici ce que fera le montage -- le trace en alpha droit pose sur
    la couche media -- et on le compare a l'apercu livre.
    """
    apercu = read(render("pile_apercu.mp4", codec="h264", flatten="media",
                         media=media(PULSE)))
    trace = read(render("pile_trace.mov", codec="prores4444", flatten="alpha",
                        media=media(PULSE)))
    couche = os.path.join(TMP, "pile_media.mov")
    renderer.render_media(payload(media=media(PULSE)), couche)
    fond = read(couche)

    count = min(len(fond), len(trace))
    alpha = trace[:count, ..., 3:4] / 255.0
    stacked = fond[:count, ..., :3] * (1.0 - alpha) + trace[:count, ..., :3] * alpha
    error = np.abs(stacked - apercu[:count, ..., :3]).mean()
    assert error < 2.0, error
    return "ecart moyen %.2f / 255 sur %d images" % (error, count)


def test_the_layers_are_aligned_to_the_frame():
    """Le meme empilement, decale d'une image, doit etre franchement pire.

    C'est la verification qui compte vraiment : un export dont les couches se
    ressemblent mais glissent d'une image serait invisible a l'oeil sur une
    image fixe, et faux partout ailleurs.
    """
    apercu = read(render("cale_apercu.mp4", codec="h264", flatten="media",
                         media=media(PULSE)))
    trace = read(render("cale_trace.mov", codec="prores4444", flatten="alpha",
                        media=media(PULSE)))
    couche = os.path.join(TMP, "cale_media.mov")
    renderer.render_media(payload(media=media(PULSE)), couche)
    fond = read(couche)

    count = min(len(fond), len(trace)) - 1
    alpha = trace[:count, ..., 3:4] / 255.0
    stacked = fond[:count, ..., :3] * (1.0 - alpha) + trace[:count, ..., :3] * alpha
    aligned = np.abs(stacked - apercu[:count, ..., :3]).mean()
    shifted = np.abs(stacked - apercu[1:count + 1, ..., :3]).mean()
    assert shifted > aligned * 5, (aligned, shifted)
    return "cale %.2f contre %.2f decale d'une image" % (aligned, shifted)


def test_the_preview_does_not_start_two_frames_early():
    """L'apercu ne doit pas porter de retard de reordonnancement.

    x264 pose deux images B par defaut : les premiers paquets sortent alors
    avec un DTS negatif, et le MP4 rattrape par une liste d'edition. Les
    lecteurs qui l'honorent affichent la bonne duree ; beaucoup de logiciels de
    montage l'ignorent et demarrent a la premiere image decodee. L'apercu
    arrivait alors deux images avant les couches ProRes -- qui n'ont pas
    d'images B -- et debutait par des images de pre-roll.

    Ce qui se verifie ici est donc la structure temporelle du fichier, pas son
    contenu : aucun DTS negatif, et le meme nombre d'images qu'on honore ou non
    la liste d'edition.
    """
    path = render("preroll.mp4", codec="h264", flatten="media", media=media(PULSE))

    raw = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=dts_time", "-read_intervals", "%+#4",
        "-of", "csv=p=0", path]).decode()
    premiers = [float(value) for value in raw.split() if value]
    assert min(premiers) >= 0.0, "DTS negatifs : %s" % premiers

    def images(ignore_editlist):
        cmd = [renderer.FFMPEG, "-v", "error"]
        if ignore_editlist:
            cmd += ["-ignore_editlist", "1"]
        cmd += ["-i", path, "-f", "rawvideo", "-pix_fmt", "rgba", "-"]
        blob = subprocess.check_output(cmd)
        return len(blob) // (WIDTH * HEIGHT * 4)

    honoree, ignoree = images(False), images(True)
    assert honoree == ignoree, \
        "%d images liste d'edition honoree, %d ignoree : le montage verra un decalage" % (
            honoree, ignoree)
    return "%d images, DTS des zero, aucun pre-roll" % honoree


def test_the_layers_align_even_without_the_edit_list():
    """Les trois couches se superposent aussi pour qui ignore la liste d'edition.

    C'est la situation reelle du montage : on importe les trois fichiers, et
    ils doivent tomber sur la meme image sans recalage.
    """
    def brut(path):
        blob = subprocess.check_output([
            renderer.FFMPEG, "-v", "error", "-ignore_editlist", "1", "-i", path,
            "-f", "rawvideo", "-pix_fmt", "rgba", "-"])
        return np.frombuffer(blob, dtype=np.uint8).reshape(
            -1, HEIGHT, WIDTH, 4).astype(np.float32)

    apercu = brut(render("brut_apercu.mp4", codec="h264", flatten="media",
                         media=media(PULSE)))
    trace = brut(render("brut_trace.mov", codec="prores4444", flatten="alpha",
                        media=media(PULSE)))
    couche = os.path.join(TMP, "brut_media.mov")
    renderer.render_media(payload(media=media(PULSE)), couche)
    fond = brut(couche)

    count = min(len(fond), len(trace), len(apercu)) - 1
    alpha = trace[:count, ..., 3:4] / 255.0
    stacked = fond[:count, ..., :3] * (1.0 - alpha) + trace[:count, ..., :3] * alpha
    aligned = np.abs(stacked - apercu[:count, ..., :3]).mean()
    shifted = np.abs(stacked - apercu[1:count + 1, ..., :3]).mean()
    assert shifted > aligned * 5, \
        "cale %.2f contre %.2f decale : les couches ne tombent pas en face" % (aligned, shifted)
    return "cale %.2f contre %.2f decale d'une image" % (aligned, shifted)


def test_the_media_layer_keeps_the_soundtrack():
    path = os.path.join(TMP, "son.mov")
    renderer.render_media(payload(media=media(RUSH)), path)
    assert "audio" in probe(path), probe(path).keys()
    silent = os.path.join(TMP, "muet.mov")
    renderer.render_media(payload(media=media(MUTE, has_audio=False)), silent)
    assert "audio" not in probe(silent), probe(silent).keys()
    return "piste reprise quand elle existe, absente sinon"


# ------------------------------------------------------------- media sans image

def sound(path, codec="pcm_s24le", rate=48000, bits=24):
    return {"path": path, "kind": "audio", "duration": 4.0,
            "in": IN_POINT, "out": OUT_POINT, "hasAudio": True,
            "audioCodec": codec, "audioRate": rate, "audioBits": bits}


def audio_stream(path):
    raw = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
        "stream=codec_name,sample_rate,duration:format_tags=time_reference",
        "-of", "json", path])
    data = json.loads(raw)
    stream = dict(data["streams"][0])
    stream["time_reference"] = (data.get("format", {}).get("tags") or {}).get("time_reference")
    return stream


def test_a_soundtrack_alone_becomes_a_wav_layer():
    """Un son n'a rien a montrer, mais tout a synchroniser : il fait une couche.

    Le WAV livre garde le format PCM de la source -- un reencodage identique,
    donc bit a bit la meme chose -- et il est coupe exactement aux bornes.
    """
    path = os.path.join(TMP, "couche_son.wav")
    renderer.render_media(payload(media=sound(WAV24)), path)
    info = audio_stream(path)
    assert info["codec_name"] == "pcm_s24le", info
    assert abs(float(info["duration"]) - (OUT_POINT - IN_POINT)) < 0.001, info
    return "pcm_s24le, %.3f s aux bornes" % float(info["duration"])


def test_the_wav_layer_carries_the_in_point_as_a_bext_reference():
    """Un WAV n'a pas de piste de timecode ; le `bext` en tient lieu.

    `time_reference` est un nombre d'echantillons : le point IN y devient la
    meme information que le timecode des couches video.
    """
    path = os.path.join(TMP, "bext.wav")
    renderer.render_media(payload(media=sound(WAV24)), path)
    info = audio_stream(path)
    assert int(info["time_reference"]) == int(IN_POINT * 48000), info
    return "time_reference = %s echantillons" % info["time_reference"]


def test_a_lossy_soundtrack_falls_back_to_sixteen_bits():
    """Un codec avec perte n'a pas de definition propre : lui en inventer une
    ne restituerait rien."""
    assert renderer.wav_codec({"audioCodec": "aac", "audioBits": 0}) == "pcm_s16le"
    assert renderer.wav_codec({"audioCodec": "flac", "audioBits": 24}) == "pcm_s24le"
    assert renderer.wav_codec({"audioCodec": "pcm_f32le", "audioBits": 32}) == "pcm_f32le"
    path = os.path.join(TMP, "compresse.wav")
    renderer.render_media(payload(media=sound(AAC, codec="aac", rate=44100, bits=0)), path)
    info = audio_stream(path)
    assert info["codec_name"] == "pcm_s16le", info
    assert int(info["time_reference"]) == int(IN_POINT * 44100), info
    return "aac -> pcm_s16le, reference a 44100 Hz"


def test_a_sound_only_media_keeps_the_stroke_on_the_solid_background():
    """Rien ne passe sous le trace, mais l'apercu emporte la bande son."""
    path = render("apercu_son.mp4", codec="h264", flatten="media", media=sound(WAV24))
    info = probe(path)
    expected = int(renderer.background_bytes(BACKGROUND)[0])
    frames = read(path)
    assert abs(frames[0, 5, 5, 0] - expected) <= 2, frames[0, 5, 5]
    assert "audio" in info, info.keys()
    assert info["video"]["tags"]["timecode"] == "00:00:01:00", info["video"]
    return "fond uni + piste son, timecode du point IN"


def test_a_sound_only_media_still_yields_an_alpha_stroke():
    """La couche trace ne change pas de nature : le son ne la concerne pas."""
    info = probe(render("trace_son.mov", codec="prores4444", flatten="alpha",
                        media=sound(WAV24)))
    assert info["video"]["pix_fmt"].startswith("yuva"), info["video"]
    assert "audio" not in info, info.keys()
    return "ProRes 4444 + alpha, sans piste son"


# --------------------------------------------------- orchestration de l'export

def client():
    return application.app.test_client()


def post(route, body):
    return client().post(route, json=body, environ_base={"REMOTE_ADDR": "127.0.0.1"})


def run_job(body):
    """Lance un export par l'API et attend sa fin."""
    import time
    started = post("/api/export", body).get_json()
    if "jobId" not in started:
        return started
    while True:
        state = client().get("/api/export/" + started["jobId"],
                             environ_base={"REMOTE_ADDR": "127.0.0.1"}).get_json()
        if state["state"] != "running":
            return state
        time.sleep(0.1)


def test_the_pro_export_lays_three_layers_in_one_folder():
    directory = os.path.join(TMP, "livraison")
    post("/api/media/open", {"path": RUSH})
    state = run_job(payload(mode="pro", folder="rush.mov", directory=directory))
    assert state["state"] == "done", state
    names = sorted(os.path.basename(path) for path in state["files"])
    assert names == ["rush_media.mov", "rush_preview.mp4", "rush_trace.mov"], names
    assert os.path.basename(state["filepath"]) == "rush", state["filepath"]
    return "dossier « rush » : " + ", ".join(names)


def test_the_default_folder_carries_the_rush_name_and_the_hour():
    """Sans nom impose : le rush pour savoir de quoi il s'agit, l'heure pour
    savoir de quel essai."""
    directory = os.path.join(TMP, "horodate")
    post("/api/media/open", {"path": RUSH})
    state = run_job(payload(mode="pro", directory=directory))
    assert state["state"] == "done", state
    name = os.path.basename(state["filepath"])
    assert re.match(r"\Arush_\d{8}_\d{6}\Z", name), name
    assert sorted(os.path.basename(path) for path in state["files"]) == [
        name + "_media.mov", name + "_preview.mp4", name + "_trace.mov"]
    return "dossier « %s », fichiers horodates avec lui" % name


def test_the_pro_export_of_a_soundtrack_lays_a_wav_layer():
    """Meme plan a trois couches, la couche media devenant un WAV."""
    directory = os.path.join(TMP, "livraison_son")
    post("/api/media/open", {"path": WAV24})
    state = run_job(payload(mode="pro", folder="ambiance", directory=directory))
    assert state["state"] == "done", state
    names = sorted(os.path.basename(path) for path in state["files"])
    assert names == ["ambiance_media.wav", "ambiance_preview.mp4",
                     "ambiance_trace.mov"], names
    layer = os.path.join(directory, "ambiance", "ambiance_media.wav")
    assert abs(float(audio_stream(layer)["duration"]) - (OUT_POINT - IN_POINT)) < 0.001
    return "dossier « ambiance » : " + ", ".join(names)


def test_the_pro_export_refuses_without_a_media():
    application.SESSION.clear_media()
    answer = post("/api/export", payload(mode="pro", directory=os.path.join(TMP, "vide")))
    assert answer.status_code == 400, answer.status_code
    assert "média" in answer.get_json()["error"], answer.get_json()
    return "refus explique plutot qu'un dossier a deux couches"


def test_a_second_pro_export_does_not_overwrite_the_first():
    directory = os.path.join(TMP, "deux_fois")
    post("/api/media/open", {"path": RUSH})
    first = run_job(payload(mode="pro", folder="rush", directory=directory))
    second = run_job(payload(mode="pro", folder="rush", directory=directory))
    assert first["state"] == "done" and second["state"] == "done", (first, second)
    assert os.path.basename(second["filepath"]) == "rush_2", second["filepath"]
    return "second export -> dossier « rush_2 »"


def test_the_export_renders_the_source_not_the_reading_copy():
    """La copie de lecture est une visee : l'export ne la regarde pas.

    Un rush illisible par un navigateur est servi par un proxy 1280 px. C'est
    pourtant la source qui doit etre encodee, sinon un tracage fait sur un
    proxy ressortirait a sa definition.
    """
    prores = os.path.join(TMP, "source_prores.mov")
    subprocess.run([renderer.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=25:duration=1",
                    "-c:v", "prores_ks", "-profile:v", "3", prores], check=True)
    answer = post("/api/media/open", {"path": prores}).get_json()["media"]
    assert answer["proxied"], answer
    chosen = application._export_media(payload())
    assert chosen["path"] == prores, chosen["path"]
    return "source ProRes retenue malgre le proxy"


def test_the_page_of_a_pdf_is_rendered_from_its_rasterisation():
    """Une page de PDF n'existe que rasterisee : c'est elle, le media."""
    import time

    from PIL import Image
    document = os.path.join(TMP, "doc.pdf")
    Image.new("RGB", (600, 850), (245, 245, 245)).save(document, "PDF")
    post("/api/media/open", {"path": document, "page": 1})
    # La rasterisation part dans un fil : on attend qu'elle soit publiee.
    for _ in range(100):
        answer = application.SESSION.snapshot()["media"]
        if answer["state"] in ("ready", "error"):
            break
        time.sleep(0.1)
    assert answer["state"] == "ready", answer
    chosen = application._export_media(payload())
    assert chosen["path"] == answer["servedPath"], chosen["path"]
    assert chosen["kind"] == "image", chosen["kind"]
    return "page rasterisee retenue, pas le PDF"


def test_an_image_media_lasts_as_long_as_the_stroke():
    """Une image n'a pas de bornes : elle tient toute la duree du trace."""
    still = os.path.join(TMP, "fixe.png")
    subprocess.run([renderer.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=640x360", "-frames:v", "1",
                    still], check=True)
    path = os.path.join(TMP, "image.mov")
    renderer.render_media(payload(media=media(still, kind="image", has_audio=False,
                                              duration=0.0)), path)
    assert len(read(path)) == TRACE_FRAMES, len(read(path))
    return "%d images, comme le trace" % TRACE_FRAMES


# ------------------------------------------------------------------ couches

def band(color, horizontal, when, seed=1):
    """Une bande franche, opaque, tracee d'un bout a l'autre du canevas.

    Elle sert de mesure : sur un aplat opaque, le pixel du croisement dit sans
    ambiguite laquelle des deux bandes est au-dessus.
    """
    if horizontal:
        points = [{"x": 40 + index * 18, "y": HEIGHT // 2, "t": when + index * 4.0, "p": 1.0}
                  for index in range(32)]
    else:
        points = [{"x": WIDTH // 2, "y": 30 + index * 10, "t": when + index * 4.0, "p": 1.0}
                  for index in range(32)]
    brush = {"shape": "round", "size": 26, "color": color, "opacity": 1.0,
             "flow": 1.0, "hardness": 1.0, "spacing": 0.08}
    return {"brush": brush, "seed": seed, "points": points}


# La rouge est tracee TARD, la bleue TOT. A plat, la rouge passerait donc
# par-dessus ; en couches, c'est la couche du dessus qui gagne, quel que soit
# le moment ou chacune a ete tracee.
RED_LATE = "#ff2020"
BLUE_EARLY = "#2040ff"


def crossing(frame):
    """La couleur au croisement des deux bandes, en RGB 0..255."""
    return frame[HEIGHT // 2, WIDTH // 2, :3]


def test_layers_stack_in_order_not_in_time():
    """Une trace du dessous posee APRES passe quand meme dessous.

    C'est la seule propriete qui distingue de vraies couches d'un empilement
    decoratif -- et celle qu'un rendu a plat, rejoue chronologiquement, rate
    exactement a l'envers. Le banc verifie les deux : le resultat en couches,
    et le fait que la meme chose a plat donne franchement l'inverse.
    """
    red = band(RED_LATE, True, 1200.0)          # couche du dessous, tracee tard
    blue = band(BLUE_EARLY, False, 0.0)         # couche du dessus, tracee tot

    layered = read(render("couches_ordre.mov", flatten="alpha", codec="prores4444",
                          layers=[{"strokes": [red], "clears": []},
                                  {"strokes": [blue], "clears": []}],
                          strokes=[], durationMs=1800.0))[-1]
    flat = read(render("couches_a_plat.mov", flatten="alpha", codec="prores4444",
                       strokes=[blue, red], clears=[], durationMs=1800.0))[-1]

    over = crossing(layered)
    under = crossing(flat)
    assert over[2] > over[0] + 40, \
        ("au croisement, la couche du dessus ne passe pas dessus : %s"
         % (over.astype(int).tolist(),))
    assert under[0] > under[2] + 40, \
        ("a plat, la trace posee en dernier devrait gagner : %s"
         % (under.astype(int).tolist(),))
    return ("croisement bleu en couches %s, rouge a plat %s"
            % (over.astype(int).tolist(), under.astype(int).tolist()))


def test_a_flat_payload_renders_exactly_as_one_layer():
    """La forme d'avant les couches doit rendre au bit pres comme avant.

    Un projet exporte par une version anterieure, ou un banc ecrit avant les
    couches, ne doit rien changer a ce qui sort du tuyau.
    """
    flat = read(render("compat_a_plat.mov", flatten="alpha", codec="prores4444"))
    one = read(render("compat_une_couche.mov", flatten="alpha", codec="prores4444",
                      layers=[{"strokes": payload()["strokes"], "clears": []}],
                      strokes=[]))
    assert flat.shape == one.shape, "nombre d'images different : %s vs %s" % (flat.shape, one.shape)
    ecart = float(np.abs(flat - one).max())
    assert ecart == 0.0, "ecart maximal de %.1f entre les deux formes" % ecart
    return "%d images identiques au bit pres" % flat.shape[0]


def test_a_clear_only_wipes_its_own_layer():
    """Un effacement remet a zero SA couche, pas la pile.

    Sans quoi effacer la couche 2 en cours d'enregistrement emporterait la
    couche 1 verrouillee -- exactement ce que les couches servent a eviter.
    """
    red = band(RED_LATE, True, 0.0)
    blue = band(BLUE_EARLY, False, 0.0)
    # La couche du dessous s'efface a mi-parcours, celle du dessus jamais.
    frames = read(render("couches_effacement.mov", flatten="alpha", codec="prores4444",
                         layers=[{"strokes": [red], "clears": [400.0]},
                                 {"strokes": [blue], "clears": []}],
                         strokes=[], durationMs=1200.0))
    last = frames[-1]
    # Loin du croisement : la bande rouge seule.
    red_alone = last[HEIGHT // 2, 80, 3]
    blue_alone = last[40, WIDTH // 2, 3]
    assert red_alone < 8, "la bande effacee est toujours la (alpha %.0f)" % red_alone
    assert blue_alone > 200, "la bande de l'autre couche a ete emportee (alpha %.0f)" % blue_alone
    return "couche du dessous effacee, couche du dessus intacte"


def test_an_empty_layer_changes_nothing():
    """Une couche sans trace -- jamais dessinee, ou masquee donc jamais envoyee --
    ne coute ni canevas ni pixel."""
    alone = read(render("couches_seule.mov", flatten="alpha", codec="prores4444",
                        layers=[{"strokes": payload()["strokes"], "clears": []}],
                        strokes=[]))
    padded = read(render("couches_vide.mov", flatten="alpha", codec="prores4444",
                         layers=[{"strokes": [], "clears": []},
                                 {"strokes": payload()["strokes"], "clears": []},
                                 {"strokes": [], "clears": []}],
                         strokes=[]))
    ecart = float(np.abs(alone - padded).max())
    assert ecart == 0.0, "ecart maximal de %.1f" % ecart
    return "deux couches vides autour : aucun effet"


TESTS = [
    test_layers_stack_in_order_not_in_time,
    test_a_flat_payload_renders_exactly_as_one_layer,
    test_a_clear_only_wipes_its_own_layer,
    test_an_empty_layer_changes_nothing,
    test_the_stroke_alone_keeps_its_alpha,
    test_a_solid_background_ignores_the_media,
    test_the_media_is_flattened_under_the_stroke,
    test_a_missing_media_falls_back_to_the_solid_background,
    test_h264_never_carries_an_alpha_channel,
    test_contain_shows_the_whole_media,
    test_crop_fills_the_frame,
    test_the_media_layer_can_keep_an_empty_alpha_around_it,
    test_the_media_layer_covers_the_whole_time_the_rush_was_on_screen,
    test_the_in_out_span_is_a_floor_not_a_ceiling,
    test_a_longer_stroke_freezes_the_media_instead_of_going_black,
    test_every_layer_starts_on_the_in_point_timecode,
    test_the_layers_stack_back_into_the_preview,
    test_the_layers_are_aligned_to_the_frame,
    test_the_preview_does_not_start_two_frames_early,
    test_the_layers_align_even_without_the_edit_list,
    test_the_media_layer_keeps_the_soundtrack,
    test_a_soundtrack_alone_becomes_a_wav_layer,
    test_the_wav_layer_carries_the_in_point_as_a_bext_reference,
    test_a_lossy_soundtrack_falls_back_to_sixteen_bits,
    test_a_sound_only_media_keeps_the_stroke_on_the_solid_background,
    test_a_sound_only_media_still_yields_an_alpha_stroke,
    test_the_pro_export_lays_three_layers_in_one_folder,
    test_the_default_folder_carries_the_rush_name_and_the_hour,
    test_the_pro_export_of_a_soundtrack_lays_a_wav_layer,
    test_the_pro_export_refuses_without_a_media,
    test_a_second_pro_export_does_not_overwrite_the_first,
    test_the_export_renders_the_source_not_the_reading_copy,
    test_the_page_of_a_pdf_is_rendered_from_its_rasterisation,
    test_an_image_media_lasts_as_long_as_the_stroke,
]


def main():
    print("Export multi-couches — %s" % TMP)
    if not shutil.which(renderer.FFMPEG) and not os.path.exists(renderer.FFMPEG):
        print("  FFmpeg introuvable : le banc ne peut rien verifier.")
        return 1

    global RUSH, MUTE, PULSE, WAV24, AAC
    # Rush vertical dans un canevas horizontal : le cas qui rend le cadrage
    # visible, et celui que l'export pro doit savoir livrer avec du vide autour.
    RUSH = make_rush("rush.mp4", "360x640")
    MUTE = make_rush("muet.mp4", "360x640", audio=False)
    PULSE = make_pulse("pulse.mp4")
    # Deux bandes son seules : une lossless 24 bits, une compressee — les deux
    # bouts de la regle de choix du format PCM livre.
    WAV24 = make_sound("ambiance.wav", "pcm_s24le")
    AAC = make_sound("ambiance.m4a", "aac", rate=44100)

    failures = 0
    for test in TESTS:
        try:
            detail = test()
            print("  OK   %-52s %s" % (test.__name__, detail or ""))
        except Exception as exc:  # noqa: BLE001 - rapport de test
            failures += 1
            print("  FAIL %-52s %s: %s" % (test.__name__, type(exc).__name__, exc))

    print("\n%d/%d tests passes" % (len(TESTS) - failures, len(TESTS)))
    if not failures:
        shutil.rmtree(TMP, ignore_errors=True)
    else:
        print("Fichiers conserves pour inspection : %s" % TMP)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
