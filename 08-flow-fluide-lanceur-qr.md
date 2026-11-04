# Flow fluide : lanceur, fenêtre de réglage, invitation par QR

Objectif : « je lance ma partie VS, ça monte deux serveurs, ça me montre mes réglages, ça me donne un QR à montrer à mon pote ». Tout ce qui peut passer par Steam Workshop passe par Steam Workshop.

---

## 1. Ce que le test vient de prouver (04/11/2026)

Deux serveurs dédiés lancés simultanément sur la même machine :

```
*** SERVER STARTED ****  server is listening on port 17261
*** SERVER STARTED ****  server is listening on port 17271
```

Et les deux mods écrivent dans le **même dossier partagé** `~/Zomboid/Lua/`, dans des fichiers distincts :

```
~/Zomboid/Lua/duel_A_out.txt : init side=A age=2.0000 ver=42.21
~/Zomboid/Lua/duel_B_out.txt : init side=B age=2.0000 ver=42.21
```

Trois conséquences majeures :

1. **La liaison ne nécessite aucun processus externe** : les deux serveurs, lancés sous le même compte système, partagent `~/Zomboid/Lua/`. Le serveur A écrit, le serveur B lit. Le transport est dans le jeu.
2. **Les horloges démarrent identiques** (2.0000 h des deux côtés) quand les mondes sont créés dans la même session — la synchronisation temporelle est un fait de configuration, pas un mécanisme à piloter.
3. Le « directeur » (règles, score, minuteur) peut vivre **dans le mod du serveur A**, qui lit l'outbox de B et écrit son inbox. Plus d'arbitre Python pour le transport ni pour les règles.

Ce qui reste impossible à un mod Lua : `os.*`, les sockets, le lancement de processus. Donc **lancer deux serveurs, restaurer des saves et ouvrir une fenêtre restent du ressort d'un lanceur externe.**

---

## 2. Ce que le Workshop peut porter — et ce qu'il ne peut pas

| Élément | Workshop ? | Détail |
|---|---|---|
| Mod serveur (`duelcore`) | **oui** | publié comme mod, requis par le serveur |
| Mod client (`duelhud`) | **oui** | le serveur pousse `WorkshopItems`, le client souscrit automatiquement |
| Presets et tables (`relations.yaml`) | **oui** | fichiers embarqués dans le mod ; l'hôte peut en charger d'autres |
| Documentation, changelog | **oui** | page Workshop |
| Lancement de deux serveurs | **non** | aucun mod ne peut créer un processus |
| Restauration des mondes entre matchs | **non** | accès disque hors du dossier Lua |
| Fenêtre de réglage / QR | **non** | pas d'UI hors du jeu |

Conclusion : **le Workshop porte tout le jeu ; un lanceur unique porte la mécanique.** C'est la seule pièce hors Workshop, et elle peut tenir en un exécutable de quelques Mo distribué par GitHub (ou copié dans le dossier du mod pour les utilisateurs avancés).

---

## 3. Le flow

```
[1] Le joueur double-clique « PZ Duel »
        │
[2] Le lanceur : vérifie l'install PZ, l'existence des mods Workshop,
    démarre serveur A (17261) + serveur B (17271), attend « SERVER STARTED » ×2
        │
[3] Fenêtre de réglage (locale) : pseudo, preset, durée, DayLength,
    liste de mods (lecture seule, issue du preset), aperçu de la table en français,
    bouton « Lancer le duel »
        │
[4] Écran de lobby : QR code + code de match à 5 caractères,
    contrat lisible, état « en attente de l'adversaire… »
        │
[5] L'invité scanne le QR
        │
[6] Le client de l'invité rejoint ; PZ lui propose les mods Workshop requis,
    il souscrit (1 clic Steam), se connecte
        │
[7] Les deux joueurs connectés → décompte 10 s → match
```

Retour arrière possible à chaque étape : si un serveur ne démarre pas, le lanceur affiche la raison (port occupé, mod manquant, RAM insuffisante) au lieu d'un écran noir.

---

## 4. L'invitation : QR et lien

Contenu du QR (tout est local, aucun service tiers, aucun compte) :

```
pzduel://join?host=82.64.x.x&port=17261&pw=K7QX2&match=8f3a
```

- Si le lanceur est installé chez l'invité, il enregistre le protocole `pzduel://` → le scan ouvre le lanceur, qui lance PZ avec les bons arguments.
- Sinon, le QR pointe vers une page locale servie par le lanceur (`http://<ip>:8787/join/8f3a`) qui affiche IP, port, mot de passe et un bouton « copier », plus les 3 clics pour la connexion directe.

**Connexion automatique vérifiée dans la doc officielle des paramètres de lancement** : `+connect {ip}:{port}` et `+password {mot de passe}` (équivalents de `-Dargs.server.connect`). Le lanceur peut donc démarrer PZ directement dans la bonne partie, sans manipulation.

Cas CGNAT : le lanceur détecte que l'IP publique ne répond pas sur le port et propose soit un tunnel, soit un serveur distant. À traiter en v2, mais l'écran doit le dire clairement plutôt que de laisser l'invité échouer.

---

## 5. Fluidité : les détails qui font la différence

1. **Aucune installation manuelle de mod** : les deux mods sont sur le Workshop ; le serveur impose la liste ; le client souscrit automatiquement en rejoignant.
2. **Aucune saisie d'IP** : QR → lanceur → `+connect` → `+password`.
3. **Aucun réglage de port** : 17261/17271 et RCON 27015/27016 sont choisis par le lanceur, avec détection de conflit.
4. **Contrat visible avant d'accepter** : l'invité voit la table en français dans son lobby (ou sur la page du QR) avant de dire oui.
5. **Rematch en un clic** : les serveurs restent up, les snapshots se restaurent, le QR reste valable. Le code de match ne change pas.
6. **Hôte non-admin** : le mot de passe admin est généré et conservé par le lanceur, jamais affiché au joueur.
7. **Un seul composant à installer côté invité** (le lanceur, optionnel mais recommandé) ; zéro côté hôte au-delà de Steam.

---

## 6. Ce que le lanceur fait exactement (et rien d'autre)

| Étape | Action | Échec → message |
|---|---|---|
| Préflight | trouve l'install PZ, Java, RAM libre, ports libres, mods présents | « port 17261 occupé », « 2 Go libres requis » |
| Start | 2 × `start-server.sh`/`bat` avec `-servername`, `-port` et **`-adminpassword`** | « le serveur A n'a pas démarré : voir A.log » |
| Contract | lit les deux `.ini`, compare `Mods=`/`WorkshopItems=` au preset | « mods différents entre les deux serveurs » |
| Window | UI locale (page web embarquée ou fenêtre native) | — |
| Invite | QR + lien + code de match | « IP publique injoignable : CGNAT probable » |
| Match | minuteur, score, journal, arrêt propre | « dérive d'horloge > 60 s : match invalidé » |
| Reset | restore des snapshots serveurs arrêtés | « snapshot absent : monde neuf » |

Aucune règle de jeu dedans : la table est évaluée par le mod `duelcore` du serveur A.

---

## 7. Deux modes, un seul binaire

Le lanceur n'a pas besoin d'être complet pour être utile. Deux modes, choisis par argument :

| Mode | Usage | Ce qu'il fait | Ce qu'il n'a pas |
|---|---|---|---|
| `duel admin` | **MVP**, hôte seul | démarre les 2 serveurs (`-adminpassword`), attend `SERVER STARTED` ×2, applique la table, tail les outbox, écrit les inbox, expose une **interface locale** (état, config, progression, lobby + QR, logs), arrêt propre sur Ctrl-C | rien : l'UI est décrite dans `09-ui-full-ingame.md` |
| `duel play` | quotidien | démarre PZ avec `+connect IP:PORT +password mdp` (+ le mod si besoin) | rien d'autre |

Point important : **le lanceur n'installe jamais de mod.** Le serveur déclare ses `WorkshopItems`, le client souscrit en rejoignant. « Lancer PZ avec le bon mod » est donc automatique ; la seule exception est un test hors ligne.

Le mode admin n'est **pas** une console : il ouvre une interface locale (`09-ui-full-ingame.md`).

## 8. Où vit le directeur pour le MVP

Deux implémentations possibles du même protocole :

| | Directeur en Python (lanceur) | Directeur en Lua (mod du serveur A) |
|---|---|---|
| Vitesse d'itération | élevée (éditer, relancer, logger) | faible (restart serveur à chaque changement) |
| Journal / debug | natif | à écrire |
| Zéro installation | non (le lanceur existe déjà) | oui (tout Workshop) |
| Risque | aucun sur le jeu | dépend des APIs Lua en MP |

Recommandation : **directeur en Python pour le MVP**, protocole identique à celui de la spec, et portage Lua seulement si le besoin « zéro installation » devient prioritaire. Le lanceur existe de toute façon pour démarrer les serveurs, donc la logique de règles y est gratuite.

---

## 9. Décisions ouvertes

1. **Techno du lanceur** — (a) Python packagé (rapide, cross-platform, ~15 Mo) *(reco)* · (b) app .NET/Avalonia · (c) Electron (lourd).
2. **UI de réglage** — (a) page web locale servie par le lanceur *(reco : même rendu que le QR et le contrat)* · (b) fenêtre native.
3. **Distribution du lanceur** — (a) GitHub Releases + le Workshop pour les mods *(reco)* · (b) tout dans le dossier du mod Workshop (Steam télécharge les fichiers mais ne lance pas l'exe).
4. **Protocole `pzduel://`** — (a) enregistré à l'installation, QR one-click *(reco)* · (b) page web avec copier/coller.
5. **Invité sans lanceur** — (a) supporté (page web + 3 clics) *(reco)* · (b) lanceur obligatoire pour les deux.
