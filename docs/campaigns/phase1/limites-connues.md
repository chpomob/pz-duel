# Limites connues de la phase 1

## Connexion du client sans Steam

Les serveurs tournent avec `-Dzomboid.steam=0`. `Ouvrir PZ` lance donc `projectzomboid.sh -nosteam +connect <adresse> +password …`, sans relais par Steam : Steam rendrait la main sans rien dire du client.

### Prouvé

Par les tests de `tests/test_modes.py`, avec un faux lanceur ou un faux script. Aucun ne lance le vrai jeu.

- `open_game` appelle le script du client, avec `-nosteam`, l'adresse et le mot de passe de A.
- Script absent ou non exécutable : aucun lancement, échec qui nomme le fichier et donne la marche à suivre à la main.
- Script qui ne démarre pas, ou qui s'arrête dans les 2 s, quel que soit son code : échec. Le vrai script finit par `exit 0` : son code ne prouve rien.
- Le mot de passe ne figure ni dans les messages ni dans les logs du service.

### Non prouvé

- **La connexion elle-même.** Elle demande un second joueur et un client réel : aucun essai n'a eu lieu. Un client `-nosteam` peut donc encore être refusé par un serveur lancé par `start-server.sh`.
- Que le vrai client accepte `-nosteam`, `+connect` et `+password` ensemble.
- Un refus après 2 s : un client qui tient ce délai compte comme lancé. C'est pourquoi le succès dit de vérifier la connexion en jeu.

Le service constate la présence d'un joueur par les `beat` du mod, jamais par ce lancement.
