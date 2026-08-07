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
- Pipeline de build desktop (Windows/macOS) — voir
  [l'état de validation](docs/DEVELOPMENT.md#état-de-validation) avant de le
  considérer prêt pour une release publique.

## En cours de validation

- Release desktop de bout en bout (tag → binaires publiés) : jamais
  déclenchée avec un vrai tag.
- Fenêtre macOS (affichage réel) et export vidéo sur macOS : à vérifier à la
  main sur une vraie machine.

## À faire

- Gestion de calques multiples pour le dessin.
- Undo/redo pour le dessin.
- Raccourcis clavier (taille du pinceau via `[` / `]`, changement de brosse).
- Zoom au trackpad (pincement) et déplacement à la barre d'espace, en
  complément du clic molette.
- Repères de zone sûre / grille optionnels sur le canevas.
- Vérification du contraste et de la navigation au clavier sur l'ensemble
  des contrôles.
