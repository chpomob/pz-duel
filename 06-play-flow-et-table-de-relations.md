# Play flow et table de relations

Décrit le produit tel qu'il se joue : un joueur définit les règles, la machine lance deux serveurs, la liaison entre les deux est volontairement bête, et tout ce qui compte est une **table de relations** action → conséquence.

---

## 1. Principe directeur

Le jeu n'est pas codé : c'est un **moteur de conséquences**. Un duel est une configuration de ce moteur.
Corollaire : le produit livrable n'est pas « un mod de duel », c'est (a) un moteur + (b) des presets. Un preset « Duel rapide » est un fichier ; un preset « Hardcore » en est un autre ; un futur mode coopératif serait encore une autre table.

Trois couches, jamais mélangées :

| Couche | Rôle | Où ça vit |
|---|---|---|
| **Match** | qui joue, quels mods, quel monde, combien de temps | `match.yaml` |
| **Relations** | ce que chaque action/combo/série produit chez l'autre | `relations.yaml` |
| **Liaison** | transport d'événements et de commandes, sans logique de jeu | arbitre (processus) |

La liaison est « bête » par choix : elle ne connaît ni le jeu, ni l'équilibrage. Elle transporte des événements, applique la table, journalise. Si un jour elle doit être remplacée (VPS, agent Java), les deux autres couches ne bougent pas.

---

## 2. Play flow

```
 1. HÔTE             2. CONTRAT            3. LANCEMENT           4. MATCH            5. FIN
┌──────────────┐   ┌─────────────────┐   ┌──────────────────┐  ┌───────────────┐  ┌────────────┐
│ choisit un   │   │ l'invité voit   │   │ snapshot mondes  │  │ les 2 joueurs │  │ résultat + │
│ preset ou    │──▶│ la table en     │──▶│ création/restore │─▶│ jouent, les   │─▶│ journal +  │
│ édite la     │   │ langage clair   │   │ 2 serveurs       │  │ 2 serveurs    │  │ replay     │
│ table        │   │ et accepte      │   │ 2 clients        │  │ tournent      │  │ JSONL      │
└──────────────┘   └─────────────────┘   └──────────────────┘  └───────────────┘  └────────────┘
       │                    │                     │                    │
       └── mods requis ─────┴── vérification ─────┴── horloge de match ─┘
           et versions          des mods/server        (départ synchrone)
```

Détail des étapes :

1. **Hôte** — écran de configuration : preset, durée (20/25/30 min), `DayLength`, liste de mods (héritée d'un preset, éditable), table de relations (éditeur ou fichier importé), mot de passe, mode symétrique ou handicaps.
2. **Contrat** — l'invité reçoit un résumé lisible, généré depuis la table : « Chaque zombie tué → 2 zombies chez toi (max 60/min). 3 constructions puis un kill en 30 s → une horde. » Il accepte ou refuse. Pas de match sans acceptation : c'est ce qui rend la table « juste » aux yeux des deux. Le contrat est **scellé par un hash** de la table : l'arbitre refuse de démarrer si la table effective ne correspond plus à celle qui a été acceptée.
3. **Lancement** — l'arbitre : restaure les snapshots de monde, démarre les deux serveurs (ports et RCON distincts), vérifie que `Mods=` et `WorkshopItems=` correspondent au contrat, attend que les deux joueurs soient connectés, puis démarre l'horloge.
4. **Match** — aucune pause. Le minuteur vit dans l'arbitre. Les serveurs ne connaissent que le jeu.
5. **Fin** — mort subite ou fin de minuteur (score). Résultat, journal JSONL, replay possible (les événements sont horodatés et séquencés).

---

## 3. La table de relations

### 3.1 Modèle : atomes → agrégats → séquences

| Niveau | Question à laquelle il répond | Exemple |
|---|---|---|
| **Atome** | quel fait de jeu brut capte-t-on ? | `zombie_kill`, `damage_taken`, `build`, `vehicle_repair`, `explosion` |
| **Agrégat** | combien, sur quelle fenêtre ? | `3 zombie_kill en 10 s`, `0 damage_taken pendant 60 s` |
| **Séquence** | dans quel ordre ? | `build ×3 puis zombie_kill en 30 s` |
| **Modificateurs** | comment l'état du match change le poids ? | combo, écart de score, PV restants |
| **Effets** | que subit l'autre ? | `spawn_pop(2)`, `fog`, `power_off` |

Chaque niveau est **déclaratif** : la table est un fichier, pas du code. L'arbitre évalue la table sur le flux d'événements (tick de 250 ms) et tient un état par camp (compteurs, fenêtres, combo).

### 3.2 Exemple concret

```yaml
version: 1
match: { duration_s: 1500, day_length: 1, symmetry: true }

# ---- atomes : uniquement des sources S1 (serveur, non falsifiables)
atoms:
  zombie_kill:   { source: server, event: OnZombieDead }
  damage_taken:  { source: server, event: OnPlayerGetDamage }
  build:         { source: server, event: OnObjectAdded, filter: player_built }
  vehicle_repair:{ source: server, event: OnMechanicActionDone }
  explosion:     { source: server, event: OnThrowableExplode }

# ---- règles : la table de relations
rules:
  - id: kill_baseline
    when: atom(zombie_kill)
    every: 1                     # par tranche de 1 kill
    effects:
      - { kind: spawn_pop, n: 2, distance: [30, 60], warn_s: 6, cap_per_min: 60 }

  - id: kill_streak
    when: atom(zombie_kill).count(3).within(10s)     # combo
    effects:
      - { kind: spawn_sprinter, n: 1, distance: [40, 70], warn_s: 10 }

  - id: untouched_minute
    when: atom(zombie_kill).count(10).within(60s).and(not atom(damage_taken).within(60s))
    effects:
      - { kind: fog, duration_s: 90 }

  - id: builder_payoff
    when: atom(build).count(3).within(120s).then(atom(zombie_kill), within: 30s)   # série
    effects:
      - { kind: horde_area, n: 25, radius: 20, distance: [50, 80], warn_s: 12 }

  - id: pain_relief
    when: atom(damage_taken).count(1).within(1s)
    effects:
      - { kind: combo_reset }                       # effet sur soi, pas sur l'autre
      - { kind: supply_drop, distance: [10, 25] }    # anti-neige-boule

modifiers:
  combo:        { per: 0.15, max: 3.0, reset_on: damage_taken }
  score_gap:    { when_gt: 30, attacker: 0.6, defender_bonus: supply_drop }

budget:                          # garde-fous globaux, non négociables par la table
  threats_per_minute: 6
  concurrent_effects: 4
  per_effect_cooldown_s: 20
```

### 3.3 Ce que la table ne peut pas faire

- Référencer un effet non déclaré par le mod (échec au chargement, message explicite).
- Dépasser le budget global (les `cap_per_min` sont plafonnés par le moteur).
- Utiliser une source non autoritaire (une règle basée sur `AddXP` serait marquée `source: client` et refusée en mode classé).
- Toucher à la pause, à l'horloge, ou à la vie du joueur par API.

C'est ce qui rend une table « éditable par un joueur » sans qu'il puisse casser le match.

---

## 4. Liaison : ce que fait l'arbitre, exactement

| Responsabilité | Détail |
|---|---|
| Vérifier le contrat | lit `Mods=`/`WorkshopItems=` des deux `.ini`, compare au contrat, refuse de démarrer en cas d'écart |
| Restaurer les mondes | snapshot des deux dossiers de save, restauré serveurs arrêtés |
| Démarrer/arrêter | deux processus serveur, ports et RCON distincts (`RCONPort` 27015/27016) |
| Transporter | tail des `outbox` des mods, écriture des `inbox`, RCON pour les commandes stock |
| Évaluer | applique la table sur le flux, tient les compteurs/fenêtres/combo par camp |
| Journaliser | une ligne JSONL par événement, conversion, effet et changement d'état |
| Arbitrer | minuteur, mort subite, déconnexion (grâce 2 min puis forfait), invalidation si dérive d'horloge |
| Rendre lisible | génère le résumé du contrat et le résumé de fin de match depuis la table et le journal |

Rien d'autre. Pas de logique de jeu dans l'arbitre : si une règle demande un comportement non prévu, on ajoute un **type d'effet** côté mod, pas un cas particulier dans l'arbitre.

---

## 5. Conséquences techniques

1. **Éditeur de table** : un formulaire qui produit le YAML + un rendu en langage naturel. Le même rendu sert au contrat et au résumé de fin. C'est la pièce d'UX qui décide si le produit est utilisable par autre chose que son auteur.
2. **Presets** : au moins trois — « Duel rapide » (kill → pop, peu de règles), « Pression » (combos + météo + objets), « Hardcore » (sprints, hordes, électricité). Ce sont des fichiers versionnés.
3. **Journal = replay** : chaque match est rejouable depuis son JSONL. C'est aussi l'outil d'équilibrage (voir `03-mecanismes-et-telemetrie.md`).
4. **Validation stricte au chargement** : une table invalide ne démarre pas. Aucun échec silencieux — c'est la différence entre un outil et une démo.
5. **Symétrie par défaut, handicaps en option** : `symmetry: true` duplique les règles pour les deux camps ; sinon `sides: [A]` ou `[B]` permet un handicap explicite.

---

## 6. Décisions tranchées (04/11/2026)

| # | Sujet | Retenu |
|---|---|---|
| 1 | Qui édite | hôte seul, l'invité accepte le contrat (hash) |
| 2 | Format | **YAML commenté** (tranché par défaut, « idgaf ») |
| 3 | Granularité | atomes + combos + séries |
| 4 | Symétrie | symétrique par défaut, handicaps optionnels |
| 5 | Mods | liste imposée par le preset, vérifiée identique des deux côtés |
| 6 | Effets EXPÉRIENCE | dans la v1 (fog, shake) |

Le reste du présent document décrit l'état d'avant décision ; la spécification consolidée est dans `07-spec-mvp.md`.
