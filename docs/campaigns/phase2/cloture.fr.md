# Phase 2 — rapport de clôture

La phase 2 a ajouté le support Windows, le packaging et une expérience sans ligne de commande
par-dessus le service de la phase 1. Ce fichier dit ce qui est livré, ce qui le prouve, et ce qui
reste ouvert. Il clôt P18 (exécutions réelles de CI et de release) et P19 (revue de toute la branche).

## Ce qui est livré

| Étape | Livré |
|-------|-------|
| P1-P8 | Résolution des chemins, primitives Windows, fichiers et processus, préflight, lancement du binaire |
| P9-P12 | État de premier lancement et routes, sortie par l'UI, génération du lanceur invité, modèle d'invitation |
| P13-P17 | Écouteur de partage, packaging (`pz-duel.exe`), CI, workflow de release, documentation |
| P18 | Exécutions réelles de CI et de release sur GitHub Actions, URL consignées dans `limites-connues` |
| P19 | Revue de toute la branche, deux passes, et les étapes de correction qu'elles ont produites |

## Les preuves

- `tests.yml`, run [42368769474](https://github.com/chpomob/pz-duel/actions/runs/42368769474) sur
  `cef2be5` : sept jobs, sept succès — Linux 3.12/3.13/3.14, Windows 3.12/3.13/3.14, `build-windows`.
- `release.yml`, run [42319708603](https://github.com/chpomob/pz-duel/actions/runs/42319708603) sur le
  tag `v0.0.0-phase2-test` : préversion avec exactement deux pièces jointes, `pz-duel.exe`
  (10 351 421 octets) et son `.sha256`, le binaire téléchargé correspondant à `db754a39…a7608`.
- Suite locale : 2 238 passés, 16 sautés (les tests à `pwsh` tournent sur `ubuntu-latest`, pas ici).
- L'historique complet des runs rouges est conservé dans `limites-connues` : c'est la preuve que les
  jobs Windows attrapent ce que Linux ne voit pas.

## Ce que les deux passes de revue ont trouvé

Douze constats à la première passe (4 majeurs), neuf à la seconde (1 majeur). Tous corrigés par une
étape relue ; aucun n'a été écarté.

- **Lancement et snapshots (P19F1)** : le client hôte partait sans dossier de travail ; les renommages
  du monde et du mod ignoraient les verrous Windows ; une restauration ratée pouvait supprimer le mod
  de l'invité.
- **Sécurité (P19F3, P19F6)** : le `.bat` invité voyage en HTTP clair, donc le seul contrôle qui tient
  est la comparaison `certutil` faite par l'invité **avant** d'ouvrir le fichier — désormais documentée
  dans les deux README, avec le code lu à voix haute par l'hôte. Le code passe de 48 à 64 bits.
- **Comportement du lanceur invité (P19F6)** : un jeu qui démarre puis s'arrête aussitôt est maintenant
  rapporté comme un échec, mod précédent restauré ; un lancement réussi conserve l'ancien `duelcore`
  en sauvegarde nommée au lieu de le supprimer.
- **Infrastructure (P19F2, P19F5, P19F7)** : journal fichier du binaire fenêtré, job de release qui
  attend les suites de tests, actions épinglées par commit, `permissions:` déclarées,
  `--require-hashes` sur les dépendances de construction, plafond par pair sur l'écouteur de partage,
  contrôle d'origine sur la route du lanceur, et un test de parse YAML — dont la ligne
  `--require-hashes` avait besoin, puisque `:all:` non quoté rendait les deux workflows illisibles et
  produisait deux runs à zéro job.
- **Défauts Windows trouvés par la CI, pas par les relecteurs** : `os.sync` absent, dix échecs dans les
  doubles d'arbre de processus et de palier d'arrêt, deux attentes sans marge, un refus fermé par RST
  au lieu d'un FIN, et un test qui appelait `powershell.exe` et tombait quand le runner ne répondait
  pas.

## Ce qui reste ouvert

- **Aucun poste Windows réel n'a été utilisé.** Toute la preuve Windows vient des runners
  `windows-latest`. Le transfert `UseShellExecute` est le seul comportement qu'un runner ne tranche
  pas : le contrôle « le jeu a-t-il démarré » suppose que le processus du client est observable, et
  cela demande un essai sur une machine réelle avec PZ installé.
- **Deux constats sont en inférence et le restent** : la présence ou l'absence réelle d'un en-tête
  `Origin` lors d'une navigation cross-site, et le débit SHA-256 supposé par le calibrage de la
  longueur du code. Les mitigations sont en place ; les mesures, non.
- **Aucune publication Workshop.** Les mods sont livrés dans le dépôt ; la voie Workshop a été
  étudiée mais jamais exécutée.
- **La chaîne de duel de la phase 1 reste non prouvée de bout en bout** : attribution du tueur en multi,
  visibilité serveur des spawns, craft via `OnProcessAction`. La phase 2 a durci le transport autour de
  ces inconnues ; elle ne les a pas testées.
- **La release de test `v0.0.0-phase2-test` est publique** et le reste jusqu'à ce que l'opérateur la
  supprime.

## Entretien, pas du code

- Trois commits de correction portent le sujet de squash de la boucle
  (`squash: phase2-p19N — adversarial approved`) au lieu du sujet conventionnel. Le harnais de campagne
  restaure le sujet ; `adversarial_loop.py` ne le fait pas. Le correctif est dans l'outil, pas dans une
  réécriture d'historique.
