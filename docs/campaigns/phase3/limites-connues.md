# Phase 3 — limites connues

État au 2026-11-10 : **trois passes de revue de branche**, la dernière rend
**APPROVE sans bloquant ni majeur** (5 mineurs, 6 nits). Les rapports des trois passes sont conservés :
`review/` (bloquant + sept régressions), `review2/` (deux majeurs), `review3/` (APPROVE).

## Résiduels assumés — mineurs

1. **Les deux README décrivent encore l'arrêt d'avant P9F3** (`README.md:319`) : « seuls les mappings
   de la dernière box sont retirés » et « un délai de requête de box supplémentaire (3 s) ». Le code
   ne fonctionne plus ainsi. Correction documentaire, à faire avant toute publication de la phase.
2. **Le budget de relâchement est global mais ne s'arrête pas sur une box après son premier délai**
   (`service/upnp.py:1338`) : une box silencieuse rencontrée en premier peut consommer le budget
   avant que les mappings de la box courante soient traités.
3. **La fenêtre « à nous » suppose que la box garde le mapping au moins aussi longtemps que le bail
   demandé** (`service/upnp.py:1206`) : une box qui raccourcit silencieusement le bail recevrait une
   suppression aveugle d'une entrée qui n'est peut-être plus la nôtre.
4. **À l'expiration du bail, l'adresse proposée est effacée même si l'hôte l'a déjà appliquée**
   (`ui/app.js:418`) : l'hôte doit alors la ressaisir.
5. **Un couple dont la pré-suppression a été refusée et dont l'ajout a expiré est enregistré comme
   « à nous »** (`service/upnp.py:816`), donc supprimé à l'arrêt sans contrôle de propriétaire.

Les nits (6) sont dans `review3/final.json`, clé `finding_details`.

## Ce qui n'est pas prouvé

- **Aucun test contre un vrai routeur.** Les preuves reposent sur un faux IGD local ; la sonde réelle
  en lecture seule a établi que la box de l'auteur expose `WANIPConnection` et une adresse publique,
  pas qu'un mapping posé puis retiré laisse la box dans son état initial.
- **Aucun invité n'a rejoint un duel par les ports ouverts.** La chaîne prouvée s'arrête au lobby ;
  l'attribution des kills, la visibilité des spawns et le craft restent non prouvés en jeu réel.
- Le CGNAT rend l'UPnP inopérant : l'UPnP est un accélérateur, jamais une condition, et les
  instructions manuelles restent la voie de secours.

## Suivi proposé

Une étape courte `P9F4` pour les points 1 et 3 (documentation et fenêtre de propriété), qui sont les
deux que la synthèse de la troisième passe nomme comme les plus importants. Les points 2, 4 et 5 sont
acceptés en l'état : ils dégradent le confort ou la propreté, jamais la sûreté du duel.

## Première mesure contre une vraie box (2026-11-10, réseau de l'auteur)

Sonde exécutée avec le vrai `service/upnp.py`, donc les mêmes chemins que le bouton du lobby, sur
l'IGD `192.168.1.254` (`WANIPConnection:1`, `/control/wan_ip_connection`).

- **Découverte** : 2,00 s, une seule passerelle, aucun candidat ambigu.
- **Forme des requêtes acceptée** : ni 402 ni 501 sur nos appels ; retirer un paramètre obligatoire
  rend 402, donc la box valide le dialecte SOAP.
- **Écriture refusée** : `AddPortMapping` rend **718 (ConflictInMappingEntry)** sur les trois ports du
  duel, sur un port libre arbitraire (18000, 18100), en UDP comme en TCP, avec un bail de 7 200 s
  comme avec 0. Or `GetSpecificPortMappingEntry` rend **714 (absent)** sur ces mêmes ports : la box
  affirme à la fois qu'il n'y a rien et qu'il y a conflit.
- **La box sait pourtant écrire** : une règle `Transmission at 51413` existe, créée depuis ce même
  poste (`192.168.1.194`), bail 0. Mapper vers un autre client LAN rend **606 (non autorisé)**.
- **Conséquence vérifiée** : la table de redirections est identique avant et après l'essai — aucune
  entrée partielle, aucun résidu. Le produit dégrade donc exactement comme prévu : `refused`, consigne
  manuelle, et rien à nettoyer.

Ce que cela prouve : la découverte, la forme des requêtes, le refus propre et l'absence de résidu sur
une vraie box. Ce que cela ne prouve pas : le chemin heureux (poser puis retirer un mapping), refusé
ici pour une raison que la box n'expose pas. **Le test de Naylo est la seconde mesure, et c'est elle
qui dira si l'UPnP est utilisable en pratique** ou si les instructions manuelles doivent passer au
premier plan dans le README.

### Chemin heureux prouvé — et la raison du refus (même jour, même box)

Bisection sur la box de l'auteur, mêmes appels SOAP, un paramètre à la fois :

| port externe | résultat |
|---|---|
| 1024, 8080, 17271, 40000, 49151 | `718 ConflictInMappingEntry` |
| 49152, 50000, 51414-51419, 65535 | **accepté** |

La box n'accepte que les ports externes de la plage dynamique (≥ 49152) et répond 718 — « conflit » —
pour tout port réservé, alors qu'aucune entrée n'existe (`714`). Ce n'est pas un défaut du produit :
c'est la politique de la box, et elle rend les trois ports fixes du duel (17271, 17272, 17280)
**impossibles à ouvrir par UPnP** sur ce modèle.

Avec des externes dans la plage acceptée et des internes inchangés, le chemin complet passe :

- `49271 -> 17271/UDP`, `49272 -> 17272/UDP`, `49280 -> 17280/TCP` ajoutés en < 0,01 s chacun,
  description `PZ Duel` visible sur la box ;
- relus par `GetSpecificPortMappingEntry` : `NewLeaseDuration` = **7199**, donc la box **honore le bail**
  demandé (7 200 s) et le décrémente — l'hypothèse inverse du mineur n° 3 ne vaut pas ici ;
- retirés, puis relus : `714` sur les trois. Table de redirections identique à l'état initial.

**Conséquence produit** : tel que livré, le bouton ne peut jamais réussir sur une box qui applique cette
politique. Le produit doit choisir ses **ports externes** dans la plage acceptée, garder les internes,
et donner les externes à l'invité — le `.bat` construit déjà `+connect hôte:port`
(`service/guest_launcher.py:234`) et les ports vivent dans `runtime.json`, donc le changement est borné.
C'est une décision de phase 4, pas une correction de la phase 3.

## P9F8 — pops dispersés et ciblage à l'apparition : ce qui reste à voir en jeu

Le preset règle deux champs que l'arbitre recopie dans chaque commande `spawn_pop` et
`spawn_sprinter` : `spacing` (cases entre deux zombies du lot, défaut 4) et `aggro` (poids du
ciblage, 0,6 ; `demo` à 0). Absents de la commande, le mod replie sur 0 et 0, soit le
comportement d'avant. Le `cmd_result` d'un pop porte `spawned`, `spread` (cases distinctes
ayant reçu un zombie) et `targeted` (zombies dont `setTarget`, `addAggro` et `spotted` ont tous
réussi).

Prouvé hors du jeu, sur le vrai `DuelCore.lua` exécuté par `mini_lua` : les cases retenues sont
dans la bande et espacées ; faute de place, le nombre est gardé en superposant (`spawned` = `n`,
`spread` < `n`) ; chaque zombie reçoit les trois appels ; une fonction absente laisse le pop
`applied` avec `targeted: 0`.

**Pas prouvé, à vérifier manette en main :**

- que la dispersion se **voit** : `isFree` et le placement réel de `addZombiesInOutfit` sur une
  case peuvent différer de la cellule simulée ;
- que les trois appels **existent et agissent** sur 42.21 (`javap` atteste la signature, pas
  l'effet) : les zombies doivent venir vers le joueur au lieu de vaquer, et `targeted` doit
  valoir `spawned` dans le journal ;
- qu'un poids d'`aggro` de 0,6 suffit — ou ne rend pas les zombies insensibles au reste du monde ;
- que la superposition, quand elle arrive (`spread` < `spawned`), reste rare à 4 cases d'écart.

`horde_area` n'est pas concernée : elle disperse déjà par `radius`, et n'est pas ciblée.

## P9F9 — un kill, un lot de dix : ce qui a changé, et ce qui reste ouvert

**Où se perdaient les kills.** Dans `RuleEvaluator.feed` (`service/rules.py`, ancienne ligne
674–676) : un `zombie_kill` d'`origin: "duel"` — un zombie envoyé par le duel — sortait avant toute
règle. Sur `734610ca`, 21 des 35 kills étaient de ce type ; les 14 kills `world` ont tous donné
leur `kill_baseline` (14 règles, plus une `hurt_supply` : les 15 `rule_fired`). Les
`late_event` (78 lignes dans ce journal) n'y sont pour rien : un événement en retard était déjà traité. Le budget non plus,
pour les règles ; il comprimait en revanche les zombies (cooldown de 20 s, `cap_per_min: 60`).

**Ce que fait la règle directe.** `kill_baseline` de `duel_rapide` porte `"direct": true` et
`n: 10`, sans `cap_per_min` (refusé par le chargeur sur une règle directe). Chaque kill reçu, du
monde ou du duel, en retard ou au tick de fin, déclenche la règle ; son lot part au tick même,
hors de `threats_per_minute`, `per_effect_cooldown_s` et `concurrent_effects`, qu'il n'entame
pas : la caisse de soin garde tout son budget. Le sprinter à 1 % remplace toujours un zombie du
lot, jamais en plus. Les autres presets et toute règle sans `direct` gardent le filtre `duel`
et le budget, inchangés.

**Borne technique, hors règle.** `runtime.json` : `direct_units_per_window: 6000`, soit 6 000
zombies directs par camp visé sur `rate_window_s` (60 s) — 10 kills par seconde tenus une
minute. Au-delà, le reste est journalisé `effect_limited` avec `limit` et `window_s`. Un test
joue deux minutes à un kill par tick des deux côtés (2 400 par minute et par camp) sans
l'atteindre. Un journal d'avant P9F9, sans la clé, se rejoue sans borne.

**Ouvert, à voir en jeu :**

- **La boucle est voulue, et elle croît.** Un kill de zombie du duel renvoie dix zombies : deux
  joueurs efficaces se renvoient une pression qui grandit. C'est la règle demandée ; la borne
  ne protège que le serveur, pas l'équilibre. Si la partie devient injouable, c'est `n` qu'il
  faut baisser, pas un budget à remettre.
- **Dix zombies tous les kills, à 12–20 cases.** Ni la tenue du serveur sous ce débit, ni la
  lisibilité pour le joueur ne sont mesurées : la superposition de P9F8 (`spread` < `spawned`)
  peut devenir fréquente.
- **Le lot du tick de fin part** avant `match_end`, vers un serveur que l'arbitre arrête
  ensuite : la commande est dans le journal, le pop peut ne jamais apparaître. Un camp en
  relance à la fin garde son `effect_dropped`, faute de serveur.

## P9F10 — pause forcée tant qu'un joueur manque (2026-11-12)

**Le handshake survit à la pause.** Lu dans `projectzomboid.jar` (javap) : `GameServer.startServer`
tire `OnServerStarted` une fois, après le chargement du monde ; le mod y lit sa session et vide
`caps` dans `out` sur-le-champ. `IngameState.updateInternal` tire `OnTickEvenPaused` en tête de
chaque passe, avant de calculer `paused = Players.isEmpty() && pauseEmpty && readyToPause()` et de
sauter `onTick` : la boucle du mod (lecture de `in`, `ack`, `beat`) y tourne donc serveur vide.
`OnTick` reste le secours, et ne fait plus que réappliquer la tenue dès qu'`OnTickEvenPaused` a
tiré. `PauseEmpty` était déjà livré à `"true"` depuis P9F5 ; un test le relit et prouve `caps` sans
un seul `OnTick`, sur le vrai `DuelCore.lua` sous `mini_lua`.

**Tenue et relâchement.** Les `caps` du mod portent `hold: true`. À la première passe du lobby, un
joueur manquant, chaque camp qui l'annonce reçoit `hold_time` ; un camp qui ne l'annonce pas est
écrit au lobby (*Start not frozen*), jamais bloqué. Le mod capte `getTimeOfDay`, le réapplique à
chaque passe (`setTimeOfDay`), fige tout joueur présent ou arrivant (`setBlockMovement(true)`) et
demande l'écran de pause (`setShowPausedMessage`, best-effort). La passe qui voit les deux joueurs
envoie `release_time` aux camps tenus, une seule fois, puis les sondes ; le décompte suit leur
résultat, d'une à deux secondes. La dérive part du premier `beat` d'après ce relâchement.

**Écart à la fiche, assumé : le relâchement précède les sondes, pas le décompte.** Le rejeu
(`service/replay.py`, hors du périmètre de l'étape) ne réserve que les `seq` des sondes. Un
`release_time` écrit après elles décalerait la numérotation du cœur et rendrait le journal
non rejouable. Sous tenue, les sondes attendent donc le relâchement : `hold_time` et
`release_time` prennent leur `seq` avant la première sonde, et le dernier `seq` d'avant
`running` reste celui d'une sonde. Bénéfice annexe : le zombie de la sonde ne naît plus près d'un
joueur figé. Les commandes de monde sont journalisées sous le type `hold_command`, que le rejeu
ignore.

**Ouvert, à vérifier manette en main (un joueur entré trois minutes avant l'autre) :**

- **`setBlockMovement` est posé sur l'`IsoPlayer` du serveur.** Le jar montre un simple champ ;
  rien ne prouve que le client, maître de son déplacement en multijoueur, s'y plie. Si le joueur
  bouge encore, la tenue de l'heure reste vraie, pas le gel du joueur.
- **`setShowPausedMessage` passe par `UIManager` du processus serveur.** Aucun envoi réseau n'y
  est visible : l'écran de pause n'apparaîtra probablement pas chez le client.
- **La simulation est arrêtée par le pas de temps, pas par le drapeau de pause.**
  `IngameState.updateInternal` recalcule `paused` à chaque passe depuis
  `GameServer.Players.isEmpty() && PauseEmpty && readyToPause()` (javap, offsets 895-930) : aucun
  appel Lua ne le force joueur présent. Le mod pose donc `GameTime:setMultiplier(0.0001)` — le
  levier de la commande serveur `setTimeSpeedMultiplier` (javap) — que `getMultiplier` applique au
  pas de zombies, besoins et horloge du serveur : trois minutes d'attente y durent 18 ms. Exigé
  comme le gel (absent : `no_hold_api`), réappliqué à chaque passe, rendu au relâchement à sa
  valeur d'avant (`getTrueMultiplier`, ou 1 si elle était déjà au ralenti : tenue sauvée par un
  serveur tombé, le champ étant écrit dans la sauvegarde). Pas zéro : `getInvMultiplier` divise
  par lui. **Non prouvé en jeu :** la commande serveur diffuse aussi `SetMultiplier` aux clients ;
  le mod, sans prise Lua sur ce paquet, ne le fait pas : le client garde son pas. Filets pour ce
  qui tournerait encore côté client, la tenue protège aussi le joueur : `setZombiesDontAttack(true)`, exigé comme
  `setBlockMovement` (absent : `no_hold_api`), et rendu à sa valeur d'avant au relâchement ; faim,
  soif, fatigue et endurance reposées à chaque passe à leur valeur d'entrée en tenue
  (`Stats:set(CharacterStat.*)`, best-effort). À mesurer sur trois minutes : que le client
  respecte ces écritures serveur en multijoueur (pas de temps compris : les zombies proches
  doivent rester immobiles), et qu'aucun autre besoin (ennui, stress,
  température) ne dérive.
- **Un joueur arrivé non figeable fait tomber la tenue.** Posée serveur vide, `hold_time` rend
  `applied` sans avoir touché un joueur ; si le premier arrivé ne peut être figé ni protégé, le mod
  lâche toute la tenue et rend, sous le même `seq`, `refused`, `no_hold_api` : l'arbitre garde le
  `seq` d'une tenue appliquée jusqu'au relâchement et annonce ce refus différé.
- **Un `release_time` non écrit retient le départ.** Journal ou `in` en panne : le camp reste en
  `release_refused`, l'écriture est relancée tous les `probe_retry_s`, et aucune sonde ne part
  tant qu'un camp tenu n'est pas relâché ; passé `probe_timeout_s`, le lobby refuse le match
  (« relâchement impossible du camp … ») plutôt que de lancer un joueur figé. Un `release_time`
  écrit mais rendu `refused` par le mod n'était qu'annoncé — corrigé en P9F11 : relancé comme
  une écriture ratée, et le relâchement n'est confirmé qu'au `applied` (voir plus bas).
- **Un départ entre le relâchement et le décompte ne re-tient rien** (« une seule fois ») : le
  décompte attend son retour, l'autre joueur est libre pendant ce temps.
- **Un mod d'avant P9F10 (sans `hold` dans `caps`)** donne le comportement d'avant : sonde à
  l'arrivée, pas de tenue, annoncé au lobby.

## P9F11 — tenue complète, relâchement confirmé (2026-11-12)

Fait du bytecode, à ne plus chercher : `GameTime.isGamePaused()` n'est vrai que si
`GameServer.server && Players.isEmpty() && ServerOptions.pauseEmpty`. Il n'existe aucune pause
générale d'un serveur où quelqu'un joue : « tenir » est la somme de leviers, tous exigés.

**Quel levier protège quoi.**

| Levier (mod) | Ce qu'il tient | Absent |
|---|---|---|
| `GameTime:setMultiplier(0.0001)`, réappliqué à chaque passe | le pas de la simulation serveur : zombies, besoins, horloge | `no_hold_api` (`setMultiplier`) |
| `GameTime:setTimeOfDay`, réappliqué | l'heure du jour | `no_hold_api` (`setTimeOfDay`) |
| `IsoGameCharacter.setInvulnerable(true)` (P9F11) | la vie : aucun coup ne l'entame, même porté | `no_hold_api` (`setInvulnerable`) |
| `setZombiesDontAttack(true)` | les zombies ne choisissent pas le joueur pour cible | `no_hold_api` (`setZombiesDontAttack`) |
| `setBlockMovement(true)` | le déplacement | `no_hold_api` (`setBlockMovement`) |
| `Stats:set(CharacterStat.*)`, à chaque passe | faim, soif, fatigue, endurance | best-effort, muet |

Ordre : protégé (invulnérable, puis hors cible) **avant** d'être figé ; au relâchement, rendu
mobile **avant** d'être rendu vulnérable. Si `setBlockMovement(false)` échoue, la protection reste
posée et le relâchement rend `refused` : jamais un joueur figé et exposé. La tenue rend la valeur
d'avant elle (un admin invulnérable le reste). Chaque refus porte `call`, le nom de l'appel
manquant, lu par le service (`refused no_hold_api (setInvulnerable)`).

**A3 — un joueur arrivé après la tenue.** La tenue est réappliquée à chaque passe (`holdTick`,
sous `OnTickEvenPaused` comme `OnTick`) à tous les joueurs en ligne : le retardataire est protégé
et figé au tick suivant son arrivée, sans commande. Si une API lui manque, la tenue tombe et un
**seul** `cmd_result` part, sous le `seq` de `hold_time`. Côté service, `held` n'est posé qu'au
`applied` du mod (`sent` ne prouve rien), et l'UI n'écrit *Start frozen* que pour `held`.

**A2 — le relâchement est une confirmation, pas un envoi.** `_released_ms` (origine de la dérive)
n'est posé qu'une fois chaque camp tenu revenu `applied` sur `release_time`. Une écriture ratée
(journal, `in`) ou un `refused` du mod laisse le camp en attente ; il est relancé tous les
`probe_retry_s` (pas à chaque passe : chaque essai consomme un `seq` journalisé), et ni sonde ni
décompte ne partent avant la confirmation. Passé `probe_timeout_s` depuis la demande, le lobby
refuse le match (« relâchement impossible du camp A… : son joueur reste tenu »). Dernier verrou, la
fin du décompte ne passe jamais en `running` avec un camp tenu non confirmé (« camp A encore tenu
à la fin du décompte : duel annulé »). Côté mod, un `release_time` sans rien à rendre (tenue
refusée plus tôt) rend `applied`, pour ne pas bloquer un camp dont l'API manque.

**Tests et mutations (tous dans `tests/test_match.py`, mutations vérifiées tuées) :**

- A1 `test_held_player_loses_no_health_and_released_player_does` — retirer `setInvulnerable(true)`
  de `freezePlayer` : le joueur tenu perd de la vie ; rendre la vulnérabilité avant la mobilité :
  l'ordre des appels échoue. Aussi `test_missing_invulnerable_api_refuses_the_hold`,
  `test_player_that_cannot_move_again_keeps_its_protection`,
  `test_godmode_admin_stays_invulnerable_after_the_release`, `test_release_with_nothing_held_is_applied`.
- A2 `test_refused_release_is_retried_then_the_match_starts` — remettre `_released_ms` à
  l'écriture : les sondes partent vers un A encore figé ; avaler le refus : jamais de relance.
  `test_release_never_confirmed_refuses_the_match_and_names_the_side` — même mutations, le duel
  démarrerait. `test_countdown_never_ends_in_running_with_a_side_still_held` — retirer
  `_unreleased_refusal` du passage en `running`. Les tests P9F10 d'écriture ratée
  (`test_unwritten_release_is_retried_before_the_probes`, `test_release_never_written_refuses_the_lobby`)
  restent verts.
- A3 `test_player_joining_after_the_hold_is_held_at_the_next_tick` — ne tenir qu'à la réception
  de `hold_time` : le retardataire reste libre. `test_late_player_missing_api_is_announced_once_with_its_call`
  — réannoncer à chaque passe multiplie les refus. `test_late_refusal_names_the_call_and_the_lobby_stops_claiming_a_hold`
  — poser `held` à l'écriture, ou le garder après le refus tardif.

**Ouvert, manette en main (un joueur entré trois minutes avant l'autre) :**

- Il **ne bouge pas** : `setBlockMovement` est posé sur l'`IsoPlayer` du serveur ; que le client,
  maître de son déplacement en multijoueur, s'y plie reste à voir.
- Il **ne perd pas de vie** : `setInvulnerable` est un champ serveur ; en multijoueur, les dégâts
  de zombie sont en partie calculés côté client. À vérifier : laisser un zombie l'atteindre (si le
  pas de temps ne l'arrête pas) et lire la barre de vie des deux côtés.
- Il **n'a pas faim** : faim/soif/fatigue reposées côté serveur à chaque passe ; vérifier
  l'affichage client après trois minutes, et l'ennui, le stress, la température, non tenus.
- L'invulnérabilité est-elle écrite dans la sauvegarde du joueur ? Si oui, un serveur tombé sous
  tenue rendrait un joueur invulnérable au redémarrage : la tenue suivante capterait `true` comme
  valeur d'avant. Non vérifié.
- Un joueur tenu qui se déconnecte est rendu au relâchement par son objet gardé ; s'il revient
  entre-temps sous un nouvel objet, il est re-tenu à la passe suivante. Non vérifié en jeu.

## P9F12 — le joueur tenu est invisible et muet (2026-11-12)

Décision du propriétaire : protégé, il restait visible et audible ; les zombies s'attroupaient
autour de lui pendant l'attente et ne repartaient pas au départ du duel. La tenue le cache donc
aussi, par `IsoGameCharacter.setInvisible(true)` (javap). Le même jar porte
`addWorldSoundUnlessInvisible` : un personnage invisible n'émet plus de bruit de monde (pas,
gestes), donc n'attire ni par la vue ni par le son.

| Levier (mod) | Ce qu'il tient | Absent |
|---|---|---|
| `setInvisible(true)` (P9F12) | la vue et le bruit de monde : le joueur n'attire plus de zombies | `no_hold_api` (`setInvisible`) |

Ordre à la tenue, réappliqué à chaque passe : `setInvulnerable(true)`, `setZombiesDontAttack(true)`,
`setInvisible(true)`, `setBlockMovement(true)`. Au relâchement, l'inverse : `setInvisible`,
`setBlockMovement`, `setZombiesDontAttack`, `setInvulnerable`, chacun rendu à sa valeur d'avant la
tenue (`false` pour un joueur ordinaire ; un admin déjà invisible le reste). Un échec garde le camp
en attente comme en P9F11. Un retardataire est caché au tick suivant son arrivée.

**Tests et mutations (`tests/test_match.py`, vérifiées tuées) :**
`test_held_player_is_invisible_before_frozen_and_visible_after_release` (retirer
`setInvisible(true)`, le poser après le gel, ne pas le défaire),
`test_player_joining_during_the_hold_is_invisible_at_the_next_tick`,
`test_missing_invisible_api_refuses_the_hold` (ignorer l'échec du `pcall`),
`test_already_invisible_admin_stays_invisible_after_the_release` (`false` en dur). L'ordre complet
est aussi tenu par `test_held_player_loses_no_health_and_released_player_does`.

**Ouvert, à confirmer manette en main :**

- `setInvisible` est posé sur l'`IsoPlayer` du serveur. Que le client et les zombies simulés côté
  client s'y plient en multijoueur (aucun zombie ne vient vers le joueur en trois minutes) reste à
  voir.
- Le joueur invisible l'est probablement **pour lui-même** (personnage transparent ou absent à
  l'écran) : attendu, c'est l'invisibilité telle que le jeu la propose ; seul l'écran de pause
  (`setShowPausedMessage`) signale la tenue. À confirmer manette en main.
- Les zombies déjà en route vers le joueur avant la tenue perdent-ils leur cible, ou finissent-ils
  leur trajet ? Non vérifié.
- L'invisibilité est-elle écrite dans la sauvegarde du joueur ? Même question que pour
  l'invulnérabilité : un serveur tombé sous tenue capterait `true` comme valeur d'avant. Non vérifié.
