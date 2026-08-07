"""Verification des proxys de lecture (mode tablette).

Lancer : py test_proxy.py   (ou pytest test_proxy.py)

Le banc encode de vrais fichiers -- ProRes 4444, DNxHD, H.264 en 4:2:2 10 bits,
H.264 a 30+ Mbps, HEVC, TIFF 16 bits, WAV PCM 24 bits -- puis verifie trois
choses, dans cet ordre d'importance :

1. ce qui n'est pas lisible par un navigateur le devient, quel que soit le mode ;
2. ce qui est lisible mais trop lourd pour un Wi-Fi n'est allege qu'en mode
   tablette -- le poste lit depuis son disque et n'a pas a payer ce prix ;
3. ce qui est deja bon n'est jamais retranscode, et un proxy deja fabrique est
   repris du cache.

Le point 1 se joue sur des pieges silencieux : un H.264 en 4:2:2 10 bits porte
un `codec_name` irreprochable et ne se lit nulle part sur le web. C'est
exactement le genre de fichier qui donne une image noire sans message d'erreur.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

import paths
import proxy
import renderer

TMP = tempfile.mkdtemp(prefix="live_notes_proxy_")

# Ce qu'un navigateur de tablette decode sans extension ni plugin.
BROWSER_VIDEO = {"h264", "vp8", "vp9", "av1"}
BROWSER_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}
BROWSER_IMAGE = {"mjpeg", "png", "gif", "webp"}


def ffmpeg(*args):
    subprocess.run([renderer.FFMPEG, "-hide_banner", "-loglevel", "error", "-y"] + list(args),
                   check=True, capture_output=True)


def probe(path):
    out = subprocess.run(
        [proxy._ffprobe(), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def stream(path, kind):
    for item in probe(path).get("streams") or []:
        if item["codec_type"] == kind:
            return item
    return None


# --------------------------------------------------------------------------
# Materiel de test
# --------------------------------------------------------------------------

def make(name, *args):
    path = os.path.join(TMP, name)
    if not os.path.exists(path):
        ffmpeg(*(list(args) + [path]))
    return path


def prores():
    """Le cas de reference : rush de montage, video ET audio illisibles."""
    return make("rush_prores.mov",
                "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=25:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-c:v", "prores_ks", "-profile:v", "4444",
                "-pix_fmt", "yuva444p10le", "-c:a", "pcm_s16le")


def dnxhd():
    return make("rush_dnxhd.mov",
                "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=25:duration=1",
                "-c:v", "dnxhd", "-b:v", "36M", "-pix_fmt", "yuv422p")


def hevc():
    return make("rush_hevc.mp4",
                "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=1",
                "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-x265-params", "log-level=error")


def hd_h264():
    return make("rush_hd.mp4",
                "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=1",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p")


def h264_422_10bit():
    """Le piege : codec_name = h264, et pourtant illisible partout sur le web."""
    return make("rush_422_10bit.mp4",
                "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=1",
                "-c:v", "libx264", "-profile:v", "high422", "-pix_fmt", "yuv422p10le")


def heavy_h264():
    """Lisible, mais au-dela de ce qu'un Wi-Fi encaisse confortablement."""
    return make("rush_lourd.mp4",
                "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=25:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-b:v", "60M", "-minrate", "60M", "-maxrate", "60M", "-bufsize", "60M")


def light_uhd_h264():
    """4K mais econome : la definition seule ne declenche plus rien."""
    return make("rush_uhd_leger.mp4",
                "-f", "lavfi", "-i", "testsrc2=size=3840x2160:rate=25:duration=1",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-b:v", "8M", "-maxrate", "8M", "-bufsize", "8M")


def tiff():
    return make("plan.tiff", "-f", "lavfi", "-i", "testsrc2=size=2000x1200",
                "-frames:v", "1", "-pix_fmt", "rgb48le")


def jpeg():
    return make("plan.jpg", "-f", "lavfi", "-i", "testsrc2=size=800x600", "-frames:v", "1")


def wav():
    return make("voix.wav", "-f", "lavfi", "-i", "sine=frequency=330:duration=1",
                "-c:a", "pcm_s24le")


def ac3():
    """Video parfaitement lisible, bande-son que le web ne decode pas."""
    return make("rush_ac3.mkv", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=1",
                "-f", "lavfi", "-i", "sine=frequency=330:duration=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "ac3")


def prepare(path, for_tablet=False, force=False, bypass=False):
    """Chemin complet : analyse, decision, transcodage si necessaire."""
    info = proxy.probe(path)
    needed, reason, dimensions = proxy.plan(info, for_tablet=for_tablet,
                                            force=force, bypass=bypass)
    produced = proxy.build(info, dimensions) if needed else path
    return info, needed, reason, produced


def decide(path, for_tablet=False, force=False, bypass=False):
    """Decision seule, sans encoder : (needed, reason)."""
    info = proxy.probe(path)
    needed, reason, _ = proxy.plan(info, for_tablet=for_tablet, force=force, bypass=bypass)
    return needed, reason


def pdf(name="doc.pdf"):
    """Un vrai PDF de trois pages, dont une en paysage.

    Fabrique avec Pillow, deja dependance de l'application : verifier la prise
    en charge des PDF ne doit pas exiger un outil de plus sur la machine.
    """
    path = os.path.join(TMP, name)
    if not os.path.exists(path):
        from PIL import Image
        pages = [Image.new("RGB", (600, 850), (250, 250, 250)),
                 Image.new("RGB", (850, 600), (200, 220, 240)),
                 Image.new("RGB", (600, 850), (255, 230, 230))]
        pages[0].save(path, "PDF", save_all=True, append_images=pages[1:])
    return path


def prepare_pdf(path, page, long_edge=1920):
    info = proxy.probe(path, page=page, long_edge=long_edge)
    needed, reason, dimensions = proxy.plan(info)
    return info, reason, proxy.build(info, dimensions)


# --------------------------------------------------------------------------
# 1. Ce qui n'est pas lisible le devient
# --------------------------------------------------------------------------

def test_prores_becomes_playable():
    info, needed, reason, out = prepare(prores())
    assert info["kind"] == "video", info["kind"]
    assert info["videoCodec"] == "prores", info["videoCodec"]
    assert needed, "un ProRes doit etre transcode"

    video, audio = stream(out, "video"), stream(out, "audio")
    assert video["codec_name"] in BROWSER_VIDEO, video["codec_name"]
    # Le 4:4:4 10 bits de la source ne passe pas : un navigateur veut du 4:2:0
    # 8 bits, meme quand il connait le codec.
    assert video["pix_fmt"] == "yuv420p", video["pix_fmt"]
    assert audio and audio["codec_name"] in BROWSER_AUDIO, audio
    return "%s -> %s + %s" % (info["videoCodec"], video["codec_name"], audio["codec_name"])


def test_dnxhd_becomes_playable():
    info, needed, reason, out = prepare(dnxhd())
    assert needed and info["videoCodec"] == "dnxhd", (needed, info["videoCodec"])
    assert stream(out, "video")["codec_name"] in BROWSER_VIDEO
    return reason


def test_tiff_becomes_playable():
    info, needed, reason, out = prepare(tiff())
    assert info["kind"] == "image", info["kind"]
    assert needed, "un TIFF 16 bits doit etre converti"
    assert stream(out, "video")["codec_name"] in BROWSER_IMAGE
    return reason


def test_a_pdf_page_becomes_an_image():
    """Un PDF n'est lisible par aucun navigateur : sa page devient un PNG."""
    info, reason, out = prepare_pdf(pdf(), 1)
    assert info["kind"] == "image", info["kind"]
    assert info["pdfPageCount"] == 3, info["pdfPageCount"]
    assert stream(out, "video")["codec_name"] in BROWSER_IMAGE
    return "%s -> %s" % (reason, os.path.basename(out))


def test_the_announced_size_is_the_size_produced():
    """`plan` sert a dimensionner le media publie : il ne doit pas mentir.

    PDFium arrondit au pixel superieur ; annoncer un arrondi au plus proche
    donnerait un pixel d'ecart entre ce que la tablette recoit et ce qu'elle
    affiche.
    """
    info, _, out = prepare_pdf(pdf(), 1)
    produced = stream(out, "video")
    assert (produced["width"], produced["height"]) == (info["width"], info["height"]), \
        "annonce %sx%s, produit %sx%s" % (info["width"], info["height"],
                                          produced["width"], produced["height"])
    return "%dx%d annonces et produits" % (info["width"], info["height"])


def test_two_pages_do_not_share_one_cache_entry():
    """La page fait partie de l'identite du proxy.

    Sans elle dans la signature, demander la page 2 ressortirait le PNG deja
    fabrique pour la page 1 -- une page silencieusement fausse, et rien pour
    s'en apercevoir.
    """
    portrait, _, first = prepare_pdf(pdf(), 1)
    landscape, _, second = prepare_pdf(pdf(), 2)
    proxy.register(portrait, first, "page 1")
    proxy.register(landscape, second, "page 2")

    assert proxy._signature(portrait) != proxy._signature(landscape), \
        "les deux pages partagent la meme signature"
    assert proxy.find_cached(portrait) == first, "page 1 mal reprise du cache"
    assert proxy.find_cached(landscape) == second, "page 2 mal reprise du cache"
    assert (portrait["width"], portrait["height"]) != (landscape["width"], landscape["height"]), \
        "une page portrait et une page paysage doivent differer"
    return "page 1 %dx%d et page 2 %dx%d, deux entrees distinctes" % (
        portrait["width"], portrait["height"], landscape["width"], landscape["height"])


def test_the_canvas_definition_invalidates_the_rasterisation():
    """Changer de format de travail doit refaire la page, pas ressortir l'ancienne."""
    small = proxy.probe(pdf(), page=1, long_edge=1280)
    large = proxy.probe(pdf(), page=1, long_edge=3840)
    assert proxy._signature(small) != proxy._signature(large), \
        "la definition n'entre pas dans la signature"
    assert large["height"] > small["height"], (small["height"], large["height"])
    return "bord long 1280 -> %dpx, 3840 -> %dpx" % (small["height"], large["height"])


def test_a_pdf_is_never_served_as_is():
    """`bypass` n'a pas de sens sur un PDF : servi tel quel, il n'affiche rien."""
    info = proxy.probe(pdf(), page=1, long_edge=1920)
    needed, _, _ = proxy.plan(info, bypass=True)
    assert needed, "un PDF servi tel quel ne s'afficherait pas"
    return "rasterisation imposee malgre bypass"


def test_a_broken_pdf_is_refused_clearly():
    """Une erreur de PDFium doit ressortir en ProxyError racontable."""
    broken = os.path.join(TMP, "casse.pdf")
    with open(broken, "wb") as handle:
        handle.write(b"%PDF-1.4 ceci n'est pas un PDF")
    try:
        proxy.probe(broken, page=1, long_edge=1920)
    except proxy.ProxyError as exc:
        return str(exc)[:60]
    raise AssertionError("un PDF corrompu doit etre refuse")


def test_unreadable_audio_track_is_transcoded():
    """Une video lisible dont seule la bande-son ne l'est pas.

    Le fichier livre est unique : garder le son impose de repasser par le
    conteneur, on ne reencode pas une piste isolee.
    """
    info, needed, reason, out = prepare(ac3())
    assert info["audioCodec"] == "ac3", info["audioCodec"]
    assert needed and "audio" in reason, reason
    assert stream(out, "audio")["codec_name"] in BROWSER_AUDIO
    return reason


# --------------------------------------------------------------------------
# 2. Ce qui l'est deja n'est pas retranscode pour rien
# --------------------------------------------------------------------------

def test_ready_h264_is_served_as_is():
    info, needed, _, out = prepare(hd_h264())
    assert not needed, "un H.264 720p est deja lisible tel quel"
    assert out == info["path"], out
    return "servi depuis la source"


def test_ready_jpeg_is_served_as_is():
    info, needed, _, out = prepare(jpeg())
    assert not needed, "un JPEG est deja lisible tel quel"
    assert out == info["path"]
    return "servi depuis la source"


def test_h264_422_10bit_is_always_transcoded():
    """Le piege du `codec_name` : bon codec, chroma impossible.

    Ce n'est pas une question de reseau -- aucun navigateur ne decode ce
    fichier, meme en local. Il doit donc etre transcode dans les deux modes.
    """
    info = proxy.probe(h264_422_10bit())
    assert info["videoCodec"] == "h264", info["videoCodec"]
    assert info["pixFmt"] == "yuv422p10le", info["pixFmt"]

    solo, reason = decide(info["path"], for_tablet=False)
    tablet, _ = decide(info["path"], for_tablet=True)
    assert solo and tablet, "un 4:2:2 10 bits doit etre transcode dans les deux modes"

    _, _, _, out = prepare(info["path"])
    assert stream(out, "video")["pix_fmt"] in proxy.BROWSER_PIX_FMTS
    return "%s %s -> %s" % (info["videoCodec"], info["pixFmt"], reason)


def test_hevc_is_served_as_is():
    """Choix explicite : HEVC compte comme lisible.

    Safari sur iPad le decode, et les navigateurs de bureau recents aussi
    lorsque la machine a le decodage materiel. Le bouton « transcoder ce rush »
    du panneau de gestion existe pour les cas ou ce n'est pas vrai.
    """
    info = proxy.probe(hevc())
    assert info["videoCodec"] == "hevc", info["videoCodec"]
    assert "hevc" in proxy.WEB_VIDEO
    needed, _ = decide(info["path"], for_tablet=True)
    assert not needed, "le HEVC est considere lisible"
    return "hevc %.1f Mbps servi tel quel" % (info["bitrate"] / 1e6)


# --------------------------------------------------------------------------
# 3. Le reseau local n'est pas un cable SDI -- mais le disque local, si
# --------------------------------------------------------------------------

def test_heavy_h264_is_transcoded_for_the_tablet_only():
    """Le coeur de la regle de debit.

    Le fichier se lit parfaitement : c'est la liaison Wi-Fi qui ne suit pas.
    Au poste, qui lit depuis son propre disque, le transcoder serait du temps
    perdu pour rien.
    """
    info = proxy.probe(heavy_h264())
    assert info["bitrate"] > proxy.MAX_BITRATE, info["bitrate"]
    assert info["pixFmt"] == "yuv420p", info["pixFmt"]

    solo, _ = decide(info["path"], for_tablet=False)
    tablet, reason = decide(info["path"], for_tablet=True)
    assert not solo, "au poste, un H.264 lourd se lit tel quel"
    assert tablet, "en mode tablette, il doit etre allege"

    _, _, _, out = prepare(info["path"], for_tablet=True)
    produced = proxy.probe(out)
    assert produced["bitrate"] < proxy.MAX_BITRATE, produced["bitrate"]
    return "%.0f Mbps : poste tel quel, tablette -> %.1f Mbps (%s)" % (
        info["bitrate"] / 1e6, produced["bitrate"] / 1e6, reason)


def test_light_uhd_h264_is_served_as_is():
    """La definition seule ne declenche plus rien : seul le debit compte."""
    info = proxy.probe(light_uhd_h264())
    assert max(info["width"], info["height"]) > proxy.MAX_EDGE, info
    assert info["bitrate"] < proxy.MAX_BITRATE, info["bitrate"]
    needed, _ = decide(info["path"], for_tablet=True)
    assert not needed, "un 4K econome reste lisible tel quel"
    return "%dx%d a %.1f Mbps servi tel quel" % (
        info["width"], info["height"], info["bitrate"] / 1e6)


def test_pcm_24_bits_is_served_as_is():
    """Le WAV d'une salle de montage se lit tel quel, en 24 bits comme en 16.

    C'etait le contre-exemple qui a motive la revision : ne whitelister que
    `pcm_s16le` envoyait au transcodeur des bandes-son que tous les navigateurs
    lisent, au seul motif de leur profondeur.
    """
    info, needed, reason, out = prepare(wav())
    assert info["kind"] == "audio", info["kind"]
    assert info["audioCodec"] == "pcm_s24le", info["audioCodec"]
    assert not needed, "un WAV PCM 24 bits est lisible tel quel : " + reason
    assert out == info["path"], out
    return "pcm_s24le servi depuis la source"


def test_forcing_transcodes_even_a_perfect_file():
    """Recours manuel quand un navigateur ne tient pas sa promesse de codec."""
    source = hd_h264()
    assert not decide(source)[0], "ce fichier n'a normalement besoin de rien"
    needed, reason = decide(source, force=True)
    assert needed and "manuel" in reason, reason
    return reason


def test_bypass_serves_a_file_the_analysis_condamned():
    """Le pendant de `force` : l'analyse se trompe aussi dans l'autre sens.

    Aucune inspection de fichier ne dit ce qu'une machine decode reellement.
    Quand la presomption d'illisibilite est fausse, il faut pouvoir la lever
    sans attendre un transcodage inutile.
    """
    source = prores()
    assert decide(source)[0], "un ProRes est normalement transcode"
    needed, reason = decide(source, bypass=True)
    assert not needed, "le contournement doit servir la source telle quelle"
    return "ProRes servi tel quel a la demande"


def test_an_explicit_force_wins_over_a_bypass():
    """Les deux corrections sont opposees : la plus recente doit trancher.

    `force` l'emporte parce qu'il est le recours de secours -- se retrouver
    coince avec une image noire est pire que d'attendre un transcodage.
    """
    needed, reason = decide(hd_h264(), force=True, bypass=True)
    assert needed and "manuel" in reason, reason
    return reason


def test_dimensions_stay_even_and_keep_aspect():
    """yuv420p impose des dimensions paires ; le cadrage doit rester juste."""
    for width, height in [(1920, 1080), (1080, 1920), (4096, 2160), (999, 501), (640, 480)]:
        out_w, out_h = proxy._target_size(width, height)
        assert out_w % 2 == 0 and out_h % 2 == 0, (width, height, out_w, out_h)
        assert max(out_w, out_h) <= proxy.MAX_EDGE, (width, height, out_w, out_h)
        source_ratio = width / height
        assert abs(out_w / out_h - source_ratio) / source_ratio < 0.01, \
            "deformation sur %dx%d -> %dx%d" % (width, height, out_w, out_h)
    return "5 formats verifies"


# --------------------------------------------------------------------------
# 4. Le cache est persistant, et c'est l'utilisateur.ice qui le vide
# --------------------------------------------------------------------------

def build_and_register(path, for_tablet=False, force=False):
    info = proxy.probe(path)
    needed, reason, dimensions = proxy.plan(info, for_tablet=for_tablet, force=force)
    assert needed, "ce test suppose un transcodage"
    produced = proxy.build(info, dimensions)
    proxy.register(info, produced, reason, forced=force)
    return info, produced


def test_a_known_proxy_is_reused_instead_of_rebuilt():
    """La raison d'etre du cache : ne pas refaire des minutes de transcodage."""
    proxy.purge()
    info, produced = build_and_register(dnxhd())
    found = proxy.find_cached(info)
    assert found == produced, (found, produced)
    assert len(proxy.entries()) == 1, proxy.entries()
    return "proxy retrouve sans reencoder"


def test_a_forced_transcode_reuses_the_automatic_proxy():
    """« force » et « auto » designent le meme fichier, pas deux fichiers.

    La signature distinguait les deux, alors que `plan` vise exactement les
    memes dimensions dans un cas comme dans l'autre : le fichier aurait ete
    refabrique a l'identique. Le parcours qui le revelait est courant --
    transcoder, puis « Servir l'original », puis redemander un transcodage --
    et relancait plusieurs minutes d'attente pour rien.
    """
    proxy.purge()
    info, produced = build_and_register(dnxhd())          # transcodage automatique
    found = proxy.find_cached(info, forced=True)          # demande manuelle
    assert found == produced, (found, produced)
    return "proxy automatique repris par une demande manuelle"


def test_an_automatic_transcode_reuses_the_forced_proxy():
    """Et dans l'autre sens : le cache n'a pas de sens de lecture."""
    proxy.purge()
    info, produced = build_and_register(dnxhd(), force=True)
    found = proxy.find_cached(info)
    assert found == produced, (found, produced)
    return "proxy manuel repris par une demande automatique"


def test_the_interface_can_tell_a_proxy_is_available():
    """Ce que l'interface doit savoir pour choisir son libelle de bouton.

    « Transcoder ce rush » et « Servir le proxy » ne promettent pas la meme
    attente : quelques minutes contre rien du tout.
    """
    proxy.purge()
    source = dnxhd()
    assert not proxy.cached_for_source(source), "cache vide, rien ne doit etre annonce"
    build_and_register(source)
    assert proxy.cached_for_source(source), "proxy fabrique mais non annonce"
    proxy.purge()
    assert not proxy.cached_for_source(source), "cache vide, rien ne doit rester annonce"
    return "disponibilite annoncee fidelement"


def test_a_source_that_moved_is_not_announced_as_cached():
    proxy.purge()
    assert not proxy.cached_for_source(os.path.join(TMP, "jamais_vu.mov"))
    assert not proxy.cached_for_source("")
    return "fichier absent -> aucun proxy annonce"


def test_a_modified_source_invalidates_its_proxy():
    """Un rush re-exporte sous le meme nom ne doit pas ressortir l'ancienne visee."""
    proxy.purge()
    twin = os.path.join(TMP, "remonte.mov")
    shutil.copy(dnxhd(), twin)
    info, _ = build_and_register(twin)
    assert proxy.find_cached(info) is not None

    # Nouvelle version livree par le montage, meme nom de fichier.
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=25:duration=2",
           "-c:v", "dnxhd", "-b:v", "36M", "-pix_fmt", "yuv422p", twin)
    assert proxy.find_cached(proxy.probe(twin)) is None, \
        "le proxy de la version precedente est ressorti"
    return "ancienne visee ecartee"


def test_deleting_by_hand_outside_the_app_is_tolerated():
    proxy.purge()
    info, produced = build_and_register(dnxhd())
    os.remove(produced)
    assert proxy.find_cached(info) is None
    assert proxy.entries() == [], proxy.entries()
    return "journal resynchronise sur le disque"


def test_entries_describe_the_source_not_the_scratch_file():
    """Le panneau doit montrer « rush.mov », pas « proxy_a1b2c3.mp4 »."""
    proxy.purge()
    info, _ = build_and_register(prores())
    listing = proxy.entries()
    assert len(listing) == 1, listing
    entry = listing[0]
    assert entry["name"] == info["name"], entry
    assert entry["source"] == os.path.abspath(info["path"]), entry
    assert entry["size"] > 0 and entry["reason"], entry
    assert proxy.total_size() == entry["size"]
    return "%s — %.1f Mo — %s" % (entry["name"], entry["size"] / 1e6, entry["reason"])


def test_removing_one_leaves_the_others():
    proxy.purge()
    _, first = build_and_register(dnxhd())
    _, second = build_and_register(tiff())
    assert len(proxy.entries()) == 2

    assert proxy.remove(os.path.basename(first))
    remaining = proxy.entries()
    assert len(remaining) == 1, remaining
    assert remaining[0]["file"] == os.path.basename(second)
    assert not os.path.exists(first) and os.path.exists(second)
    return "1 supprime, 1 conserve"


def test_remove_refuses_to_escape_the_cache_directory():
    """Ces noms viennent d'une requete HTTP : « ../ » ne doit rien atteindre."""
    victim = os.path.join(TMP, "precieux.txt")
    with open(victim, "w", encoding="utf-8") as handle:
        handle.write("rush de l'utilisateur")
    for attempt in ("../precieux.txt", "../../etc/hosts", "sous/dossier.mp4",
                    proxy.INDEX_NAME):
        try:
            proxy.remove(attempt)
        except proxy.ProxyError:
            continue
        raise AssertionError("« %s » aurait du etre refuse" % attempt)
    assert os.path.exists(victim), "un fichier hors du cache a ete supprime"
    return "4 tentatives d'echappement refusees"


def test_purge_empties_everything_including_the_index():
    proxy.purge()
    build_and_register(dnxhd())
    build_and_register(tiff())
    removed, kept = proxy.purge()
    assert removed >= 2, removed
    assert proxy.entries() == [], proxy.entries()
    assert proxy.total_size() == 0
    return "%d fichier(s) supprime(s), journal vide" % removed


def test_source_is_never_touched():
    """Le menage ne doit jamais s'approcher du rush de l'utilisateur.ice."""
    source = prores()
    before = os.path.getsize(source)
    build_and_register(source)
    proxy.purge()
    assert os.path.exists(source), "le fichier source a disparu"
    assert os.path.getsize(source) == before
    return "source intacte (%.1f Mo)" % (before / 1e6)


def test_unreadable_file_is_refused_clearly():
    broken = os.path.join(TMP, "pas_un_media.txt")
    with open(broken, "w", encoding="utf-8") as handle:
        handle.write("ceci n'est pas un rush")
    try:
        proxy.probe(broken)
    except proxy.ProxyError as exc:
        return str(exc)[:60]
    raise AssertionError("un fichier illisible doit lever ProxyError")


TESTS = [
    # 1. illisible -> lisible, quel que soit le mode
    test_prores_becomes_playable,
    test_dnxhd_becomes_playable,
    test_tiff_becomes_playable,
    test_a_pdf_page_becomes_an_image,
    test_the_announced_size_is_the_size_produced,
    test_two_pages_do_not_share_one_cache_entry,
    test_the_canvas_definition_invalidates_the_rasterisation,
    test_a_pdf_is_never_served_as_is,
    test_a_broken_pdf_is_refused_clearly,
    test_unreadable_audio_track_is_transcoded,
    test_h264_422_10bit_is_always_transcoded,
    # 2. lisible : on n'y touche pas
    test_ready_h264_is_served_as_is,
    test_ready_jpeg_is_served_as_is,
    test_hevc_is_served_as_is,
    test_light_uhd_h264_is_served_as_is,
    test_pcm_24_bits_is_served_as_is,
    # 3. trop lourd pour le reseau : tablette seulement
    test_heavy_h264_is_transcoded_for_the_tablet_only,
    # 3 bis. les deux corrections manuelles, en sens inverse l'une de l'autre
    test_forcing_transcodes_even_a_perfect_file,
    test_bypass_serves_a_file_the_analysis_condamned,
    test_an_explicit_force_wins_over_a_bypass,
    test_dimensions_stay_even_and_keep_aspect,
    # 4. cache persistant
    test_a_known_proxy_is_reused_instead_of_rebuilt,
    test_a_forced_transcode_reuses_the_automatic_proxy,
    test_an_automatic_transcode_reuses_the_forced_proxy,
    test_the_interface_can_tell_a_proxy_is_available,
    test_a_source_that_moved_is_not_announced_as_cached,
    test_a_modified_source_invalidates_its_proxy,
    test_deleting_by_hand_outside_the_app_is_tolerated,
    test_entries_describe_the_source_not_the_scratch_file,
    test_removing_one_leaves_the_others,
    test_remove_refuses_to_escape_the_cache_directory,
    test_purge_empties_everything_including_the_index,
    test_source_is_never_touched,
    test_unreadable_file_is_refused_clearly,
]


def main():
    print("Proxys de lecture — format %s" % proxy.PROXY_FORMAT)
    failures = 0
    for test in TESTS:
        try:
            detail = test()
            print("  OK   %-46s %s" % (test.__name__, detail or ""))
        except Exception as exc:  # noqa: BLE001 - rapport de test
            failures += 1
            print("  FAIL %-46s %s: %s" % (test.__name__, type(exc).__name__, exc))
    proxy.purge()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d/%d tests passes" % (len(TESTS) - failures, len(TESTS)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
