# Phase 3 — Ouvrir les ports pour les testeurs (UPnP)

Brief de campagne. Écrit le 09/11/2026, après le premier essai Windows réussi : deux serveurs montés
en 21 s, contrat vérifié, lobby atteint (match `c3f1b335`, testeur Naylo, 42.22.2).

## Contexte

Le produit fonctionne maintenant de bout en bout jusqu'au lobby sur un vrai poste Windows. Le
prochain obstacle n'est plus le code mais le **réseau** : pour qu'un invité entre depuis chez lui, il
faut aujourd'hui que l'hôte ouvre trois ports à la main dans l'interface de sa box, puis retrouve son
adresse publique et la tape dans le lobby.

Deux gestes que personne ne fait correctement du premier coup, et qui sont exactement ce qui bloque un
testeur qui n'est pas sur le même réseau local.

## Faits vérifiés le 09/11/2026 (à ne pas re-discuter)

- **Le serveur du jeu sait faire de l'UPnP**, et nous le désactivons : `service/runtime.json` impose
  `"UPnP": "false"` dans `managed_ini`, et `tests/test_servers.py` l'assert. Les chaînes du jar
  42.22.2 le confirment : « UPnP-enabled internet gateway found: », « No UPnP-enabled Internet gateway
  found, you must configure port forwarding on your gateway manually… », et surtout « **If the server
  hangs here, set UPnP=false** ». Le sien ne couvre donc pas notre port de partage et peut figer un
  démarrage.
- **Sonde réelle sur la machine de développement** (lecture seule, aucun mapping ajouté) : un IGD
  répond en SSDP sur `192.168.1.254` (description `:5678/desc/root`), services `Layer3Forwarding`,
  `WANCommonInterfaceConfig`, `WANIPConnection` ; `GetExternalIPAddress` rend une adresse publique et
  `GetStatusInfo` rend `Connected` avec 13 jours d'uptime. L'adresse exacte n'a pas sa place dans ce
  dépôt.
- **Trois flux seulement doivent sortir**, tous du côté B (le camp A reste local) :
  - `runtime.json` → `ports.B.game` (UDP) et `ports.B.udp` (UDP), le jeu de l'invité ;
  - `runtime.json` → `share_port` (TCP), l'écouteur de partage qui sert le lien et le `.bat`.
  Le RCON (`ports.*.rcon`) ne doit **jamais** être exposé : l'arbitre est local.
- **L'UI a déjà le champ et le bouton** : `ui/app.js` rend « Public address, for a guest outside your
  network » et « Use this address », qui poste `POST /api/invite/address` →
  `engine.set_invite_address` (`service/engine.py`, section invitation). Ce chemin **ne touche pas au
  réseau** aujourd'hui, volontairement (AC58 de la phase 2), et cette propriété vaut pour la saisie.
- **Le service est stdlib uniquement**, et un test le vérifie (`tests/test_stdlib_only.py`). SSDP se
  fait en UDP multicast avec `socket`, le SOAP en `http.client`/`urllib` : aucune dépendance nouvelle.
- La suite compte 2 247 tests, 16 skips, et la CI tourne sur Linux 3.12/3.13/3.14 **et**
  `pytest-windows` sur les mêmes versions. Un test qui parle réseau serait une bombe à retardement
  dans cette matrice.

## Décisions prises par l'opérateur (09/11/2026)

1. **UPnP implémenté par nous**, dans un module à part, stdlib. L'UPnP du jeu reste désactivé et le
   contrat continue de l'asserter : une seule implémentation, un seul état.
2. **Jamais automatique.** Rien ne s'ouvre au démarrage, ni au lancement d'un duel, ni en tirant une
   revanche. Une action explicite de l'hôte, depuis le lobby, et elle seule.
3. **Trois mappings, pas plus** : les deux UDP du camp B et le TCP de partage. Aucun port du camp A,
   jamais le RCON, pas de DMZ, pas de plage.
4. **Bail borné et nettoyage** : un bail de deux heures au maximum, et un `DeletePortMapping` à la
   fermeture propre. Si le processus meurt, il reste au pire le bail. Un mapping posé deux fois ne
   doit pas échouer : on supprime avant d'ajouter.
5. **L'adresse publique est lue, pas devinée, et reste locale** : lue par `GetExternalIPAddress`,
   elle sert à pré-remplir le champ du lobby (l'hôte peut la corriger). Elle ne va ni dans le journal,
   ni dans un log, ni sur le réseau, ni dans l'invitation tant que l'hôte ne l'a pas acceptée.
6. **Trois résultats honnêtes, jamais un succès silencieux** : mappé (avec l'adresse), pas de box
   compatible, ou refus de la box. Un échec ne doit jamais empêcher de lancer un duel — il doit dire
   quoi faire à la main, et le duel en LAN doit continuer à marcher comme si la phase n'existait pas.
7. **Aucun test ne touche au vrai routeur ni à Internet.** Un faux IGD local (serveur HTTP qui répond
   le SOAP attendu) sert de double, comme le faux serveur de jeu le fait déjà. La sonde sur une vraie
   box reste une mesure d'opérateur, pas un test.

## Objectif de la phase

Depuis le lobby, un hôte qui n'a jamais touché à sa box doit pouvoir :

1. cliquer une fois pour demander l'ouverture des ports ;
2. voir le résultat en clair — ports ouverts, pas de box compatible, ou refus — et l'adresse publique
   déjà remplie dans le champ quand elle est connue ;
3. envoyer son invitation, l'invité entre depuis son propre réseau ;
4. quitter l'outil sans laisser un port ouvert derrière lui.

Et si rien de tout cela n'est possible (UPnP absent, box en CGNAT), l'outil doit le dire et laisser
l'hôte faire à la main, exactement comme aujourd'hui.

## Non-objectifs explicites

- NAT-PMP, PCP, IPv6, tunnels, relais VPS, STUN : hors périmètre.
- Vérification de joignabilité depuis l'extérieur : elle exigerait un tiers, donc non.
- Activer l'UPnP du jeu, ou changer la façon dont le jeu annonce ses ports.
- Publier ou transmettre l'adresse publique à quoi que ce soit d'autre que l'UI locale.
- Toute modification des règles du duel, du protocole, du journal, du mod Lua.
- DMZ, redirection de plage, mapping des ports RCON ou du camp A.

## Contraintes

- **Stdlib uniquement** à l'exécution ; `tests/test_stdlib_only.py` reste vert.
- Python ≥ 3.12, Linux et Windows (CI) : tout ce qui est réseau doit être testable sans routeur, et
  aucun test ne doit dépendre d'une interface réseau réelle.
- **Aucune valeur en dur** : ports, description du mapping, durée du bail et délais de découverte
  viennent de `runtime.json`, jamais d'un littéral dans le code — c'est la règle du projet depuis la
  phase 1.
- Les chaînes **ajoutées** à l'UI sont en anglais, comme celles de la phase 2 ; le français reste la
  langue des documents de campagne.
- Aucune régression : 2 247 tests verts aujourd'hui, dont les tests réels du jeu
  (`pytest -m pz_integration`) et les trois jobs `pytest-windows`.
- Sécurité : trois ports, une description identifiable, un bail, un nettoyage. Un exe non signé qui
  ouvre des ports doit demander, dire ce qu'il ouvre, et refermer.
