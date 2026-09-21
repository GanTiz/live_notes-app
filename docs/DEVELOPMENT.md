# Guide développeur

Ce document est destiné à quiconque veut comprendre le fonctionnement interne
de live_notes, contribuer, ou reprendre le projet. Pour l'usage de
l'application, voir le [guide utilisateur](USER_GUIDE.md).

## Stack

- **Front-end** : HTML/CSS/JS vanilla — aucun framework, aucun build (pas de
  bundler, pas de transpilation).
- **Back-end** : Python (Flask) + numpy + Pillow.
- **Export vidéo** : FFmpeg, appelé en sous-processus (pipe de pixels bruts,
  pas de fichier intermédiaire).
- **Pas de base de données** : les presets de pinceaux sont persistés en JSON
  local (`user_presets.json`).

## Structure du dépôt

| Fichier | Rôle |
| --- | --- |
| `index.html` + `static/app.js` | interface, dessin, capture des métadonnées de tracé |
| `static/transport.js` | lecteur média maison (timecode, scrubbing, IN/OUT) |
| `static/remote.js` | liaison temps réel poste ↔ tablette (WebSocket) |
| `static/brushes.js` | moteur de pinceaux, aperçu live dans le navigateur |
| `brush_engine.py` | même moteur de pinceaux, miroir exact du précédent, utilisé au rendu final |
| `renderer.py` | rejeu frame par frame, compositing alpha, pipe vers FFmpeg |
| `colorspace.py` | conversion sRGB → Rec. 709 à l'encodage |
| `proxy.py` | copie de lecture d'un rush illisible par un navigateur |
| `pdfdoc.py` | rasterisation d'une page de PDF en image |
| `session.py` | état partagé, appairage tablette, relais des gestes |
| `paths.py` | résolution des chemins, valable en dev comme une fois figé par PyInstaller |
| `app.py` | API Flask (export asynchrone, presets, session, proxys) |
| `brushes.json` | bibliothèque de pinceaux, source de vérité unique |
| `packaging/` | build desktop Windows/macOS — voir [packaging/README.md](../packaging/README.md) |

## Lancer en développement

```bash
pip install -r requirements.txt
python app.py
```

FFmpeg et FFprobe doivent être dans le `PATH`, ou pointés par
`LIVE_NOTES_FFMPEG` / `LIVE_NOTES_FFPROBE`.

## Tests

```bash
py test_colorspace.py   # conversion sRGB → Rec. 709, aller-retour fichier
py test_export.py       # couches d'export : cadrage, bornes IN/OUT, empilement
py test_launch.py       # choix du port, sélecteur de fichier natif (bugs de compilation)
py test_proxy.py        # transcodage, lisibilité, cache, rasterisation PDF
py test_session.py      # appairage tablette, bornage et publication de la config
py test_ui.py           # parcours complet dans un vrai navigateur (Chrome DevTools Protocol)
```

Ces bancs tournent aussi en intégration continue
(`.github/workflows/tests.yml`), mais **sur le dépôt public seulement** :
le crédit de minutes de runner est épuisé sur les dépôts privés, un dépôt
public dispose de runners gratuits. Le fichier est identique dans les deux
dépôts, comme `release.yml` — chaque job est gardé par `github.repository`, et
un job *skipped* ne consomme aucune minute. Aucun déclencheur sur un tag :
pousser une branche sur le dépôt public ne peut pas produire de release.

`test_ui.py` pilote un vrai navigateur sans interface (Chrome ou Edge) — les
autres bancs s'arrêtent au bord du navigateur et ne vérifient que ce que le
serveur répond, jamais ce que la page en fait. Sans Chrome ni Edge sur la
machine, ce banc le signale et ne prétend rien avoir vérifié
(`LIVE_NOTES_CHROME=<chemin>` pour en désigner un explicitement).

## Architecture : du tracé à l'export

Le navigateur ne rasterise rien pour l'export. Il enregistre la
**description** du tracé — points horodatés, pression, pinceau, graine
aléatoire — et c'est Python qui la rejoue frame par frame, en écrivant des
pixels bruts RGBA directement dans FFmpeg via un pipe. Conséquence directe :
la résolution et le FPS de sortie sont indépendants de ce qui a été dessiné à
l'écran, puisque le tracé est **recalculé**, jamais redimensionné.

`static/brushes.js` (aperçu écran) et `brush_engine.py` (rendu final)
partagent le même format de pinceau, le même RNG et le même algorithme —
c'est ce qui garantit que ce qu'on voit sous le stylet correspond exactement
à ce que produit l'export.

La conversion colorimétrique (`colorspace.py`) n'a lieu qu'à la toute
dernière étape, sur la couleur démultipliée, juste avant l'écriture des
octets dans FFmpeg. Le compositing lui-même reste en sRGB comme le canvas du
navigateur — c'est ce qui garantit que l'aperçu à l'écran ne diverge jamais
de l'export.

## Couches de dessin

Une couche est ce que le projet contenait déjà — des traces, des effacements,
une durée — mis dans une boîte nommée, plus le canevas où elle s'accumule.
**Aucun champ nouveau sur une trace** : le moteur de pinceaux n'est touché ni
côté navigateur ni côté Python.

Trois règles portent tout le reste :

1. **Une seule origine des temps pour le projet : le point IN du média.** Une
   couche n'a pas d'horloge à elle — le `t` de ses traces compte toujours
   depuis là. C'est ce qui permet d'ajouter un trait « à la douzième seconde »
   sur une prise déjà faite.
2. **L'ordre d'empilement l'emporte sur l'ordre chronologique.** Une trace de
   la couche 1 posée à `t = 5 s` passe *sous* une trace de la couche 2 posée à
   `t = 1 s`. Sur une toile unique rejouée chronologiquement, elle passerait
   dessus — c'est la seule chose qui distingue de vraies couches d'un
   empilement décoratif, et `test_export.py` vérifie les deux sens.
3. **« Verrouillée » et « pas active » sont la même chose.** Il y a toujours
   exactement une couche déverrouillée : un seul état à tenir, au lieu d'un
   cadenas par couche à garder cohérent avec lui.

Conséquence de la règle 2 : un tampon par couche, des deux côtés. Dans le
navigateur, chaque couche a son `<canvas>` et `redraw()` les compose dans
l'ordre — ce qui n'est pas encore fusionné (le trait sous le stylet, une trace
qui arrive de la tablette, une trace en cours de rejeu) se compose au-dessus de
**sa** couche, jamais au-dessus de la pile. Dans `renderer.py`, chaque couche a
son `_Sheet` (canevas `float32` premultiplié, file de traces, effacements), et
`_write_region` les empile avec `_stack_sheet`. Seules les couches non vides
sont allouées : à 4K, un tampon coûte 132 Mo.

Le **rejeu** (`makeReplay` / `feedReplay`) est un seul moteur pour deux usages :
la prévisualisation le fait tourner sur toutes les couches, le REC sur toutes
sauf l'active. C'est ce second cas qui fait la fonctionnalité — pendant qu'on
enregistre la couche 2, la couche 1 se rejoue animée dessous, calée sur le
média, sans quoi on dessinerait à l'aveugle au-dessus d'une image figée.
`feedReplay` dit si quelque chose a bougé, et `recTick` ne recompose que dans
ce cas : le stylet est prioritaire, et la plupart des images ne voient rien
arriver dans les couches du dessous.

Deux pièges propres à ce rejeu :

- **`recElapsed()` lit l'horloge du REC sans la poser**, là où `elapsed()` la
  pose quand elle manque. Poser l'origine à la première image d'animation qui
  passe la déplacerait avant que le rush n'ait présenté la sienne, et tout le
  tracé serait décalé.
- **Les canevas des couches rejouées doivent partir vides**, sans quoi leur
  état final resterait affiché par-dessus le rejeu naissant. Et à l'arrêt, elles
  sont reconstruites : le rejeu s'est arrêté en plein milieu, l'état qu'elles
  ont vraiment est l'état final.

La charge d'export porte `layers`. `renderer.payload_sheets()` lit
indifféremment cette forme et la forme à plat (`strokes` / `clears`) : une
charge produite par une version antérieure rend à l'identique, au bit près.
Même histoire pour le format projet, passé en **`.lvn` v2** : un fichier v1 se
relit comme une couche unique. Un fichier dont la `version` dépasse celle que
l'application connaît est annoncé comme tel plutôt que déclaré invalide — l'un
demande une mise à jour, l'autre un autre fichier.

Côté tablette, le poste structure et la tablette dessine : la pile voyage en
entier (`{t: 'layers', list, activeId}`) plutôt qu'en commandes, ce qui interdit
aux deux écrans de diverger sur une commande perdue. Le contenu, lui, ne voyage
pas — chaque écran a déjà le sien par les tracés relayés.

## Couches d'export

`renderer.py` produit deux sortes de fichiers, qui partagent le même
référentiel — le canevas de tracé — et la même origine des temps — le point IN
du média : `render` (le tracé, seul en alpha, aplati sur la couleur de fond ou
sur le média) et `render_media` (le média seul, placé dans ce même canevas).
L'export pro en enchaîne trois : aperçu, média, tracé.

Le placement du média n'est **pas** refait à la main : il est confié aux
filtres FFmpeg (`crop`, `scale`, `pad`), décrits par la même arithmétique que
le `fitBox()` du navigateur. Les expressions n'utilisent que `iw`/`ih`, donc
les dimensions réellement décodées — une vidéo portant une rotation dans ses
métadonnées est cadrée sur ce que le navigateur affichait, pas sur ce
qu'annonce ffprobe.

Un seul processus FFmpeg par couche : quand le tracé est aplati sur le média,
il arrive en alpha droit comme seconde entrée d'un `overlay` plutôt que de
passer par un fichier intermédiaire — qui coûterait un encodage et un décodage
entiers pour le même résultat. La couleur qui entoure un média hors format
passe par la même conversion que le fond sur lequel le tracé est aplati
(`background_hex`), sans quoi les deux moitiés de la même image de fond ne
s'accorderaient pas d'un pixel à l'autre.

C'est la **source** qui est encodée, jamais la copie de lecture — seule une
page de PDF fait exception, puisque rasterisée elle n'existe que sous cette
forme. Le chemin du média vient de la session, jamais de la requête : rien ne
doit permettre de faire encoder un fichier arbitraire du disque en passant par
la page.

Un média qui n'est que du son donne une couche WAV. Le ré-encodage PCM n'y est
pas évité même quand `-c:a copy` serait possible : la copie coupe au paquet,
donc jusqu'à une vingtaine de millisecondes à côté du point IN — sur une couche
dont la raison d'être est la synchronisation, c'est le seul détail qui ne se
rattrape pas. Un ré-encodage PCM → même PCM est bit à bit identique, et
échantillon-exact.

Une couche qui échoue ou qu'on annule emporte les précédentes : un dossier où
l'aperçu existe mais pas le tracé serait pris pour un export terminé.

`test_export.py` encode de vrais fichiers, les redécode et **recompose
lui-même** le tracé sur la couche média pour le comparer à l'aperçu livré ; il
vérifie aussi que le même empilement décalé d'une seule image est franchement
pire — c'est ce qui distingue « les couches se superposent » de « les couches
se ressemblent ».

## Contrôle par tablette

Le serveur tient deux états séparés : `mode` dit si une tablette est
connectée, `controller` dit laquelle des deux parties (poste ou tablette)
dessine actuellement. Un seul émetteur envoie ses gestes à la fois ; l'autre
applique sans jamais renvoyer, ce qui interdit structurellement à un
aller-retour de reboucler. Le son suit le stylet, la tête de lecture du
suiveur est verrouillée, un seul enregistrement peut être actif à la fois.

Les routes qui touchent au disque (sélecteurs de fichiers, export) sont
réservées à la machine hôte et répondent `403` au reste du réseau. La
tablette doit présenter le jeton d'appairage (régénéré à chaque activation,
invalidé quand le poste termine la session) pour lire le média.

La liaison est surveillée par ping/pong plutôt que par l'état déclaré du
socket : `WebSocket.send()` ne signale rien quand le Wi-Fi tombe, il empile
les octets dans un tampon local, et un lien mort peut paraître ouvert
plusieurs dizaines de secondes.

## Proxys de rush

Un rush illisible par un navigateur (ProRes, DNxHD, 4:2:2 10 bits...) est
analysé (`probe`), un plan de transcodage est décidé (`plan`), puis exécuté
(`build`) — triptyque de `proxy.py`. Les copies de lecture sont conservées en
cache (`index.json` note la provenance de chaque fichier) ; une source
modifiée depuis invalide son proxy plutôt que de resservir une visée
périmée. Le seuil de débit qui déclenche un transcodage allégé
supplémentaire ne s'applique qu'en mode tablette — le poste lit toujours
depuis son disque.

Une page de PDF passe par le même triptyque (`pdfdoc.py` fait la
rasterisation elle-même) : une fois rasterisée, elle n'est plus un PDF pour
personne, et hérite gratuitement des mêmes options de cadrage que n'importe
quel autre média. La page **et** la définition entrent dans la signature de
cache — sans elles, changer de page ressortirait l'image déjà calculée pour
une autre.

## Pièges connus (pour qui modifie ce code)

- **Ne jamais passer le zoom du canevas par `transform: scale()`** : une
  couche promue par `transform`/`will-change` est composée par le navigateur
  dans un chemin colorimétrique différent du rendu normal, avec une perte de
  saturation visible. Le zoom agit sur la taille réelle du conteneur
  (largeur/hauteur en pixels).
- **Cadrage du média : jamais en `transform`, toujours en pourcentages du
  cadre** (`cropRect`), pour la même raison.
- **Le fond du canevas n'est pas du blanc en dur** : un export aplati l'est
  sur la couleur de fond choisie, elle-même passée par `colorspace.py` comme
  n'importe quelle autre couleur (un gris `#808080` s'écrit à 135, pas à 128,
  dans le fichier) — y compris quand c'est FFmpeg qui remplit les côtés d'un
  média hors format.
- **Ce que la boucle de rendu écrit dans le tuyau n'a pas toujours d'alpha** :
  il en faut un dès qu'il reste quelque chose à composer en aval (le média),
  sous peine de recouvrir ce média d'un aplat opaque ; il n'en faut pas quand
  le tracé est aplati sur le fond dans numpy. Deux notions distinctes de
  l'alpha du fichier livré, à ne pas confondre (`pipe_alpha` / `alpha_mode`).
- **`_open_media` est rappelé par plusieurs chemins** (réouverture après
  export, passage en mode tablette, retranscodage manuel) qui ne connaissent
  que le chemin du fichier, pas son contexte complet (page PDF notamment) —
  tous doivent relire la page courante du média, sous peine de retomber
  silencieusement sur la page 1.
- **Deux notions de « couche » cohabitent dans le code** : un *trace*
  (`makeTrace` / `feedTrace` / `commitTrace`) est la rasterisation d'un seul
  tracé dans son propre tampon, du posé du stylet au lâché ; une *couche*
  (`layers[]`, `_Sheet`) en contient plusieurs. Les deux avaient le même nom
  avant les couches, ce qui rendait le code illisible dès qu'on les mêlait.
- **swscale retombe sur BT.601 en dessous de la HD** sans consigne explicite
  de matrice, produisant un fichier étiqueté `bt709` mais encodé en 601.
  `colorspace.py` force la matrice, `test_colorspace.py` le vérifie par un
  aller-retour réel.

## Releases & Packaging

Le build desktop (installateur Windows, `.dmg` macOS arm64/x64, FFmpeg
embarqué, aucune installation de Python requise) est documenté en détail
dans [packaging/README.md](../packaging/README.md) : deux architectures
macOS et pourquoi, résolution des chemins une fois figé par PyInstaller
(`paths.py`), choix du fenêtrage par OS, épinglage `numpy<2` sur Mac Intel.

Déclenchement : un tag `v*.*.*` pousse une release GitHub en **brouillon**
(rien n'est publié sans validation manuelle), ou `workflow_dispatch` pour
tester un build sans tag.

### État de validation

Ce qui a été vérifié (CI `workflow_dispatch` + build local + usage réel) :
build PyInstaller sur les deux OS, résolution des chemins figés, ouverture
Windows en mode app Chrome/Edge, export vidéo de bout en bout sur Windows,
installateur Windows (compilation, installation, désinstallation), démarrage
de `live_notes.app` sur macOS, montage et lancement depuis le `.dmg`.

Ce qui **reste à valider** avant une release publique — à ne pas considérer
comme acquis :

- **Le job `release` complet n'a jamais tourné** : il n'a été déclenché ni
  par un vrai tag ni testé de bout en bout. La configuration a été relue et
  les noms d'artefacts/chemins sont cohérents entre les jobs, mais rien ne
  remplace un run réel.
- **L'affichage de la fenêtre macOS** (`pywebview`/WKWebView) : le smoke
  test CI tourne sans écran, il confirme que l'app démarre et sert
  l'interface, pas que la fenêtre s'affiche correctement.
- **L'export vidéo sur macOS** : testé sur Windows uniquement pour l'instant.

## Contribuer

Voir [CONTRIBUTING.md](../CONTRIBUTING.md).
