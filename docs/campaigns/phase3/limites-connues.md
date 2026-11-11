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
