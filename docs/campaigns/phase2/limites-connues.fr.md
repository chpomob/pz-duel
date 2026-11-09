# Limites connues de la phase 2

*Français — [English version](limites-connues.md)*

La phase 2 a été écrite sous Linux. **Aucune de ses étapes n'a été essayée sur un vrai poste Windows, et
aucune face au vrai jeu sous Windows.** La CI tourne sur `windows-latest` mais n'a pas le jeu : elle
prouve notre code contre un double, pas contre Project Zomboid.

Comment lire ce fichier : une preuve s'écrit `fichier::test`, et `tests/test_ci_workflows.py` vérifie
que chaque nom existe. Un nom placé dans un bloc `win32` ne prouve quelque chose qu'une fois exécuté
sur `windows-latest`.

## État des preuves

- **Linux** : `python -m pytest -q` est vert, les branches Windows passant par une plateforme injectée.
- **Windows** : les jobs `pytest-windows` et `build-windows` de `.github/workflows/tests.yml` ont tourné
  sur `windows-latest`. Un test d'un bloc `win32` cité ici prouve quelque chose à partir des exécutions
  consignées dans [Exécutions réelles de CI et de release (P18)](#exécutions-réelles-de-ci-et-de-release-p18),
  pour ce qu'un runner peut montrer.
- **Exécutions de CI (AC49)** : leurs URL sont dans cette même section.

## Suivi des builds du jeu (au 2026-11-09)

42.22.2 est devenue la stable le 2026-11-09 et Steam a mis à jour l'installation locale ; le mod a été
re-prouvé sur ce build par exécution, pas par lecture. Rien dans le code ne fige un build : le mod est
livré en `mods/duelcore/42.21/` avec `versionMin=42.21.0`, que le jeu lit comme un **plancher** — le même
dossier se charge donc sur un build plus récent — et `caps` sur 42.22.2 annonce `build=42.22.2` avec les
mêmes 13 hooks, 4 fonctions, 5 atomes et 7 effets que sur 42.21.0.

- `tests/test_integration_pz.py::test_caps_from_real_mod` — le build attendu est celui que le serveur
  annonce dans son propre log, plus un `>= versionMin` lu dans le `mod.info` livré ; aucun build n'est
  écrit dans le test (la constante `42.21.0` est exactement ce qui est passé au rouge à la mise à jour).
- `tests/test_integration_pz.py::test_version_floor_is_read_from_the_shipped_mod` — le plancher est lu,
  donc le prochain patch du jeu ne peut pas périmer ce test.
- `tests/test_integration_pz.py::test_real_servers_boot_contract_stop` — `verify(caps) == []`, qui
  compare déjà `caps.build` au build lu dans le log de chaque serveur.

Un nouveau build ne prouve jamais que les hooks **tirent**. 42.22.2 déplace le calcul des dégâts
multijoueur côté serveur, soit précisément la zone de `OnBeingHitByZombie` et `OnPlayerGetDamage` — les
deux signaux derrière `damage_taken`. On ne tient que l'enregistrement ; seule une session avec un vrai
joueur tranche le tir.

## Lanceurs sous Windows (au 2026-11-09)

L'installation Windows de 42.22 peut être livrée **sans `StartServer64.bat`** — le dossier d'un vrai
testeur contenait `ProjectZomboid64.exe` et aucun script, ce que le binaire publié a rapporté comme
« `StartServer64.bat` manquant ». Deux conséquences, toutes deux dans le code :

- **La détection accepte les deux dispositions** : le client et le script fournisseur, ou le client,
  `projectzomboid.jar` et `jre64\bin\java.exe` — le JRE que le jeu embarque.
  `tests/test_preflight.py::test_windows_folder_without_the_script_uses_the_bundled_jre` et
  `tests/test_preflight.py::test_windows_folder_without_script_nor_jre_names_the_jre`.
- **Le lancement ne dépend plus du script** : quand il est absent, le service compose la commande du
  JRE lui-même (`-cp .;projectzomboid.jar … zombie/network/GameServer`, tas lu dans `server_heap_mb`),
  exactement ce que fait `start-server.sh` sous Linux. Le script fournisseur garde la main quand il
  existe : `tests/test_platform_windows.py::test_windows_command_prefers_the_supplied_script` et
  `tests/test_platform_windows.py::test_windows_command_keeps_its_eight_arguments`.

La commande du JRE a été mesurée sous Linux 42.22.2 : `server is listening on port …` 15 s après le
départ, avec `-Xms2048m`. **`ProjectZomboid64.exe -server` n'est pas un serveur** : le lanceur prend sa
classe principale dans `ProjectZomboid64.json` et lui transmet les arguments inconnus, donc ce raccourci
n'est jamais utilisé.

Ce que cela ne prouve pas : que la même commande fonctionne depuis `jre64\bin\java.exe` sur un vrai
bureau Windows (DLL manquantes, gestion de la console, objet Job enveloppant un JVM et non `cmd`). Seule
une exécution réelle sous Windows le dira — c'est le rôle du testeur.

## Durabilité disque sous Windows (au 2026-11-07)

Un vrai match ne s'arrête plus à la préparation des mondes : depuis P18F1, `service/snapshots.py`
appelle `sync_disk`, dans `begin_creation` et dans le remplacement des mondes, et jamais `os.sync`, qui
n'existe pas sous Windows. Reste une garantie plus étroite sur ce système. `sync_disk` synchronise
chaque fichier d'un monde un par un : les entrées de dossier, les fichiers en lecture seule et les
cibles de liens restent hors de cette garantie :
`tests/test_disk_sync.py::test_windows_flushes_every_file_without_os_sync` et
`tests/test_disk_sync.py::test_windows_skips_a_read_only_file`. Aucune coupure de courant n'a été
simulée sur un vrai disque Windows.

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

## Code de vérification du `.bat` invité (P19F3)

L'invitation est **prévue pour un réseau de confiance**. Le lien est en HTTP simple, comme l'impose AC37,
et le `.bat` exécute PowerShell : quelqu'un sur le chemin réseau peut remplacer la réponse, et ce qu'il
sert s'exécute alors sous le compte de l'invité. Les SHA-256 du `.bat` ne lient que ses fichiers embarqués
entre eux. **Le code est la seule protection contre un fichier remplacé.**

- **Ce qu'est le code** : les 16 premiers caractères hexadécimaux (64 bits) du SHA-256 de tout le `.bat`,
  tel que servi (P19F6 ; 12 caractères avant). Le lobby l'affiche à côté de l'invitation, et l'hôte le lit à l'invité, jamais à côté du lien :
  `tests/test_share.py::test_shown_code_comes_from_the_very_bytes_served` et
  `tests/test_share.py::test_changed_byte_no_longer_matches_the_code_and_the_check_refuses`.
- **Seule une comparaison faite avant d'ouvrir le fichier arrête un attaquant.** L'invité lance
  `certutil -hashfile pz-duel-join.bat SHA256`, et le résultat doit commencer par le code. Un fichier
  remplacé donne un autre résultat, et l'invité refuse de l'ouvrir :
  `tests/test_guest_launcher.py::test_windows_code_matches_what_certutil_prints`.
- **Le contrôle interne au `.bat` ne protège pas d'un attaquant.** Il refuse un code qui ne correspond pas
  au fichier, avant toute installation :
  `tests/test_guest_launcher.py::test_embedded_check_refuses_the_right_code_on_a_changed_byte` et
  `tests/test_guest_launcher.py::test_windows_changed_byte_is_refused_with_the_code_of_the_host`. Mais il
  s'exécute dans le fichier qu'il contrôle : un `.bat` remplacé peut l'omettre, accepter n'importe quel
  code ou lancer d'abord ses propres commandes. Il n'arrête qu'un fichier abîmé, ou une modification qui
  laisse le contrôle en place.
- **Un invité qui saute `certutil` n'est pas protégé**, et rien dans PZ Duel ne peut l'y obliger.
- **Le code protège le fichier, pas le secret de l'invitation.** Le mot de passe circule toujours en
  clair. Qui lit le trafic peut s'en servir, quoi que dise le code.
- **64 bits** : un fichier remplacé de même code coûte environ 2^64 calculs, hors de portée pendant la
  durée d'un lobby. Le README dit à l'invité de lancer `certutil` avant d'ouvrir le fichier, et à l'hôte
  de lire le code à voix haute, séparément du lien :
  `tests/test_guest_launcher.py::test_code_is_64_bits` et
  `tests/test_ci_workflows.py::test_guest_checks_the_file_with_certutil_before_opening_it`.
- **Le lanceur n'annonce un succès que si le jeu tourne.** Un processus `ProjectZomboid64` absent
  avant le lancement doit apparaître et tourner encore 5 secondes plus tard, dans les 30 secondes qui
  suivent le lancement. Le contrôle suit le nom du processus, pas l'objet rendu par `Process.Start` : un
  client qui passe la main à un autre processus compte quand même. Un jeu qui se ferme aussitôt, ou
  n'apparaît jamais, est un échec : le mod précédent de l'invité est remis, et le mod essayé reste dans
  le dossier `.duelcore-*` de `Zomboid`, dont la fenêtre donne le chemin :
  `tests/test_guest_launcher.py::test_client_closing_at_once_fails_and_puts_the_old_mod_back`.
  Un plantage plus tardif, au menu ou sur le serveur, n'est pas vu. Un autre `ProjectZomboid64` lancé
  par quelqu'un d'autre pendant ces secondes serait pris pour le jeu.
- **Le `duelcore` remplacé est gardé, dans `Zomboid\duelcore.previous`**, hors de `mods` pour que le
  jeu ne voie pas deux mods de même identifiant. La fenêtre donne son chemin et la façon d'y revenir. Un
  second lancement du même mod y laisse la copie antérieure de l'invité ; un lancement d'un autre mod la
  remplace :
  `tests/test_guest_launcher.py::test_running_client_succeeds_and_keeps_the_previous_mod`.
- **Ces deux comportements n'ont tourné que sur PowerShell 7 sous Linux**, avec un double en script
  shell ; PowerShell 5.1 et les vrais noms de processus ne sont exercés que par
  `tests/test_guest_launcher.py::test_windows_client_closing_at_once_fails`.
- **Le code couvre le mot de passe**, puisqu'il couvre tout le fichier, mais il ne révèle rien que le
  `.bat` ne donne déjà à l'invité. `host_password` n'est pas dans le `.bat`.
- **`-ExecutionPolicy Bypass` n'a pas été resserré, car il n'y avait rien à resserrer.** L'option ne vaut
  que pour le processus PowerShell qu'elle démarre et pour ses enfants. Elle n'écrit aucun réglage
  persistant, une stratégie de groupe l'emporte, et la stratégie ne régit de toute façon pas le texte de
  `-Command` :
  `tests/test_guest_launcher.py::test_execution_policy_stays_a_process_flag` et
  `tests/test_guest_launcher.py::test_windows_execution_policy_is_unchanged_after_a_run`.
- **Le contrôle n'a tourné que sur PowerShell 7 sous Linux.** `set /p`, `certutil` et PowerShell 5.1 ne
  sont exercés que par le bloc `win32` :
  `tests/test_guest_launcher.py::test_windows_wrong_code_stops_before_the_game`.

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

- **La construction n'installe que des wheels figées et hachées** (P19F6) :
  `packaging/requirements-build.txt` donne à chaque paquet de la construction sa version exacte et le
  SHA-256 de ses wheels PyPI, et `tests.yml` comme `release.yml` l'installent avec
  `--require-hashes --only-binary :all:` :
  `tests/test_ci_workflows.py::test_build_requirements_are_pinned_with_their_hashes` et
  `tests/test_ci_workflows.py::test_build_and_release_install_the_same_hashed_file`. Cela fait confiance
  aux fichiers de PyPI tels qu'ils étaient le jour où ils ont été figés : un paquet déjà compromis ce
  jour-là passerait quand même.
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
