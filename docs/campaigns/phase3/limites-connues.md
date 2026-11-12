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

## P9F13 — lobby lisible (2026-11-12)

Retour d'une vraie session : le QR et le jeton d'invitation en gros occupaient le haut de la carte,
le code du lanceur et le bouton de jeu étaient noyés. La carte d'invitation (`ui/app.js`,
`inviteCard`) suit désormais l'ordre d'usage : une seule action primaire (`primaryAction` :
`Waiting for your opponent…` grisé avec sa raison, `Open the game`, puis `Starting the duel…` grisé
quand les deux joueurs sont là), le code du lanceur en gros et en monospace (copié d'un clic), le
lien avec `Copy link` / `Save launcher`, et le QR replié dans un `<details>` fermé par défaut, dont
l'ouverture survit aux rendus (`qrOpen`). `inv.code` reste dans l'API mais n'est plus rendu. Les deux
secrets restent dans deux blocs distincts, avec leurs libellés et la phrase qui les sépare. Aucun
changement de service ni de protocole.

**Tests et mutations (`tests/test_setup_flow.py`, vérifiées tuées) :**
`test_p9f13_invitation_token_is_not_rendered` (remettre `<div class="code">`),
`test_p9f13_launcher_code_comes_before_the_link` (remonter le bloc du lien au-dessus du code),
`test_p9f13_qr_is_folded_in_a_closed_details` (rendre le `<details>` ouvert d'office),
`test_p9f13_one_primary_action_whose_label_follows_the_lobby` (libellé fixe ; `disabled` retiré
quand les deux joueurs sont là).

**Ouvert, à confirmer en session réelle :**

- Lisibilité mesurée seulement par la structure du HTML : aucun testeur non technique n'a encore
  vu la page. À vérifier : comprend-il en dix secondes quoi cliquer et quel code dicter ?
- `Starting the duel…` s'affiche dès que les deux joueurs sont identifiés ; si une sonde tarde ou
  un relâchement n'est pas confirmé, le décompte attend et le bouton reste sur ce libellé (la raison
  détaillée est dans la carte `Lancement`, `holdNotice`).
- Le clic sur le code passe par `navigator.clipboard`, qui exige un contexte sûr : sur
  `http://127.0.0.1` les navigateurs l'acceptent, à confirmer sur le navigateur réel de l'hôte ;
  à défaut, le code reste sélectionnable d'un clic (`user-select: all`).
- Les textes ajoutés sont en anglais (règle AC29) ; le reste de la carte mêle encore français et
  anglais.

## P9F14 — mode arbitre : deux invités, aucun joueur local (2026-11-12)

Demande du propriétaire : séparer le serveur et les joueurs, avoir deux invités, que la machine qui
héberge ne soit pas forcément un joueur. Le lobby propose désormais **qui joue** au lancement :
`Me and a guest` (`mode: "host"`, le défaut, strictement le comportement d'avant) ou `Two guests`
(`mode: "referee"`). Le choix part dans le corps de `POST /api/action {"action":"start", …}`, se
relit dans `/api/state` sous `config.mode` et survit à la revanche. Ce n'est pas un réglage de preset.

**Où se lit le mode.** La spécification demandait `/api/state.mode` ; ce champ de premier niveau dit
déjà `mock` ou `real` et l'ensemble des clés de premier niveau est figé par
`tests/test_modes.py::test_api_backward_compatible`. Le mode de jeu vit donc dans `config.mode`. En
mode hôte, `config` gagne ce seul champ (`"host"`) ; rien d'autre ne change.

**Ce qui change en mode arbitre :**

- **Deux invitations complètes.** Celle de B reste à la racine de `invite`, comme avant ; celle de A
  est rangée sous `invite.A`. Chacune a son lien (jeton propre), son code de lanceur, son `.bat`
  (`GET /api/invite/launcher?side=A|B`, sans `side` : B ; `?side=A` hors mode arbitre : 409) et son
  mot de passe serveur.
- **Un second écouteur de partage**, pour A, sur `share_port + 1` (17281). `service/share.py` ne
  sert qu'un jeton par écouteur et n'était pas dans le périmètre : A a donc le sien, qui reçoit un
  adaptateur (`_SideShare`) rendant le `.bat` de A. La spécification parlait de « 17280 TCP pour
  chaque camp » ; c'est 17280 pour B et 17281 pour A. Port pris : le lien de A est annoncé
  indisponible, le `.bat` s'enregistre encore depuis l'UI, comme pour B.
- **Six redirections.** Le même clic ouvre B (inchangé), puis A sur le même fil, une fois B
  fusionné : un échec sur A ne touche ni le résultat ni les mappings de B, et s'annonce à part
  (`ports_opening.A`, journal `port opening (side A): …`). Les externes de A sont ceux de B décalés
  de `SIDE_A_EXTERNAL_OFFSET = -10` (49261, 49262 UDP, 49270 TCP), refusés s'ils sortiraient de
  [1024, 65535] ou croiseraient un externe de B ; ils suivent ainsi les externes configurés de B
  dans la plage haute. `runtime.json` n'était pas dans le périmètre : aucune clé nouvelle. Même
  description `PZ Duel`, même bail, même repli unique sur 718, même registre (`upnp.Ledger`) ;
  le budget de retrait à l'arrêt passe à deux fois `MAPPING_COUNT` requêtes.
- **`open_game` refusé** : `refused, reason: no_local_player`, sans journal, sans client lancé ; le
  bouton n'existe pas (carte `refereeCard`).
- **Renversement assumé du contrat de `host_password`.** Jusqu'ici, c'était un secret de machine :
  il n'ouvrait que A, seul l'hôte le lisait au lobby, et `_invite_fields` garantissait que `invite`
  ne le portait jamais (`tests/test_share.py::test_invite_never_carries_the_host_password`, toujours
  vrai en mode hôte). En mode arbitre, personne ne joue sur la machine : ce mot de passe n'est plus
  un secret de machine mais **l'accès d'un participant**, qui doit pouvoir se connecter depuis chez
  lui. Il part donc dans l'invitation de A et dans son `.bat`, exactement comme celui de B dans les
  siens. Les deux secrets restent séparés par camp (code du lanceur, mot de passe serveur) ; un camp
  ne reçoit jamais ceux de l'autre. Conséquence : l'invitation de A survit au lobby comme celle de B,
  et y garde ce mot de passe (`config.host_password`, lui, disparaît toujours hors du lobby).

**Inchangé :** la tenue (P9F10-P9F12) fige les deux mondes tant que les deux joueurs ne sont pas là,
quel que soit le mode ; l'identité d'un camp reste le premier nom vu sur son serveur. Le duel, les
règles, les presets, la dérive : inchangés.

**Tests et mutations :**
`tests/test_upnp.py::test_p9f14_referee_opens_six_mappings_and_two_invitations` (`ports_a = None`
dans `open_ports` : trois redirections seulement),
`test_p9f14_failure_on_a_is_reported_apart_and_leaves_b_open` (l'issue de A publiée dans
`_ports_opening` : l'échec de A remplace le `mapped` de B),
`test_p9f14_host_mode_opens_exactly_three_and_one_invitation` (`_referee()` toujours vrai),
`test_p9f14_side_a_couples_follow_b_and_never_cross_it` ;
`tests/test_match.py::test_p9f14_open_game_is_refused_in_referee_mode_and_opens_nothing` (refus
retiré de `_open_game`), `test_p9f14_each_invitation_carries_the_password_of_its_own_side`
(`config["password"]` pour les deux camps), `test_p9f14_launcher_route_serves_each_side_and_refuses_the_rest`
(`side` ignoré), `test_p9f14_host_mode_has_one_invitation_and_no_launcher_for_a` ;
`tests/test_setup_flow.py::test_p9f14_referee_lobby_shows_two_invitations_and_no_game_button`
(bascule vers `refereeCard` retirée), `test_p9f14_start_carries_the_chosen_play_mode`.

**Ouvert, à vérifier en session réelle :**

- Aucune session à deux joueurs distants et aucun client sur la machine hôte n'a encore eu lieu.
  À voir : les deux `.bat` rejoignent chacun leur serveur depuis deux réseaux différents ; la tenue
  fige bien le premier arrivé, quel que soit le camp ; le duel part seul quand le second arrive.
- Six redirections sur une vraie box : seule la box de l'auteur a vu trois redirections (P9F5). Une
  box qui limite le nombre de redirections par appareil, ou qui rend 718 sur 49261/49262/49270, est
  possible ; le repli unique couvre le 718, pas une limite de nombre.
- Le second écouteur de partage (17281) passe par le pare-feu Windows comme le premier : invite à
  autoriser à confirmer sur Windows réel.
- Le préflight ne contrôle pas 17281 ; un port pris ne bloque rien mais coupe le lien de A.
- Le texte du lanceur (`LAUNCHER_CODE_TEXT`) dit « your guest » pour les deux camps : juste pour
  l'arbitre, qui a deux invités.

## P9F15 — réglages du monde et des règles avant le départ (2026-11-12)

**Ce qui a changé :** `Configurer un duel` montre vingt réglages, à plat, en deux blocs (« Monde »,
« Règles »), rendus depuis le catalogue que sert `GET /api/settings` (`service/settings.py`) ; la page
ne connaît aucun champ. `POST /api/action {"action":"start", "settings":{…}}` contrôle tout avant le
premier effet : clé inconnue, type faux ou valeur hors bornes refusent le départ, message qui nomme
la clé, la borne et la valeur reçue, sans écrêtage. Le preset effectif est construit en mémoire,
repasse par `load_table`, est scellé comme avant (le recontrôle du décompte refait la table réglée
depuis le fichier), et part au journal avec `settings` et `sandbox` dans `match_start`. Sans
`settings`, le départ est celui d'avant (preset tel quel, monde gardé) : la revanche relit ceux du
départ précédent.

**Écarts assumés avec la fiche, sur relevé du vrai `duelA_SandboxVars.lua` (PZ 42) :**

- `Zombies` est une option énumérée (1 Hardcore … 6 Aucun), pas un multiplicateur : bornes 1–6, défaut
  3 « Élevé » (le « Élevé (3) » du résumé). Le fichier relevé avait `Zombies = 3` avec
  `PopulationMultiplier = 0.65` (Normal) : un serveur dédié ne synchronise pas les deux. Le réglage
  écrit donc aussi `ZombieConfig.PopulationMultiplier` et `PopulationStartMultiplier`, valeurs tirées
  des commentaires du fichier (1,2 et 1,5 pour Élevé).
- `StartTime` est un indice (2 = 9 h) : le défaut « 9 » de la fiche est l'heure, gardée, écrite `2`.
  La page propose les neuf heures que PZ connaît (0, 2, 5, 7, 9, 12, 14, 17, 21 h).
- `Sight` et `Hearing` vont de 1 à 3 (aiguisée, normale, pauvre), `Memory` de 1 à 4 (longue à
  aucune) : 0 n'existe pas dans PZ, et les options aléatoires sont laissées de côté.
- `DayLength` 1–7, pas 1–20 : la dérive convertit par `runtime.json/day_length_minutes`, qui ne
  connaît que 1 à 7, et trois tests hors du périmètre de l'étape (`test_modes`, `test_api_security`,
  `test_rules_load`) fixent le refus de 8. Étendre la table (8 = 5 h … 20 = 17 h, relevé dans le
  `SandboxVars`) est une étape à part.
- `zombies_par_kill` 1–30, pas 1–50 : `limits.max_n` vaut 30, et `test_rules_load` (hors périmètre)
  fixe le refus de 31. Le relever à 50 est une étape à part ; le service découpe déjà en tranches
  de 30.
- `settings_summary` est dans `config` de `/api/state` (`config.settings_summary`), pas à sa racine :
  `test_modes` fixe l'ensemble exact des clés de l'état. Au repos, la page montre le résumé des
  défauts, servi avec le catalogue.
- `settings` est classé à la frontière comme champ objet (`api.OBJECT_FIELDS`, 400 sinon) et lu par
  le moteur sous `SETTINGS_FIELD` : la table `NOT_IDENTIFIERS` de `test_api_security` (hors
  périmètre) ne le connaît pas encore, à y ajouter.
- Le bouton « Valeurs par défaut » prend son texte dans le catalogue (`reset_label`) : les lignes
  ajoutées à `ui/app.js` restent en anglais (AC29).
- Les défauts des règles suivent le preset choisi (`?preset=`) : ceux de `duel_rapide` sont ceux de
  la fiche ; `demo` garde 2 zombies par kill, et un preset sans `sprinter_percent` propose 0 %.

**Monde neuf :** efface `Saves/Multiplayer/duelA|duelB`, `db/duelA|duelB.db`, `duel-snapshots/duelA|
duelB` et le marqueur de création, serveurs arrêtés, avant `prepare_worlds`. Sans les snapshots, la
restauration rendrait l'ancien monde. L'effacement se dit dans les logs de la page et dans
`world_reset` (chemin, existait, octets). Il coûte une passe de création à chaque départ.

**Tests et mutations :** `tests/test_settings.py` — `test_catalog_defaults_are_the_owner_values`
(`Toughness` à 4), `test_catalog_is_served_by_the_api` (route retirée),
`test_out_of_bounds_refuses_the_start_and_names_the_key` (borne ramenée au lieu de refuser),
`test_refused_settings_touch_no_world`, `test_speed_and_sprinter_percentage_are_never_settable`
(`LOCKED_SANDBOX` retiré de `sandbox_values`), `test_dotted_key_is_written_inside_its_block_only`,
`test_effective_preset_carries_the_requested_values` (`effect["n"]` oublié),
`test_summary_matches_the_settings`, `test_new_world_erases_then_journals` (`_reset_worlds` retiré),
`test_kept_world_is_left_alone` (effacement inconditionnel),
`test_settled_table_passes_the_seal_at_countdown` (recontrôle sur le fichier brut),
`test_settings_must_be_an_object_at_the_api` (`OBJECT_FIELDS` vidé) ;
`tests/test_setup_flow.py::test_p9f15_settings_panel_is_flat_and_built_from_the_served_catalog`
(rendu sous Node : deux blocs, un contrôle par champ, aucun `<details>`),
`test_p9f15_start_sends_the_settings_and_no_loose_duration`.

**Ouvert, à vérifier manette en main :**

- Un match réglé à la main de bout en bout, sans éditer un fichier : par exemple 20 z par kill, 5 %
  de sprinters, pops à 15–30, monde neuf ; relire `match_start` et compter les sprinters.
- Que PZ 42 relise bien, à la création, `Zombies` et les deux multiplicateurs écrits, et que la
  population de départ change à vue entre « Faible » et « Hardcore ».
- Que la passe de création d'un monde neuf garde les clés écrites (PZ réécrit le fichier) : le
  contrat les relit et refuserait sinon.
- Premier départ sur une machine sans `SandboxVars` : la passe de création se fait avec les défauts
  du jeu, les réglages ne sont écrits qu'après ; heure, mois et population de départ ne comptent
  alors qu'au monde neuf suivant.
- La durée de la passe de création, refaite à chaque monde neuf, sur une vraie machine.

## P9F16 — compilation du double Windows bornée, réessayée et lisible (2026-11-12)

**Cause :** CI Windows rouge sur `392f4df` (commit purement Markdown) : 36 erreurs au setup, toutes
`subprocess.TimeoutExpired` sur `csc.exe` après 120 s. La compilation à froid du double
`ProjectZomboid64.exe` dépassait la borne sur un runner chargé ; l'outillage, pas le produit,
décidait de la couleur du dépôt.

**Fait :**

- `COMPILE_TIMEOUT_S` vaut 900 s ; `PZDUEL_COMPILE_TIMEOUT_S` (secondes, > 0) le remplace sans
  toucher au code. Une valeur absurde lève un `ValueError` qui nomme la variable.
- Un `TimeoutExpired` est réessayé une fois, après suppression du `client.exe` partiel. Deux
  dépassements lèvent un `RuntimeError` qui nomme le compilateur, la borne et la variable à monter.
  Ni `skip` ni `xfail` : Windows cassé reste rouge.
- Chaque tentative imprime `compile_windows_client: compiled in X.Xs (limit 900s)` ou
  `... timed out after X.Xs`.
- Une compilation par processus : les trois points d'appel sont des fixtures `scope="session"`
  (`test_platform_windows.py` compilait dans son test), et le premier PE compilé est copié aux
  appels suivants, chaque copie gardant son propre `.plan`.

**Tests et mutations :** `tests/test_platform_windows.py` —
`test_compile_bound_is_900_s_and_read_from_the_environment` (`COMPILE_TIMEOUT_S = 120`, variable
ignorée), `test_compile_bound_refuses_a_meaningless_value`,
`test_compile_timeout_is_retried_once_on_a_clean_target` (`COMPILE_ATTEMPTS = 1`, `unlink` retiré),
`test_two_compile_timeouts_say_which_variable_to_raise` (`TimeoutExpired` relancé brut),
`test_compile_duration_is_printed` (impression retirée),
`test_second_compile_in_a_session_copies_the_first` (copie retirée),
`test_real_compilation_happens_in_a_session_fixture` (compilation rendue au test
`test_windows_client_double_is_a_real_pe`), `test_session_fixture_check_sees_a_per_test_compilation`.
Le compilateur y est un fichier vide et son appel un enregistreur : rien ne lance `csc.exe`.

**Ouvert :**

- La preuve finale est la CI Windows verte sur ce commit, à relire après le push.
- La durée imprimée est capturée par pytest : elle paraît dans le journal quand le fixture échoue,
  ou sous `-s` ; un run vert ne la montre pas. La faire paraître toujours demanderait de toucher
  `tests.yml`, que `test_ci_workflows` veut identique à la publication, hors du périmètre.
