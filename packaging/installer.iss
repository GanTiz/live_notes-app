; Script Inno Setup — installateur Windows pour live_notes.
;
; Installation par utilisateur.ice (pas Program Files) : evite la demande
; d'elevation UAC, qui s'ajouterait a l'avertissement SmartScreen deja
; present tant que l'exe n'est pas signe (pas de signature en v1).
;
; Attend le build PyInstaller onedir deja produit dans dist\win\live_notes\
; (voir .github/workflows/release.yml, qui lance ISCC apres pyinstaller).

#define MyAppName "live_notes"
#define MyAppVersion GetEnv("LIVE_NOTES_VERSION")
#if MyAppVersion == ""
  #define MyAppVersion "0.0.0-dev"
#endif
#define MyAppPublisher "HOKO Productions"
#define MyAppExeName "live_notes.exe"

; Chemins relatifs a la racine du depot, pas au dossier de ce .iss :
; Inno Setup resout le relatif depuis l'emplacement du script.
#define RepoRoot ".."
; Surchargeable depuis la ligne de commande (ISCC /DMyAppSourceDir=...)
; pour pointer un build situe ailleurs (tests locaux hors du depot).
#ifndef MyAppSourceDir
  #define MyAppSourceDir RepoRoot + "\dist\win\live_notes"
#endif

[Setup]
AppId={{8F1C2C1E-2B0E-4C6A-9A9E-6B7C4A9C7B10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
UsePreviousAppDir=yes
OutputDir={#RepoRoot}\dist\installer
OutputBaseFilename=live_notes-setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile={#RepoRoot}\packaging\vendor\icons\icon.ico
ArchitecturesInstallIn64BitMode=x64compatible

; Installation par defaut sous le profil utilisateur.ice (pas de UAC) :
; {autopf} pointe vers Program Files, mais PrivilegesRequired=lowest bascule
; automatiquement DefaultDirName sur l'equivalent par utilisateur.ice.

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "Créer un raccourci sur le Bureau"; GroupDescription: "Raccourcis supplémentaires :"
; Coche par defaut : sans cette regle, le mode tablette ne fonctionne pas et
; rien ne l'explique (voir le bloc [Code] plus bas). Decochable pour qui ne
; s'en sert pas et prefere eviter l'elevation.
Name: "firewall"; Description: "Autoriser le mode tablette dans le pare-feu Windows (demande une élévation)"; GroupDescription: "Réseau local :"

[Files]
Source: "{#MyAppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Désinstaller {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Lancer {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Ne supprime pas %APPDATA%\live_notes (presets, profil navigateur) : un
; desinstallateur ne doit pas emporter les reglages sans le demander.

[Code]
{ ------------------------------------------------------------------------
  Autorisation pare-feu pour le mode tablette.

  Sans elle, le serveur ecoute bien sur le reseau local mais le pare-feu de
  Windows *jette* les connexions entrantes sans repondre : la tablette scanne
  le QR code et tourne indefiniment sur une page qui ne charge jamais, alors
  que la machine repond au ping. ICMP passe, TCP non -- et rien ne le dit.

  L'invite « autoriser cette application ? » n'apparait qu'a la premiere
  ecoute, et un refus (ou une fenetre fermee sans lire) laisse une regle de
  blocage silencieuse derriere elle. Trop fragile pour en dependre.

  La regle porte sur le programme et non sur un port : elle survit donc au
  decalage automatique du port quand celui par defaut est pris (voir
  app.choose_port). Profil « private » seulement : un reseau public n'a
  aucune raison d'atteindre le poste.

  Ajouter une regle demande des droits administrateur, que cet installateur
  n'a deliberement pas (PrivilegesRequired=lowest, pour eviter une UAC qui
  s'ajouterait a l'avertissement SmartScreen). D'ou une elevation ponctuelle
  via le verbe « runas », et seulement si la tache est cochee : qui n'utilise
  pas le mode tablette n'a aucune raison de voir une UAC.
  ------------------------------------------------------------------------ }

const
  FirewallRuleName = '{#MyAppName} (mode tablette)';

procedure SetFirewallRule(Add: Boolean);
var
  Params: String;
  ResultCode: Integer;
begin
  if Add then
    Params := 'advfirewall firewall add rule name="' + FirewallRuleName
      + '" dir=in action=allow program="'
      + ExpandConstant('{app}\{#MyAppExeName}') + '" enable=yes profile=private'
  else
    Params := 'advfirewall firewall delete rule name="' + FirewallRuleName + '"';

  { Un echec n'interrompt pas l'installation : le mode tablette est une
    fonctionnalite parmi d'autres, et l'application reste utilisable au poste.
    Le guide utilisateur donne la commande a passer a la main. }
  ShellExec('runas', 'netsh', Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('firewall') then
    SetFirewallRule(True);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { La regle est a nous : la desinstallation la reprend, sinon elle survit a
    l'application qu'elle autorisait. }
  if CurUninstallStep = usUninstall then
    SetFirewallRule(False);
end;
