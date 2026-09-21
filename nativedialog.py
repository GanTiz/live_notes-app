"""Boites de dialogue natives « choisir un fichier / un dossier / enregistrer ».

Un `<input type=file>` ne donne au navigateur qu'un blob anonyme : Python n'a
jamais le chemin du fichier, donc rien a transcoder ni a rendre. Le detour par
une boite de dialogue native est ce qui rend possible la prise en charge de
n'importe quel codec -- d'ou ce module.

Le dialogue ne peut pas s'ouvrir dans le processus du serveur : une fenetre Tk
creee depuis un thread de Flask est instable, et sur macOS Tk exige purement et
simplement le thread principal. Il tourne donc a l'exterieur.

Pourquoi pas `subprocess.run([sys.executable, "-c", <code>])`
-----------------------------------------------------------
C'etait l'implementation precedente, et elle ne survit pas a la compilation :
une fois l'application figee par PyInstaller, `sys.executable` n'est plus un
interprete Python mais l'application elle-meme. Le `-c` devient un argument que
le lanceur ignore, et le clic sur « Parcourir » demarre une *seconde instance*
de live_notes ; ce qui remonte alors dans le champ « chemin du fichier », c'est
la banniere de demarrage de Flask capturee sur sa sortie standard. Le bug ne se
voyait que sur les binaires distribues, jamais en developpement.

Deux consequences pour la solution retenue :

1. On s'appuie d'abord sur les selecteurs fournis par le systeme -- osascript
   sur macOS, PowerShell/WinForms sur Windows. Ce sont de vrais dialogues
   natifs, presents sur toute machine, et surtout ils n'ont rien a voir avec
   l'interpreteur : la compilation ne peut plus rien casser.
2. Tk ne sert plus que de repli (developpement, Linux). Pour que ce repli reste
   correct meme fige, il ne passe pas par `-c` mais par un drapeau que le
   lanceur intercepte au demarrage (voir `run_picker_if_requested`, appele en
   tete de packaging/launcher.py) : re-executer l'application avec ce drapeau
   ouvre un selecteur au lieu de demarrer un serveur.

Le resultat transite par un fichier temporaire, jamais par stdout : le binaire
est construit avec `console=False` (voir packaging/live_notes.spec), et une
application Windows sans console n'a pas de sortie standard ou quoi que ce soit
survivrait.
"""

import os
import subprocess
import sys
import tempfile

# Drapeau du mode selecteur. Volontairement long et prefixe : il est lu sur la
# ligne de commande de l'application elle-meme, il ne doit ressembler a aucune
# option qu'on pourrait vouloir passer un jour au lanceur.
PICK_FLAG = "--live-notes-pick"

# Une boite de dialogue attend un humain : la minute serait trop courte, mais
# laisser un processus orphelin indefiniment ne l'est pas davantage.
PICKER_TIMEOUT = 600


class PickerError(RuntimeError):
    """Le selecteur n'a pas pu s'ouvrir (l'utilisateur.ice n'a rien annule)."""


def _hidden_window_options():
    """Empeche l'apparition d'une console noire derriere le dialogue Windows."""
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _run(command, **extra):
    return subprocess.run(command, capture_output=True, text=True,
                          timeout=PICKER_TIMEOUT, **_hidden_window_options(),
                          **extra)


# --------------------------------------------------------------------------
# macOS minimal pris en charge
# --------------------------------------------------------------------------

# Le bundle est construit pour macOS 11+ (numpy<2 et pypdfium2<=5.9.0, roues
# OpenBLAS — voir packaging/requirements-macos.txt) : sous ce seuil, l'import
# numpy crashait au lancement sans aucune explication pour l'utilisateur.ice.
# Mieux vaut un refus explicite, en toutes lettres, qu'un echec muet. macOS
# 10.13+ peut fonctionner mais n'est ni teste ni garanti.
MIN_MACOS = (11, 0)


def _parse_macos_version(version_str):
    """`"13.5.1"` -> (13, 5) ; `"10.15"` -> (10, 15) ; illisible -> None."""
    if not version_str:
        return None
    parts = version_str.split(".")
    # Tout ce qui se lit doit se lire : un « 12.x » n'est pas un macOS 12 dont
    # on ignorerait le mineur, c'est une chaine qu'on ne comprend pas. Et une
    # version incomprise passe (voir `refuse_unsupported_macos`) plutot que
    # d'etre devinee -- deviner « 10.x » en macOS 10 refuserait le demarrage
    # sur la foi d'une moitie de chaine.
    if not parts[0].isdigit():
        return None
    if len(parts) > 1 and not parts[1].isdigit():
        return None
    major = int(parts[0])
    minor = int(parts[1]) if len(parts) > 1 else 0
    return (major, minor)


def macos_version():
    """Version de macOS sous forme (majeur, mineur), None hors macOS ou si
    elle ne se lit pas. `platform.mac_ver()` renseigne le vrai systeme, meme
    une fois l'application figee par PyInstaller."""
    if sys.platform != "darwin":
        return None
    try:
        import platform
        return _parse_macos_version(platform.mac_ver()[0])
    except Exception:
        return None


def refuse_unsupported_macos():
    """Refuse de demarrer sur un macOS anterieur a MIN_MACOS.

    A appeler en tete du lanceur, avant d'importer l'application : l'import
    numpy crashait sans message sur ces systemes. Une version illisible est
    laissee passer — on ne refuse que ce qu'on sait trop ancien.
    """
    version = macos_version()
    if version is None or version >= MIN_MACOS:
        return
    message = ("live_notes necessite macOS %d (Big Sur) ou plus recent. "
               "Votre version (macOS %s) n'est pas prise en charge."
               % (MIN_MACOS[0], ".".join(str(v) for v in version)))
    try:
        _run(["osascript", "-e",
              'display alert "live_notes" message "%s"'
              % message.replace('"', '\\"')])
    except Exception:
        # Une alerte qui ne part pas ne change rien au verdict : on refuse.
        pass
    sys.exit(1)


# --------------------------------------------------------------------------
# macOS — osascript
# --------------------------------------------------------------------------

# `choose file`/`choose folder` levent une erreur -128 quand on annule : c'est
# une annulation, pas une panne, et elle doit se distinguer d'un selecteur
# introuvable. On la rattrape pour rendre une chaine vide, que l'appelant lit
# comme « rien de choisi ».
_OSASCRIPT_FILE = '''
on run argv
    try
        set startHere to item 1 of argv
        if startHere is not "" then
            try
                set chosen to choose file with prompt "Choisir un media" default location (POSIX file startHere as alias)
            on error
                set chosen to choose file with prompt "Choisir un media"
            end try
        else
            set chosen to choose file with prompt "Choisir un media"
        end if
        return POSIX path of chosen
    on error number -128
        return ""
    end try
end run
'''

_OSASCRIPT_DIR = '''
on run argv
    try
        set startHere to item 1 of argv
        if startHere is not "" then
            try
                set chosen to choose folder with prompt "Dossier de destination" default location (POSIX file startHere as alias)
            on error
                set chosen to choose folder with prompt "Dossier de destination"
            end try
        else
            set chosen to choose folder with prompt "Dossier de destination"
        end if
        return POSIX path of chosen
    on error number -128
        return ""
    end try
end run
'''


_OSASCRIPT_SAVE = '''
on run argv
    set defaultName to item 1 of argv
    set startHere to item 2 of argv
    try
        if startHere is not "" then
            try
                set chosen to choose file name with prompt "Enregistrer le projet" default name defaultName default location (POSIX file startHere as alias)
            on error
                set chosen to choose file name with prompt "Enregistrer le projet" default name defaultName
            end try
        else
            set chosen to choose file name with prompt "Enregistrer le projet" default name defaultName
        end if
        return POSIX path of chosen
    on error number -128
        return ""
    end try
end run
'''


def _ask_macos(kind, initial, default_name=""):
    if kind == "directory":
        script, arguments = _OSASCRIPT_DIR, [initial or ""]
    elif kind == "save":
        script, arguments = _OSASCRIPT_SAVE, [default_name or "", initial or ""]
    else:
        script, arguments = _OSASCRIPT_FILE, [initial or ""]
    command = ["osascript", "-e", script] + arguments
    completed = _run(command)
    if completed.returncode != 0:
        raise PickerError((completed.stderr or "osascript a echoue").strip())
    # `POSIX path of` rend un dossier avec une barre finale : on la retire pour
    # que le chemin se compare et se joigne comme n'importe quel autre.
    path = completed.stdout.strip()
    return path.rstrip("/") if kind == "directory" and path != "/" else path


# --------------------------------------------------------------------------
# Windows — PowerShell / WinForms
# --------------------------------------------------------------------------

# Faire passer le dialogue devant l'application demande deux choses, et la
# premiere ne suffit pas.
#
# 1. Une fenetre proprietaire qui existe reellement. Un Form seulement
#    construit, ou pose hors ecran avec une taille nulle, n'a pas de presence
#    dans l'ordre d'empilement : le dialogue qu'il possede s'ouvrait derriere
#    l'application. D'ou un Form de 1x1 pixel, sans bordure, entierement
#    transparent (Opacity 0) et hors barre des taches -- invisible, mais une
#    vraie fenetre, et TopMost.
#
# 2. Le droit de passer au premier plan. Windows refuse `SetForegroundWindow`
#    a un processus qui n'est pas deja au premier plan : il fait clignoter le
#    bouton de la barre des taches au lieu d'activer la fenetre. Or PowerShell
#    est lance en arriere-plan par le serveur, il n'y a jamais droit. La parade
#    documentee est `AttachThreadInput` : en attachant le fil d'execution du
#    selecteur a celui de la fenetre actuellement au premier plan, les deux
#    partagent leur etat d'entree et l'appel est accorde. On detache aussitot.
_POWERSHELL_OWNER = r'''
$owner = New-Object System.Windows.Forms.Form
$owner.FormBorderStyle = 'None'
$owner.StartPosition = 'Manual'
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Opacity = 0
$owner.ShowInTaskbar = $false
$owner.TopMost = $true
$owner.Show()
$owner.Activate()

# Le tour de premier plan ne doit jamais empecher le dialogue de s'ouvrir :
# un dialogue derriere la fenetre reste rattrapable a l'alt-tab, une erreur
# ici ne laisserait rien du tout.
try {
    Add-Type -Namespace LiveNotes -Name Fg -MemberDefinition @'
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
[DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr pid);
[DllImport("user32.dll")] public static extern bool AttachThreadInput(uint attach, uint to, bool join);
[DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
'@
    $foreground = [LiveNotes.Fg]::GetForegroundWindow()
    $theirs = [LiveNotes.Fg]::GetWindowThreadProcessId($foreground, [IntPtr]::Zero)
    $ours = [LiveNotes.Fg]::GetCurrentThreadId()
    $attached = $false
    if ($theirs -ne 0 -and $theirs -ne $ours) {
        $attached = [LiveNotes.Fg]::AttachThreadInput($ours, $theirs, $true)
    }
    [void][LiveNotes.Fg]::BringWindowToTop($owner.Handle)
    [void][LiveNotes.Fg]::SetForegroundWindow($owner.Handle)
    if ($attached) {
        [void][LiveNotes.Fg]::AttachThreadInput($ours, $theirs, $false)
    }
} catch { }
'''

_POWERSHELL_FILE = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
''' + _POWERSHELL_OWNER + r'''
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = 'Choisir un media'
$dialog.Filter = 'Tous les fichiers (*.*)|*.*'
if ($env:LIVE_NOTES_PICK_INITIAL -and (Test-Path -LiteralPath $env:LIVE_NOTES_PICK_INITIAL)) {
    $dialog.InitialDirectory = $env:LIVE_NOTES_PICK_INITIAL
}
$result = ''
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    $result = $dialog.FileName
}
[System.IO.File]::WriteAllText($env:LIVE_NOTES_PICK_OUTPUT, $result, (New-Object System.Text.UTF8Encoding($false)))
$owner.Dispose()
'''

_POWERSHELL_DIR = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
''' + _POWERSHELL_OWNER + r'''
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Dossier de destination'
if ($env:LIVE_NOTES_PICK_INITIAL -and (Test-Path -LiteralPath $env:LIVE_NOTES_PICK_INITIAL)) {
    $dialog.SelectedPath = $env:LIVE_NOTES_PICK_INITIAL
}
$result = ''
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    $result = $dialog.SelectedPath
}
[System.IO.File]::WriteAllText($env:LIVE_NOTES_PICK_OUTPUT, $result, (New-Object System.Text.UTF8Encoding($false)))
$owner.Dispose()
'''


_POWERSHELL_SAVE = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
''' + _POWERSHELL_OWNER + r'''
$dialog = New-Object System.Windows.Forms.SaveFileDialog
$dialog.Title = 'Enregistrer le projet'
$dialog.Filter = 'Projet live_notes (*.lvn)|*.lvn|Tous les fichiers (*.*)|*.*'
$dialog.DefaultExt = 'lvn'
$dialog.AddExtension = $true
$dialog.FileName = $env:LIVE_NOTES_PICK_NAME
if ($env:LIVE_NOTES_PICK_INITIAL -and (Test-Path -LiteralPath $env:LIVE_NOTES_PICK_INITIAL)) {
    $dialog.InitialDirectory = $env:LIVE_NOTES_PICK_INITIAL
}
$result = ''
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    $result = $dialog.FileName
}
[System.IO.File]::WriteAllText($env:LIVE_NOTES_PICK_OUTPUT, $result, (New-Object System.Text.UTF8Encoding($false)))
$owner.Dispose()
'''


def _ask_windows(kind, initial, default_name=""):
    if kind == "directory":
        script = _POWERSHELL_DIR
    elif kind == "save":
        script = _POWERSHELL_SAVE
    else:
        script = _POWERSHELL_FILE
    handle, outfile = tempfile.mkstemp(prefix="live_notes_pick_", suffix=".txt")
    os.close(handle)
    try:
        # Le dossier de depart et le chemin choisi passent tous deux par
        # l'environnement/un fichier, jamais par la console. `CreateProcessW`
        # (le bloc d'environnement) et un fichier ecrit en UTF-8 explicite
        # transportent un accent sans perte ; la console, elle, encode selon
        # sa page de code active (OEM), que Python decodait avec la page ANSI
        # par defaut -- deux pages differentes, d'ou un « e » accentue rendu
        # en tout autre chose (une virgule, observe en usage reel).
        environment = dict(os.environ, LIVE_NOTES_PICK_INITIAL=initial or "",
                           LIVE_NOTES_PICK_OUTPUT=outfile)
        if kind == "save":
            environment["LIVE_NOTES_PICK_NAME"] = default_name or ""
        completed = _run(
            ["powershell", "-NoProfile", "-NonInteractive", "-STA",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            env=environment,
        )
        if completed.returncode != 0:
            raise PickerError((completed.stderr or "PowerShell a echoue").strip())
        # `utf-8-sig` plutot que `utf-8` : tolerant si .NET a tout de meme
        # pose un BOM, exact sinon -- aucune des deux ecritures ne casse
        # l'autre lecture.
        with open(outfile, encoding="utf-8-sig") as result:
            return result.read().strip()
    finally:
        try:
            os.unlink(outfile)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Repli — Tk dans un sous-processus
# --------------------------------------------------------------------------

def _tk_command(kind, initial, outfile, default_name=""):
    arguments = [PICK_FLAG, kind, initial or "", outfile, default_name or ""]
    if getattr(sys, "frozen", False):
        # Fige : re-executer l'application, que `run_picker_if_requested`
        # detourne avant qu'elle ne demarre un serveur.
        return [sys.executable] + arguments
    return [sys.executable, os.path.abspath(__file__)] + arguments


def _ask_tk(kind, initial, default_name=""):
    handle, outfile = tempfile.mkstemp(prefix="live_notes_pick_", suffix=".txt")
    os.close(handle)
    try:
        completed = _run(_tk_command(kind, initial, outfile, default_name))
        if completed.returncode != 0:
            raise PickerError((completed.stderr or "Tk a echoue").strip())
        with open(outfile, encoding="utf-8") as result:
            return result.read().strip()
    finally:
        try:
            os.unlink(outfile)
        except OSError:
            pass


def _tk_dialog(kind, initial, default_name=""):
    """Ouvre reellement la boite Tk. Ne tourne que dans le sous-processus."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if kind == "directory":
            return filedialog.askdirectory(title="Dossier de destination",
                                           initialdir=initial or None) or ""
        if kind == "save":
            return filedialog.asksaveasfilename(title="Enregistrer le projet",
                                                initialdir=initial or None,
                                                initialfile=default_name or None) or ""
        return filedialog.askopenfilename(title="Choisir un media",
                                          initialdir=initial or None) or ""
    finally:
        root.destroy()


def run_picker_if_requested(argv=None):
    """Detourne un lancement `<application> --live-notes-pick ...`.

    A appeler en tete du point d'entree, avant d'importer ou de demarrer quoi
    que ce soit : c'est ce qui empeche une application figee de demarrer un
    second serveur quand elle est re-executee pour ouvrir un selecteur. Ne rend
    la main que si la ligne de commande ne demande pas de selecteur.
    """
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 2 or argv[1] != PICK_FLAG:
        return
    kind = argv[2] if len(argv) > 2 else "file"
    initial = argv[3] if len(argv) > 3 else ""
    outfile = argv[4] if len(argv) > 4 else ""
    default_name = argv[5] if len(argv) > 5 else ""
    try:
        path = _tk_dialog(kind, initial, default_name)
    except Exception:
        # Le code de sortie porte l'echec : ecrire une trace sur une sortie
        # standard qui n'existe pas (console=False) ne menerait nulle part.
        sys.exit(1)
    if outfile:
        with open(outfile, "w", encoding="utf-8") as result:
            result.write(path or "")
    sys.exit(0)


# --------------------------------------------------------------------------
# Interface publique
# --------------------------------------------------------------------------

def _ask(kind, initial="", default_name=""):
    if sys.platform == "darwin":
        backends = (_ask_macos, _ask_tk)
    elif os.name == "nt":
        backends = (_ask_windows, _ask_tk)
    else:
        backends = (_ask_tk,)

    failure = None
    for backend in backends:
        try:
            return backend(kind, initial, default_name=default_name)
        except (PickerError, OSError, subprocess.SubprocessError) as exc:
            failure = exc
    raise PickerError(failure or "aucun selecteur disponible")


def ask_open_file(initial=""):
    """Chemin du fichier choisi, ou "" si l'utilisateur.ice a annule.

    `initial` est le dossier dans lequel le selecteur s'ouvre -- charge a
    l'appelant de decider lequel (dernier dossier utilise, bibliotheque
    Videos...), ce module ne fait qu'honorer le choix."""
    return _ask("file", initial)


def ask_directory(initial=""):
    """Chemin du dossier choisi, ou "" si l'utilisateur.ice a annule."""
    return _ask("directory", initial)


def ask_save_path(initial="", default_name=""):
    """Chemin complet du fichier a enregistrer, ou "" si l'utilisateur.ice a
    annule. `default_name` est le nom propose dans le dialogue."""
    return _ask("save", initial, default_name)


if __name__ == "__main__":
    # Seul usage direct de ce module : le sous-processus selecteur hors gel.
    run_picker_if_requested()
