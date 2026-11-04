---
spec: "duel-phase1-mock-to-real"
version: "1.0"
author: "adversarial-plan"
based-on: "adversarial-spec"
findings-input: true
---

# Implementation Plan

Plan de la phase 1 : du mock au duel réel. Couvre R1 à R9 et AC1 à AC77 de `spec.md` (§ Traçabilité en fin de document). Le contenu est en français, comme la spec ; les identifiants et les noms de fichiers sont en anglais.

Conventions :

- Tests avec `.venv/bin/pytest` (Python 3.14 dans `.venv`, syntaxe limitée à 3.11).
- Un AC cité dans un step est écrit et vert à la fin de ce step, sauf mention « complété en Pn ».
- **Aucun fichier hors de `targets`** : pas de module `config.py`, `match.py` ni `redact.py`. Les tests vont uniquement dans les fichiers nommés par les AC, plus `tests/conftest.py`, `tests/doubles/` et `tests/fixtures/`.
- Toute valeur de délai, seuil ou équilibrage vit dans `service/runtime.json`, `service/effects.json` ou un preset.

## Architecture retenue

Trois couches, pour que le rejeu et les tests partagent le code de production.

1. **`MatchCore` (`service/rules.py`)** : cœur pur et déterministe. Ne lit ni horloge, ni fichier, ni réseau. Reçoit des intrants (`submit_*`), avance par `advance(now_ms)`, rend des enregistrements ordonnés (`event`, `late_event`, `rule_fired`, `effect_*`, `command`, `cmd_timeout`, `match_end`). Temps interne en **entiers** : millisecondes et index de tick `k`, `mt` n'étant calculé (`k * tick_ms / 1000`) qu'à la sortie.
2. **`RealMatch` (`service/engine.py`)** : conducteur du déroulé réel. Un seul fil exécute `pump(now_ms)` : lire `out`, surveiller les serveurs, avancer le cœur, journaliser, écrire les commandes. Le thread ne fait que boucler sur `pump(clock())`. Les tests appellent `pump` directement avec un temps fourni.
3. **Briques système** : `transport`, `journal`, `servers`, `rcon`, `snapshots`, `preflight`, sans connaissance des règles.

Le rejeu instancie le même `MatchCore` et lui repasse les enregistrements du journal : il n'existe qu'une implémentation de l'horloge.

## Décisions signalées

La spec laisse ces points ouverts ou ils découlent de constats sur la machine. Chacun est tranché ici pour qu'aucun ne soit une décision silencieuse. Les reviewers peuvent les contester.

| # | Sujet | Décision | Motif |
|---|---|---|---|
| D1 | Valeurs de migration des presets | `06` § 3.2 : `spawn_pop` `[30,60]`, `warn_s` 6 ; `spawn_sprinter` `[40,70]`, 10 ; `horde_area` rayon 20, `[50,80]`, 12 ; budget 6 / 4 / 20. Spec : `supply_drop` `[25,40]`, `warn_s` 3. Sans source : `weather_rain` et `noise` `warn_s` 0, `noise` `[30,60]`. `disconnect_grace_s` 120 | Reprendre la conception avant d'inventer |
| D2 | `score.points` | `zombie_kill` = 1, autres atomes absents (0). `when_gt` 30 vaut alors 30 kills d'écart | Aucune source ; le ×10 du mock est simulé. Valeur de table, modifiable sans code |
| D3 | Modificateurs des presets | Seul `pression` en porte (`combo` et `score_gap` de `06`, `on: zombie_kill`), avec une ligne de résumé. Les trois autres : `{}` | `pression` est le preset « combos » de `06` |
| D4 | Valeurs de `runtime.json` sans source | `server_start_timeout_s` 300, `stop_timeout_s` 30, `kill_grace_s` 10, `contract_timeout_s` 60, `ack_timeout_s` 15 (supérieur aux 5 s de l'ack), `cmd_result_timeout_s` 60 (supérieur à `max_warn_s`), `max_units_per_command` 30, `min_free_ram_mb` 6144 | 2 JVM de 2 Go plus marge. AC63 et AC65 ne tiennent pas avec 5 comme valeur livrée (AC65 attend n=6) : le « 5 et 1 sur le disque » d'AC63 est une condition du test, obtenue en redirigeant `rules.RUNTIME_PATH` vers un fichier temporaire |
| D5 | Paramètres de `probe_spawn` | Dans `runtime.json` : `probe_spawn` = `{"n":1,"distance":[30,60],"warn_s":0}`, `origin` = `{"rule":"probe_spawn"}` | « Aucune valeur dans le code » |
| D6 | Bornes des fenêtres sur `te` | `count`, absence : un âge égal à D est dans la fenêtre (`te − t ≤ D`), comme la borne fermée de `]t, t+D]` des séries. Les fenêtres sur `mt` gardent `]mt − 60, mt]` | La spec ne fixe que les séries et les fenêtres de 60 s |
| D7 | Arithmétique des multiplicateurs | `Fraction(str(x))` pour `n × produit`, jamais de flottant binaire avant `floor` | `floor` sur flottant perd une unité sur des valeurs exactes |
| D8 | `latency_ms` | `t0_ms + mt × 1000 − t_ms` de l'événement le plus ancien du lot, calculé dans le cœur | Une mesure murale ferait diverger le rejeu, qui compare les `command` |
| D9 | `defender_bonus` | Une fois par `rule_fired` dont la règle produit au moins un effet à `n`, passe par les budgets, sans modificateur sur son propre `n`, rattaché à la règle source | La spec ne dit pas « par effet ». Le cooldown absorbe de toute façon les répétitions |
| D10 | `late_event` | S'applique à tout événement accepté, `beat` et `cmd_result` compris | La règle 4 dit « un événement » ; la règle 3 inclut `cmd_result` |
| D11 | Après `match_end` | `out` n'est plus lu. Un événement arrivé après n'est pas journalisé | Évite des `event` sans tick ; le verdict s'arrête à `match_end` |
| D12 | Fin de match et rejeu | Le cœur décide minuteur et mort subite ; `RealMatch` décide forfait, dérive et serveur perdu, et soumet `match_end` en intrant. Le rejeu réinjecte `match_end`, et la fin est idempotente dans le cœur | Forfait et dérive dépendent de l'horloge du service |
| D13 | Absence d'un joueur | Comptée depuis le dernier heartbeat qui contenait le joueur, sur l'instant courant, pas depuis le passage en `suspect` | AC36 : « absent 119 s » |
| D14 | `player_death` à `te` < 0 | Ignoré pour le résultat, journalisé | « Postérieur au début du match » s'applique à toute la logique de match |
| D15 | Durée et `day_length` choisis dans l'UI | `match_start` porte en plus `duration_s` et `day_length` effectifs ; `preset` y reste le fichier exact, le hash reste valide. Le rejeu lit ces deux champs | L'UI permet de les changer (`ui/app.js:178`, `:183`) ; sans cela le rejeu finirait au mauvais tick |
| D16 | Un seul monde présent, sans snapshot | Lancement refusé avec un message | « Jamais deux mondes d'origine différente » |
| D17 | Étape `contract` pendant la fenêtre | L'étape passe `ok` après le contrôle statique, son détail continue d'être mis à jour dans `lobby`. Un refus la remet en `error` | AC68 : « passe l'étape `contract` et atteint `lobby` » |
| D18 | `pytest` sur une machine avec PZ | Les tests `pz_integration` ne démarrent de vrais serveurs que sous `-m pz_integration`. Sinon `skip` avec motif, sans `addopts` | Sans cela chaque `pytest` lancerait deux JVM sur cette machine |
| D19 | Chemin explicite invalide | `--pz-dir` ou `PZ_INSTALL_DIR` invalide échoue, sans repli sur les chemins Steam | Un choix explicite de l'utilisateur ne se contourne pas |
| D20 | Auteur de `build`, `vehicle_repair`, `explosion` | `side` vient de l'argument du hook s'il porte un joueur, sinon `null` | R8 interdit d'inventer un camp, et la spec ne définit l'auteur que pour `zombie_kill` et `damage_taken`. **Conséquence** : si `OnObjectAdded` ne porte pas d'auteur, `builder_payoff` ne se déclenche jamais. Mesuré par `--summary`, à constater en jeu |
| D21 | Plancher 25 dans le Lua | Constante de garde-fou | Exigé par § Mod duelcore ; c'est une sécurité, pas un équilibrage |
| D22 | Ligne complète et avance de `lastCmdSeq` côté mod | Une ligne de `in` est complète si et seulement si son `\n` a été lu. Le lecteur parcourt `in` caractère par caractère avec `BufferedReader.read()`, jamais avec `readLine()`. Un objet JSON entier sans `\n` reste incomplet. `lastCmdSeq` avance à la prise en charge, avant l'attente de `warn_s` et avant le `pcall` | Le contrat définit une ligne par son `\n` final. `readLine()` rend le même résultat avec ou sans ce `\n`, alors que `read()` le rend observable. Constaté hors du jeu, sur le Kahlua de `projectzomboid.jar` : `\n` vaut 10, la fin de fichier est négative, `é` survit à `string.char`. `BufferedReader` figure dans `LuaManager.Exposer`, et `getFileReader` lit en UTF-8. Avancer à l'application ferait reprendre la commande à chaque relecture pendant `warn_s`. Avancer avant le `pcall` garde « au plus une fois » même sur exception |
| D23 | Préparation et passe de création | `RealMatch` prépare le lancement **avant** `prepare_worlds`, qui reçoit `prepare_launch` et l'appelle avant de démarrer la passe de création. `prepare_launch` est rejoué après `prepare_worlds`, sur tous les chemins | Sans réglages, la passe n'a ni ports gérés, ni `RCONPassword` pour l'arrêt gradué, ni session pour le mod. Le second appel efface `caps` et `out` de la passe, et garde l'ordre de la spec sur le chemin de restauration |

Constats sur la machine, pris en compte dans les steps :

- `start-server.sh` est dans `<steam>/steamapps/common/ProjectZomboid/projectzomboid/`, pas à la racine de `ProjectZomboid/`.
- `version=42.21.0 4a0e9546ec` sort **avant** `*** SERVER STARTED ****` dans `server-console.txt`, et après dans `04-spike-0-preuves.md` : le build se lit quel que soit l'ordre.
- `duelA_SandboxVars.lua` contient `PopulationStartMultiplier` : « `Start*` » doit viser quatre clés exactes (`StartYear`, `StartMonth`, `StartDay`, `StartTime`), jamais un motif.
- `duelA.ini` a aujourd'hui `Mods=duelsidea` et `UDPPort=16262`, écrasés par les réglages gérés. `duelA` et `duelB` existent déjà : le premier lancement les capture en snapshot sans passe de création.

## Steps

### P1: Socle de test et fichiers de données
- **Files:** [pytest.ini, tests/conftest.py, tests/test_stdlib_only.py, service/runtime.json, service/effects.json, .gitignore]
- **Description:** Couvre R9, et la partie données de R2 et R4. `pytest.ini` : `testpaths`, `pythonpath = .`, marqueur `pz_integration`. `tests/conftest.py` : fixtures `zomboid_home` (dossier temporaire tenant lieu de `~/Zomboid`), `runtime` (copie modifiable de `runtime.json`), règle de `skip` de D18. `service/runtime.json` : ports, tous les délais et seuils de la spec et de D4, `tick_ms` 250, `hook_window_s` 120, `probe_timeout_s` 30, `beat_timeout_s` 30, `max_drift_s` 60, `max_server_restarts` 1, `attribution_min_kills` 100, `day_length_minutes` (1→15 … 7→240), `limits`, `probe_spawn`, `sandbox_start` (les 4 clés, valeurs du fichier réel), `mod_version_dir` `42.21`, `steam_app_id`. `service/effects.json` : `atoms` (5 atomes, `source: server`, `damage_taken` avec `hook_proof`), `effects` (obligatoires, facultatifs, `table: true|false`), `supply_items` (6 objets), `probe_spawn` en `table: false`. `.gitignore` : ajoute `matches/`, en conservant la ligne `.adversarial-plan/` non commitée.
- **Dependencies:** []
- **Tests:** AC62 `tests/test_stdlib_only.py::test_imports`, par `ast` et `sys.stdlib_module_names`, sur tout `service/`. Le même test parse avec `ast.parse(..., feature_version=(3, 11))` pour tenir la contrainte de syntaxe.
- **Risks:** Une valeur oubliée ici finirait en dur dans le code : P24 vérifie l'absence de littéraux de délai. Modification locale de `.gitignore` à préserver.

### P2: Validation du contrat v1
- **Files:** [service/protocol.py, tests/test_protocol.py]
- **Description:** Couvre R1. `parse_event`, `parse_command`, `parse_ack`, `parse_session`, `parse_beat`, purs, levant `ProtocolError(code, detail)`. Tout champ inconnu est ignoré, `v` ≠ 1 rejette, types stricts (les booléens ne sont pas des entiers). `kind` limité aux atomes du catalogue passé en argument, plus `player_death`, `caps`, `hook_seen`, `cmd_result`, `beat`. Sérialisation `dump_line` (JSON UTF-8 compact terminé par `\n`).
- **Dependencies:** [P1]
- **Tests:** AC1 `test_contract_examples_and_version`, avec les exemples de la spec copiés littéralement.
- **Risks:** Un validateur trop strict sur `data` rejetterait des lignes réelles : seuls les champs lus par le service sont contrôlés.

### P3: Transport, lecture de `out`
- **Files:** [service/transport.py, tests/test_transport.py, tests/doubles/__init__.py, tests/doubles/fake_mod.py]
- **Description:** Couvre R1. `OutReader(path, side, catalog)` : lecture **binaire** par offset en octets, seules les lignes complètes sont traitées, retour à 0 si la taille passe sous l'offset. Dédup lexicographique `(boot, seq)`, `seq_gap` dans un même `boot`, `bad_line` (JSON ou UTF-8 invalide), `side_mismatch` (`src` ≠ camp du fichier, ou `side` ∉ {`src`, `null`}). `poll()` rend les événements acceptés et les anomalies, sans jamais lever sur une ligne. `tests/doubles/fake_mod.py` écrit les fichiers du contrat et émet `caps`, `beat`, `hook_seen`, `cmd_result` sur demande.
- **Dependencies:** [P2]
- **Tests:** AC2 `test_partial_line_waits_for_newline`, AC3 `test_shrink_resets_offset_and_dedups`, AC4 `test_new_boot_restarts_seq_without_loss`, AC5 `test_gap_and_bad_line_are_visible`, AC6 `test_foreign_side_rejected`.
- **Risks:** Lire en texte fausserait l'offset dès le premier caractère accentué. Le `seq_gap` ne doit pas se déclencher au changement de `boot`.

### P4: Transport, `in`, `ack`, session, redémarrage
- **Files:** [service/transport.py, tests/test_transport.py]
- **Description:** Couvre R1. `CommandChannel(side, dir)` : écriture de `in`, `\n` compris, en un seul `write` plus `fsync`, service seul écrivain. `poll_ack()` : un `ack` vide ou illisible vaut « pas d'ack », car le mod le réécrit de façon non atomique. Troncature à 0 octet, sans suppression, seulement si `ack.boot` est courant et `ack.seq` égal au dernier écrit ; ack supérieur ou d'un autre `boot` : anomalie rendue à l'appelant. `ack_timeout` sans réémission. `write_session(match, boot, cmd_floor)`, écriture temporaire puis `os.replace`. `prepare_restart(unresolved_seqs)` : tronque `in`, écrit la session à `boot` + 1 et `cmd_floor`, rend les `lost_on_restart`. `reset()` vide `out`, `in`, `ack`.
- **Dependencies:** [P3]
- **Tests:** AC7 `test_truncate_only_on_full_ack`, AC8 `test_restart_is_at_most_once`.
- **Risks:** Garantie au plus une fois (non vérifiable par le typage) : écriture et troncature tournent sur le seul fil de `pump`, ce qui est documenté et non verrouillé par ailleurs. L'appelant doit vider `out` avant `prepare_restart` (traité en P21).

### P5: Journal JSONL
- **Files:** [service/journal.py, tests/test_journal.py]
- **Description:** Couvre R6. `Journal(dir, clock)` : `write(type, data, side=None, mt=None)` attribue `n` sans trou, `ts_ms`, écrit et vide chaque ligne, `fsync` si `type == "state"`. `mt` `None` est sérialisé `null`. `read_journal(path)` lit et sépare une dernière ligne incomplète, sans exception. Une liste de champs interdits refuse d'écrire `password`.
- **Dependencies:** [P1]
- **Tests:** Tests unitaires de `n`, du `fsync` de phase et de la dernière ligne coupée dans `tests/test_journal.py`. AC44 `test_sequenced_records` est complété en P10, car il exige un match synthétique.
- **Risks:** `n` doit rester exact si plusieurs fils écrivent : verrou interne, car les anomalies et les ticks sont écrits depuis `pump`, mais `cancel` peut journaliser depuis le fil HTTP.

### P6: Chargeur strict et migration des presets
- **Files:** [service/rules.py, service/presets/duel_rapide.json, service/presets/pression.json, service/presets/hardcore.json, service/presets/demo.json, tests/test_rules_load.py]
- **Description:** Couvre R4. `RUNTIME_PATH`, `EFFECTS_PATH`, `load_runtime()`, `load_effects()`. `load_table(raw, catalog, limits, file_stem=None, active_atoms=None)` : schéma strict à tous les niveaux, cohérence de `atoms` (`when`, `score.points`, `modifiers.combo`), bornes de `limits`, `items` et `supply_items`, refus de `probe_spawn` et `combo_reset`, messages français nommant la règle ou la section et le champ. `parse_when()` : grammaire exacte vers un petit AST. `active_atoms` restreint le catalogue pour la revalidation de la fenêtre de contrat. `rules_hash()` : SHA-256 du JSON canonique. Presets au schéma strict selon D1, D2, D3 ; `id` des règles de `06`, `hurt_supply` pour la caisse (exemple de la spec), `explosion_horde` et `repair_noise` pour `hardcore`. `pression` : `fog` devient `weather_rain` de 90 s, `shake` est retiré, résumé corrigé. `horde_area` de `pression` : `n` 25. Le chargement des presets de `Engine` n'est pas touché ici (P17), mais `load_presets()` actuel reste compatible car les clés lues par le mock existent toujours.
- **Dependencies:** [P1]
- **Tests:** AC30 `test_forbidden_tables` (17 cas paramétrés, le catalogue de test ajoutant un atome `source: client`), AC70 `test_supply_items_schema` (10 cas), première moitié d'AC31 `test_shipped_presets_and_data_driven` (complété en P9).
- **Risks:** `true` passe pour un entier en Python si le test de type est naïf (AC70 `count`=`true`). `id` égal au nom de fichier : `file_stem` obligatoire pour les fichiers livrés, pas pour les tables de test.

### P7: Évaluateur de règles et modificateurs
- **Files:** [service/rules.py, tests/test_rules.py]
- **Description:** Couvre R4 et R8. `RuleEvaluator` par camp, pur : `feed(event, te_ms)` rend les règles déclenchées. Atome avec `every`, agrégat consommant ses N occurrences, absence réévaluée sans consommer, série sur `]t, t+D]` jugée sur le `te` du `b`, réarmement qui repousse l'échéance. Fenêtres triées par `te` (D6), un retard s'y insère à sa place. Filtre `origin: duel` et `side` nul, score sur `te` ≥ 0. Modificateurs : état mis à jour avant l'évaluation, `combo` puis `score_gap`, `max(1, floor(n × produit))` en `Fraction` (D7), `defender_bonus` (D9).
- **Dependencies:** [P6]
- **Tests:** AC20 `test_count_within_consumes`, AC21 `test_every`, AC22 `test_absence_condition`, AC23 `test_series_window`, AC24 `test_combo`, AC25 `test_score_gap`, AC32 `test_duel_spawns_do_not_loop`, AC55 `test_null_side_never_credited`.
- **Risks:** AC24 exige que la 4e occurrence compte 4 (mise à jour avant évaluation). Une série dont la base est un agrégat consomme à l'armement, pas au déclenchement.

### P8: `MatchCore`, horloge à ticks et files par cible
- **Files:** [service/rules.py, tests/test_clock.py, tests/test_budget.py]
- **Description:** Couvre R4. `MatchCore(table, runtime, catalog, t0_ms, duration_s)`. `submit_event`, `submit_state`, `advance(now_ms)` : exécute tous les ticks non clos jusqu'au dernier multiple de `tick_ms` ≤ instant courant ; un `t_ms` en avance ne fait jamais avancer `mt`. Ordre dans un tick : événements triés `(t_ms, src, boot, seq)`, intrants du service, échéances, émission. `late_event` si `te` ≤ `mt` du dernier tick clos. Une file FIFO par camp cible, A puis B, tête servie tant que les 4 conditions tiennent (cooldown, `threats_per_minute`, `concurrent_effects`, serveur hors `restarting`). `seq` de commande strictement croissant, avec `reserve_seq(seq)` pour les sondes. Suivi des effets en cours et libération par `cmd_result`, `duration_s` comprise. Les enregistrements sortent dans l'ordre d'écriture attendu du journal.
- **Dependencies:** [P7]
- **Tests:** AC73 `test_ticks_advance_in_silence`, AC74 `test_late_event_next_tick`, AC75 `test_tick_order` (partie `match_end` complétée en P9), AC26 `test_cooldown_merges_not_drops`, AC72 `test_queues_are_per_target`.
- **Risks:** Comparer des `mt` flottants accumulerait une erreur : tout le cœur compte en entiers. Le tick 0 doit traiter ce qui est en attente sans qu'aucun tick ne soit « clos » : un `te` < 0 au tick 0 n'est pas un retard.

### P9: Budgets complets et fin de match dans le cœur
- **Files:** [service/rules.py, tests/test_budget.py, tests/test_clock.py, tests/test_rules_load.py]
- **Description:** Couvre R4 et une partie de R5. `cap_per_min` par règle et effet sur `]mt − 60, mt]`, excédent en `effect_capped`, admission partielle. Clé de lot (cible, type, paramètres sans `n`, `items` inclus), `effect_coalesced` pour les effets sans `n`. Tranches `max_units_per_command` : une tranche est **une** commande, le reste du lot reste en tête et repasse les conditions. `cmd_timeout` au premier tick où `mt` ≥ écriture + `cmd_result_timeout_s`, `submit_lost(seq)`, libération unique. `match_end` : minuteur en échéance, mort subite, `submit_end(result)` (D12), fin idempotente, aucun envoi dans le tick de fin, `effect_dropped` pour les lots restants. Résultat `win`/`draw`/`death`.
- **Dependencies:** [P8]
- **Tests:** AC27 `test_cap_per_min`, AC28 et AC77 `test_threats_and_concurrency`, AC29 `test_pending_dropped_at_end`, AC71 `test_supply_items_reach_command`, fin d'AC75 `test_tick_order`, fin d'AC31 `test_shipped_presets_and_data_driven`.
- **Risks:** Une place libérée deux fois (`cmd_timeout` puis `cmd_result` tardif) fausserait `concurrent_effects`. L'ordre `cmd_timeout` puis `command` dans le journal est testé par AC77.

### P10: Rejeu et télémétrie
- **Files:** [service/replay.py, tests/test_replay.py, tests/test_journal.py, tests/fixtures/replay_1000.jsonl, tests/fixtures/replay_clock.jsonl, tests/fixtures/summary_known.jsonl, tests/fixtures/restart_records.jsonl]
- **Description:** Couvre R6. `python -m service.replay <journal>` : relit `match_start`, construit un `MatchCore` neuf depuis `preset`, `runtime`, `effects`, `duration_s` (D15), sans lire `service/runtime.json` ni `service/effects.json` et sans importer `time`. Applique dans l'ordre de `n`, à son `mt`, `event`, `cmd_result`, `state`, `lost_on_restart`, `match_end`, réinjecte les sondes via `reserve_seq`. Parcourt les ticks de 0 au `mt` de `match_end` ou au dernier `mt` lu. Compare `command` et `late_event` : mêmes enregistrements, même ordre, même tick. Codes de sortie et messages de la spec : champ `runtime`, `effects` ou `t0_ms` manquant, `mt` hors grille, premier écart avec son `n`, ligne finale ignorée. `--summary` : par camp, commandes et unités par minute, unités plafonnées, événements à `side` nul, minutes sans événement, p95 de `latency_ms`.
- **Dependencies:** [P5, P9]
- **Tests:** AC45 `test_replay_matches_and_detects_tamper`, AC46 `test_truncated_tail`, AC47 `test_summary_values`, AC63 `test_replay_uses_journaled_runtime`, AC64 `test_replay_applies_restart_records`, AC76 `test_replay_same_clock_rule`, AC44 `test_sequenced_records`. Les fixtures de journaux sont produites par un flux scripté passant par `MatchCore` et `Journal`, puis figées, pour ne pas tester le rejeu contre lui-même sans vérité indépendante : AC64 et AC76 assertent aussi des valeurs littérales de `mt`.
- **Risks:** Un écart de sémantique entre `pump` et le rejeu n'apparaît que si `pump` passe par la même API ; P20 ajoute donc un rejeu du journal produit par le parcours complet. `state` doit être un intrant : l'oublier casse AC65 au rejeu.

### P11: Verdict
- **Files:** [service/verdict.py, tests/test_verdict.py]
- **Description:** Couvre R8. Fonctions pures sur les enregistrements d'un journal : `attribution(records, min_kills)`, intervalle H0..Hk propre à chaque camp, heartbeats à `kills` non nul et `mt` non nul, baisses à 0, bornes strictes sur `(boot, seq)`, camp à moins de 2 heartbeats à 0 ; `spawn_status(...)` ; `fallback(spawn, attribution)` selon la table ; `publish_capabilities(caps, hooks_seen)`, qui distingue enregistré et vu. CLI `python -m service.verdict <journal>` : JSON sur la sortie, code 0, code 1 sur journal illisible.
- **Dependencies:** [P5]
- **Tests:** AC54 `test_attribution_thresholds`, AC56 `test_fallback_table`, AC58 `test_hook_seen_distinct_from_registered`, AC66 `test_attribution_interval`.
- **Risks:** Un `zombie_kill` de `side` nul reste hors de `attribués` mais sa hausse compte dans `référence` : c'est ce qui rend le `ko` d'AC66.

### P12: Mod `duelcore`
- **Files:** [mods/duelcore/mod.info, mods/duelcore/42.21/mod.info, mods/duelcore/42.21/media/lua/server/DuelCore.lua, tests/test_mod_package.py]
- **Description:** Couvre R3. Avant d'écrire, vérifier dans les classes du jeu la signature de chaque hook utilisé (`OnObjectAdded`, `OnMechanicActionDone`, `OnThrowableExplode`), pour D20. Camp par `getServerName()`, inerte sinon. Lecture de `duel_X_session.txt`, `boot`, `cmd_floor` ; session absente ou illisible : aucune commande traitée, le plancher étant inconnu. Boucle sur `Events.OnTick` cadencée par `getTimestampMs()` : vidage de `out` au plus 1 s, relecture de `in` au plus 1 s, `ack` au plus 5 s, `beat` à 10 s (`EveryOneMinute` suivrait le temps de jeu). `caps` en première ligne, sondes des hooks et des fonctions, `hook_seen` à la première réception. **Lecteur de `in`** (D22), dans cet ordre : (1) `lastCmdSeq` vaut `cmd_floor` au démarrage et vit en mémoire ; (2) à chaque passage, `readCommandLines`, seule fonction à ouvrir `in`, le relit depuis le début par `reader:read()`, un caractère à la fois : le code 10 clôt une ligne, un code négatif marque la fin du fichier, et `readLine()` n'y est pas utilisé (D22) ; (3) le reste sans `\n` à la fin du fichier est une ligne incomplète, même si son JSON est entier : il n'est pas décodé, n'avance pas `lastCmdSeq` et sera relu au passage suivant ; le service écrivant seul et en ajout, il ne peut être que la dernière ligne. Une ligne terminée mais indécodable, sans `seq` entier ou `kind` chaîne, ou de `v` différent de 1, est rejetée sans résultat et sans avancer `lastCmdSeq` ; les suivantes sont traitées ; (4) une ligne de `seq` inférieur ou égal à `lastCmdSeq` ou à `cmd_floor` est ignorée, sans résultat ; (5) sinon `lastCmdSeq` prend `seq` **tout de suite**, et la commande entre dans la file `pending` avec l'échéance `getTimestampMs() + warn_s × 1000` ; (6) la boucle applique sous `pcall` les commandes échues et écrit un seul `cmd_result` par `seq`. `ack` porte `boot` et `lastCmdSeq` : il signifie « pris en charge » et n'attend pas `warn_s`. Une troncature de `in` ne touche ni `lastCmdSeq` ni `pending`. Décodeur JSON local, pour le seul format des commandes, le mod étant sans dépendance. Capture : `zombie_kill` (`getAttackedBy()`, sinon dernier attaquant via `OnHitZombie`, sinon `null`), `damage_taken`, `build`, `vehicle_repair`, `explosion`, `player_death` (`side` = `src`). Zombies du duel marqués en `getModData()` pour `origin`. Table de traitement par `kind`, dont `probe_spawn` : attente de `warn_s`, refus (joueur endormi, en véhicule, absent, `distance[0]` < 25), `pcall` donnant `failed`, `unknown_kind`, `supply_drop` qui crée tout par `instanceItem` avant de déposer (`unknown_item`, rien de déposé). Aucune valeur d'équilibrage.
- **Dependencies:** [P1]
- **Tests:** AC16 `test_layout_and_forbidden_calls`, AC17 `test_no_balance_and_effects_covered`, AC69 `test_supply_content_not_in_mod`, plus `test_command_reader_guards` dans `tests/test_mod_package.py` : `readCommandLines` appelle `:read()`, compare à 10 et n'appelle pas `readLine` ; le lecteur compare `seq` à `lastCmdSeq` et à `cmd_floor`, et l'affectation de `lastCmdSeq` précède l'entrée dans `pending`. Contrôles statiques seulement : aucun de ces tests ne prouve le comportement du Lua, et aucun interpréteur Lua autonome n'est présent ici. Ce comportement est prouvé en P23 par `test_command_reader_at_most_once`, AC18 et AC19.
- **Risks:** Le plus incertain du plan, car rien ne s'exécute ici. Les noms du catalogue contiennent `spawn_pop`, etc. : AC17 ne doit interdire que les `id` de règles et les clés de budget ou de score. `Base.` ne doit apparaître dans aucun commentaire (AC69). Sans `pcall` autour de chaque traitement, une exception couperait la boucle. Un `seq` avancé à l'application, et non à la prise en charge, doublerait chaque effet dont `warn_s` dépasse 1 s, sans qu'aucun test du service ne le voie. Une commande en `pending` à l'arrêt du serveur est perdue, ce que `lost_on_restart` couvre. Un retour à `readLine()`, ou un test sur le seul JSON, appliquerait une commande dont le `\n` n'est pas arrivé. `read()` n'a été constaté qu'hors du jeu, avec un exposeur de test : seul P23 le confirme à travers le vrai `LuaManager`. Un appel par caractère reste acceptable, car `in` est court et tronqué après ack.

### P13: Préflight et installation du mod
- **Files:** [service/preflight.py, service/main.py, tests/test_preflight.py, tests/test_integration_pz.py]
- **Description:** Couvre R2 et R9. `detect_pz(pz_dir, env, home)` : `--pz-dir`, `PZ_INSTALL_DIR`, puis les chemins Steam connus, **avec le sous-dossier `projectzomboid/`**, `start-server.sh` exécutable (D19). `run_preflight(...)` : six ports par liaison réelle (UDP pour le jeu, TCP pour RCON), `MemAvailable`, mod (`<version>/mod.info`, `media/lua/server/`, empreinte SHA-256 sur chemins relatifs triés et contenus), `.ini` présents lisibles, table valide. Rend les valeurs mesurées et un message français par défaut. `install_mod(src, zomboid_dir)` : copie temporaire puis remplacement de `duelcore/` seul. `main.py` : `--pz-dir`, `--install-mod` (installe, affiche, quitte avec 0).
- **Dependencies:** [P6, P12]
- **Tests:** AC9 `test_each_failure_is_named` (4 cas plus le cas nominal, `install_mod` compris), AC60 `tests/test_integration_pz.py::test_detection_reports_absent`, qui lance un `pytest -m pz_integration` enfant avec `HOME` temporaire et `PZ_INSTALL_DIR` vide.
- **Risks:** Test de port flaky si un service tourne déjà : le cas nominal utilise des ports libres pris dans un `runtime` de test. `--install-mod` ne doit rien toucher hors de `duelcore/` (les mods `duelsidea` existent). Récursion de `pytest` évitée en ne sélectionnant que le test `pz_integration`.

### P14: Réglages gérés, RCON, expurgation
- **Files:** [service/servers.py, service/rcon.py, tests/test_servers.py, tests/doubles/fake_server.py]
- **Description:** Couvre R2 et R7. `write_ini(path, managed)` : réécrit les seules clés gérées, ajoute celles qui manquent, conserve commentaires et octets (lecture en `surrogateescape`), écriture atomique, `0600`. `write_sandbox(path, day_length, start)` : quatre clés exactes plus `DayLength`, ancrées en début de ligne (jamais `PopulationStartMultiplier`). `read_ini()` pour le contrat. `launch_command(side, cfg)` : `-servername`, `-port`, `-udpport`, `-adminpassword`. `Redactor(secrets)` : expurgation par ligne de toute sortie, de tout message d'exception. `gen_secret()` par `secrets`. `RconClient` stdlib (protocole Source, `save`, `quit`, délais courts, sans jamais journaliser le paquet d'authentification). `tests/doubles/fake_server.py` : sortie scriptée, enfant lancé, refus de quitter, crash, RCON factice.
- **Dependencies:** [P1]
- **Tests:** AC10 `test_managed_keys_only`, AC11 `test_launch_arguments`, plus `test_rcon_save_quit` et un cas `PopulationStartMultiplier` intact dans `tests/test_servers.py`.
- **Risks:** Les commentaires français de `duelA.ini` ne doivent pas être réencodés. PZ peut réécrire le `.ini` au démarrage et en relâcher les droits : `0600` est réappliqué après disponibilité (P15). `Password` vient de l'utilisateur : un saut de ligne injecterait des clés, il est refusé en P17.

### P15: Démarrage et arrêt gradué
- **Files:** [service/servers.py, tests/test_servers.py, tests/doubles/fake_server.py]
- **Description:** Couvre R2. `ServerProcess(side, ...)` : `Popen` avec `start_new_session=True`, `cwd` = dossier d'installation, stdin en tube, stderr fusionné. Un fil de lecture vide toujours le tube, expurge, écrit `matches/<id>/server_X.log`, garde une queue de log. Disponibilité : `*** SERVER STARTED ****` **puis** `server is listening on port <port>` exact (mot entier). Build par `version=(\S+)` quel que soit l'ordre. `ServerPair` : départ simultané, une sortie anticipée ou un délai dépassé arrête l'autre. Arrêt gradué : RCON `save` puis `quit`, sinon `quit` sur stdin, SIGTERM au groupe après `stop_timeout_s`, SIGKILL 10 s plus tard. L'arrêt n'est fini que quand `os.killpg(pgid, 0)` échoue : le script peut sortir avant son enfant `java`. `exited_unexpectedly()` distingue un arrêt demandé.
- **Dependencies:** [P14]
- **Tests:** AC12 `test_readiness_paths`, AC14 `test_graded_stop_kills_group`.
- **Risks:** Tube non vidé : le serveur se bloque sans erreur. Un `wait()` sur le seul script laisserait `java` vivant, d'où le contrôle de groupe. Le délai de 10 s avant SIGKILL vit dans `runtime.json` (`kill_grace_s`), sinon AC14 durerait trop.

### P16: Snapshots
- **Files:** [service/snapshots.py, tests/test_snapshots.py]
- **Description:** Couvre R5. `SnapshotStore(zomboid_dir)` : `has`, `capture`, `restore`. Restauration seulement serveurs arrêtés et ports de jeu libres, copie dans un dossier temporaire du même système de fichiers, remplacement par `rename` seulement après copie complète, l'ancien monde gardé jusqu'à la bascule. `prepare_worlds(pair, store, *, prepare_launch)` : snapshots des deux côtés, restaurer ; d'un seul côté, refus ; aucun snapshot et deux mondes, capturer ; aucun snapshot et aucun monde, passe de création (`prepare_launch()`, puis démarrage, disponibilité, arrêt, capture, D23) ; un seul monde, refus (D16). `prepare_launch` est obligatoire et n'est appelé ici que par la passe de création, avant tout démarrage ; son contenu appartient à `RealMatch` (P18). Un échec de la passe ne capture rien. Fonction de copie injectable.
- **Dependencies:** [P4, P14, P15]
- **Tests:** AC40 `test_restore_exact`, AC41 `test_restore_guards` (3 cas plus le snapshot d'un seul côté), AC42 `test_first_run_creation_pass`, complété en P18. Ici, sur `prepare_worlds` : le `prepare_launch` du test appelle les vrais `write_ini`, `write_session` et `reset()`, et le double de serveur relève au démarrage ce qu'il trouve sur le disque. Ordre attendu : `prepare_launch`, démarrage de A et B, disponibilité, arrêt, capture de A et B ; au démarrage, `.ini` géré et session `boot`=1 présents, `in`, `out`, `ack` vides. Sur les chemins restauration et capture, `prepare_launch` n'est pas appelé.
- **Risks:** Destruction du monde sur panne : aucun `rmtree` avant la bascule. `duelA` et `duelB` réels existent : un défaut ici écraserait un vrai monde, les tests n'utilisent donc que `zomboid_home`. Une passe interrompue après la création d'un seul monde laisse le cas « un seul monde » : le lancement suivant est refusé (D16), avec un message qui le nomme.

### P17: État et séparation mock / réel
- **Files:** [service/state.py, service/engine.py, tests/test_modes.py]
- **Description:** Couvre R7 et l'appui de R2. `MatchState` : ajouts `match_id`, `capabilities`, `result`, sans retrait de clé. `Engine(mode, *, zomboid_dir=None, pz_dir=None, runtime=None, effects=None, clock=None, matches_dir=None, steam_launcher=None, server_factory=None)`, valeurs réelles par défaut. `_begin` garde le mode demandé. `load_presets()` strict : preset invalide écarté, erreur consignée. `preset()` : refus en réel, comportement actuel en mock. Mock : étapes et logs à « simulé », sans valeur mesurée ni port (`engine.py:193`, `:200`, `:207`, `:222`), message d'accueil selon le mode. Réel : `simulate_join` refusé ; `open_game` par Steam, `+connect 127.0.0.1:<port A>` et `+password`, `ok=false` sans Steam ; mot de passe et pseudo refusés s'ils portent un retour à la ligne ou `=` en tête ; `_run_real` exécute le préflight puis s'arrête en `error` avec un message français explicite, jusqu'à P18.
- **Dependencies:** [P6, P13]
- **Tests:** AC48 `test_mock_never_claims_real`, AC49 `test_real_never_falls_back`, AC50 `test_unknown_preset_refused`, AC51 `test_open_game_real`, AC53 `test_api_backward_compatible`.
- **Risks:** Changement d'API : voir la table des appelants. Le parcours mock doit rester identique pour l'UI (`ui/app.js` non modifié). Le message d'arrêt de P17 est transitoire : P18 le retire, P24 vérifie qu'il n'en reste rien.

### P18: Lancement réel et contrat statique
- **Files:** [service/engine.py, service/servers.py, tests/test_contract.py, tests/test_servers.py, tests/test_snapshots.py]
- **Description:** Couvre R2 et R5. `RealMatch`, préparation dans cet ordre (D23) : (1) `match_id`, dossier `matches/<id>/`, secrets tirés, `Redactor` armé, car la passe de création écrit déjà `server_X.log` ; (2) `prepare_launch` défini, idempotent, mêmes secrets à chaque appel : réglages gérés, sessions `boot`=1 et `cmd_floor`=0, `reset()` du transport ; (3) `prepare_worlds(pair, store, prepare_launch=prepare_launch)` ; (4) `prepare_launch()` sur tous les chemins, ce qui donne l'ordre de la spec après une restauration, et efface après une création `caps`, `out` et toute réécriture du `.ini` par PZ ; (5) `Journal` et `match_start` sans secret ni `password`, avec `runtime`, `effects`, D15 ; (6) démarrage, étapes `serverA`, `serverB`. `verify_contract(...)` dans `servers.py`, sur les fichiers **relus** : `Mods`, `WorkshopItems`, `PauseEmpty`, ports distincts, `DayLength`, `Start*`, `caps` reçus dans `contract_timeout_s`, `protocol`, `mod`, `build` égal à celui du log, atomes et effets de la table dans `caps` des deux camps. Aucun `hook_seen` jugé. Un écart refuse et nomme clé et valeurs, jamais `RCONPassword`. `shutdown()` unique, emprunté par `cancel`, erreur, fin. `cancel` pendant `starting` interrompt l'attente.
- **Dependencies:** [P4, P5, P16, P17]
- **Tests:** AC13 `test_each_mismatch_refused` (6 cas plus conforme), AC15 `test_every_exit_stops_servers` pour `cancel`, dont `cancel` pendant la passe de création, et l'erreur de contrat (cas SIGTERM en P22), fin d'AC42 `test_first_run_creation_pass` par `RealMatch` : 2 lancements par serveur, 2 snapshots avant le lobby ; à **chaque** lancement, réglages gérés, session `boot`=1 et `cmd_floor`=0 déjà écrits ; au second, `out`, `in`, `ack` vides, donc aucun `caps` de la passe lu par le contrat.
- **Risks:** H1 : si PZ ne garde pas un `.ini` partiel, la relecture le détecte et refuse. Course entre `cancel` et `Popen` : le fil de lancement revérifie `_stop` après chaque `start`. Le `reset()` du point (4) n'a lieu que serveurs arrêtés, sinon un mod encore vivant écrirait dans `out` après le vidage : `prepare_worlds` ne rend la main qu'une fois l'arrêt gradué fini (P15). Un échec de la passe de création emprunte `shutdown()`.

### P19: Lobby, sondes, fenêtre de contrat, décompte
- **Files:** [service/engine.py, tests/test_contract.py, tests/test_match.py]
- **Description:** Couvre R2, R3, R5, R8. `pump` en lobby : `players` depuis les `beat`, aucune panne sans heartbeat. `probe_spawn` à la première connexion de chaque camp (commande à `mt` nul, `seq` réservé, D5), `ko` sur résultat non `applied`, `spawned` < 1, ou `probe_timeout_s` : `fallback` `solo_ipc`, `error` nommant camp, fonction et branche, serveurs arrêtés. `ContractWindow` : ouverte au premier `caps`, `L` quand les deux joueurs sont là et les sondes résolues, clôture selon la spec, `atom_withdrawn`, `active_atoms`, `withdrawn`, revalidation par `load_table(active_atoms=...)`, refus sans toucher `fallback`, décision figée. Détail de `contract` (hook, camps, temps restant, D17). Recontrôle de `rules_hash` sur le fichier avant le décompte. Décompte 3-2-1 sur l'instant courant.
- **Dependencies:** [P11, P18]
- **Tests:** AC67 `test_unseen_hook_withdraws_atom` (3 cas), AC68 `test_hook_window_outlives_contract_step` (partie redémarrage complétée en P21), AC33 `test_rules_hash_sealed` (l'octet modifié est dans une valeur : un blanc ne change pas le JSON canonique), AC57 `test_spawn_probe_failure_is_signalled`.
- **Risks:** Sans coup porté dans les 120 s, `duel_rapide` et `pression` sont refusés sur les deux camps : voulu par la spec, à dire dans le README. Une table sans `damage_taken` publie quand même le retrait (AC67 cas 2).

### P20: Match réel : ticks en direct, score, dérive, forfait
- **Files:** [service/engine.py, tests/test_match.py]
- **Description:** Couvre R5, R6, R8. Entrée en `running` : `t0_ms`, `state` à `mt`=0, `MatchCore`. `pump` : `poll` de `out`, anomalies au dernier tick clos, `submit_event`, `advance`, journalisation en sortie du cœur, écriture des `command` sur le canal, `poll_ack`. Publication `score`, `clock`, `threats_per_min`, `events`. Dérive à chaque `beat` (`age` de référence, `round(..., 6)` avant comparaison stricte à `max_drift_s`). Suspect à `beat_timeout_s`, absence selon D13, `forfeit`, double absence `invalidated`, `match_end` soumis au cœur. `kills` de référence. À la fin : `verdict` relu sur le journal, `result`, mention d'attribution dans `reason`, `logs` en `warn`, `match_end`, `shutdown()`, `stopping`, `over`.
- **Dependencies:** [P10, P19]
- **Tests:** AC34 `test_full_real_path`, AC35 `test_outcomes`, AC36 `test_disconnect_grace`, AC37 `test_drift_real_seconds`, AC39 `test_beat_silence_by_phase`, AC59 `test_attribution_ko_reported_at_end`. `test_full_real_path` rejoue en plus son journal par `service.replay` et attend le code 0 : c'est la preuve que `pump` et le rejeu sont d'accord.
- **Risks:** Non-trivial majeur : ordre d'écriture du journal contre ordre du tick. Un `(3 + 1,6) − 3` donne 1,5999… : l'arrondi est nécessaire à AC37. Le verrou de `Engine` ne doit pas être tenu pendant `fsync` ou un arrêt de serveur, sinon `/api/state` se fige.

### P21: Redémarrage en match et revanche
- **Files:** [service/engine.py, tests/test_match.py, tests/test_contract.py]
- **Description:** Couvre R1 et R5. Sortie inattendue pendant `running` : **dernier `poll` de `out` d'abord**, pour que les `cmd_result` déjà écrits ne deviennent pas de faux `lost_on_restart`, puis `prepare_restart`, `state` `restarting` et `lost_on_restart` soumis au cœur, relance du même monde sans restauration, attente de disponibilité hors du fil de `pump` pour que le minuteur et l'autre camp continuent. Grâce suspendue, repart de zéro à `up`. Deuxième sortie : `invalidated`. Une sortie hors `running` reste une erreur. Revanche : `rematch` en réel garde `code` et `password` (`_begin` les régénère aujourd'hui), nouveau `match_id`, `boot` à 1, restauration par la même préparation que P18 (`prepare_worlds`, puis `prepare_launch`), fenêtre de contrat rouverte puisque c'est un nouveau match.
- **Dependencies:** [P20]
- **Tests:** AC38 `test_restart_in_match`, AC65 `test_no_command_while_restarting`, AC43 `test_rematch`, fin d'AC68 `test_hook_window_outlives_contract_step`.
- **Risks:** Une attente bloquante figerait les ticks. La disponibilité du serveur relancé arrive sur un autre fil et revient en intrant à `pump`, sans toucher `MatchCore` directement.

### P22: Secrets et arrêt sur signal
- **Files:** [service/main.py, service/engine.py, tests/test_secrets.py, tests/test_servers.py, tests/doubles/run_service.py]
- **Description:** Couvre R2 et R7. `main.py` : gestion de SIGINT et SIGTERM, `engine.shutdown()` puis arrêt du serveur HTTP **depuis un fil dédié**, car `httpd.shutdown()` appelé depuis le fil de `serve_forever` se bloque. `.duel-service.json` retiré. Le garde-fou `except Exception` de `_run` passe par le `Redactor`. Relecture de chaque chemin d'écriture : `state`, `logs`, `matches/`, lignes de commande journalisées. `tests/doubles/run_service.py` lance `main` avec un `Engine` de test.
- **Dependencies:** [P21]
- **Tests:** AC52 `test_no_secret_leak` (marqueurs sur tout le parcours, double qui les imprime, y compris une exception les citant), AC15 cas SIGTERM de `test_every_exit_stops_servers`.
- **Risks:** Un message d'exception contenant la commande est la fuite la plus probable. La redondance SIGINT puis SIGTERM ne doit pas lancer deux arrêts : `shutdown()` est idempotent.

### P23: Intégration PZ et README
- **Files:** [tests/test_integration_pz.py, README.md]
- **Description:** Couvre R3, R7, R8, R9. `test_caps_from_real_mod`, `test_real_servers_boot_contract_stop`, `test_command_reader_at_most_once`, `test_live_journal_checklist`, qui lit `PZ_LIVE_JOURNAL` et `PZ_LIVE_STATE`, `skip` sans l'une d'elles, échec si les deux sources se contredisent sur le retrait. README : `--mode real`, `--install-mod`, `--pz-dir`, copie manuelle du mod chez l'invité (H2), procédure de verdict en jeu, `python -m service.replay` et `service.verdict`, besoin de frapper un zombie dans `hook_window_s`, section « Ce qui est simulé » mise à jour. Exécuter `pytest -m pz_integration` sur cette machine et consigner le résultat : c'est le premier contact avec le vrai jeu. H1, H3 et les signatures de hooks y sont confirmées ou contredites.
- **Dependencies:** [P22]
- **Tests:** AC18 `test_caps_from_real_mod`, AC19 `test_live_journal_checklist`, AC61 `test_real_servers_boot_contract_stop`. `test_command_reader_at_most_once` (`pz_integration`, exigence R1 sans AC propre, sans joueur car une commande sans joueur finit `refused` après `warn_s`) : session à `cmd_floor`=2, `in` jamais tronqué par le test. `seq` 1 et 2 ne donnent aucun `cmd_result`. `seq` 3 avec `warn_s`=3 donne `ack` à 3 et un seul `cmd_result`, relevé après plusieurs passages pendant et après l'attente. La ligne 3 réécrite n'en donne pas d'autre. La ligne 4 écrite à moitié ne donne ni résultat ni `ack` à 4, puis sa fin ajoutée donne un seul `cmd_result`. La ligne 5, objet JSON entier sans `\n`, ne donne ni résultat ni `ack` à 5 après plusieurs passages, puis le seul `\n` ajouté donne `ack` à 5 et un seul `cmd_result`. **Un `skip` n'est pas une preuve** : AC19 exige un joueur et reste ouvert tant que le procédé du README n'a pas été déroulé par une personne.
- **Risks:** Ce test écrit dans le vrai `~/Zomboid` (réglages de `duelA`, `duelB`, snapshots) : l'exécuter avec le premier snapshot des mondes existants. Tout écart de Lua découvert ici repasse par P12, dont les tests statiques ne peuvent pas le voir. Sans PZ, `test_command_reader_at_most_once` est en `skip` : le lecteur de `in` reste alors non prouvé, et le rapport de P24 doit le dire.

### P24: Revue de branche complète avant PR
- **Files:** [service/protocol.py, service/transport.py, service/rules.py, service/journal.py, service/replay.py, service/verdict.py, service/preflight.py, service/servers.py, service/rcon.py, service/snapshots.py, service/engine.py, service/state.py, service/main.py, mods/duelcore/42.21/media/lua/server/DuelCore.lua, README.md]
- **Description:** Couvre R1 à R9 pris ensemble. Porte obligatoire avant la livraison : revue de `git diff main...HEAD` sur tous les fichiers à la fois, pas commit par commit. Une revue par commit rate par construction les bugs inter-fichiers : chaque commit peut être correct seul et faux une fois combiné, car décomposer en composants n'est pas intégrer. Points à contrôler ici : (1) contrat, mêmes clés et types entre `protocol.py`, `fake_mod.py` et `DuelCore.lua`, pour que le double ne masque pas un écart avec le vrai Lua ; (2) un seul chemin d'horloge entre `pump` et `replay` ; (3) chaque chemin d'erreur passe par `shutdown()` et le `Redactor` ; (4) aucune valeur de délai, seuil ou équilibrage dans le code ; (5) aucun fichier hors de `targets`, aucune trace du message transitoire de P17 ; (6) `python3.11 -m compileall service tests` ; (7) `.venv/bin/pytest` complet, sans test relâché ni `skip` non motivé ; (8) chaque ligne du § Traçabilité ; (9) au plus une fois de bout en bout : lecture de `in` jusqu'au `\n` et `lastCmdSeq` de `DuelCore.lua`, `cmd_floor` et troncature de `transport.py`, `prepare_restart` en P21 ; (10) ordre de préparation : aucun démarrage de serveur, passe de création comprise, ne précède `prepare_launch`. À signaler sans corriger, hors spec : `/api/action` accepte toute requête locale, y compris celle d'une page web, alors que l'action lance désormais de vrais processus.
- **Dependencies:** [P23]
- **Tests:** Suite complète, `pytest -m pz_integration`, rejeu d'un journal de parcours complet, et confrontation d'un flux `fake_mod` aux champs effectivement écrits par `DuelCore.lua`.
- **Risks:** Les défauts trouvés ici peuvent toucher plusieurs steps : ils se corrigent avec leur test, sans relâcher d'assertion.

## Ordering rationale

Le plan va du déterministe vers le système, pour que chaque couche soit testée avant d'être utilisée.

- **P1 d'abord** : aucun step ne peut lire une valeur ni lancer `pytest` sans lui.
- **P2 à P5 avant le moteur** : le contrat de transport et le journal sont les frontières que le reste consomme. P4 suit P3 car ils partagent `transport.py`.
- **P6 → P7 → P8 → P9** : un évaluateur a besoin de tables valides, l'horloge a besoin de l'évaluateur, et les budgets complets ont besoin de l'horloge. Les presets migrent en P6, ce qui est sûr : le mock ne lit que des clés conservées.
- **P10 juste après P9** : le rejeu est la preuve de déterminisme. Il doit exister avant que `RealMatch` ne dépende de `MatchCore`, pour que tout écart apparaisse sur des fixtures et non sur un parcours complet.
- **P11 à P16 sont indépendants du moteur** et préparent les briques système, sans toucher `engine.py`. P12 précède P13 car le préflight calcule l'empreinte du mod. P16 suit P4, P14 et P15 : la passe de création démarre de vrais serveurs, et ses prérequis sont déjà testés quand elle les appelle.
- **P17 à P22 modifient `engine.py` un à la fois**, dans l'ordre du déroulé : mode, lancement, lobby, match, redémarrage, signaux. Ils sont séquentiels pour éviter tout conflit sur ce fichier. P22 arrive en dernier car l'absence de fuite ne se prouve que sur le parcours complet.
- **P23 après tout** : le vrai jeu ne peut confirmer que du code terminé.
- **P24 en dernier** : la revue de branche n'a de sens que sur l'ensemble.

## Caller enumeration

Recherche `grep -rn` sur `service/` et `ui/`, lignes vérifiées sur le code actuel (la spec cite `engine.py:61` pour `to_dict`, l'appel est à `:63`). Aucun appelant externe connu. Angle mort : le JS de l'UI n'est couvert que par ses chemins d'API littéraux.

| File | Function/Method | Migration Note |
|------|----------------|----------------|
| `service/main.py:37` | `Engine(mode=args.mode)` | Devient `Engine(mode=args.mode, pz_dir=args.pz_dir)` ; les autres arguments sont facultatifs (P13, P17) |
| `service/main.py:39` | `make_server(engine, port=port)` | Inchangé |
| `service/api.py:62`, `service/api.py:86` | `Engine.snapshot()` | Signature inchangée ; dict enrichi de `match_id`, `capabilities`, `result`, `config.rules_hash` |
| `service/api.py:65` | `Engine.presets()` | Inchangé ; `id`, `label`, `tagline`, `summary`, `duration_s`, `day_length` conservés, nouvelles clés ajoutées |
| `service/api.py:85` | `Engine.action(name, payload)` | Inchangé ; `simulate_join` et `open_game` branchés sur le mode (P17) |
| `service/engine.py:57` | `load_presets()` (définie à `:25`) | Stricte : preset invalide écarté, erreur consignée (P17) |
| `service/engine.py:114` | `Engine.preset(id)` | Id inconnu : refus en réel, comportement actuel en mock (P17) |
| `service/engine.py:53`, `service/engine.py:127` | `MatchState(mode=...)` | `:127` cesse d'écrire `mode="mock"` en dur (P17) |
| `service/engine.py:63` | `MatchState.to_dict()` | Ajouts seulement, 15 clés conservées (AC53) |
| `service/engine.py:90` | `Engine._begin(cfg)` depuis `rematch` | En réel garde `code` et `password` (P21) ; mock inchangé |
| `service/engine.py`, `RealMatch` (nouveau, P18) | `prepare_worlds(pair, store, *, prepare_launch)` (nouvelle, P16) | Seul appelant avec la revanche ; `prepare_launch` obligatoire, pour qu'aucun appelant ne lance une passe de création sans préparation |
| `service/engine.py`, revanche (P21) | `prepare_worlds(pair, store, *, prepare_launch)` | Même séquence que P18 ; chemin de restauration, `prepare_launch` rejoué après |
| `ui/app.js:24`, `ui/app.js:36`, `ui/app.js:394` | `/api/state`, `/api/action`, `/api/presets` | Aucun changement ; l'UI ignore les nouveaux champs, et `S.mode` règle déjà le badge et le bouton de simulation |

## Traçabilité

| Exigence | Steps | Critères |
|---|---|---|
| R1 | P2, P3, P4, P12, P21, P23 | AC1 à AC8 ; côté mod, `test_command_reader_at_most_once` |
| R2 | P1, P13, P14, P15, P18, P19, P22 | AC9 à AC15, AC68 |
| R3 | P12, P19, P23 | AC16 à AC19, AC67, AC69 |
| R4 | P6, P7, P8, P9, P19 | AC20 à AC33, AC70 à AC75, AC77 |
| R5 | P9, P16, P18, P19, P20, P21 | AC34 à AC43, AC65 |
| R6 | P5, P10, P20 | AC44 à AC47, AC63, AC64, AC76 |
| R7 | P14, P17, P22, P23 | AC48 à AC53 |
| R8 | P7, P11, P19, P20, P23 | AC54 à AC59, AC66 |
| R9 | P1, P13, P23 | AC60, AC61, AC62 |

Cibles de la spec : tous les fichiers de `targets` sont affectés à au moins un step. `tests/` reçoit uniquement les fichiers nommés par les critères, `tests/conftest.py`, `tests/doubles/` et `tests/fixtures/`.

Findings de revue traités (leurs identifiants `P1` et `P2` sont ceux de `findings.json`, pas des steps) :

| Finding | Sujet | Steps | Preuve |
|---|---|---|---|
| `P1` | Lecteur de `in` du mod : doublons, lignes incomplètes, dont un JSON entier sans `\n`, avance du `seq` | P4, P12, P23, P24, D22 | `test_command_reader_guards`, `test_command_reader_at_most_once` |
| `P2` | Passe de création avant ses prérequis | P16, P18, P21, P24, D23 | AC42 `test_first_run_creation_pass`, en P16 et P18 |

Preuves qui resteront hors de portée sans joueur : AC19, H2, la visibilité côté client du spawn, l'auteur de `build` et d'`explosion` (D20), le verdict d'attribution. Ces points se constatent par la procédure du README.
