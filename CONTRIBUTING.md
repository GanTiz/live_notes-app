# Contribuer

Merci de l'intérêt porté à live_notes. Ce projet est jeune et son
fonctionnement interne (architecture, décisions de conception, pièges
connus) est documenté dans le **[guide développeur](docs/DEVELOPMENT.md)** —
à lire avant de modifier le code.

## Avant d'ouvrir une pull request

1. Vérifier que les bancs de test passent :
   ```bash
   py test_colorspace.py
   py test_export.py
   py test_launch.py
   py test_proxy.py
   py test_session.py
   py test_ui.py
   ```
2. Décrire le problème résolu ou la fonctionnalité ajoutée, et le
   raisonnement derrière un choix d'implémentation non évident — c'est ce
   qui permet de garder [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) à jour.
3. Pour un changement touchant au rendu (pinceaux, couleur, export), vérifier
   que l'aperçu à l'écran et le fichier exporté restent identiques : c'est
   l'invariant central du projet (voir « Architecture » dans le guide
   développeur).

## Signaler un bug

Ouvrir une issue avec : ce qui était attendu, ce qui s'est produit, l'OS et
la version de live_notes, et si possible les logs du terminal au moment du
problème.

## Licence

En contribuant, vous acceptez que vos changements soient distribués sous les
termes de la [GPLv3](LICENSE), comme le reste du projet.
