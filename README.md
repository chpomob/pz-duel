# PZ Duel — service local

Interface de lancement et d'arbitrage d'un duel Project Zomboid : deux mondes séparés, aucune rencontre, chaque action d'un joueur devient une pression pour l'autre.

**État actuel : MVP d'interface, backend simulé.** Toute l'expérience (configuration, lancement, lobby, QR, score, logs) est fonctionnelle ; les deux serveurs PZ et le mod ne sont pas encore branchés. C'est volontaire : on valide l'UX et le protocole avant d'ajouter 2 × 2 Go de JVM.

## Lancer

```bash
cd /home/chpo/pz-duel
python3 -m service.main            # ouvre http://127.0.0.1:8765/
python3 -m service.main --port 8766 --no-browser
```

Aucune dépendance Python : stdlib uniquement. Le service écrit `.duel-service.json` (URL, port, pid) pour que les outils et, plus tard, le mod in-game retrouvent l'interface.

Parcours de test : `Configurer un duel` → preset **Démo 90 s** → `Lancer le duel` → dans le lobby, `Simuler l'adversaire`.

## Structure

```
service/
  main.py      point d'entrée, choix du port, fichier de découverte
  api.py       serveur HTTP local (UI + /api/state, /api/presets, /api/action)
  engine.py    machine à états du duel (préflight → serveurs → contrat → lobby → match)
  state.py     état partagé, étapes de lancement, journal
  presets/     duel_rapide, pression, hardcore, demo (règles + résumés)
ui/
  index.html   coquille + crédits
  app.js       rendu, actions, QR
  style.css    thème sombre
  vendor/      qrcode-generator (MIT, © Kazuhiko Arase) + licence
```

## Ce qui est simulé

- démarrage des serveurs (latences réalistes : 2,4 s et 3,6 s) ;
- connexion des joueurs, décompte, score et événements ;
- vérification des mods (aujourd'hui toujours « ok »).

## Ce qui reste à brancher

1. `engine` réel : lancer `start-server.sh|bat -servername duelA|duelB -port … -adminpassword …`, attendre `SERVER STARTED` ×2, vérifier `Mods=`/`WorkshopItems=`.
2. Mod `duelcore` (événements → `duel_A_out.txt`, commandes ← `duel_A_in.txt`, ack), mod `duelhud` (HUD).
3. Table de relations évaluée côté service (atomes → agrégats → séries → effets) et journal JSONL.
4. Snapshots de mondes + revanche en un clic.

## Documentation

- `01-etat-des-lieux.md` — faisabilité, écosystème, charges
- `02-archi-deux-serveurs.md` — deux serveurs, horloge, config réelle
- `03-mecanismes-et-telemetrie.md` — atomes/effets, règles, télémétrie
- `04-spike-0-preuves.md` — preuves d'exécution (serveur, IPC)
- `05-observable-vs-generable.md` — ce qui est captable / générable
- `06-play-flow-et-table-de-relations.md` — flow et table
- `07-spec-mvp.md` — spécification consolidée et critères d'acceptation
- `08-flow-fluide-lanceur-qr.md` — lanceur, invitation, deux modes
- `09-ui-full-ingame.md` — écrans, UI locale, cap in-game
- `CREDITS.md` — mods et auteurs dont on s'inspire (à lire avant toute reprise de code)
