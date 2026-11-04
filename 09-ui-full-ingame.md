# UI du lanceur — tout visible, pas de console

Décision du 04/11/2026 : **pas de mode console**. Le lanceur est une application à interface dès le MVP : configuration, lancement, état, invitation, score — tout doit être visible et cliquable.

Contrainte de départ : un mod Lua ne peut pas lancer de processus, donc le service reste externe. Mais il est **pilotable et observable par une UI**, jamais par un terminal. Et le même état alimentera plus tard un menu in-game (voir §5).

---

## 1. Choix d'interface

| Option | Coût | Avantages | Inconvénients |
|---|---|---|---|
| **Page web locale servie par le service** *(reco MVP)* | 1-2 j | zéro dépendance, QR natif en HTML, même rendu réutilisable pour l'invité, itération instantanée | s'ouvre dans un onglet |
| Fenêtre native (pywebview) | +2 j | sensation d'app, pas d'onglet | dépendances webview par OS |
| Menu in-game uniquement (mod) | +1 semaine | immersion totale | il faut quand même démarrer le service avant le jeu |
| Console / TUI | — | — | **rejeté** |

Recommandation : **page web locale** au MVP (`http://127.0.0.1:8765`, ouverte automatiquement), fenêtre native en packaging (v1.2), miroir in-game en v1.1. Les trois consomment le même état : rien n'est réécrit.

Bonus : le QR, difficile à afficher en in-game, est trivial en HTML. Il vit donc dans l'UI du lanceur, pas dans le mod.

---

## 2. Écrans

### 2.1 Accueil / état
```
┌──────────────────────────────────────────────────────────┐
│  PZ DUEL                                    service : OK │
│                                                          │
│  Serveur A   ● prêt     17261   joueur : durand          │
│  Serveur B   ● prêt     17271   joueur : —               │
│  Horloge     —           Dérive : —                      │
│                                                          │
│  [ Configurer un duel ]      [ Reprendre le dernier ]     │
│  [ Voir les logs ]           [ Quitter ]                 │
└──────────────────────────────────────────────────────────┘
```

### 2.2 Configuration
```
┌──────────────────────────────────────────────────────────┐
│  Nouveau duel                                            │
│                                                          │
│  Preset        [ Duel rapide ▾ ]  (Duel rapide, Pression, │
│                                    Hardcore, Personnalisé)│
│  Durée         [ 25 min ▾ ]                              │
│  Jour de jeu   [ 15 min ▾ ]  (= DayLength 1)             │
│  Pseudo        [ durand            ]                     │
│  Mot de passe  [ aléatoire  ⟳ ]                           │
│                                                          │
│  Mods requis   duelcore ✓   duelhud ✓   (lecture seule)   │
│                ⚠ 1 mod manquant → [ ouvrir le Workshop ]  │
│                                                          │
│  Règles (aperçu)                                         │
│   • 1 zombie tué → 2 zombies chez l'adversaire (max 60/min)│
│   • 3 constructions puis 1 kill en 30 s → une horde       │
│   • 10 kills en 60 s sans être touché → brouillard        │
│                                        [ Modifier la table ]│
│                                                          │
│  [ Annuler ]                         [ Lancer le duel ]   │
└──────────────────────────────────────────────────────────┘
```

### 2.3 Lancement — progression honnête, étape par étape
```
  ✓ Préflight        install PZ, ports libres, 2 Go RAM, mods
  ✓ Serveur A        démarré en 34 s
  ⟳ Serveur B        génération du monde…              12 s
  · Contrat          vérification des mods
  · Lobby
```
Chaque étape a un état (attente / en cours / ok / échec) et un message actionnable (« port 17261 occupé : fermer l'instance précédente »). Jamais d'écran figé : si ça bloque, on voit où.

### 2.4 Lobby
```
┌──────────────────────────────────────────────────────────┐
│  Code de match   K7QX2                                   │
│  82.64.x.x:17261        mot de passe  K7QX2              │
│  ┌─────────┐   [ Copier le lien ]   [ Ouvrir PZ ]        │
│  │  QR     │                                             │
│  └─────────┘                                             │
│  Adversaire : en attente…                                │
│  [ Annuler le duel ]                                     │
└──────────────────────────────────────────────────────────┘
```

### 2.5 Match — panneau d'observation
Score des deux camps, minuteur, menaces/minute, derniers événements, dérive d'horloge. Sert à l'arbitrage et à l'équilibrage ; c'est aussi la source du HUD in-game en v1.1.

### 2.6 Logs
Panneau déroulant (pas une console système) : événements du service, logs des deux serveurs, erreurs surlignées, bouton « copier le diagnostic ».

---

## 3. Le service derrière l'UI

`duel-service` (Python), un seul processus, trois responsabilités :

1. **Cycle de vie** — préflight, démarrage/arrêt des deux serveurs (`-servername`, `-port`, `-adminpassword`), attente de `SERVER STARTED` ×2, restauration des snapshots.
2. **Directeur** — évaluation de la table (atomes → agrégats → séries → effets), score, horloge, budgets, journal JSONL.
3. **Serveur d'UI** — `127.0.0.1:8765` : page + `/api/state` (poll 1 s) + `/api/action` (start/stop/cancel/rematch). Écrit aussi `duel_state.json` dans `~/Zomboid/Lua/` pour le futur menu in-game.

Aucune fenêtre native au MVP : `webbrowser.open()` au démarrage. Bind sur `127.0.0.1` uniquement.

---

## 4. Découpage

| Étape | UI | Ce qui marche |
|---|---|---|
| **MVP** | page locale : état, config basique, progression, lobby + QR, logs | duel complet entre deux joueurs |
| **v1.1** | + panneau match, + éditeur de table, + revanche 1 clic ; miroir in-game | confort |
| **v1.2** | fenêtre native packagée (pas d'onglet) | sensation d'app |
| **v2** | menu in-game complet, service invisible | immersion |

---

## 5. Cap sur le full in-game (v1.1+)

Le mod client peut écrire et lire dans `~/Zomboid/Lua/` (prouvé). Donc le même service peut être piloté par le jeu :

- `duel_ctl.txt` (mod → service) : `{"cmd":"start","preset":"duel_rapide"}` · `stop` · `reset` · `rematch`
- `duel_state.json` (service → mod) : phase, serveurs, joueurs, invitation, horloge, score, menaces/min, erreur

Le menu in-game devient alors un miroir de la page web. On garde les deux : la page pour l'hôte (config, logs, QR), le menu pour l'immersion. Aucun changement de protocole entre les deux.

---

## 6. Points de vigilance

| Risque | Parade |
|---|---|
| L'onglet est fermé pendant un match | le service continue ; rouvrir `127.0.0.1:8765` |
| Port 8765 pris | port libre choisi automatiquement, URL affichée dans une petite fenêtre de secours |
| Service non lancé | raccourci « PZ Duel » ; message clair dans le mod in-game s'il tourne sans service |
| Config invalide | validation avant lancement, bouton désactivé + raison affichée |
| Mods manquants | badge rouge + bouton « ouvrir le Workshop », refus de lancer |
| Confusion « qui héberge » | l'UI dit explicitement « tu héberges ; envoie ce QR » |

---

## 7. Décisions ouvertes

1. **Techno UI MVP** — (a) page web locale servie par le service *(reco)* · (b) fenêtre native pywebview · (c) menu in-game direct.
2. **Éditeur de table** — (a) aperçu en lecture seule au MVP, éditeur en v1.1 *(reco)* · (b) éditeur dès le MVP.
3. **Onglet vs fenêtre** — (a) onglet au MVP, fenêtre packagée en v1.2 *(reco)* · (b) fenêtre native dès le MVP.
