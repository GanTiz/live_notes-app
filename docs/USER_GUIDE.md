# Guide utilisateur

Ce guide décrit le parcours complet dans live_notes : préparer une session,
dessiner, enregistrer, exporter, et travailler à deux avec une tablette.

## Lancement

```bash
python app.py
```

Puis ouvrir <http://127.0.0.1:7341> dans un navigateur (Chrome, Edge ou
Safari recommandés). L'adresse exacte s'affiche au démarrage : le port se
décale automatiquement s'il est déjà pris, et `LIVE_NOTES_PORT=8080 python
app.py` en impose un.

> Le port 5000 des versions précédentes a été abandonné : depuis macOS
> Monterey, le récepteur AirPlay l'occupe dès l'ouverture de session, et
> l'application ne s'affichait pas.

Le serveur écoute aussi sur le réseau local, pour permettre à une tablette de
se connecter (voir [Contrôle par tablette](#contrôle-par-tablette)) ; les
adresses utilisables s'affichent au démarrage dans le terminal.
`LIVE_NOTES_HOST=127.0.0.1 python app.py` referme le serveur sur la machine
seule, si le réseau n'est pas nécessaire.

## Setup

Au premier écran :

- **Média de fond** (optionnel) : vidéo, image, audio, ou une page de PDF.
  Le sélecteur natif du système s'ouvre — tous les formats de rush de
  montage sont acceptés (ProRes, DNxHD, MXF...), même ceux qu'aucun
  navigateur ne lit nativement : l'outil en prépare automatiquement une copie
  de lecture. Il s'ouvre dans le dernier dossier utilisé, ou à défaut dans
  le dossier `live_notes` de la bibliothèque Vidéos (Windows) / Films
  (macOS).
- **Résolution et FPS** de travail.
- **Export avec ou sans alpha** (fond transparent).

Ces réglages peuvent être changés à tout moment depuis la barre d'outils,
sans perdre le tracé déjà posé.

## Dessiner

- Bibliothèque de pinceaux en haut de l'écran (feutre, surligneur, texturé,
  crayon, gomme) — cliquer une vignette pour le sélectionner.
- Panneau de réglages à droite : taille, opacité, flux, dureté, espacement,
  aplatissement, angle, variations aléatoires, réponse à la pression du
  stylet. Les réglages sont mémorisés par pinceau.
- Import d'une texture PNG et enregistrement de presets personnalisés
  (bouton dans le panneau).
- Zoom / dézoom / déplacement de la vue : `Ctrl` + molette (ou pincement au
  trackpad) pour zoomer, glisser à deux doigts ou clic molette pour se
  déplacer, `Ctrl 0` pour revenir au cadrage complet.
- Fonctionne à la souris, au tactile ou au stylet (pression prise en
  compte).

## Enregistrer (REC)

Le bouton REC lance un décompte de 3 secondes, puis le média de fond démarre
(s'il y en a un) et chaque point de chaque trait est capturé avec son
timestamp. Une prévisualisation H.264 se rejoue en temps réel pour valider le
tracé — l'export final, lui, sera recalculé à partir des métadonnées, pas de
cette prévisualisation.

- **Points IN/OUT** : posés à la poignée sur la barre de progression ou en
  timecode, pour ne rejouer qu'une portion du média pendant le REC. Une borne
  désigne toujours une image entière : elle se cale sur la grille du média, et
  vaut la même des deux côtés — poste, tablette et export. En fin de plage, la
  lecture s'arrête sur la dernière image annotée, jamais sur la suivante.
- **Effacer pendant le REC** repart d'une toile vierge sans perdre
  l'enregistrement en cours : l'effacement devient un évènement de la
  timeline, rejoué à l'export comme le reste.
- **Avance image par image** (`❘◀` / `▶❘`) pour se positionner précisément
  hors REC.

## Fond du canevas et cadrage du média

Le fond (blanc, gris 50 %, noir, une couleur libre, ou le damier de
transparence) se choisit au setup et reste modifiable depuis la barre
d'outils ; le choix est mémorisé d'une session à l'autre.

Quand le format du média diffère de celui du canevas, une fenêtre de
cadrage (« Cadrage… » dans le bandeau média) propose :

- **Conserver le ratio d'origine** : le média entre en entier, sans rognage,
  et la couleur de fond choisie comble l'espace restant.
- **Recadrer** : la scène prend le format du canevas et *est* le cadre — le
  média glisse dedans (glisser + zoom) et ce qui dépasse est rogné. Ce que
  montre la fenêtre est exactement ce que donnera l'export.

## Enregistrer et rouvrir un projet (.lvn)

Les deux boutons de la barre d'outils enregistrent et rechargent un projet :
le tracé (sous forme de métadonnées, pas de pixels), le format du canevas, le
fond, et de quoi retrouver le média — son chemin, la page si c'est un PDF, son
cadrage et ses points IN/OUT.

Rouvrir un projet rouvre donc le média tout seul, à son dernier emplacement
connu, avec le cadrage et les bornes qui étaient les siens : le tracé retombe
en face de l'image sans rien avoir à refaire. Si le fichier a été déplacé,
renommé, ou vit sur un disque débranché, le chemin cherché est affiché et le
reste du projet est chargé quand même — il ne reste qu'à rouvrir le média à la
main.

Un média ouvert depuis le sélecteur du navigateur (le repli quand le sélecteur
natif n'est pas disponible) n'a pas de chemin connu du serveur : ce projet-là
ne peut pas le rouvrir seul.

## Exporter

L'export recalcule le tracé frame par frame à la résolution et au FPS choisis,
indépendamment de ce qui a été dessiné à l'écran — un tracé fait en 1920×1080
peut sortir en 4K sans perte. Trois types :

| Type | Fichier(s) | Ce qu'on y trouve |
| --- | --- | --- |
| **Aperçu H.264** | un `.mp4` | le tracé aplati, sans alpha — pour valider ou faire circuler |
| **ProRes 4444** | un `.mov` | le tracé, avec couche alpha ou aplati |
| **Export pro** | un dossier, trois fichiers | l'aperçu, le média, le tracé |

Pour les deux premiers, un seul réglage décide de **ce qui passe sous le
tracé** :

- **le média**, aplati sur la couleur d'arrière-plan — donc exactement ce que
  montrait le canevas, cadrage et fond compris ;
- **un fond uni** : la couleur d'arrière-plan seule, le média est écarté ;
- **rien** : le tracé sort sur du transparent. Réservé au ProRes 4444, seul
  format des deux à savoir porter un canal alpha.

Quand le média est un **son**, rien ne passe sous le tracé : la première option
devient « le son du média, sur le fond uni » — le fichier garde la bande son,
coupée aux points IN/OUT.

L'export est asynchrone (barre de progression réelle, annulable). Par
défaut, le fichier atterrit dans un dossier `live_notes/` de la bibliothèque
Vidéos (Windows) ou Films (macOS) ; un sélecteur de dossier natif permet de
choisir un autre emplacement et un autre nom à chaque export.

### Export pro : trois couches, un dossier

L'export pro dépose dans un sous-dossier — nommé d'après le média et horodaté
(`rush_20260802_143210`), renommable — trois fichiers construits sur le **même
canevas**, la **même cadence** et la **même première image** :

| Fichier | Format | Contenu |
| --- | --- | --- |
| `<nom>_preview.mp4` | H.264 | l'aperçu aplati : pinceaux + média + arrière-plan |
| `<nom>_media.mov` | ProRes 4444 | le média seul, cadré dans le canevas |
| `<nom>_trace.mov` | ProRes 4444 + alpha | le tracé seul, sur du transparent |

Un média qui n'est **que du son** suit le même plan : `<nom>_media.wav`
remplace la couche vidéo, coupé aux mêmes bornes et à la définition de la
source, et l'aperçu montre le tracé sur le fond uni avec la bande son.

Les trois fichiers reprennent le nom du dossier : l'horodatage les suit quand
ils sont importés dans un chutier, où ils perdent le contexte du dossier et où
deux essais du même rush se ressembleraient sinon.

Les reposer l'un sur l'autre au montage redonne l'aperçu, à la compression
près. L'intérêt est de garder les couches **indépendantes** : réétalonner le
rush, changer l'opacité du tracé ou le décaler d'un cran ne demande pas de
refaire le rendu. Sans média, l'export pro n'a rien à décomposer : l'option
est grisée.

Deux points méritent d'être connus :

- **La couche média peut garder son alpha.** Une case à cocher remplace la
  couleur d'arrière-plan par du vide autour du média. Un rush vertical dans un
  canevas horizontal ressort alors en horizontal, à son format d'origine,
  centré, avec du transparent de chaque côté — et recadré si on l'avait
  recadré.
- **La couche média dure aussi longtemps que le rush était affiché.** Elle part
  du point IN, joue jusqu'au point OUT, et si le tracé déborde — la lecture a
  bufferisé, le geste s'est terminé après le point OUT — elle prolonge la
  dernière image figée jusqu'à la fin du tracé, exactement comme l'aperçu. Les
  bornes restent un plancher : un tracé arrêté avant le point OUT ne raccourcit
  pas la couche.

### Synchronisation : le point IN est l'origine

Le tracé a été dessiné à partir du point IN : `t = 0` du tracé, c'est le point
IN du média. Toutes les couches partent donc de la même image, et il n'y a rien
à recaler.

Le **timecode de départ** le dit explicitement : un point IN posé à
`00:00:12:04` devient le timecode de la première image des trois fichiers.
Poser les couches dans un montage à ce timecode suffit à les aligner, et à les
retrouver au bon endroit sous le rush d'origine.

Un WAV n'a pas de piste de timecode ; le `bext` des Broadcast Wave en tient
lieu. La couche son porte donc un `time_reference` — le point IN, exprimé en
échantillons — qui est la même information dans la seule forme qu'un fichier
son sache transporter.

Quand le média a une piste son, elle est reprise dans l'aperçu et dans la
couche média. Elle ne sert pas au montage, qui a le rush d'origine, mais à
vérifier d'un coup d'oreille que la couche tombe où l'on croit. Une couche son
livrée seule, elle, est faite pour le montage : elle sort à la définition de la
source — une source PCM ressort dans sa propre variante, une source lossless
garde ses 24 bits, et seul un codec avec perte retombe sur 16 bits, faute
d'avoir une définition propre.

## PDF comme média de fond

Ouvrir un PDF par le même sélecteur que n'importe quel média : une fenêtre
demande ensuite quelle page (aperçu, navigation page par page). La page
choisie est rasterisée côté serveur et se comporte ensuite exactement comme
une image — mêmes options de cadrage, de fond, de publication vers la
tablette. Le bouton **Page…** du bandeau média permet d'en changer sans
resélectionner le fichier.

## Gestion des copies de lecture (proxys)

Les rushes illisibles tels quels par un navigateur (4:2:2 10 bits, codecs
exotiques...) sont automatiquement transcodés en une copie de lecture,
conservée en cache pour que les réouvertures suivantes soient instantanées.
Le bouton **Proxys** (bas de la barre d'informations) ouvre un panneau pour
consulter le poids total, la liste des rushes préparés, et faire le ménage
plan par plan ou d'un coup — sans jamais toucher au fichier source.

Deux corrections manuelles y sont aussi disponibles si le verdict automatique
se trompe (un rush affiché tel quel qui reste noir à l'écran, ou l'inverse) :
**Transcoder ce rush** et **Servir l'original**.

## Contrôle par tablette

Une tablette du réseau local peut prendre la main sur le dessin pendant que
le poste reste maître du rush et de l'export.

1. **Tablette…** dans la barre d'outils affiche un QR code.
2. La tablette le scanne et rejoint la session — même format de travail,
   même bibliothèque de pinceaux, même média — et prend la main.
3. Le poste passe en **spectateur** : son canevas devient une fenêtre sur ce
   que dessine la tablette, en direct.
4. **Reprendre la main** rend le stylet au poste ; la tablette reste
   connectée et devient suiveuse.
5. **Rendre la main à la tablette** refait le trajet inverse, autant de fois
   que nécessaire.
6. **Terminer la session** rompt le lien : la tablette affiche que la
   session est terminée, et son jeton d'appairage est invalidé.

Une fois la tablette connectée, la fenêtre du QR code se referme d'elle-même
sur le poste : l'appairage a abouti, le canevas reprend l'écran. Le bouton
**Tablette…** la rouvre à tout moment pour reprendre le stylet.

Ce qui circule sur le réseau, ce sont les gestes (points, pression, pinceau),
jamais des pixels — quelques kilooctets par seconde, pas un flux vidéo.

### L'écran de la tablette

Sur la tablette, les barres d'outils disparaissent au profit de bulles
flottantes posées sur le canevas : pinceaux en haut à gauche, contrôles
d'enregistrement en haut au centre, zoom en haut à droite, transport en bas à
gauche. Elles s'effacent à demi après deux secondes d'inaction, et laissent
passer le stylet pendant un tracé.

Deux boutons à droite : **PC** rend la main au poste, et **⤢** passe en plein
écran pour récupérer la hauteur de la barre d'adresse et des onglets. Pour
s'en passer définitivement, ajouter la page à l'écran d'accueil : elle
s'ouvrira alors comme une application, sans barre du tout (le bouton
disparaît, devenu inutile).

Si l'appairage échoue, l'interface explique pourquoi (serveur injoignable,
session terminée, ou adresse réseau utilisée pour une action réservée au
poste) plutôt que d'afficher un écran vide.

## Dépannage

- **Un rush reste noir sans message d'erreur** : cas typique du HEVC sans
  décodage matériel sur la machine. Utiliser **Transcoder ce rush** dans le
  panneau Proxys.
- **FFmpeg introuvable au lancement** : vérifier qu'il est dans le `PATH`,
  ou définir `LIVE_NOTES_FFMPEG` / `LIVE_NOTES_FFPROBE`.
- **La tablette n'arrive pas à s'appairer** : elle doit être sur le même
  réseau local que le poste ; le message affiché dans la fenêtre
  d'appairage indique la cause précise.
- **La tablette tourne indéfiniment sur une page qui ne charge pas** (alors
  que le poste répond au ping) : c'est le pare-feu Windows. Il *jette* les
  connexions entrantes sans répondre — d'où le chargement sans fin plutôt
  qu'un message d'erreur. L'installateur pose la règle nécessaire si la case
  « Autoriser le mode tablette dans le pare-feu Windows » est cochée ; sinon,
  dans un terminal **administrateur** :

  ```
  netsh advfirewall firewall add rule name="live_notes (mode tablette)" dir=in action=allow program="%LOCALAPPDATA%\Programs\live_notes\live_notes.exe" enable=yes profile=private
  ```

  La règle porte sur le programme, pas sur un port : elle reste valable même
  si le port se décale.
- **Sur macOS**, le problème ne se pose pas de la même façon : le pare-feu y
  est désactivé par défaut, et lorsqu'il est actif il raisonne par
  **application** et non par port — il demande une fois « Voulez-vous
  autoriser live_notes à accepter les connexions entrantes ? ». Répondre
  *Autoriser* suffit, et le changement de port n'y change rien. En cas de
  refus initial, la case se corrige dans Réglages Système → Réseau →
  Pare-feu → Options.
