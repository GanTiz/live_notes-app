# Packaging desktop (Windows / macOS)

Terrain prepare sur la branche `packaging`.

## Contenu

- `.github/workflows/release.yml` — build Windows (installateur Inno Setup)
  et macOS (.dmg, **une par architecture** : `arm64` pour Apple Silicon,
  `x64` pour Intel ; contient un raccourci `/Applications` a cote de l'app
  pour le glisser-deposer standard) sur tag `v*.*.*` ou declenchement
  manuel ; publie une release GitHub en **brouillon** (rien n'est visible
  publiquement sans validation manuelle derriere).
- `packaging/launcher.py` — point d'entree desktop : lance `app.py` (Flask)
  dans un thread, puis ouvre une fenetre dessus au lieu du navigateur
  systeme. Bibliotheque differente selon l'OS (voir plus bas pourquoi) :
  `pywebview` (WKWebView) sur macOS, Chrome/Edge en mode "app" via
  sous-processus sur Windows.
- `packaging/live_notes.spec` — spec PyInstaller, build **onedir** (pas
  onefile — voir plus bas pourquoi).
- `packaging/requirements.txt` — dependances du build desktop uniquement
  (`pywebview`, `pyinstaller`), separees de `requirements.txt`.
- `packaging/requirements-macos.txt` — memes dependances applicatives que
  `requirements.txt`, mais avec `numpy<2` et `pypdfium2<=5.9.0` (voir
  "numpy sur macOS" et "pypdfium2 est lui aussi epingle" ci-dessous). Utilise
  par le job `build-macos` pour les **deux** architectures (arm64 et Intel) ;
  Windows et le mode serveur restent sur `requirements.txt`.
- `THIRD_PARTY_LICENSES.md` (racine du depot) — notice GPL pour le binaire
  FFmpeg embarque.
- `paths.py` (racine du depot) — resolution des chemins, partagee par
  `app.py`/`brush_engine.py`/le lanceur desktop (voir ci-dessous).

FFmpeg n'est **pas** commite dans le depot : le workflow le telecharge a
chaque build (gyan.dev pour Windows, martin-riedl.de pour macOS, dans
l'architecture du runner) dans `packaging/vendor/ffmpeg/`, un dossier
ignore par git.

## Deux builds macOS : pourquoi

PyInstaller ne produit un binaire que pour l'architecture de la machine
qui compile, et un Mac Intel refuse de lancer un `.app` arm64 — avec un
message qui n'aide pas (« impossible d'ouvrir l'app car elle n'est pas
prise en charge par ce Mac »). D'ou un runner par cible plutot qu'une
compilation croisee, qui exigerait un Python et des roues `universal2`
pour toutes les dependances.

Le job verifie explicitement l'architecture (`lipo -archs`) du lanceur
**et** de FFmpeg avant d'empaqueter : rien d'autre ne signalerait une
archive FFmpeg telechargee pour la mauvaise plateforme, et l'erreur ne
se manifesterait qu'a l'export, chez l'utilisateur.ice.

## numpy sur macOS : pourquoi `numpy<2` (les deux architectures)

Le bundle vise macOS **11+** (Big Sur). Sur un vrai Mac plus ancien, le build
crashait au lancement :

```
ImportError: dlopen(.../numpy/_core/_multiarray_umath...): Symbol not found:
(_cblas_caxpy$NEWLAPACK$ILP64)
Expected in: /System/Library/Frameworks/Accelerate.framework/Versions/A/Accelerate
```

`numpy>=2` publie des roues macOS `macosx_14_0_arm64/x86_64` qui lient leur
BLAS via de nouveaux symboles Accelerate absents avant macOS 14. Sur un
runner macOS 14/15 (le `macos-latest` arm64, et le `macos-15-intel`), pip
choisit cette roue : le bundle qui en resulte crash chez l'utilisateur.ice
sur macOS 12/13. Le smoke test CI ne le voit pas, il tourne sur un Mac
recent qui a les bons symboles — le crash n'apparait que chez l'utilisateur.
ice, d'ou l'absence d'alerte avant un test manuel reel (constate sur
Monterey 12.7.6 en Intel, et sur un Apple Silicon sous Ventura 13).

`packaging/requirements-macos.txt` epingle `numpy<2` pour contourner ca, sur
les deux builds macOS : la 1.26 (derniere branche 1.x) n'embarque que des
roues OpenBLAS (`macosx_11_0_arm64` / `macosx_10_9_x86_64`), compatibles du
macOS 11 au plus recent. Windows et le mode serveur gardent `numpy>=1.24`
(donc potentiellement 2.x) via `requirements.txt`. Si une prochaine version
de numpy 2.x corrige la compatibilite Accelerate sur les anciens macOS, cette
epingle pourra etre retiree — a re-tester sur un vrai Mac 11/12/13 avant de
le faire, le smoke test CI ne le detecterait pas.

### pypdfium2 est lui aussi epingle (`<=5.9.0`)

Depuis la 5.10, les roues macOS de pypdfium2 exigent macOS 13
(`macosx_13_0_*`). Sans epingle, le build embarquerait un PDFium qui refuse
de charger sur un Mac plus ancien — exactement le meme type de bug que numpy,
et le garde-fou `minos <= 11.0` ci-dessous le bloquerait de toute facon. La
5.9 est la derniere a publier des roues `macosx_11_0_arm64/x86_64` ;
l'API utilisee par `pdfdoc.py` (`PdfDocument`, `page.render(scale)`,
`bitmap.to_pil()`) est stable de la 4.x a la 5.9.

### Deux garde-fous contre une regression silencieuse

1. **Au build** (`release.yml`, job `build-macos`) : une etape passe chaque
   binaire du bundle (`vtool -show-build`, champ `minos:`) et echoue si un
   binaire declare un macOS minimal superieur a 11.0. C'est ce qui aurait
   bloque la v1.0.1, et ca couvre aussi ffmpeg/ffprobe, Pillow, pypdfium2 et
   pyobjc, pas seulement numpy. Ce test ne remplace pas un lancement sur un
   vrai macOS 11 : il garantit que les binaires *declarent* la compatibilite,
   pas que le systeme se comporte pareil partout.
2. **Au demarrage** (`nativedialog.refuse_unsupported_macos`, appele en tete
   de `packaging/launcher.py`) : sur un macOS < 11, l'application s'arrete
   avec une alerte explicite (`osascript`) au lieu de crasher muettement a
   l'import numpy. Une version macOS illisible est laissee passer. macOS
   10.13+ peut fonctionner (toutes les roues embarquees le permettent) mais
   n'est ni teste ni garanti — Apple a arrete ces systemes, aucun runner CI
   ne peut les verifier, et le WebKit de WKWebView y est ancien.

## Resolution des chemins une fois le code fige (`paths.py`)

`app.py` et `brush_engine.py` resolvaient auparavant leurs chemins
(`index.html`, `static/`, `brushes.json`, `user_presets.json`, dossier
d'export par defaut) via `os.path.dirname(os.path.abspath(__file__))` —
ce qui casse une fois le code fige par PyInstaller (`__file__` ne pointe
plus a cote de l'executable livre). C'est maintenant centralise dans
`paths.py` (racine du depot) :

- **`resource_dir()`** — dossier des assets embarques en lecture seule.
  Utilise `sys._MEIPASS`, que PyInstaller renseigne correctement aussi
  bien en onedir qu'en onefile (contrairement a `__file__`). Utilise aussi
  pour trouver le binaire FFmpeg vendorise depuis `packaging/launcher.py`.
- **`user_data_dir()`** — dossier ecrivable et stable pour
  `user_presets.json`. En mode fige : `%APPDATA%\live_notes` (Windows) ou
  `~/Library/Application Support/live_notes` (macOS), independant de
  l'endroit ou l'utilisateur.ice pose le `.exe`/le dossier de l'app. En
  mode developpement (`python app.py`), comportement inchange : a cote du
  code source.
- **`default_export_dir()`** — dossier d'export **propose par defaut**,
  desormais la bibliotheque Videos (`%USERPROFILE%\Videos`, via l'API
  Windows `SHGetKnownFolderPath`/`FOLDERID_Videos` pour gerer la
  localisation et un deplacement de bibliotheque) ou Movies (`~/Movies`
  sur macOS) de l'utilisateur.ice, dans un sous-dossier `live_notes/`.
  Reste modifiable a chaque export via le selecteur de dossier natif
  (`/api/browse`) — ce changement ne touche que la valeur proposee par
  defaut, pas la possibilite de choisir un autre dossier.

`app.py` passe aussi explicitement `root_path=HERE` (= `paths.resource_dir()`)
au constructeur Flask : sans ca, la detection automatique du `root_path`
par Flask repose elle aussi sur `__file__` et casserait de la meme facon.

## Fenetrage Windows : pourquoi pas pywebview

`pywebview` est utilise sur macOS (WKWebView, via le pont `pyobjc`), mais
pas sur Windows : son backend `edgechromium` pilote WebView2 via
`pythonnet`/`clr`, dont le chargement casse de facon connue et non
resolue une fois fige par PyInstaller (`RuntimeError: Failed to resolve
Python.Runtime.Loader.Initialize`, verifie par un build local — voir
r0x0r/pywebview#1215, #1292, #1638). Le paquet `flaskwebgui`, qui fait la
meme chose sans passer par pywebview, a aussi ete ecarte : sa version
resolue par pip sous Python <3.12 contient elle-meme un `SyntaxError`
(f-string imbriquee, syntaxe reservee a 3.12+).

A la place, `packaging/launcher.py` lance Chrome ou Edge (le premier
trouve via le registre Windows, `App Paths`) en mode `--app=<url>` (sans
barre d'adresse, comme une PWA installee), dans un profil dedie pour
eviter que Chromium ne delegue simplement l'ouverture a une instance deja
lancee. Edge etant desinstallable (pas garanti present), Chrome est
cherche en premier.

## Build local (test avant de passer par la CI)

```bash
pip install -r requirements.txt -r packaging/requirements.txt
python packaging/launcher.py   # sans PyInstaller : verifie juste le lanceur
```

Pour un vrai build fige local, placer un binaire ffmpeg dans
`packaging/vendor/ffmpeg/ffmpeg(.exe)` puis :

```bash
pyinstaller packaging/live_notes.spec --noconfirm
```

## Etat de la validation

Verifie (CI `workflow_dispatch` + build local Windows + usage reel) :
- URLs de telechargement FFmpeg (Windows et macOS).
- Build PyInstaller sur les deux OS ; `sys._MEIPASS` avec la structure
  `_internal/` (Windows) et le bundle `.app` (macOS).
- Windows : ouverture de la fenetre Chrome/Edge en mode app, export video
  de bout en bout, installateur (compilation, installation, entree dans
  "Applications installees", desinstallation, survie des donnees).
- macOS : demarrage reel de `live_notes.app`, serveur joignable, binaire
  FFmpeg present et executable dans `Contents/Frameworks` (= l'emplacement
  que `packaging/launcher.py` consulte, pas seulement "quelque part dans
  le bundle") ; montage du `.dmg` et lancement depuis le volume.

Reste a valider :
- **macOS 11 en vrai** : aucun runner GitHub n'existe sous macOS 14 (le
  `macos-13` a ferme le 4 decembre 2025). Le garde-fou `minos <= 11.0` et le
  smoke test (sur macOS 15) ne garantissent pas le comportement runtime d'un
  vrai macOS 11. A verifier une fois a la main sur un Mac Big Sur avant une
  release publique — c'est la que le crash numpy de la v1.0.1 a ete decouvert
  et que les deux dialogues cotes fenetre (sauvegarde de projet et nom de
  preset) ont montre leurs defauts.
- **macOS 10.13+** : potentiellement compatible mais non teste ni garanti
  (voir la section numpy sur macOS).
- **Les deux dialogues de la fenetre macOS** : `saveProject` passe desormais
  par un dialogue natif (`POST /api/project/save`) et le nom de preset par
  une modale interne (`#prompt-screen`) — `window.prompt` et le `<a download>`
  ne marchent pas dans WKWebView. A reverifier une fois a la main sur Mac.
- **Affichage de la fenetre macOS** : le smoke test CI tourne sans ecran,
  il confirme que l'app demarre et sert l'interface mais pas que la
  fenetre `pywebview`/WKWebView s'affiche. A verifier une fois a la main
  sur un vrai Mac avant une release publique.
- **Export video sur macOS** (teste sur Windows uniquement).
- **Le build Intel (`macos-15-intel`)** : `macos-13` a ferme le 4 decembre
  2025 (le job est reste bloque en file d'attente sans jamais demarrer,
  confirmant l'arret). Bascule sur `macos-15-intel`, la derniere image
  x86_64 de GitHub Actions, garantie jusqu'a fin aout 2027. Passe cette
  date, GitHub a annonce l'arret complet de l'architecture x86_64 sur
  macOS ; la seule voie restante pour les Mac Intel sera alors un binaire
  `universal2` (Python et roues universelles pour toutes les dependances).
- **Le job `release`** : n'a jamais tourne, il faut un vrai tag `v*.*.*`.
  Il publie un brouillon, donc rien ne devient public sans validation.
