# Limites connues de la phase 1

## Détection de `damage_taken`

Constaté dans les classes de 42.21, jamais en session réelle.

- Le signal premier est le relevé de `getOverallBodyHealth` dans `step()`, une fois par seconde : une chute d'au moins 1 point donne l'atome.
- `OnWeaponHitCharacter` ne suit qu'un coup d'arme de joueur : aucune morsure ne le tire.
- `OnBeingHitByZombie` est déclaré par `LuaEventManager`, mais aucune classe du jar ni aucun Lua du jeu ne le déclenche. Le mod l'enregistre sans en attendre de preuve.
- `OnPlayerGetDamage` porte `hook_proof` et confirme le relevé. Une morsure n'y paraît qu'à travers le saignement (`BLEEDING`, depuis `BodyPart.DamageUpdate`).

### Non prouvé

- **Que la santé bouge sur le serveur pour un joueur connecté, et que `BodyDamage.Update` y tourne.** `calculateOverallHealth` en dépend. Si ni l'un ni l'autre ne vaut, aucun `damage_taken` n'arrive : le match se joue sans que `hurt_supply` tire, sans le dire, jusqu'à la première mort, qui rend le couple `failed` et retire l'atome des matchs suivants.
- **Que le seuil de 1 point soit le bon.** Il vient de la lecture du code, pas d'une mesure. Une griffure plus légère passe dessous, sauf si le hook la voit. Une chute ou une perte brutale sans attaquant passe dessus et compte comme blessure, ce que le hook seul excluait.
- Une mort lente, de faim par exemple, sans chute d'au moins 1 point ni blessure, rend le couple `failed` à tort. Le premier signal reçu efface cet échec, y compris au lobby.
- Pendant un saignement, une seconde morsure ne donne pas de nouvel atome : il faut 5 s sans perte de sang.

## Mort avant `t0`

- Toute mort d'un camp lue avant `running` perd le match, lobby compris, même si le joueur a recréé un personnage depuis. Ce que devient un mort dans le vrai jeu n'a pas été observé.
- Si le relevé `isDead` du mod rate la mort et qu'aucun hook ne tire, rien ne la signale.

## Attente avant conclusion

- Toute fin non due à une panne arrive 1,5 s plus tard. Pendant cette attente, les règles tournent encore : des kills datés d'après une mort en attente peuvent partir en commandes et figurer au score final, sans changer le vainqueur.
- Un saut de l'heure murale vers l'avant, juste avant le tick du minuteur, peut dater après le terme un fait du dernier instant : il ne compte alors pas.
- Le mod écrit `out` à chaque atome : une horde tuée d'un coup ouvre le fichier autant de fois. Le coût sur le vrai serveur n'a pas été mesuré.

## Connexion du client sans Steam

Les serveurs tournent avec `-Dzomboid.steam=0`. `Ouvrir PZ` lance donc `projectzomboid.sh -nosteam +connect <adresse> +password …`, sans relais par Steam : Steam rendrait la main sans rien dire du client.

### Prouvé

Par les tests de `tests/test_modes.py`, avec un faux lanceur ou un faux script. Aucun ne lance le vrai jeu.

- `open_game` appelle le script du client, avec `-nosteam`, l'adresse et le mot de passe de A.
- Script absent ou non exécutable : aucun lancement, échec qui nomme le fichier et donne la marche à suivre à la main.
- Script qui ne démarre pas, ou qui s'arrête dans les 2 s, quel que soit son code : échec. Le vrai script finit par `exit 0` : son code ne prouve rien.
- Le mot de passe ne figure ni dans les messages ni dans les logs du service.
- Hors de la phase `lobby` : refus explicite, aucun lancement. Tant que le client lancé tourne, ou démarre encore : second lancement refusé.

### Non prouvé

- **La connexion elle-même.** Elle demande un second joueur et un client réel : aucun essai n'a eu lieu. Un client `-nosteam` peut donc encore être refusé par un serveur lancé par `start-server.sh`.
- Que le vrai client accepte `-nosteam`, `+connect` et `+password` ensemble.
- Un refus après 2 s : un client qui tient ce délai compte comme lancé. C'est pourquoi le succès dit de vérifier la connexion en jeu.

Le service constate la présence d'un joueur par les `beat` du mod, jamais par ce lancement.

## Machine mono-utilisateur

Le service suppose une machine de bureau à un seul utilisateur. Sur une machine partagée, tout compte local peut lire les mots de passe et piloter le duel : ne l'y lancez pas.

- **API locale sans jeton.** Le contrôle de `Host` et d'`Origin` n'arrête que les navigateurs. Un processus local qui envoie `Host: 127.0.0.1:<port>` sans `Origin` lit `/api/state` et poste les actions, dont `start` et `cancel`.
- **Mots de passe dans `/api/state`.** `config.password`, celui de B, y reste à toute phase. `config.host_password`, celui de A, n'y paraît qu'en phase `lobby`, où l'hôte en a besoin.
- **Secrets dans `ps`.** Le mot de passe admin figure dans la ligne de commande de chaque serveur (`-adminpassword`), et celui de A dans celle du client lancé par `Ouvrir PZ`.

## Constats de la revue de clôture laissés ouverts (2026-11-06)

Réels, non corrigés, assumés pour la phase 1. Aucun ne change la promesse tenue ici — le service
et le contrat — mais chacun a un effet observable, décrit tel quel.

- **Deux joueurs avec le même pseudo bloquent le lobby sans cause visible.** La liaison d'identité
  refuse le second, et rien dans l'UI ne dit pourquoi.
- **Un serveur figé devient un forfait pour son joueur** : l'absence est comptée contre lui, et
  rien ne le redémarre. À traiter avec la supervision des serveurs, en phase 2.
- **Serveurs orphelins si le service meurt par `SIGHUP` ou par crash** : aucun processus ne les
  récupère. Un arrêt propre les arrête.
- **Le `Redactor` ne couvre pas les mots de passe joueurs** : la règle « `host_password` en lobby
  seulement » ne protège qu'un champ nommé, pas la classe.
- **Une écriture partielle dans le fichier `in` corrompt la commande suivante** : le mod lit une
  ligne tronquée comme une commande invalide.
- **L'URI d'invitation est construite sans encodage** : un mot de passe ou un nom contenant un
  caractère réservé casse le lien.
- **Le jugement attend un intervalle de vidage** (`conclusion_settle_ms`) avant de conclure. Pendant
  cette fenêtre bornée, le match tourne encore sans résultat alors qu'une mort est déjà journalisée.
  C'est volontaire : c'est ce qui garantit que la mort la plus ancienne décide même si elle est
  encore dans le tampon du mod (cf. constat A3 de la revue de clôture).

Ces six points sont des correctifs courts ; ils sont différés parce qu'aucun ne touche la chaîne
prouvée (mod chargé, `caps`, contrat, arrêt RCON), et parce que les corriger sans le premier duel
réel reviendrait à régler des cas dont on ignore la fréquence.

## Table annoncée et table jouée

Une seule table fait foi : celle que le cœur joue, dont `rules_hash` dérive. Deux écarts d'annonce restent, faute de savoir plus tôt.

- **L'aperçu de `Nouveau duel` montre le preset, pas la table jouée.** Le couple (build, mod) n'est
  connu qu'aux `caps` : avant, rien ne dit si un atome sera retiré. L'écran le dit, et le lobby
  annonce ensuite la table jouée. Une table réduite perd aussi son `summary`, texte libre sans lien
  aux règles : le lobby liste les identifiants de règles, pas leur description.
- **Le `rules_hash` publié au lobby reste provisoire jusqu'à `L`** : un signal reçu au lobby peut
  lever l'échec, et le hash change alors avec la table. `table_sealed` est le seul hash définitif.
