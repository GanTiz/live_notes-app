# live_notes

Un utilitaire local (100 % hors-ligne) pour annoter des médias — rushes vidéo,
images, PDF, audio — au dessin, en direct, et exporter le tracé animé en
**ProRes 4444 avec canal alpha** pour l'intégrer dans un montage professionnel.

Pensé pour une réalisatrice ou un·e monteur·euse qui veut annoter ses rushes
pendant le visionnage et récupérer un calque de dessin propre, prêt à poser
sur le montage — pas une capture d'écran, un vrai rendu recalculé image par
image.

## Fonctionnalités

- **Dessin** : pinceaux (feutre, surligneur, texturé, crayon, gomme),
  entièrement paramétrables — forme, taille, opacité, flux, dureté,
  variations aléatoires, réponse à la pression du stylet — avec presets
  personnalisés.
- **N'importe quel média de fond** : vidéo (tout codec, y compris ProRes/DNxHD
  via une copie de lecture automatique), image, audio, ou une page de PDF
  rasterisée à la volée.
- **Export fidèle** : le tracé est enregistré comme une **description**
  (points horodatés, pression, pinceau) et rejoué frame par frame à l'export —
  la résolution et le FPS de sortie sont indépendants de ce qui a été dessiné
  à l'écran, sans perte de qualité.
- **Export en couches** : aperçu H.264 aplati, ProRes 4444 (avec ou sans
  alpha), ou **export pro** — un dossier de trois fichiers (aperçu, média,
  tracé) partageant canevas, cadence et timecode de départ, à réempiler au
  montage sans recalage. Un média qui n'est que du son y donne une couche WAV
  coupée aux mêmes bornes.
- **Contrôle par tablette** : une tablette du réseau local peut prendre la
  main sur le dessin pendant que le poste reste maître du rush et de
  l'export, appairage par QR code.
- **Espace colorimétrique correct** : travail en sRGB, export en Rec. 709
  (gamma 2.4) — la couleur affichée à l'écran correspond à celle du fichier
  livré.

Le détail de l'usage (setup, dessin, tablette, export) est dans le
**[guide utilisateur](docs/USER_GUIDE.md)**.

## Installation

Nécessite Python 3.12+, ainsi que **FFmpeg et FFprobe** installés et
accessibles dans le `PATH` (ou pointés par les variables d'environnement
`LIVE_NOTES_FFMPEG` / `LIVE_NOTES_FFPROBE`).

```bash
pip install -r requirements.txt
python app.py
```

Puis ouvrir <http://127.0.0.1:7341> — le démarrage affiche l'adresse exacte,
qui se décale si le port est déjà occupé.

Des builds packagés (Windows `.exe`, macOS `.dmg`, FFmpeg inclus, aucune
installation de Python requise) sont publiés dans l'onglet
[Releases](../../releases) — voir [l'état de leur validation](docs/DEVELOPMENT.md#releases--packaging).

## Structure du dépôt

```
index.html + static/app.js     interface, dessin, capture des métadonnées
static/transport.js            lecteur média (timecode, scrubbing, IN/OUT)
static/remote.js               liaison temps réel poste ↔ tablette (WebSocket)
static/brushes.js  ─┐
                    ├── même format de pinceau, même RNG, même algorithme
brush_engine.py    ─┘   → l'aperçu à l'écran correspond au rendu final
renderer.py                    rejeu frame par frame, compositing, pipe FFmpeg
colorspace.py                  conversion sRGB → Rec. 709 à l'encodage
proxy.py                       copie de lecture d'un rush (n'importe quel codec)
pdfdoc.py                      rasterisation d'une page de PDF en image
session.py                     état partagé, appairage, relais des gestes
app.py                         API Flask (export asynchrone, presets, session)
brushes.json                   bibliothèque de pinceaux (source de vérité unique)
packaging/                     build desktop Windows/macOS (voir son README)
```

Pour l'architecture détaillée, les décisions de conception et le
fonctionnement du pipeline de build, voir le
**[guide développeur](docs/DEVELOPMENT.md)**.

## Tests

```bash
py test_colorspace.py   # conversion sRGB → Rec. 709
py test_export.py       # couches d'export : cadrage, bornes, synchronisation
py test_proxy.py        # transcodage / lisibilité des rushes, PDF
py test_session.py      # appairage tablette, publication de la config
py test_ui.py           # parcours complet dans un vrai navigateur (CDP)
```

## Feuille de route

Voir [ROADMAP.md](ROADMAP.md).

## Licence

Code sous [GPLv3](LICENSE) — Copyright (C) 2026 HOKO Productions.

Les builds packagés (installateur Windows, `.dmg` macOS) embarquent un
binaire FFmpeg compilé avec des composants sous licence GPL et sont donc
distribués sous GPLv3 également ; détail des licences tierces dans
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
