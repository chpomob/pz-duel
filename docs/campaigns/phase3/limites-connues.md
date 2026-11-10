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
