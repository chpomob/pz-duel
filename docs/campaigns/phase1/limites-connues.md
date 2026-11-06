# Limites connues de la phase 1

## Preuve de `damage_taken`

Constaté dans les classes de 42.21, jamais en session réelle.

- `OnWeaponHitCharacter` ne suit qu'un coup d'arme de joueur : aucune morsure ne le tire.
- `OnBeingHitByZombie` est déclaré par `LuaEventManager`, mais aucune classe du jar ni aucun Lua du jeu ne le déclenche. Le mod l'enregistre sans en attendre de preuve.
- `OnPlayerGetDamage` porte donc `hook_proof`. Une morsure n'y paraît qu'à travers le saignement (`BLEEDING`, depuis `BodyPart.DamageUpdate`). `Bite.process` applique bien la blessure sur le serveur, et rien dans `BodyDamage.Update` ne l'en exclut.

### Non prouvé

- **Que `BodyDamage.Update` tourne sur le serveur pour un joueur connecté.** S'il n'y tourne pas, `damage_taken` est retiré à chaque match : le duel reste jouable, sans ses règles de blessure. Le repli serait alors un écart de santé lu dans `step()`.
- Une morsure qui ne saigne pas ne compte pas.
- Pendant un saignement, une seconde morsure ne donne pas de nouvel atome : il faut 5 s sans perte de sang.

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

Ces six points sont des correctifs courts ; ils sont différés parce qu'aucun ne touche la chaîne
prouvée (mod chargé, `caps`, contrat, arrêt RCON), et parce que les corriger sans le premier duel
réel reviendrait à régler des cas dont on ignore la fréquence.
