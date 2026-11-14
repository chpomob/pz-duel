# Chacun son serveur — étude de faisabilité et spikes (14/11/2026)

Question : peut-on se passer de l'hôte unique, chaque joueur faisant tourner sa propre partie chez
lui ? Réponse courte : **oui, sans toucher au mod ni au cœur de match**. Ce document dit ce qui a été
mesuré, sur quoi, et ce qui ne l'a pas été.

Tout le code des spikes est dans `spikes/chacun_son_serveur/`. Aucun fichier de `service/`, de
`mods/` ni de `tests/` n'a été modifié.

## 1. La forme retenue

```
   PC de l'hôte                                          PC de l'invité
┌───────────────────────────────┐                  ┌──────────────────────────────┐
│ jeu ── 127.0.0.1 ── serveur A │                  │ serveur B ── 127.0.0.1 ── jeu│
│              mod A            │                  │           mod B              │
│        Lua/duel_A_*           │                  │        Lua/duel_B_*          │
│              │                │                  │              │               │
│   ARBITRE (juge, journal)     │                  │   AGENT (aucune décision)    │
│   Lua/duel_B_* (miroir) ──────┼── lien TCP ──────┼── relais de fichiers         │
└───────────────────────────────┘   (ou relais     └──────────────────────────────┘
                                    de rendez-vous)
```

- Chaque joueur lance le même programme, un serveur local, et joue sur `127.0.0.1`.
- L'arbitre de l'hôte reste **le seul juge** : règles, score, horloge, journal, rejeu.
- L'agent de l'invité ne décide rien. Il tient les quatre fichiers du mod B en miroir avec ceux
  que l'arbitre lit et écrit, et exécute chez lui les ordres de l'hôte (réglages, mondes, serveur).
- Plus aucun port de jeu ne sort d'une machine. Il ne circule que des lignes JSON.

Le vrai solo (sans serveur du tout) reste écarté : le mod sort à sa ligne 31 hors d'un serveur,
toute la capture est prouvée côté serveur, et le solo rend la pause et l'accélération du temps au
joueur (voir `02-archi-deux-serveurs.md`, §1).

## 2. Ce qui a été mesuré

| Spike | Question | Résultat |
|---|---|---|
| S1a | Le moteur supporte-t-il un camp joint par TCP ? | **284 tests sur 285** passent, moteur inchangé |
| S1b | Et le vrai jeu, le vrai mod ? | Contrat vérifié, 8 commandes sur 8, 0 anomalie |
| S2 | Que fait un écart d'horloge entre deux PC ? | Verdict faux sans correction ; 0 faux sur 6 100 avec |
| S3 | Peut-on n'ouvrir aucun port ? | Oui, par relais de rendez-vous : mêmes 284 sur 285 |
| S4 | Une coupure du lien en plein match ? | 2,6 s de coupure, reprise en 0,1 s, ni perte ni doublon |

### S1a — la suite du moteur, le camp B derrière un lien TCP

`relayed_suite.py` est un greffon pytest. Il déplace les fichiers du mod B dans un second dossier,
comme sur un autre PC, et pose entre les deux un relais (`relay.py`) sur un vrai socket TCP avec
latence injectée. Ni le moteur ni un seul test ne sont modifiés.

- 285 tests (`test_match`, `test_contract`, `test_settings`, `test_announce`), 169 matchs relayés.
- À 40 ms + 20 ms de gigue par sens : **284 passent**. À 80 ms + 40 ms par le relais de
  rendez-vous : **284 passent** aussi.
- Le test restant (`test_held_match_replays_with_commands_after_the_probes`) lit le fichier `in`
  du mod sans attendre, 50 ms avant que la commande n'arrive : artefact du test, pas du produit.

Deux choses ont dû être réglées pour en arriver là, et ce sont les vrais enseignements :

1. **Vider un fichier n'est plus instantané.** Sur un disque partagé, l'arbitre vide `out` avant
   un lancement et c'est fait. Sur un lien, des octets encore en vol traversent ce vidage : au
   premier essai, le `caps` de la passe de création a contaminé le match suivant (« mod différent
   entre les camps »). Le relais numérote donc des **époques** : la remise à zéro est un ordre
   acquitté, et tout ce qui arrive d'une époque passée est jeté (≈ 300 messages jetés sur 169
   matchs). Règle de conception : *remise à zéro, démarrage et arrêt du serveur distant voyagent
   sur le même lien ordonné que les données.*
2. **L'horloge simulée des tests.** Les tests figent une horloge et l'avancent d'un coup ; un octet
   en vol au moment du saut arrive « après son tick » et rien ne retique. Sans correctif, 40 tests
   échouaient pour cette seule raison. Le greffon fait attendre au double du mod la livraison de
   ses octets (ils traversent toujours TCP) ; une vraie horloge avance toujours.

### S1b — le vrai jeu (build 42.22.2, Linux)

`real_game.py` lance deux vrais serveurs par le vrai `RealMatch`, chacun sur son dossier cache
(`-cachedir`, hors du vrai `~/Zomboid`). Les fichiers de transport de B ne rejoignent l'arbitre
que par le relais, à 80 ms + 40 ms de gigue par sens.

| | Camp A (local, témoin) | Camp B (relayé) |
|---|---|---|
| `caps` reçus, contrat vérifié | oui | oui |
| commandes écrites / résolues | 8 / 8 | 8 / 8 |
| statut | `refused no_player` | `refused no_player` |
| délai commande → `ack` (médiane) | 0,48 s | 0,67 s |
| délai commande → résultat (médiane) | 1,44 s | 1,37 s |
| anomalies de transport | 0 | 0 |

- Les fichiers de B sont identiques à l'octet des deux côtés en fin de course.
- Le lien ajoute 0,2 s à l'`ack` et rien de mesurable au résultat : le délai est celui du mod
  (lecture de `in` chaque seconde, vidage de `out` chaque seconde).
- Débit : 11 Ko vers l'invité et 19 Ko vers l'hôte sur toute la course (≈ 2 min, préparation et
  mesure d'horloge comprises).
- Un serveur dédié pèse environ **3,1 Go** de mémoire résidente (tas de 2 048 Mo, relevé sur un
  duel réel le même jour). C'est ce que la machine de l'invité doit porter, en plus du jeu.

Limites de ce spike : pas de joueur, donc ni spawn ni kill réel ; `Server/`, `Saves/`, `db/` et
`mods/` de B étaient des liens vers ceux de l'hôte (dans la vraie version, l'agent les écrit chez
lui) ; une seule machine, donc aucun vrai NAT.

### S2 — deux PC, deux horloges

Le mod date ses événements à l'heure de sa machine (`getTimestampMs`). Le cœur ne s'en sert que
pour trancher. `clock_skew.py` le mesure sur le vrai `MatchCore` :

- **Morts presque simultanées.** A meurt, B meurt 400 ms après : B doit gagner. Dès que l'horloge
  de B retarde de plus que cet écart, A est déclaré vainqueur. Sur un écart d'horloge balayé de
  −3 s à +3 s : **26 verdicts faux sur 61**.
- **Terme du minuteur.** Un kill lu au tick final est comparé au terme. Horloge de B en avance de
  500 ms : un kill fait 300 ms avant la fin est **perdu**. En retard de 500 ms : un kill fait
  300 ms après la fin est **compté**.
- Le reste du match n'y est pas sensible : hors de ces deux cas, un fait compte quand il arrive.

La correction : l'arbitre mesure l'écart par allers-retours sur le lien (`ClockSync`, on garde
l'échantillon au plus petit temps d'aller-retour) et ramène chaque `t_ms` à son heure.

| Lien (aller / retour / gigue) | erreur médiane | erreur au 99ᵉ centile |
|---|---|---|
| réseau local 1 / 1 / 0,5 ms | 0,0 ms | 0,1 ms |
| fibre 15 / 15 / 5 ms | 0,3 ms | 1,5 ms |
| ADSL ou 4G 40 / 40 / 20 ms | 1,2 ms | 6,5 ms |
| par relais 80 / 80 / 30 ms | 1,8 ms | 8,8 ms |
| asymétrique 20 / 90 / 20 ms | 35 ms | 40 ms |

Avec la correction, le scénario des morts à 400 ms d'écart donne **0 verdict faux sur 6 100
essais**, tous liens confondus. L'erreur résiduelle reste sous le tick du cœur (250 ms) ; un lien
très asymétrique laisse un biais de la moitié de l'asymétrie, sans conséquence à cette échelle.

À prévoir dans la vraie version : figer l'écart au départ du match (une estimation qui bouge en
cours de route déplacerait des faits déjà datés), et refuser un départ tant qu'il n'est pas mesuré.

### S3 — aucun port à ouvrir

`rendezvous.py` : un relais de 100 lignes. Les deux arbitres s'y connectent **en sortie** avec le
même code de duel, et il recopie les octets sans les lire. Une connexion sortante traverse une
box, un double NAT ou un CGNAT : c'est exactement le cas que l'UPnP de la phase 3 ne couvre pas.

- Même suite, mêmes 284 sur 285, par 171 appariements et 507 Ko recopiés.
- Coût : il faut **une machine joignable** quelque part. Le brief de la phase 3 a mis le relais
  hors périmètre ; cette décision est à rouvrir si « zéro port » devient l'objectif.
- Le relais voit le trafic en clair. La vraie version chiffre de bout en bout, avec un secret tiré
  du code d'invitation.

Sans relais, il reste **un seul port TCP** à ouvrir chez l'hôte, au lieu de deux UDP et un TCP
aujourd'hui. L'UPnP existant sait déjà le faire.

### S4 — coupure du lien

`link_cut.py`, vrai `CommandChannel` et vrai `OutReader` côté arbitre : le lien est coupé pendant
que le mod écrit 50 événements et que l'arbitre écrit 3 commandes, puis rétabli.

- Pendant la coupure : rien ne passe, des deux côtés, et rien ne casse.
- À la reprise (0,1 s) : l'hôte annonce où il en est (époque, octets de `out` déjà posés),
  l'invité renvoie la suite. **110 événements lus, `seq` sans trou ni doublon, 0 anomalie**, les 4
  commandes chez l'invité.
- Horloge de l'invité faussée de 2 500 ms : écart estimé 2 500,0 ms, tous les `t_ms` ramenés à
  l'heure de l'hôte.

Ce que le moteur fait aujourd'hui d'un camp muet : `suspect` après 30 s sans battement, **forfait
après 120 s**. Un lien coupé ressemblerait donc à un joueur parti, et l'hôte gagnerait par forfait
sur sa propre panne de box. La vraie version doit distinguer « lien perdu » de « joueur absent » et
annuler le match plutôt que de l'attribuer.

## 3. Ce que l'arbitre fait sur un camp, et que l'agent devra faire chez lui

Relevé dans `service/` : 18 usages de `zomboid_dir` dans `engine.py`, 11 dans `preflight.py`, 6
dans `snapshots.py`, 4 dans `servers.py`. Tout y est déjà rangé par camp (`duelA`, `duelB`).

| Opération par camp | Aujourd'hui | Chez l'invité |
|---|---|---|
| Préflight : jeu, mod, mémoire, ports | `preflight.py`, local | l'agent mesure, l'hôte affiche |
| Installer le mod | `install_mod` | l'agent |
| Écrire `.ini` et `SandboxVars` | `write_ini`, `write_sandbox` | l'hôte envoie les valeurs, l'agent écrit |
| Mondes : création, capture, restauration | `SnapshotStore`, `prepare_worlds` | l'agent, sur ordre |
| Serveur : démarrer, arrêter, relancer, état, log | `ServerProcess` | l'agent, sur ordre |
| Transport : `out`, `in`, `ack`, `session` | fichiers | **relais, prouvé ici** |
| Contrat : relire `.ini`, `SandboxVars`, `caps` | `verify_contract` | l'agent relit, l'hôte juge |
| Ouvrir le jeu | `launch_client` | l'agent, vers `127.0.0.1` |
| Annonces en jeu (RCON) | `announce.py` | l'agent, sur ordre |

Ce qui disparaît : le `.bat` d'invitation et son code dicté, le mot de passe serveur de l'invité
en circulation, l'ouverture des ports de jeu, l'écouteur de partage.

## 4. Ce qui n'est pas prouvé

- **Aucun duel joué de cette façon.** Sans joueur, ni spawn ni kill n'a traversé le lien en vrai
  jeu ; seuls `caps`, session, commandes refusées et résultats l'ont fait.
- **Aucun vrai réseau.** Tout tourne sur une machine ; la latence est injectée, pas subie. Ni NAT,
  ni perte de paquets, ni Wi-Fi.
- **Windows.** Le pare-feu ne devrait plus rien demander d'utile (le serveur n'est joint que par
  `127.0.0.1`), mais personne ne l'a constaté.
- **L'arrêt d'un serveur distant.** Le moteur relève une sortie de serveur avant sa dernière
  lecture de `out`. À distance, la notification de sortie doit suivre les derniers octets sur le
  lien. Dans ces spikes, le cycle de vie du serveur est resté local : ce point est conçu, pas testé.
- **La confiance.** L'invité possède son serveur, sa sauvegarde et son agent. Le produit dit déjà
  « pas d'anti-triche, entre amis » ; c'est un cran de moins qu'aujourd'hui.

## 5. Chemin proposé

1. **Découper par camp, sans réseau.** Sortir du moteur une interface « camp » (préparer, lancer,
   arrêter, transport, contrat) avec une seule implémentation locale. Refactoring pur, sous le
   filet des ≈ 3 000 tests existants. C'est le gros du travail et il ne change rien pour le joueur.
2. **Le camp distant.** Une seconde implémentation qui parle à un agent par le lien : relais de
   fichiers à époques, ordres acquittés, mesure d'horloge, reprise sur coupure. Le relais des
   spikes en est le brouillon.
3. **Le mode « Rejoindre »** de `pz-duel.exe`, qui remplace le `.bat`.
4. **Le lien** : un port TCP chez l'hôte d'abord (UPnP existant), relais de rendez-vous ensuite si
   l'on veut zéro port.
5. **Un duel réel à deux machines**, seule preuve qui manque et que rien ici ne remplace.

## 6. Rejouer les spikes

```bash
# S1a — la suite du moteur derrière le lien (≈ 2 min 30)
SPIKE_DELAY_MS=40 SPIKE_JITTER_MS=20 python -m pytest -p spikes.chacun_son_serveur.relayed_suite \
    tests/test_match.py tests/test_contract.py tests/test_settings.py tests/test_announce.py
# S3 — la même, par le relais de rendez-vous
SPIKE_RENDEZVOUS=1 SPIKE_DELAY_MS=80 SPIKE_JITTER_MS=40 python -m pytest \
    -p spikes.chacun_son_serveur.relayed_suite tests/test_match.py tests/test_contract.py
# S1b — le vrai jeu : 6 Go de mémoire, ports 17261-17272 et 27015-27016 libres, ≈ 2 min
python -m spikes.chacun_son_serveur.real_game --work /un/dossier/vide --delay-ms 80
# S2 et S4
python -m spikes.chacun_son_serveur.clock_skew
python -m spikes.chacun_son_serveur.link_cut
```

`SPIKE_SYNC_TESTS=0` retire le correctif d'horloge simulée de S1a et fait réapparaître les 40
échecs d'artefact.
