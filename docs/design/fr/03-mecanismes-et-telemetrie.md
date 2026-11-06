# Mécanismes et télémétrie — le cœur du duel

Concept retenu (précisé le 04/11/2026) : **les joueurs ne se croisent jamais.** Deux mondes séparés, aucune interaction spatiale. Le seul lien est un flux de menaces : chaque avancée de l'un devient une pression pour l'autre. Il n'y a donc ni arène, ni contact, ni PvP direct — et le « point de départ dense » que j'avais proposé n'a plus de sens.

Conséquence de conception : ce qui compte n'est pas la carte, c'est **la conversion**. Un mécanisme = (signal capté) → (poids) → (menace livrée). Le jeu tient ou s'effondre sur cette table de conversion.

---

## 1. Le vrai problème : tout n'est pas observable côté serveur

Le serveur est l'autorité (saves, spawns, anti-triche), mais la documentation des événements 42.x marque explicitement une partie des signaux comme **(Client)**. C'est la contrainte structurante du projet, pas un détail d'implémentation.

| Mécanisme | Signal | Observabilité | Conséquence |
|---|---|---|---|
| Kill de zombie | `OnZombieDead` + `IsoZombie:getAttackedBy()` | **Serveur** | Pilier v1 |
| Kill d'animal / de survivant | `OnCharacterDeath` | **Serveur** | Pilier v1 |
| Coup porté / dégâts subis | `OnHitZombie`, `OnPlayerGetDamage` | **Serveur** (doc : « fires on the server » pour les coups) | Combo, état de santé |
| Bruit (tir, alarme, monde) | `OnWorldSound`, `OnAmbientSound`, `OnWeaponSwing` | **Serveur** | Menace « bruit » |
| Construction / destruction | `OnObjectAdded`, `OnTileRemoved`, `OnWeaponHitThumpable` (Server) | **Serveur** | Menace « effondrement / bruit » |
| Mécanique véhicule | `OnMechanicActionDone` | **Serveur** | Menace « épave / bruit » |
| Explosif / piège | `OnThrowableExplode` | **Serveur** | Menace « incendie » |
| Feu | `OnNewFire`, `OnGridBurnt` | **Serveur** | Propagation de menace |
| Conteneurs générés | `OnFillContainer` (Server) | **Serveur** | Métrique de progression |
| **Craft** | `Recipe_OnCreate`, `CraftRecipe_OnCreate` (umbrella) | **Incertain** — appelé dans le script de recette, donc probablement côté client en multi ; candidat serveur : `OnProcessAction` (Server) | **À trancher par spike** |
| **XP / montée de niveau** | `AddXP`, `LevelPerk` | **Client uniquement** (marqué `(Client)`) | Ne peut pas être une source autoritaire |
| Loot trouvé | `OnItemFound` | Serveur (sémantique à vérifier) | Menace « richesse » |
| Mort du joueur | `OnCharacterDeath` (serveur) / `OnPlayerDeath` (client) | Serveur via `OnCharacterDeath` | Fin de match |

Trois niveaux de source, à assumer explicitement dans la config :

- **S1 — serveur** : autoritaire, non falsifiable par le joueur. Cœur du jeu.
- **S2 — client déclaré** : le mod client (HUD) remonte l'événement ; falsifiable. Tolérable entre amis, à exclure d'un mode classé.
- **S3 — dérivé** : calculé par l'arbitre (temps survécu, distance parcourue, état de santé, score) — autoritaire par construction.

Recommandation : **v1 = S1 + S3 uniquement**, avec le craft testé en spike. Si `OnProcessAction` permet un craft serveur fiable, le craft entre en v1 ; sinon il passe en S2 (mode amical) et la v1 se contente de kills, bruit, constructions, mécanique, feu, mort.

---

## 2. Catalogue des menaces livrables (ce que l'adversaire peut subir)

| Menace | Réalisation | Réglages |
|---|---|---|
| Pop de zombies | `addZombiesInOutfit(x, y, z, n, outfit, chance)` | n, outfit, distance [min,max], étalement |
| Sprinter | spawn avec walk type « sprinter » (méthodes publiques B42) | n, âge de jeu avant expiration |
| Horde ciblée | `createhorde <count> <user>` (RCON) | count, préavis |
| Bruit | `gunshot` / `OnWorldSound` forcé / alarme | rayon, durée |
| Hélico | `chopper` (RCON) | fenêtre, probabilité |
| Météo | `startrain` / `stoprain`, brouillard | durée |
| Objet « cadeau empoisonné » | spawn d'objet au sol près du joueur | type, leurre |
| Épave / véhicule | `addvehicle` (RCON) | type, position |
| Incendie | allumage contrôlé (à valider : dangereux, risque de perte totale) | rayon, cap |
| Coupure élec/eau | **pas d'API runtime fiable** | hors v1 |

Règle de sûreté : toute livraison passe par un **préavis** (son + texte HUD) et un **rayon minimal** (30-60 cases), jamais dans le dos immédiat, jamais pendant le sommeil ni la conduite (liste noire de contextes).

---

## 3. Schéma de règles — tout est donnée, rien n'est codé en dur

Objectif explicite : « on met en place les mécanismes, l'équilibrage est le point délicat ». Donc le code ne contient aucune valeur d'équilibrage ; il lit un fichier de règles rechargeable.

```yaml
# rules.yaml — versionné, un fichier par mode (duel_rapide, duel_long, test)
meta:
  name: duel_rapide
  match_seconds: 1500          # 25 min réels
  day_length: 1                # 1 jour de jeu = 15 min réelles
  pressure_floor: 0.2          # pression ambiante minimale (évite un match vide)
  pressure_ceiling: 6.0        # plafond global, en "unités de menace" par minute

mechanisms:
  zombie_kill:
    source: { kind: server_event, event: OnZombieDead, actor: getAttackedBy }
    weight: 1.0
    batch: 15                  # agrégation en secondes
    caps: { per_minute: 60, per_match: 2000 }
    converts:
      - { threat: zombie_pop, per: 1, amount: 2, distance: [30, 60], warn_s: 6 }
      - { threat: sprinter,   per: 25, amount: 1, warn_s: 10 }

  construction:
    source: { kind: server_event, event: OnObjectAdded, filter: player_built }
    weight: 2.0
    batch: 20
    caps: { per_minute: 10 }
    converts:
      - { threat: noise, per: 1, amount: 1, radius: 40 }

  damage_taken:
    source: { kind: server_event, event: OnPlayerGetDamage }
    weight: 0.0                 # ne génère pas de menace, casse le combo
    effects: { reset_combo: true }

modifiers:
  combo:
    step: 0.15                  # +15 % par kill sans être touché
    max: 3.0
    reset_on: damage_taken
  anti_snowball:
    when_score_gap_gt: 30
    attacker_multiplier: 0.6    # celui qui mène génère moins
    defender_bonus: supply_drop # celui qui subit reçoit une caisse
  decay:
    window_s: 90
    factor: 0.9
```

Chaque `threat` référence une **capacité** déclarée par le mod (nom, paramètres, prérequis). Un mode qui demande une capacité absente échoue au démarrage avec un message clair — jamais silencieusement.

---

## 4. Télémétrie : équilibrer avec des mesures, pas des impressions

Chaque match écrit un JSONL (une ligne par événement et par conversion) :

```json
{"t":312.4,"side":"A","kind":"zombie_kill","raw":3,"weight":3.0,"combo":1.45,
 "emitted":[{"threat":"zombie_pop","amount":9}],"caps_hit":false,
 "state":{"hp":87,"score":41,"threats_pending":12}}
```

Exploitable pour :

- courbe de pression par minute et par camp (le match est-il plate ou explosif ?) ;
- taux de conversion réel vs théorique (les caps mordent-ils ?) ;
- corrélation pression subie ↔ mort (la menace est-elle létale ou décorative ?) ;
- temps morts (périodes sans aucun événement) → ajuster `pressure_floor`.

Sans ce fichier, l'équilibrage se fera à l'aveugle et le projet n'aura aucune preuve de progression. C'est la première brique à écrire, avant même les menaces.

---

## 5. Mode « ghost » : équilibrer à un seul joueur

L'arbitre peut générer un flux de menaces scripté (courbe de pression paramétrable) sans second joueur. Un seul humain suffit alors pour :

- sentir la courbe de difficulté d'un match de 25 min ;
- vérifier la lisibilité des préavis ;
- mesurer la létalité d'un paquet de N zombies à distance D ;
- calibrer `pressure_floor` / `ceiling`.

C'est le moyen le moins cher de faire les 20-30 premières itérations d'équilibrage, avant les duels réels. À prévoir dès le MVP (≈1 journée), pas après.

---

## 6. Ce que ça change dans le plan

- Le « point de départ dense / arène » disparaît : les joueurs ne se croisent pas. La carte doit seulement être **survivable et génératrice d'actions** (zombies, loot, matériaux). Le choix de la map devient un réglage de « densité d'opportunités », pas de contact.
- Le reset entre matchs reste nécessaire (mondes qui se dégradent).
- Le craft devient le point d'incertitude n°1 : c'est le mécanisme le plus intéressant (fabriquer = armer l'autre) et le seul dont l'observabilité serveur n'est pas garantie. À traiter en premier dans le spike.
- L'équilibrage devient un travail sur données : `rules.yaml` + JSONL + mode ghost. Prévoir 4-8 jours, dont une partie peut être faite en solo.
