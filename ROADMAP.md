# Feuille de route

## Fait

- Rendu 100 % Python à partir des métadonnées de tracé, pipe direct vers
  FFmpeg (pas d'image intermédiaire), gestion de l'alpha et du fond
  transparent.
- Bibliothèque de pinceaux multiples (feutre, surligneur, texturé, crayon,
  gomme), paramétrage complet, presets personnalisés.
- Interface révisée (charte gris/noir/blanc, sliders accessibles, panneau de
  pinceau dynamique, lecteur média maison avec IN/OUT en timecode, zoom/fit
  du canevas).
- Couleur de fond du canevas au choix, cadrage du média sans letterboxing
  imposé.
- Prévisualisation avant export : rejeu temps réel du tracé, synchronisé
  avec le média de fond.
- Ouverture d'une page de PDF comme média de fond.
- Export en couches : aperçu H.264, ProRes 4444, et export « pro » livrant
  aperçu + média + tracé dans un dossier, synchronisés par le point IN et le
  timecode de départ.
- Contrôle par tablette (appairage QR code, transfert de rôle, relais des
  gestes en temps réel).
- Couches de dessin : verrouiller une prise et travailler par-dessus, les
  couches verrouillées se rejouant animées sous le stylet. Une couche se
  masque (elle quitte alors aussi l'export) ou se supprime, la première comme
  la troisième.
- Annuler / rétablir (`Ctrl + Z`, `Ctrl + Maj + Z`) — sans limite de nombre,
  la profondeur étant la couche active.
- Zoom au pincement à deux doigts sur écran tactile, déplacement compris.
- Raccourcis clavier et souris : taille et dureté du pinceau au glisser,
  gomme à la volée (`E`), zoom au pincement du pavé tactile et à `Ctrl` +
  molette, déplacement à la barre d'espace, export à `Ctrl + Alt + E`.
- Bancs d'essai en intégration continue (sur le dépôt public, où les runners
  sont gratuits) : six bancs, du colorimétrique au parcours navigateur complet.
- Pipeline de build desktop (Windows/macOS) — voir
  [l'état de validation](docs/DEVELOPMENT.md#état-de-validation) avant de le
  considérer prêt pour une release publique.

## En cours de validation

- Release desktop de bout en bout (tag → binaires publiés) : jamais
  déclenchée avec un vrai tag.
- Fenêtre macOS (affichage réel) et export vidéo sur macOS : à vérifier à la
  main sur une vraie machine.

## À faire

- Édition fine d'une couche : entrer dedans, supprimer une trace précise,
  décaler une couche ou une trace dans le temps, en changer la vitesse.
- Export pro avec **une couche de dessin par fichier**, dans la continuité de
  l'export en trois couches existant.
- Gestion des couches depuis la tablette (aujourd'hui le poste structure, la
  tablette dessine).
- Renommer une couche.
- Raccourcis clavier restants : taille du pinceau via `[` / `]`, changement
  de brosse au clavier.
- Repères de zone sûre / grille optionnels sur le canevas.
- Vérification du contraste et de la navigation au clavier sur l'ensemble
  des contrôles.
