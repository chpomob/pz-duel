# Limites connues de la phase 2

*Français — [English version](limites-connues.md)*

La phase 2 a été écrite sous Linux. **Aucune de ses étapes n'a tourné sur une vraie machine Windows, et
aucune face au vrai jeu sous Windows.** La CI n'a pas le jeu : elle prouve notre code contre un double,
pas contre Project Zomboid.

Comment lire ce fichier : une preuve s'écrit `fichier::test`, et `tests/test_ci_workflows.py` vérifie
que chaque nom existe. Un nom placé dans un bloc `win32` ne prouve quelque chose qu'une fois exécuté
sur `windows-latest`.

## État des preuves

- **Linux** : `python -m pytest -q` est vert, les branches Windows passant par une plateforme injectée.
- **Windows** : les jobs `pytest-windows` et `build-windows` de `.github/workflows/tests.yml` existent,
  mais aucune exécution n'est encore consignée. D'ici là, chaque test d'un bloc `win32` cité ici est
  écrit, pas prouvé.
- **Exécutions de CI (AC49)** : aucune consignée pour l'instant. L'étape P18 ajoute leurs URL ici.

## Blocage : un vrai match ne peut pas démarrer sous Windows

`service/snapshots.py` appelle `os.sync()` dans `begin_creation` et dans le remplacement des mondes.
`os.sync` n'existe pas sous Windows : **tout match réel s'arrête sur `AttributeError` à la préparation
des mondes**, avant le démarrage des serveurs. Cela garde aussi `windows-latest` au rouge, et bloque la
release dont dépend l'étape de téléchargement du README. Mesure locale sous wine : 168 échecs et
15 erreurs sur 1418 tests sans doublure d'`os.sync`, 3 échecs avec. C'est un constat, pas une preuve.

`service/snapshots.py` est hors des `targets` de la phase : seul l'opérateur peut ouvrir ce correctif.
Une doublure sans effet ferait perdre sans le dire la durabilité dont ce fichier dépend.

## Non prouvé sur le vrai jeu

### La transmission par `StartServer64.bat` de ses 8 arguments

L'installation Linux ne contient pas ce fichier : il n'a jamais été lu (H4). Rien ne montre qu'il
transmet `-servername`, `-port`, `-udpport`, `-adminpassword` et leurs valeurs au serveur. S'il en perd
un, le serveur démarre sous le mauvais nom ou le mauvais port, ou le lancement expire.

Seul notre côté est prouvé : la commande construite, et sa remise à un double.

- `tests/test_platform_windows.py::test_windows_command_keeps_its_eight_arguments`
- `tests/test_platform_windows.py::test_engine_launches_windows_servers_and_client_as_specified`

### La valeur de `-Dzomboid.steam` face à un client `-nosteam`

`start-server.sh` fixe `-Dzomboid.steam=0`, et nous ne passons pas cette option. La valeur fixée par
`StartServer64.bat` est inconnue. S'il démarre le serveur avec Steam, un client `-nosteam` peut être
refusé.

Ce qui est prouvé : les arguments reçus par le client, et l'absence de cette option dans la commande
du serveur.

- `tests/test_guest_launcher.py::test_script_asks_the_client_for_the_specified_arguments`
- `tests/test_platform_windows.py::test_windows_command_keeps_its_eight_arguments`

### Son comportement de fin de script

On ne sait pas si le script finit sur un `PAUSE`, ni ce qu'il fait à la sortie du serveur. L'arrêt ne
suppose donc pas qu'il rend la main : il termine tout l'arbre par un objet Job.

Ce qui est prouvé, contre un double dont le script finit sur `PAUSE` :

- `tests/test_platform_windows.py::test_windows_stop_ends_a_paused_tree_at_the_sigkill_stage`
- `tests/test_platform_windows.py::test_windows_early_child_cannot_escape_the_job`

### La connexion effective d'un invité

Aucun invité n'a jamais rejoint un serveur lancé par PZ Duel, sur aucun système. Un `.bat` n'a jamais
démarré qu'un double de `ProjectZomboid64.exe`. Le contrôle du mod côté client, les arguments
`+connect` et `+password` sur le vrai client et la poignée de main elle-même n'ont pas été observés. La
procédure en jeu du README reste le seul moyen de trancher.

Ce qui est prouvé : le mod arrive intact et le double reçoit les arguments attendus.

- `tests/test_guest_launcher.py::test_windows_launcher_installs_the_mod_and_connects`

### La joignabilité depuis Internet d'une adresse saisie

Le service ne teste pas une adresse saisie (H7) : il refuse seulement les adresses inutilisables, puis
écrit l'adresse dans le lien et le `.bat`. Le routeur, le fournisseur d'accès (CGNAT compris) et le
pare-feu sont à la charge de l'hôte. Il n'y a ni relais, ni traversée de NAT.

Ce qui est prouvé : aucune connexion n'est tentée, l'adresse est reprise telle quelle, et l'hôte
apprend quels ports ouvrir.

- `tests/test_share.py::test_ac58_entry_and_generation_touch_no_network`
- `tests/test_share.py::test_ac43_entered_address_reaches_link_qr_and_launcher`
- `tests/test_share.py::test_ac57_entered_address_names_the_shipped_ports`

## Jamais observé sous Windows

- **L'invite du pare-feu Windows (H5).** PZ Duel n'ouvre ni ne pilote le pare-feu, ce qui exigerait une
  élévation. On n'a pas vu quels programmes déclenchent une invite, ni quand : le serveur du jeu,
  l'écouteur de partage et le contrôle des ports du préflight se lient tous hors de la boucle locale.
  Une invite refermée peut empêcher l'invité de se connecter, sans que PZ Duel puisse le dire.
- **SmartScreen.** Le binaire et le `.bat` ne sont pas signés, par décision. Le libellé de
  l'avertissement et les étapes du README viennent du comportement habituel de Windows, pas d'un test.

## `.bat` de l'invité (P11)

Aucun Windows PowerShell 5.1 ne l'a exécuté : sa logique a tourné sur PowerShell 7 sous Linux, et sa
couche `cmd` sous wine avec une doublure. Ce sont des constats, pas des preuves. La première exécution
réelle est le bloc `win32` de `tests/test_guest_launcher.py`.

- **PowerShell bloqué par une stratégie, ou en mode de langage restreint** : le `.bat` échoue. Non testé.
- **Mark of the Web** : un `.bat` téléchargé le porte, d'où SmartScreen. Non testé.
- **Heuristiques antivirus** : un `.bat` avec PowerShell et base64 peut être signalé ou supprimé. Non
  testé.
- **Le mot de passe client est visible** sur la ligne de commande du jeu, pour tout processus de la
  session de l'invité.
- **`%` dans le chemin du `.bat`** : wine le perd, seul le vrai Windows peut répondre :
  `tests/test_guest_launcher.py::test_windows_self_path_with_cmd_characters`.
- **`Read-Host` sur une entrée redirigée** :
  `tests/test_guest_launcher.py::test_windows_closed_input_does_not_hang_the_pause` et
  `tests/test_guest_launcher.py::test_windows_empty_answer_fails_and_keeps_the_mod`.
- **Mots de passe saisis** : seule la liste admise voyage dans un `.bat` ; sinon, pas de `.bat` :
  `tests/test_guest_launcher.py::test_hostile_password_is_refused_without_being_cited`.
- **Un invité sous Linux** n'a pas de `.bat` et rejoint à la main (H2) :
  `tests/test_share.py::test_manual_block_stays_for_a_linux_guest`.

## Processus (P5)

- **La reprise d'un serveur suspendu** passe par `NtResumeProcess`, stable mais non documenté, avec un
  repli sur les fils du processus. Prouvé contre un noyau injecté seulement :
  `tests/test_platform_windows.py::test_resume_goes_through_nt_resume_process` et
  `tests/test_platform_windows.py::test_resume_falls_back_on_the_threads_of_the_process`.
- **La mort des serveurs avec le service** repose sur le Job :
  `tests/test_platform_windows.py::test_windows_forced_end_of_the_service_leaves_no_double` et
  `tests/test_platform_windows.py::test_windows_closed_console_leaves_no_double`.
- **Le client de l'hôte** quitte le Job si Windows l'accepte. Sinon, il meurt avec le service :
  `tests/test_platform_windows.py::test_windows_client_falls_back_when_the_job_refuses_breakaway`.
- **Un caractère que `cmd` interpréterait** dans un argument refuse le lancement plutôt que de risquer
  une injection. Le contrôle lit chaque argument, dossier du jeu compris ; le test prend le mot de passe :
  `tests/test_platform_windows.py::test_interpreted_password_is_refused_without_being_cited`.

## Binaire (P15)

- **Un arrêt forcé laisse le dossier `_MEI*` dans `%TEMP%`.** Le chargeur onefile ne le nettoie qu'à une
  sortie normale. La place reste perdue jusqu'à ce qu'on le supprime.
- **Un arrêt forcé du chargeur** termine le service, car le point d'entrée le surveille :
  `packaging/smoke_test.py::check_forced_end`. Ce contrôle a passé sur des binaires Windows sous wine,
  Python 3.11.9 et 3.13.1 ; le vrai `windows-latest` doit encore le confirmer.
- **Sur un binaire figé Linux**, cette même surveillance n'arrête pas les serveurs : il n'y a pas de Job.

## Écouteur de partage (P13)

- **Relier le port de partage pour une revanche juste après un téléchargement** n'est prouvé que sous
  Linux. Sous Windows, `SO_EXCLUSIVEADDRUSE` reste posé et une connexion en TIME_WAIT peut refuser la
  liaison : `tests/test_share.py::test_rematch_rebinds_right_after_a_download`. Dans ce cas, le lobby
  s'ouvre sans lien, et le `.bat` reste enregistrable :
  `tests/test_share.py::test_ac42_taken_port_keeps_the_lobby_and_the_save`.
- **Le lien est en HTTP simple.** Le jeton et le `.bat`, mot de passe compris, circulent en clair.
- **Le lien ne vit que pendant le lobby**, et chaque lobby a son jeton :
  `tests/test_share.py::test_ac40_every_exit_closes_the_listener` et
  `tests/test_share.py::test_ac38_rematch_keeps_the_launcher_and_changes_the_token`.

## Interface (P14)

- **Un préflight en échec reste dans la phase `error`.** Les routes de configuration refusent alors, et
  `Try again` ne fait que relancer les contrôles. Un défaut de mod ou de dossier demande de quitter et
  rouvrir, comme l'écran le dit :
  `tests/test_setup_flow.py::test_error_screen_can_run_the_checks_again`. Le correctif relève de
  `service/engine.py`.
- **L'interface mêle français et anglais** : les écrans de la phase 2 sont en anglais, les anciens en
  français.

## Exécutions réelles de CI et de release (P18)

Tout ce qui suit a été produit par GitHub Actions sur le dépôt public, pas en local. Les heures sont en UTC.

- **`tests.yml`** tourne sur `master` : Linux 3.12/3.13/3.14, plus `pytest-windows` sur les trois
  mêmes versions et `build-windows` :
  - [42316172795](https://github.com/chpomob/pz-duel/actions/runs/42316172795) — commit `5bfa440`,
    **vert deux fois** (la seconde passe est une relance, parce que le défaut corrigé en P18F4 était
    une course).
- **`release.yml`**, run [42319708603](https://github.com/chpomob/pz-duel/actions/runs/42319708603)
  sur le tag `v0.0.0-phase2-test`, a créé la release
  [v0.0.0-phase2-test](https://github.com/chpomob/pz-duel/releases/tag/v0.0.0-phase2-test), marquée
  **préversion**, avec exactement deux pièces jointes : `pz-duel.exe` (10 351 421 octets) et
  `pz-duel.exe.sha256`. Le binaire téléchargé correspond à son empreinte :
  `db754a39e7a8d6a2b2bea76514ae90916ee76d5a459efcf9e8bf7e1a4b6a7608`.
- **Les jobs Windows font référence pour tout ce qui touche à la plateforme.** Ils ont attrapé cinq
  vagues de défauts que plus de 2 000 tests Linux ne pouvaient pas voir, chacune corrigée par son
  étape relue : `os.sync` absent sous Windows (P18F1), dix échecs dans les doubles d'arbre de
  processus et de palier d'arrêt (P18F2), deux attentes sans marge (P18F3), et un refus de connexion
  fermé par RST au lieu d'un FIN (P18F4). Les runs rouges restent dans l'historique comme preuves :
  [42296912989](https://github.com/chpomob/pz-duel/actions/runs/42296912989),
  [42309448633](https://github.com/chpomob/pz-duel/actions/runs/42309448633),
  [42313084724](https://github.com/chpomob/pz-duel/actions/runs/42313084724).
- **La release de test reste publique jusqu'à ce que l'opérateur la supprime.** C'est une préversion
  sur un tag `v0.0.0-*`, elle contient un binaire non signé, et elle existe pour prouver
  `release.yml` de bout en bout.
- **Aucune machine Windows n'a été touchée à la main.** Toute la preuve Windows vient des runners
  `windows-latest` ; aucun poste réel n'a servi à jouer un duel.
