# SPEC MVP — Duel PZ

Statut : spécification consolidée, décisions verrouillées le 04/11/2026.
Documents de référence : `01-etat-des-lieux.md` (faisabilité), `02-archi-deux-serveurs.md` (archi), `03-mecanismes-et-telemetrie.md` (règles/télémétrie), `04-spike-0-preuves.md` (preuves), `05-observable-vs-generable.md` (inventaire), `06-play-flow-et-table-de-relations.md` (flow).

## 1. Décisions verrouillées

| Sujet | Décision |
|---|---|
| Architecture | deux serveurs dédiés sur la machine d'un joueur (VPS ensuite) |
| Pause | aucune ; matchs courts (20-30 min), `DayLength` 1-2 |
| Sécurité de l'hôte | admin tenu par l'arbitre, jamais par le joueur hôte |
| Transport | fichiers dans `~/Zomboid/Lua/` + RCON localhost |
| Granularité | lots de 10-15 s (temps réel en v2) |
| Aucune correction d'horloge | invalidation au-delà de ±60 s de dérive |
| Règles | table déclarative YAML, atomes + combos + séries |
| Contrat | hôte seul édite, l'invité accepte, hash de la table |
| Mods | liste imposée par le preset, vérifiée identique des deux côtés |
| Menaces | canal MONDE (serveur) + 2 effets EXPÉRIENCE en v1 (fog, shake) + META |
| Sources | S1 (serveur) + S3 (dérivé) uniquement ; jamais `AddXP`/`LevelPerk` |

## 2. Périmètre

### Dans la v1
- deux serveurs, deux mondes, un match de 25 min, mort subite ou score.
- 6 atomes serveur : `zombie_kill`, `damage_taken`, `build`, `vehicle_repair`, `explosion`, `idle_check` (dérivé).
- 8 effets : `spawn_pop`, `spawn_sprinter`, `horde_area`, `noise`, `fog`, `shake`, `power_off`, `supply_drop`.
- table YAML, contrat avec hash, journal JSONL, replay.
- snapshot/restore des deux mondes entre matchs.
- 3 presets : Duel rapide, Pression, Hardcore.
- mode ghost (pression scriptée pour équilibrer à un joueur).

### Hors v1
- temps réel event-par-event (agent Java), matchmaking, classement, anti-triche, map dédiée, modes asymétriques autres que handicaps, effets NPC/animaux du catalogue Chaos, éditeur graphique complet.

## 3. Composants

| Composant | Langage | Responsabilité | Ne fait jamais |
|---|---|---|---|
| `duelcore` (mod serveur) | Lua | capte les atomes, applique les commandes (spawn/effets), écrit `out`, lit `in`, heartbeat | aucune règle, aucun équilibrage |
| `duelhud` (mod client) | Lua | HUD (score, minuteur, alerte), applique les 2 effets EXPÉRIENCE | aucune décision |
| `referee` | Python | contrat, cycle de vie des serveurs, transport, évaluation de la table, journal, arbitrage | aucune logique de jeu en dur |
| `presets/` | YAML | tables livrées | — |

## 4. Protocole (v1)

Fichiers par camp dans `~/Zomboid/Lua/` : `duel_A_out.txt`, `duel_A_in.txt`, `duel_A_ack.txt` (idem B).
Une ligne = un JSON, terminée par `\n`. Le lecteur ne traite que les lignes complètes.

**Événement (mod → arbitre)**
```json
{"v":1,"seq":42,"t":312.44,"side":"A","kind":"kill","data":{"z":1234,"w":"Base.Axe","x":10500,"y":9900}}
```

**Commande (arbitre → mod)**
```json
{"v":1,"seq":7,"kind":"spawn_pop","n":2,"distance":[30,60],"warn_s":6}
```

**Heartbeat (mod → arbitre, toutes les 10 s)**
```json
{"v":1,"kind":"beat","side":"A","age":2.4657,"hp":87,"players":["durand"],"kills":41}
```

Règles de transport :
- l'arbitre écrit `in` en un seul `write` puis `fsync` (pas de ligne partielle) ;
- le mod mémorise le dernier `seq` traité et écrit `ack` toutes les 5 s ;
- quand `ack == dernier seq`, l'arbitre tronque `in` (pas de suppression de fichier, pas de course) ;
- côté `out`, l'arbitre suit un offset en octets ; si le fichier rétrécit (redémarrage serveur), il repart de 0 et déduplique par `seq`.

## 5. Modèle de données

- `match.yaml` : durée, `day_length`, `symmetry`, mods (ids Workshop), sandbox surchargée, mot de passe, presets.
- `relations.yaml` : atomes, règles, modificateurs, budget (cf. `06`, §3.2).
- `journal.jsonl` : un enregistrement par événement, conversion, effet et changement d'état ; source unique du replay et de l'équilibrage.

## 6. Critères d'acceptation (mesurables)

1. 100 kills en jeu → 100 événements, attribution correcte à 100 %, zéro perte.
2. Latence p95 événement → effet < 3 s (mesurée sur journal).
3. Aucun spawn à moins de 25 cases du joueur ; aucun spawn pendant sommeil ou conduite.
4. 3 matchs de 25 min consécutifs sans crash ; RSS des serveurs stable (±10 %).
5. Redémarrage d'un serveur en match : reprise sans perte de règles ; dérive > 60 s → match invalidé proprement, jamais silencieusement.
6. Table invalide (effet inconnu, source non autoritaire) → refus au chargement, message explicite.
7. Table modifiée après acceptation → refus de démarrage (hash).
8. Replay du journal : mêmes effets aux mêmes horodatages (±250 ms).
9. `Mods=`/`WorkshopItems=` différents entre les deux serveurs → refus de démarrage.
10. Déconnexion > 2 min → forfait enregistré, serveurs arrêtés proprement.

## 7. Ordre de construction

| # | Étape | Jours | Bloquant ? |
|---|---|---|---|
| 1 | `referee` : moteur de règles + journal, testé sur des flux d'événements enregistrés, sans jeu | 2-3 | non |
| 2 | `duelcore` : atomes, heartbeat, commandes, spawns | 4-5 | oui (spike 1) |
| 3 | Spike 1 en jeu (5 vérifs de `05`) | 1 | oui |
| 4 | `duelhud` : HUD + `fog` + `shake` | 3-4 | non |
| 5 | Cycle de vie : snapshots, ports, vérification des mods, contrat | 2-3 | non |
| 6 | Éditeur/rendu de table + presets | 2-3 | non |
| 7 | Équilibrage (mode ghost + duels réels) | 4-8 | non |
| 8 | QA, doc, packaging | 3-5 | non |

Total : **21-32 jours**, hors aléas B42. L'étape 1 est faisable immédiatement et sans le jeu : elle donne un moteur testable et un journal avant même de toucher à PZ.

## 8. Risques ouverts

| Risque | Impact | Traitement |
|---|---|---|
| Craft non observable serveur | atome `craft` absent de la v1 | diff d'inventaire (A5 de `05`) si `OnProcessAction` échoue |
| `OnWeaponHitCharacter` / `OnEnterVehicle` côté serveur | atomes `damage_taken`/véhicule incertains | spike 1, repli sur `OnCharacterDeath` + PV heartbeat |
| Mods MP B42 instables | match perdu | versionnage strict, pas de `reloadlua` en match, redémarrage propre |
| Table éditable par un joueur | match cassé ou injuste | validation stricte, budgets plafonnés, contrat hashé |
| Hôte = admin | triche | mot de passe RCON détenu par l'arbitre |
| Dérive d'horloge | match inéquitable | surveillance + invalidation (jamais de correction) |
