# Spike 0 — preuves d'exécution (04/11/2026)

Objectif : prouver la moitié « ops » de l'architecture B sans joueur : démarrage d'un serveur dédié, chargement d'un mod Lua côté serveur, IPC fichier dans les deux sens, surface de configuration réelle.

Machine : Ryzen 7 7700, 31 Go RAM, Linux. Jeu installé : `/home/chpo/.steam/steam/steamapps/common/ProjectZomboid/projectzomboid`.

---

## 1. Ce qui a été exécuté

```bash
# serveur headless depuis l'install client (pas besoin du paquet serveur séparé)
./start-server.sh -servername duelspike -port 17261
```

Résultat :

```
LOAD : Loading RakNet64...
LOAD : Loading ZNetNoSteam64...
LOG  : server name is "duelspike"
LOG  : *** SERVER STARTED ****
LOG  : server is listening on port 17261
LOG  : version=42.21.0 4a0e9546ec demo
```

Démarrage en ~5 s après génération du monde, ~2048 Mo de heap, aucune fenêtre, aucun Steam (`-Dzomboid.steam=0` déjà dans le script).

**Correction importante : la version installée est 42.21.0 (unstable), pas 42.20.4.** Le `console.txt` du profil datait de septembre (42.20.4) ; c'est le serveur qui fait foi.

---

## 2. Mod Lua serveur chargé

Emplacement et structure exigés par B42 (layout versionné, confirmé en comparant avec les mods Workshop installés) :

```
~/Zomboid/mods/duelspike/mod.info                 # manifeste racine
~/Zomboid/mods/duelspike/42.20/mod.info           # variante de version (versionMin=42.20.0)
~/Zomboid/mods/duelspike/42.20/media/lua/server/DuelSpike.lua
```

et dans `~/Zomboid/Server/duelspike.ini` : `Mods=duelspike`.

Un mod placé à plat (`~/Zomboid/mods/<id>/media/...` sans dossier de version) n'est **pas** trouvé :

```
WARN : Mod at ZomboidFileSystem.loadModAndRequired> required mod "DuelSpike" not found
```

C'est un piège de packaging à retenir : sans le sous-dossier de version, le mod est silencieusement ignoré.

---

## 3. IPC fichier — les deux sens

Le serveur écrit et lit dans **`~/Zomboid/Lua/`** (même dossier que côté client, résolu relativement).

Fichier écrit par le mod (chemin sortant) :

```
init age=2.0922 ver=42.21
tick=300 age=2.2253
tick=600 age=2.3586
init age=2.4657 ver=42.21
in lines=2 last=from_referee 2 spawn 3
```

Ligne `in lines=2 last=...` = le mod a relu un fichier **écrit par un processus externe** (deux lignes `from_referee ...` déposées par le shell) au moment de son initialisation. Les deux sens du transport sont donc prouvés, sans client, sans réseau, sans Java mod.

`OnTick` se déclenche bien côté serveur, et `getGameTime():getWorldAgeHours()` est lisible (2.09 → 2.47 h de monde sur la session).

---

## 4. Surface de configuration réelle (`duelspike.ini` généré)

```
PauseEmpty=true          # défaut : le temps s'arrête quand personne n'est connecté
UPnP=true
RCONPort=27015           # défaut 27015, PAS "port de jeu + 1"
RCONPassword=            # vide par défaut
Mods=
WorkshopItems=
```

Deux corrections par rapport à l'étude initiale :
- **RCON a son propre port** (27015 par défaut). Deux serveurs sur la même machine doivent donc utiliser deux `RCONPort` distincts (27015 / 27016), sinon le second échoue.
- `PauseEmpty` est à `true` par défaut : à passer à `false` sur les deux serveurs, sinon un joueur qui décroche fige son monde et pas l'autre (dérive immédiate).

---

## 5. Comportement observé qui reste à mesurer avec deux joueurs

Avec zéro joueur connecté, le serveur avance mais **pas au rythme nominal** : ~0,37 h de jeu sur ~4 min réelles, alors que `DayLength=4` devrait donner 0,27 h/min. Le loop de tick semble se throttler quand la partie est vide (les compteurs `tick=300/600` s'arrêtent ensuite).

Conséquence pratique : ne pas compter sur des serveurs vides pour rester synchro. L'horloge de match doit démarrer **quand les deux joueurs sont connectés**, et la surveillance de dérive doit être active pendant le match, pas avant.

À mesurer au prochain spike (avec deux clients) : ticks/s réels à deux joueurs, dérive sur 20 min, et comportement de `PauseEmpty=false` quand un joueur se déconnecte.

---

## 6. Ce qui reste à prouver (spike 1, avec un joueur)

1. `OnZombieDead` + `getAttackedBy()` : attribution correcte à 100 % sur 100 kills.
2. `addZombiesInOutfit(x, y, z, n, nil, nil)` côté serveur : spawn visible côté client, pas de crash, FPS stable.
3. Craft : `Recipe_OnCreate` / `OnProcessAction` — le signal existe-t-il côté serveur en multi ?
4. Déconnexion/reconnexion d'un joueur : effet sur l'horloge et sur les events.

---

## 7. Artefacts laissés sur la machine

- Mod de spike : `~/Zomboid/mods/duelspike/` (à supprimer ou à promouvoir en harnais de test).
- Serveur de spike : `~/Zomboid/Server/duelspike.ini`, monde `~/Zomboid/Saves/Multiplayer/duelspike` (5,6 Mo).
- Journaux : `~/.hermes/cache/scratch/pzspike/`.

Nettoyage : supprimer ces trois chemins (serveur arrêté) ; rien d'autre n'a été modifié.
