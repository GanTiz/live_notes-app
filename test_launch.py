"""Verification du demarrage : choix du port, et selecteur de fichier natif.

Lancer : py test_launch.py

Ces deux mecanismes ont un point commun : ils ne cassaient que chez
l'utilisateur.ice, jamais en developpement, et sans message d'erreur.

1. Le port. Se lier a un port deja pris n'ouvre pas une fenetre d'erreur mais
   une page blanche. C'est ce qui arrivait sur macOS, ou le recepteur AirPlay
   occupe 5000 des l'ouverture de session.

2. Le selecteur de fichier. Il tourne dans un sous-processus, et la commande
   qui le lance n'est pas la meme selon que l'application est figee par
   PyInstaller ou non : `sys.executable` designe l'interprete dans un cas et
   l'application elle-meme dans l'autre. Confondre les deux relancait
   live_notes au lieu d'ouvrir une boite de dialogue. Ce qui se verifie ici,
   c'est donc la *commande construite* -- ouvrir une vraie boite de dialogue
   demanderait un ecran, et un humain devant.

3. Le dossier initial du selecteur de media, et sa memoire. Le dialogue
   Windows lui-meme (osascript, PowerShell) ne s'ouvre pas sur cette machine
   Linux -- ce qui se verifie ici, c'est que le dossier voulu (dernier choix,
   ou a defaut la bibliotheque Videos/live_notes) arrive bien jusqu'au
   selecteur, et qu'il survit -- ou s'efface -- correctement d'un lancement a
   l'autre.
"""

import os
import shutil
import socket
import sys
import tempfile

import app
import nativedialog
import paths


LOOPBACK = "127.0.0.1"


class holding:
    """Occupe un port pour de bon, le temps du test."""

    def __init__(self, port):
        self.port = port

    def __enter__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((LOOPBACK, self.port))
        self.sock.listen(1)
        return self

    def __exit__(self, *_):
        self.sock.close()


# --------------------------------------------------------------------------
# Choix du port
# --------------------------------------------------------------------------

def test_the_default_port_is_not_the_airplay_one():
    # 5000 est le port historique de Flask, et celui du recepteur AirPlay.
    # Le garder par defaut, c'est livrer une page blanche a tout macOS recent.
    assert app.DEFAULT_PORT != 5000, app.DEFAULT_PORT
    return "defaut = %d, pas 5000" % app.DEFAULT_PORT


def test_a_free_port_is_taken_as_is():
    with holding(app.DEFAULT_PORT + 1):
        chosen = app.choose_port(LOOPBACK)
    assert chosen == app.DEFAULT_PORT, chosen
    return "port libre -> %d" % chosen


def test_an_occupied_port_makes_the_server_step_aside():
    with holding(app.DEFAULT_PORT):
        chosen = app.choose_port(LOOPBACK)
    assert chosen != app.DEFAULT_PORT, chosen
    assert _is_bindable(chosen), chosen
    return "%d occupe -> %d" % (app.DEFAULT_PORT, chosen)


def test_a_long_run_of_occupied_ports_still_yields_something():
    # Plusieurs instances ouvertes en meme temps : il faut continuer a
    # descendre la liste plutot que d'echouer sur le deuxieme port.
    held = [holding(app.DEFAULT_PORT + offset).__enter__() for offset in range(5)]
    try:
        chosen = app.choose_port(LOOPBACK)
    finally:
        for entry in held:
            entry.__exit__()
    assert chosen >= app.DEFAULT_PORT + 5, chosen
    return "5 ports pris d'affilee -> %d" % chosen


def test_an_explicit_port_wins():
    free = _free_port()
    os.environ["LIVE_NOTES_PORT"] = str(free)
    try:
        chosen = app.choose_port(LOOPBACK)
    finally:
        del os.environ["LIVE_NOTES_PORT"]
    assert chosen == free, (chosen, free)
    return "LIVE_NOTES_PORT=%d respecte" % free


def test_an_explicit_but_occupied_port_still_starts():
    # Mieux vaut demarrer ailleurs en le disant que ne pas demarrer du tout :
    # l'URL reelle se propage a la fenetre comme au QR code d'appairage.
    with holding(app.DEFAULT_PORT + 7):
        os.environ["LIVE_NOTES_PORT"] = str(app.DEFAULT_PORT + 7)
        try:
            chosen = app.choose_port(LOOPBACK)
        finally:
            del os.environ["LIVE_NOTES_PORT"]
    assert chosen != app.DEFAULT_PORT + 7, chosen
    return "port impose mais pris -> repli sur %d" % chosen


def _is_bindable(port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((LOOPBACK, port))
        except OSError:
            return False
    return True


def _free_port():
    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


# --------------------------------------------------------------------------
# Selecteur de fichier natif
# --------------------------------------------------------------------------

class frozen:
    """Fait passer le processus pour une application figee par PyInstaller."""

    def __enter__(self):
        sys.frozen = True

    def __exit__(self, *_):
        del sys.frozen


def test_the_unfrozen_picker_runs_python_on_the_module():
    command = nativedialog._tk_command("file", "", "/tmp/sortie.txt")
    assert command[0] == sys.executable, command
    assert command[1].endswith("nativedialog.py"), command
    return "interprete + module"


def test_the_frozen_picker_never_relaunches_the_application_blind():
    # Le bug d'origine : `[sys.executable, "-c", <code>]` une fois fige lance
    # l'application, qui ignore le `-c` et demarre un second serveur. Ce qui
    # remontait dans le champ « chemin du fichier », c'etait la banniere de
    # Flask. Le drapeau doit donc arriver en premier, pour etre intercepte
    # avant tout demarrage.
    with frozen():
        command = nativedialog._tk_command("file", "", "/tmp/sortie.txt")
    assert "-c" not in command, command
    assert command[1] == nativedialog.PICK_FLAG, command
    return "drapeau en tete, pas de -c"


def test_the_flag_is_intercepted_before_anything_starts():
    intercepted = []

    def fake_dialog(kind, initial):
        intercepted.append((kind, initial))
        return "/rushes/plan.mov"

    outfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pick_test")
    real = nativedialog._tk_dialog
    nativedialog._tk_dialog = fake_dialog
    try:
        argv = ["live_notes", nativedialog.PICK_FLAG, "file", "", outfile]
        try:
            nativedialog.run_picker_if_requested(argv)
        except SystemExit as exit_code:
            assert exit_code.code == 0, exit_code.code
        else:
            raise AssertionError("le selecteur n'a pas quitte le processus")
        with open(outfile, encoding="utf-8") as written:
            assert written.read() == "/rushes/plan.mov"
    finally:
        nativedialog._tk_dialog = real
        if os.path.exists(outfile):
            os.unlink(outfile)
    assert intercepted == [("file", "")], intercepted
    return "detourne, et le chemin ressort par fichier"


def test_an_ordinary_launch_is_left_alone():
    # Sans le drapeau, la fonction doit rendre la main sans quitter : c'est le
    # cas de tous les demarrages normaux.
    assert nativedialog.run_picker_if_requested(["live_notes"]) is None
    assert nativedialog.run_picker_if_requested(["live_notes", "--autre"]) is None
    return "demarrage normal intact"


def test_the_result_never_travels_through_stdout():
    # Le binaire est construit avec console=False : une application Windows
    # sans console n'a pas de sortie standard. Le chemin doit passer par un
    # fichier, et le fichier doit figurer dans la commande.
    with frozen():
        command = nativedialog._tk_command("directory", "/exports", "/tmp/sortie.txt")
    assert command[-1] == "/tmp/sortie.txt", command
    assert "/exports" in command, command
    return "chemin de sortie transmis en argument"


def test_each_platform_has_a_native_first_choice():
    # Tk n'est pas embarque dans les binaires distribues : sur les deux
    # plateformes livrees, un selecteur fourni par le systeme doit passer
    # avant lui.
    for platform, expected in (("darwin", nativedialog._ask_macos),
                               ("win32", nativedialog._ask_windows)):
        chain = _backends_for(platform)
        assert chain[0] is expected, (platform, chain)
        assert nativedialog._ask_tk in chain, (platform, chain)
    return "osascript sur macOS, PowerShell sur Windows, Tk en repli"


def _backends_for(platform):
    """La chaine de selecteurs que `_ask` retiendrait sur cette plateforme."""
    real_platform, real_name = sys.platform, os.name
    tried = []
    try:
        sys.platform = platform
        os.name = "nt" if platform == "win32" else "posix"

        def spy(backend):
            def run(kind, initial):
                tried.append(backend)
                raise nativedialog.PickerError("essai")
            return run

        originals = {name: getattr(nativedialog, name)
                     for name in ("_ask_macos", "_ask_windows", "_ask_tk")}
        for name, backend in originals.items():
            setattr(nativedialog, name, spy(backend))
        try:
            nativedialog._ask("file")
        except nativedialog.PickerError:
            pass
        finally:
            for name, backend in originals.items():
                setattr(nativedialog, name, backend)
    finally:
        sys.platform, os.name = real_platform, real_name
    return tried


# --------------------------------------------------------------------------
# Dossier initial du selecteur de media, et sa memoire
# --------------------------------------------------------------------------

def test_the_initial_directory_reaches_the_backend():
    # Sur cette machine (ni macOS ni Windows), c'est _ask_tk qui repond : le
    # test verifie la chaine complete ask_open_file -> _ask -> backend, pas
    # une plateforme simulee.
    seen = []
    original = nativedialog._ask_tk

    def fake(kind, initial):
        seen.append((kind, initial))
        return "/rushes/plan.mov"

    nativedialog._ask_tk = fake
    try:
        result = nativedialog.ask_open_file("/dossier/de/depart")
    finally:
        nativedialog._ask_tk = original
    assert seen == [("file", "/dossier/de/depart")], seen
    assert result == "/rushes/plan.mov", result
    return "dossier de depart transmis jusqu'au backend"


def test_last_media_directory_persists_and_forgets_what_vanished():
    # Le fichier de reglage et le dossier qu'il designe sont deux choses
    # distinctes : les separer permet de faire disparaitre le second sans
    # emporter le premier, exactement le cas d'un disque externe retire.
    settings_dir = tempfile.mkdtemp(prefix="live_notes_settings_")
    media_dir = tempfile.mkdtemp(prefix="live_notes_media_")
    original_path = app.LAST_MEDIA_DIR_PATH
    app.LAST_MEDIA_DIR_PATH = os.path.join(settings_dir, "last_media_dir.txt")
    try:
        assert app._read_last_media_dir() is None, "rien d'ecrit -> pas de dernier dossier"

        app._write_last_media_dir(media_dir)
        assert app._read_last_media_dir() == media_dir, "le dossier ecrit doit revenir tel quel"

        # Le dossier disparait : le proposer quand meme ouvrirait le
        # selecteur sur du vide, sans que rien ne l'explique.
        os.rmdir(media_dir)
        assert app._read_last_media_dir() is None, \
            "dossier disparu -> pas de proposition fantome"
    finally:
        app.LAST_MEDIA_DIR_PATH = original_path
        shutil.rmtree(settings_dir, ignore_errors=True)
    return "persiste, revient, et s'efface si le dossier a disparu"


def test_the_media_picker_falls_back_to_the_videos_library():
    settings_dir = tempfile.mkdtemp(prefix="live_notes_settings_")
    original_path = app.LAST_MEDIA_DIR_PATH
    app.LAST_MEDIA_DIR_PATH = os.path.join(settings_dir, "last_media_dir.txt")
    try:
        assert app._media_pick_initial_dir() == paths.default_export_dir()
    finally:
        app.LAST_MEDIA_DIR_PATH = original_path
        shutil.rmtree(settings_dir, ignore_errors=True)
    return "pas de dernier dossier -> bibliotheque Videos/live_notes"


def test_the_media_picker_remembers_the_last_directory():
    settings_dir = tempfile.mkdtemp(prefix="live_notes_settings_")
    media_dir = tempfile.mkdtemp(prefix="live_notes_media_")
    original_path = app.LAST_MEDIA_DIR_PATH
    app.LAST_MEDIA_DIR_PATH = os.path.join(settings_dir, "last_media_dir.txt")
    try:
        app._write_last_media_dir(media_dir)
        assert app._media_pick_initial_dir() == media_dir
    finally:
        app.LAST_MEDIA_DIR_PATH = original_path
        shutil.rmtree(settings_dir, ignore_errors=True)
        shutil.rmtree(media_dir, ignore_errors=True)
    return "dernier dossier connu -> reutilise"


TESTS = [
    test_the_default_port_is_not_the_airplay_one,
    test_a_free_port_is_taken_as_is,
    test_an_occupied_port_makes_the_server_step_aside,
    test_a_long_run_of_occupied_ports_still_yields_something,
    test_an_explicit_port_wins,
    test_an_explicit_but_occupied_port_still_starts,
    test_the_unfrozen_picker_runs_python_on_the_module,
    test_the_frozen_picker_never_relaunches_the_application_blind,
    test_the_flag_is_intercepted_before_anything_starts,
    test_an_ordinary_launch_is_left_alone,
    test_the_result_never_travels_through_stdout,
    test_each_platform_has_a_native_first_choice,
    test_the_initial_directory_reaches_the_backend,
    test_last_media_directory_persists_and_forgets_what_vanished,
    test_the_media_picker_falls_back_to_the_videos_library,
    test_the_media_picker_remembers_the_last_directory,
]


def main():
    print("Demarrage — port et selecteur de fichier")
    failures = 0
    for test in TESTS:
        try:
            detail = test()
            print("  OK   %-52s %s" % (test.__name__, detail or ""))
        except Exception as exc:  # noqa: BLE001 - rapport de test
            failures += 1
            print("  FAIL %-52s %s: %s" % (test.__name__, type(exc).__name__, exc))
    print("\n%d/%d tests passes" % (len(TESTS) - failures, len(TESTS)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
