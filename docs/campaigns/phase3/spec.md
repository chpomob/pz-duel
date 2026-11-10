---
name: "phase3-upnp-port-opening"
version: "1.0"
author: "adversarial-spec"
status: "draft"
tags: [adversarial, spec, pz-duel, phase3, upnp, network]
targets:
  - file: service/upnp.py
    description: "Nouveau : client IGD stdlib — découverte SSDP bornée dans le temps, lecture de la description, appels SOAP DeletePortMapping / AddPortMapping / GetExternalIPAddress ; rend l'un des résultats de R4 (mappé, pas de box compatible, refus) sans jamais lever vers l'appelant"
  - file: service/engine.py
    description: "Action explicite d'ouverture des ports, permise au seul lobby d'un duel réel ; état de l'ouverture exposé dans `snapshot` (neutre, en cours, mappé, pas de box, refus, configuration invalide) avec l'adresse publique proposée, hors invitation ; suppression dans `shutdown` des mappings posés et non confirmés supprimés (y compris ceux dont le retrait après refus a échoué)"
  - file: service/api.py
    description: "Nouvelle route POST d'ouverture des ports vers l'action du moteur ; `POST /api/invite/address` inchangé"
  - file: service/runtime.json
    description: "Nouvelle section UPnP : délais de découverte et de requête, durée du bail (≤ 7200 s), description du mapping, taille maximale d'une réponse ; `managed_ini.UPnP` reste \"false\""
  - file: ui/app.js
    description: "Dans le lobby d'un duel réel : bouton qui nomme les trois ports avant le clic, affichage du résultat et de la consigne manuelle en cas d'échec, pré-remplissage du champ d'adresse publique ; chaînes ajoutées en anglais"
  - file: ui/style.css
    description: "Mise en forme du bloc d'ouverture des ports et de ses trois résultats"
  - file: tests/doubles/fake_igd.py
    description: "Nouveau : faux IGD local (répondeur de découverte + serveur HTTP de description et SOAP sur 127.0.0.1) configurable en succès, refus par code d'erreur UPnP, réponse malformée, silence (global, ou à partir d'un appel donné après l'avoir enregistré), adresse externe configurable (publique ou à usage spécial) ; enregistre chaque appel reçu"
  - file: tests/test_upnp.py
    description: "Nouveau : tests du client contre le faux IGD et des cas sans box, aucun accès au vrai réseau"
  - file: tests/test_setup_flow.py
    description: "Reçoit les tests d'écran d'AC27 et AC28, à côté des assertions d'écran existantes qui lisent `ui/app.js` par `ui_script()` : bouton d'ouverture présent dans le lobby d'un duel réel et absent en mode simulé, pré-remplissage du champ `#f-address` (vide rempli, saisie existante conservée), textes de résultat (consigne manuelle après `no_gateway`/`refused`, heure d'expiration après `mapped`)"
  - file: tests/test_api_security.py
    description: "La nouvelle route entre dans `Handler.POST_ROUTES`, donc dans les balayages existants Origin/Host ; ajout des cas propres à la route : `Content-Type` non JSON et méthode `GET` refusés sans aucun appel au faux IGD"
  - file: tests/test_rules_load.py
    description: "Reçoit l'assertion de la section `upnp` livrée par `runtime.json` (les 5 clés, leurs types et leurs bornes), à côté des contrôles existants du fichier de configuration"
  - file: tests/test_modes.py
    description: "Reçoit les cas de refus de l'action d'ouverture hors du lobby d'un duel réel (phases idle/running/ended, mode simulé), avec les doubles de serveurs existants"
  - file: tests/doubles/run_service.py
    description: "Le double de service apprend la nouvelle route d'ouverture, pour que les tests de l'API et du parcours complet puissent la poster sans vrai moteur"
  - file: README.md
    description: "Étape « Open ports » du lobby, ce qu'elle ouvre, et la consigne manuelle si elle échoue"
  - file: README.fr.md
    description: "Même étape, en français"
---

# Phase 3 — Ouverture des ports par UPnP, à la demande de l'hôte

## Problem

Le produit atteint le lobby sur un vrai poste Windows (match `c3f1b335`, 42.22.2). Pour qu'un invité
entre depuis son propre réseau, l'hôte doit aujourd'hui ouvrir à la main, dans l'interface de sa box,
deux ports UDP du camp B et le port TCP de partage, puis trouver son adresse publique et la taper dans
le champ du lobby. Ces deux gestes bloquent tout testeur hors du réseau local.

Le serveur du jeu sait faire de l'UPnP, mais nous le désactivons (`managed_ini.UPnP = "false"`,
asserté par `tests/test_servers.py`) : il ne couvre pas le port de partage et peut figer un démarrage
(« If the server hangs here, set UPnP=false »). Une box de développement réelle répond en SSDP avec
un `WANIPConnection` fonctionnel (sonde en lecture seule du 09/11/2026).

Cette phase ajoute une implémentation UPnP à nous, stdlib, déclenchée par un seul clic de l'hôte dans
le lobby, qui ouvre exactement trois mappings à bail borné, lit l'adresse publique pour la proposer
dans le champ existant, dit honnêtement le résultat, et referme en quittant. Un échec ne change rien
au parcours d'aujourd'hui : la consigne manuelle reste, le duel en LAN marche comme avant.

### Hypothèses documentées

- **Profil IGD** : est « box compatible » un appareil découvert en SSDP dont la description expose un
  service `WANIPConnection` (v1 ou v2) ou `WANPPPConnection` v1. Les autres (NAT-PMP, PCP, IPv6
  pinhole) sont hors périmètre (brief, non-objectifs).
- **Pas de renouvellement automatique** : le bail est d'au plus deux heures et n'est jamais prolongé
  sans l'hôte. Aucune garantie de couverture du duel n'est donnée : un duel dure au plus 3 600 s
  (`MAX_DURATION_S`), et le temps passé au lobby après le clic n'est pas borné ; un clic ne couvre
  donc le duel entier que si le duel est lancé au plus (bail − 3 600 s) après le clic (avec le bail
  par défaut de 7 200 s, une heure de lobby). C'est pourquoi l'UI affiche l'heure d'expiration du
  bail (R4, R8) : l'hôte qui s'attarde au lobby, ou qui configure un bail court, clique de nouveau
  avant de lancer. Au lobby suivant (revanche), même chose. Cela respecte la décision « jamais
  automatique, ni en tirant une revanche ».
- **Pas de bouton « fermer les ports »** séparé : la fermeture se fait à la sortie propre de l'outil
  (objectif 4 du brief). Un second clic d'ouverture repose les mêmes mappings (suppression puis
  ajout), il n'en crée pas de nouveaux.
- **Adresse externe non publique** : toute adresse IPv4 d'un bloc à usage spécial du registre IANA
  (RFC 6890) — privée RFC 1918, partagée `100.64.0.0/10`, boucle, lien local, nulle, documentation
  `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24` (RFC 5737), etc. Est « publique » une IPv4
  hors de tous ces blocs. Les tests prennent `5.6.7.8` comme adresse publique : elle n'appartient à
  aucun bloc spécial et n'est jamais contactée (AC20). Une adresse non publique est le signe d'une
  double NAT, d'un CGNAT ou d'une box défaillante : signe d'une double NAT ou d'un CGNAT. Les mappings ont pu être posés sur la box, mais
  l'invité ne pourra pas passer par cette adresse : le résultat est « mappé » sans adresse proposée,
  avec un texte qui le dit. Le brief demande que l'outil « le dise ».
- **Journal du match** : `match_start.data.runtime` recopie déjà tout `runtime.json` (`engine.py`,
  écriture du `match_start`). Les nouvelles clés de configuration y apparaîtront par ce mécanisme
  existant, comme `share_port` en phase 2 ; ce n'est pas un changement de format du journal, et
  aucun résultat d'ouverture ni aucune adresse n'y entre.
- **Adresse interne du mapping** : celle de l'hôte sur l'interface qui a joint la box (adresse locale
  de la connexion vers la box), jamais une adresse saisie par l'utilisateur. Sur une vraie box ce
  n'est donc jamais la boucle ; avec le faux IGD local, c'est `127.0.0.1`.
- **Configuration invalide** : `runtime.json` n'a pas de validation au chargement aujourd'hui
  (`rules.load_runtime` lit le JSON). Une durée de bail hors de ]0, 7200] est donc refusée au moment
  de l'ouverture, sans trafic, plutôt qu'au démarrage ; ce rejet local a sa propre forme de résultat
  (`invalid_config`), distincte d'un refus de la box.

## Requirements

- R1: Le service sait découvrir une box compatible sur le réseau local et dialoguer avec elle en
  stdlib seule. La découverte et chaque requête sont bornées par des délais lus dans `runtime.json` ;
  une réponse plus grande que la taille maximale configurée, malformée, sans service compatible, ou
  dont l'URL de description désigne un hôte autre que celui qui a répondu à la découverte, est
  traitée comme « pas de box compatible » et n'entraîne aucune requête vers un tiers. Aucune
  exception du client ne remonte à l'appelant : il rend toujours l'un des résultats de R4.
- R2: L'ouverture n'a lieu que sur une action explicite de l'hôte, reçue par une route POST locale
  dédiée, pendant la phase `lobby` d'un duel réel. Hors de ce cas (autre phase, mode simulé,
  ouverture déjà en cours), l'action est refusée sans trafic réseau. Aucune découverte ni aucun
  mapping n'a lieu au démarrage du service, au lancement d'un duel, en tirant une revanche, ni par
  la saisie d'une adresse (`POST /api/invite/address` reste sans effet réseau, AC58 de la phase 2).
- R3: Une ouverture pose exactement trois mappings, pris dans `runtime.json` : `ports.B.game` en UDP,
  `ports.B.udp` en UDP, `share_port` en TCP, chacun avec port externe = port interne, la description
  configurée, et un bail égal à la durée configurée, strictement positive et au plus 7 200 s (une
  valeur hors de cet intervalle fait rendre `invalid_config` sans trafic). Chaque mapping est
  supprimé avant d'être ajouté, de sorte qu'un mapping déjà présent ne fait pas échouer l'ouverture.
  Aucun port du camp A, aucun port RCON, aucune plage ni DMZ n'est jamais demandé. Si l'un des trois
  ajouts échoue (erreur de la box, délai dépassé, réponse illisible), les mappings de cette ouverture
  qui ont pu être posés — ajouts confirmés et ajout sans réponse — sont supprimés avant de rendre le
  refus. Un mapping dont la suppression n'est pas confirmée par la box reste « réputé actif » : il
  est mentionné dans le résultat et retenu pour l'arrêt (R6).
  Règle d'abandon : une ouverture s'arrête à la première requête dont le délai expire ; elle
  n'émet plus aucune requête d'ouverture (suppression préalable, ajout, lecture d'adresse) après
  cette expiration. Si la requête expirée est `GetExternalIPAddress`, les trois ajouts sont
  confirmés et le résultat est `mapped` sans adresse proposée. Sinon, le retrait qui suit fait au
  plus une tentative de `DeletePortMapping` par mapping possiblement posé et s'arrête lui aussi à la
  première suppression dont le délai expire ; les mappings dont le retrait a expiré ou n'a pas été
  tenté restent réputés actifs. Une ouverture subit donc au plus deux expirations de requête (une
  pour l'ouverture, une pour le retrait), en plus de la lecture de la description, elle-même bornée
  par un délai de requête.
- R4: Le résultat d'une ouverture est exposé à l'UI locale dans l'état du service, sous l'une des
  formes : neutre (aucune ouverture demandée), `in_progress`, `mapped` (avec les trois ports, l'heure
  d'expiration du bail, et l'adresse publique ou l'absence motivée d'adresse), `no_gateway`,
  `refused` (une box compatible a été trouvée mais l'ouverture n'a pas abouti ; porte soit le code et
  le texte d'erreur UPnP rendus par la box, soit, sans erreur de la box, la nature de l'échec : délai
  dépassé ou réponse illisible ; et la liste des mappings encore réputés actifs, éventuellement
  vide), `invalid_config` (rejet local avant tout trafic ; porte le nom de la clé de `runtime.json`
  fautive). Les formes `no_gateway`, `refused` et `invalid_config` portent une consigne manuelle qui
  nomme les trois ports et leur protocole. Pendant `in_progress`, l'état du service reste lisible et le
  lancement du duel reste possible ; aucun résultat n'empêche ni ne retarde le lancement d'un duel,
  et sans clic l'état et l'invitation sont identiques à ceux d'aujourd'hui.
- R5: L'adresse publique est lue par `GetExternalIPAddress`, jamais devinée. Elle n'est proposée que
  si elle est une IPv4 publique, c'est-à-dire hors de tout bloc à usage spécial IANA (RFC 6890),
  documentation RFC 5737 comprise ; elle sert seulement à pré-remplir le champ d'adresse du lobby, que
  l'hôte peut corriger. Elle n'entre dans l'invitation, le lien, le `.bat` ou le QR qu'après que l'hôte
  l'a acceptée par le chemin existant (« Use this address »). Les seuls envois qui peuvent la porter
  sont, limitativement : (a) la réponse de l'API locale à l'UI ; (b) la requête
  `POST /api/invite/address` que l'UI envoie à l'API locale quand l'hôte accepte l'adresse ; (c) après
  cette acceptation seulement, l'invitation et le `.bat` de l'invité, servis par l'écouteur de
  partage existant (`_build_invite`), comme toute adresse acceptée aujourd'hui. Elle n'apparaît
  jamais ailleurs : ni dans le journal JSONL du match, ni dans le journal d'événements du service
  (`state.log`), ni dans les logs Python, ni sur stdout/stderr, ni dans aucun envoi vers une adresse
  hors de la boucle locale émis par le service de sa propre initiative (requêtes UPnP comprises), et
  aucune connexion n'est ouverte vers elle.
- R6: À l'arrêt propre du service (sortie par l'UI ou signal d'arrêt), chaque mapping posé par le
  service et encore réputé actif — mappings d'une ouverture `mapped`, et mappings d'une ouverture
  `refused` dont le retrait n'a pas été confirmé (R3) — est supprimé par `DeletePortMapping`, dans un temps borné par les
  délais configurés : l'arrêt fait au plus une tentative de suppression par mapping retenu, vers la
  box de l'ouverture, sans nouvelle découverte ni nouvelle lecture de description ; au plus trois
  mappings étant retenus, il subit au plus trois expirations de requête, sans empêcher l'arrêt des serveurs ni la fin du processus en cas d'échec ou de
  box muette. Un échec de suppression est consigné sans adresse. Si le processus meurt sans arrêt
  propre, seul le bail limite la durée du mapping.
- R7: Toutes les valeurs de la fonction (délais de découverte et de requête, durée du bail,
  description du mapping, taille maximale d'une réponse) viennent de `runtime.json` ; aucun port de
  `runtime.json` (ports du jeu, du RCON et de partage) ni aucune de ces valeurs n'est un littéral du
  code. Les constantes du protocole SSDP — le port 1900 et le groupe multicast `239.255.255.250` —
  ne sont pas de la configuration : ce sont des littéraux assumés, chacun défini une seule fois
  comme constante nommée dans `service/upnp.py`. `managed_ini.UPnP` reste `"false"`, et le service
  n'importe que la stdlib.
- R8: Le lobby d'un duel réel montre un bouton qui nomme, avant le clic, les trois ports et protocoles
  qui seront ouverts et la durée du bail ; puis le résultat de R4 en clair ; et, quand une adresse
  publique est proposée, la place dans le champ « Public address » sans écraser une saisie que
  l'hôte a déjà faite. Les chaînes ajoutées sont en anglais. En mode simulé, le bouton n'existe pas.
- R9: Les tests de la fonction n'ouvrent aucune connexion vers une adresse autre que la boucle locale
  et n'émettent aucun datagramme multicast : un faux IGD local sert de double, et la suite reste
  verte sur Linux et sur `pytest-windows` (3.12, 3.13, 3.14).

## Acceptance criteria

- AC1 (R1): Contre le faux IGD en succès, le client découvre la box, lit sa description et identifie
  un service `WANIPConnection:1` ; contre un faux IGD qui n'expose que `WANPPPConnection:1`, il
  l'identifie aussi. Les deux cas rendent `mapped`.
- AC2 (R1): Découverte sans aucune réponse : le client rend `no_gateway` en au plus
  (délai de découverte configuré + 1 s), mesuré avec un délai réglé à 0,5 s par le test.
- AC3 (R1): Description de 1 octet au-delà de la taille maximale configurée, XML malformé, et
  description sans service compatible : chacun des 3 cas rend `no_gateway`, aucune exception ne
  remonte, et 0 requête SOAP n'est reçue par le faux IGD.
- AC4 (R1): Une réponse de découverte venue de `127.0.0.1` dont l'URL de description désigne un autre
  hôte (`127.0.0.2` ou un nom) rend `no_gateway`, et 0 connexion n'est tentée vers cet hôte (sockets
  espionnées).
- AC5 (R1): Un faux IGD qui accepte la connexion SOAP sans jamais répondre fait rendre au client un
  résultat `refused` dont la nature d'échec est « délai dépassé » (sans code UPnP) en au plus (délai de requête configuré × 3 + 1 s) pour une
  ouverture complète, mesuré avec un délai réglé à 0,5 s. La borne découle de la règle d'abandon de
  R3 : au plus une expiration pour la lecture de la description, une pour la requête d'ouverture qui
  arrête l'ouverture, une pour le retrait, soit 3 délais ; la seconde de marge couvre la découverte
  (délai de découverte réglé par le test à au plus 0,5 s) et les requêtes qui reçoivent une réponse.
- AC6 (R2): `POST` sur la route d'ouverture en phase `lobby` d'un duel réel (doubles de serveurs et
  faux IGD) rend une réponse d'acceptation et le faux IGD reçoit des appels ; la même requête en
  phases `idle`, `running`, `ended` et en mode simulé est refusée avec 0 paquet émis (sockets
  espionnées) et un message qui dit pourquoi.
- AC7 (R2): Un parcours complet démarrage → lancement d'un duel → lobby → revanche → lobby, sans clic
  sur l'ouverture, produit 0 appel reçu par le faux IGD et 0 datagramme émis vers un port 1900 ou un
  groupe multicast (sockets espionnées).
- AC8 (R2): Un second `POST` d'ouverture pendant qu'une ouverture est `in_progress` est refusé, et le
  faux IGD reçoit au total 1 série d'appels (3 suppressions + 3 ajouts + 1 lecture d'adresse, au plus).
- AC9 (R2): `POST /api/invite/address` avec `192.0.2.1` ne déclenche toujours aucune connexion ni
  aucun datagramme (AC58 de la phase 2 reste vert, inchangé).
- AC10 (R3): Après une ouverture réussie, le faux IGD a reçu exactement 3 `AddPortMapping` dont les
  couples (port externe, protocole) sont {(`ports.B.game`, UDP), (`ports.B.udp`, UDP),
  (`share_port`, TCP)} de `runtime.json`, chacun avec port interne = port externe, la description
  configurée, `NewLeaseDuration` égal à la durée configurée, `NewEnabled` à 1, et
  `NewInternalClient` égal à l'adresse locale de la socket qui a joint le faux IGD (dans le test,
  `127.0.0.1`), jamais l'adresse saisie dans le champ du lobby (`198.51.100.9` saisie avant le clic).
- AC11 (R3): Pour chacun des 3 ports, le `DeletePortMapping` correspondant est reçu avant son
  `AddPortMapping` ; un faux IGD qui rend l'erreur 714 (`NoSuchEntryInArray`) aux suppressions laisse
  l'ouverture aboutir à `mapped`.
- AC12 (R3): Aucun appel reçu par le faux IGD, sur toute la suite, ne porte un port de `ports.A.*`,
  `ports.B.rcon`, ni un port externe à 0, ni un `NewRemoteHost` non vide.
- AC13 (R3): Avec une durée de bail configurée à 7 201 s, puis à 0 s (bail illimité en UPnP), une
  ouverture demandée au lobby rend `invalid_config` (et non `refused`) avec le nom de la clé de
  configuration fautive, sans code UPnP, et le faux IGD reçoit 0 appel et 0 datagramme de découverte
  n'est émis (sockets espionnées) ; 7 200 s est accepté et rend `mapped`.
- AC14 (R3): Un faux IGD qui refuse le troisième `AddPortMapping` (erreur 718 `ConflictInMappingEntry`)
  fait rendre `refused` avec le code 718, et a reçu ensuite un `DeletePortMapping` pour chacun des 2
  mappings ajoutés juste avant ; son tableau de mappings final est vide, et la liste des mappings réputés actifs du résultat
  est vide.
- AC15 (R4): Pour chacun des 3 cas (succès, aucun IGD, refus 718), `GET` de l'état rend un objet
  d'ouverture dont le statut vaut respectivement `mapped`, `no_gateway`, `refused` ; `refused` porte
  le code 718 ; `mapped` porte les 3 ports et une heure d'expiration égale à l'heure du clic + la
  durée du bail (± 2 s) ; `no_gateway` et `refused` portent un texte qui contient les 3 numéros de
  port, `UDP` et `TCP` ; il en va de même pour `invalid_config` dans le cas d'AC13.
- AC16 (R4): Avec un faux IGD qui retarde chaque réponse de 2 s, un `GET` de l'état pendant
  l'ouverture répond en moins de 0,5 s avec le statut `in_progress`, et l'action de lancement du duel
  est acceptée pendant ce temps.
- AC17 (R4): Après `no_gateway` et après `refused`, un duel se lance, atteint `running` avec les
  doubles de serveurs, et l'invitation produite est identique, champ par champ, à celle produite sans
  aucun clic d'ouverture (même adresse détectée, même portée, même texte de ports à ouvrir).
- AC18 (R4): Sans aucun clic, l'état rendu par l'API pour les phases `idle` et `lobby` ne diffère de
  celui de la phase 2 que par la présence d'un objet d'ouverture au statut neutre (aucune ouverture
  demandée) ; le champ `invite` est identique.
- AC19 (R5): Avec un faux IGD qui rend l'adresse externe `5.6.7.8`, l'état expose cette adresse
  comme adresse proposée, et le champ `invite.address` reste l'adresse détectée tant que
  `POST /api/invite/address` n'a pas été appelé avec elle ; après cet appel, le lien de l'invitation
  la contient.
- AC20 (R5): Après le parcours d'AC19 jusqu'à la fin d'un duel puis l'arrêt du service, la chaîne
  `5.6.7.8` apparaît 0 fois dans le journal JSONL du match, 0 fois dans `state.log`, 0 fois
  dans les logs Python capturés (`caplog`) et 0 fois dans stdout/stderr ; et 0 connexion n'a été
  ouverte vers elle (sockets espionnées). La réponse de l'API locale à l'UI, la requête
  `POST /api/invite/address`, ainsi que l'invitation et le `.bat` servis à l'invité après
  l'acceptation sont des envois autorisés par R5 et hors du périmètre de ce critère.
- AC21 (R5): Avec une adresse externe rendue égale à `10.0.0.5`, `100.64.1.1`, `0.0.0.0`,
  `203.0.113.77` (documentation RFC 5737), `192.0.2.1` (documentation RFC 5737), puis une chaîne vide,
  et une réponse en erreur à `GetExternalIPAddress` : les 7 cas rendent `mapped` sans
  adresse proposée, avec un texte qui dit que l'adresse publique n'est pas connue ou n'est pas
  joignable depuis Internet.
- AC22 (R6): Après une ouverture `mapped`, l'arrêt du service par `POST /api/quit` puis par signal
  d'arrêt (deux tests) fait recevoir au faux IGD exactement 3 `DeletePortMapping` portant les 3
  couples (port, protocole) d'AC10, et son tableau de mappings final est vide.
- AC23 (R6): Avec un faux IGD qui ne répond plus au moment de l'arrêt, `Engine.shutdown` rend la main
  en au plus (3 × délai de requête configuré + 1 s) avec un délai réglé à 0,5 s — borne qui découle
  de R6 : au plus une tentative de suppression par mapping retenu, trois mappings au plus, sans
  découverte —, les doubles de
  serveurs sont arrêtés, et un message d'échec de suppression est consigné sans l'adresse externe.
- AC24 (R6): Sans ouverture préalable, ou après `no_gateway`, `invalid_config`, ou un `refused` dont
  la liste des mappings réputés actifs est vide (cas d'AC14), l'arrêt du service ne fait
  recevoir aucun appel au faux IGD et n'émet aucun datagramme.
- AC25 (R7): Un test qui parcourt l'AST de `service/upnp.py` y trouve 0 littéral entier égal à l'un des
  ports de `runtime.json` (17271, 17272, 17280, ni ceux du camp A ou RCON), et 0 littéral égal à la
  durée du bail ou à la description configurées ; changer ces 3 valeurs dans une copie de
  `runtime.json` passée au moteur change d'autant les arguments reçus par le faux IGD.
- AC26 (R7): `tests/test_servers.py` asserte toujours `UPnP=false` dans l'ini écrit, et
  `tests/test_stdlib_only.py` passe avec `service/upnp.py` présent.
- AC27 (R8): Le rendu du lobby d'un duel réel contient un bouton dont le texte ou le libellé voisin
  nomme les 3 ports avec `UDP`/`TCP` et la durée du bail, tels que rendus par l'API ; le rendu en mode
  simulé ne contient pas ce bouton.
- AC28 (R8): Après un résultat `mapped` avec adresse proposée, le champ `#f-address` contient cette
  adresse si sa valeur était vide, et garde sa valeur si l'hôte y avait déjà saisi `198.51.100.9` ;
  après `no_gateway` ou `refused`, le texte affiché contient la consigne manuelle d'AC15 ; après `mapped`, le texte affiché contient
  l'heure d'expiration rendue par l'API (heure du clic + durée du bail, ± 2 s).
- AC29 (R8): Chaque chaîne visible ajoutée à `ui/app.js` par la phase est en anglais : relecture du
  diff, et, dans les lignes ajoutées, aucun caractère accentué (`é`, `è`, `à`, `ç`) et aucune
  occurrence des mots `adresse`, `ouvrir`, `échec`.
- AC30 (R9): Pendant toute l'exécution de `tests/test_upnp.py` et des tests ajoutés par la phase, un
  garde de socket enregistre 0 `connect`/`sendto` vers une adresse hors `127.0.0.0/8` et 0 envoi vers
  `239.255.255.250` ; la suite complète passe sur Linux 3.12/3.13/3.14 et sur les trois jobs
  `pytest-windows`, avec un nombre de tests supérieur à 2 247 et 0 échec.
- AC31 (R2): La route d'ouverture figure dans `Handler.POST_ROUTES` ; pour chacune des origines
  étrangères (`FOREIGN_ORIGINS`) et chacun des hôtes étrangers (`FOREIGN_HOSTS`) de
  `tests/test_api_security.py`, un `POST` sur elle rend 403 ; un `POST` avec un `Content-Type` non
  JSON est refusé (statut 4xx) ; un `GET` sur la même route rend 404 et ne déclenche pas
  l'ouverture. Dans tous ces cas, `engine.actions` reste vide, le faux IGD reçoit 0 appel et 0
  datagramme n'est émis.
- AC32 (R3, R6): Un faux IGD qui refuse le troisième `AddPortMapping` (718) puis ne répond plus aux
  `DeletePortMapping` de retrait fait rendre `refused` avec le code 718 et une liste de 2 mappings
  réputés actifs (les 2 couples ajoutés) ; quand le faux IGD répond de nouveau, l'arrêt du service
  lui fait recevoir exactement 2 `DeletePortMapping` portant ces 2 couples, et son tableau de
  mappings final est vide.
- AC33 (R3, R6): Un faux IGD qui reçoit le premier `AddPortMapping`, l'enregistre, puis ne répond
  jamais, fait rendre `refused` (délai dépassé) ; il reçoit ensuite un `DeletePortMapping` pour ce
  couple (retrait du mapping possiblement posé), et, si ce retrait reste aussi sans réponse, l'arrêt
  du service lui envoie de nouveau exactement 1 `DeletePortMapping` pour ce couple.

## Amendment A1 — trois fichiers de test ajoutés aux cibles (10/11/2026)

Le plan dérivé de cette spécification touche trois fichiers qui ne figuraient pas dans `targets`, et
chacun pour une raison mécanique, pas pour élargir le périmètre :

- `tests/test_rules_load.py` : la nouvelle section `upnp` de `runtime.json` doit être assertée là où les
  autres clés le sont déjà.
- `tests/test_modes.py` : les refus hors lobby (R2) s'expriment avec les doubles de serveurs que ce
  fichier possède déjà.
- `tests/doubles/run_service.py` : le double de service doit connaître la nouvelle route, sinon les
  tests d'API ne peuvent pas la poster sans vrai moteur.

Aucune autre cible ne s'ajoute sans un nouvel amendement, et aucun critère d'acceptation n'est
modifié, affaibli ou retiré par celui-ci.
