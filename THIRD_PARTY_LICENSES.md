# Licences tierces

## FFmpeg

Les packages desktop (Windows/macOS) de live_notes embarquent un binaire
FFmpeg statique, invoque en sous-processus (ligne de commande + pipes) par
`renderer.py` — il n'est pas lie (linked) au code de l'application.

FFmpeg est publie sous LGPL 2.1+, avec des composants optionnels (dont
libx264, utilise ici pour l'encodage H.264 de previsualisation) sous GPL
2+. Les binaires embarques par live_notes incluent ces composants GPL et
sont donc distribues sous **GPLv3**.

- Site du projet : <https://ffmpeg.org/>
- Texte de la licence : <https://ffmpeg.org/legal.html>, <https://www.gnu.org/licenses/gpl-3.0.html>
- Code source correspondant aux binaires embarques :
  - Windows : build "release essentials" — <https://www.gyan.dev/ffmpeg/builds/> (scripts et provenance des sources documentes sur cette page)
  - macOS (arm64) : build — <https://ffmpeg.martin-riedl.de/>
  - Source amont FFmpeg (toutes plateformes) : <https://github.com/FFmpeg/FFmpeg>

Conformement au GPL, ce fichier vaut offre de fournir/pointer vers les
sources correspondant aux binaires distribues avec chaque release ; en cas
de doute sur la version exacte embarquee dans un package donne, se referer
au numero de version affiche par `ffmpeg -version` dans ce package, et
contacter contact@hoko-productions.com pour toute demande de source.

## PDFium (via pypdfium2)

Utilise pour rasteriser une page de PDF en image de fond (voir `pdfdoc.py`).
Contrairement a FFmpeg, cette bibliotheque n'est **pas** un sous-processus :
elle est chargee dans le process de l'application et donc **liee** a elle.
C'est precisement pour cette raison qu'elle a ete retenue plutot que PyMuPDF,
qui est sous AGPL et dont le caractere lie aurait contamine l'application
distribuee.

- `pypdfium2` (l'enveloppe Python) : Apache-2.0 ou BSD-3-Clause, au choix.
- `PDFium` (le moteur, embarque sous forme de binaire dans la roue) :
  BSD-3-Clause, avec des composants sous licences compatibles (dont FreeType et
  zlib). Aucune de ces licences n'impose de reciprocite sur l'application.

- Enveloppe Python : <https://github.com/pypdfium2-team/pypdfium2>
- Moteur PDFium : <https://pdfium.googlesource.com/pdfium/>
- Binaires embarques et leur provenance :
  <https://github.com/bblanchon/pdfium-binaries>

## pywebview

Utilise pour la fenetre desktop (build packaging uniquement, absent de
l'usage serveur local `python app.py`). Licence BSD-3-Clause.

- <https://github.com/r0x0r/pywebview>
