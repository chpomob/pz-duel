# Crédits et inspirations

*Français — [English version](CREDITS.md)*


Ce projet n'existerait pas sans le travail de la communauté PZ. Règle de maison : **on crédite toujours, on ne recopie rien sans le dire**, et on ne réutilise aucun code de mod tiers sans licence compatible.

## Ce que le projet reprend, et de qui

| Projet | Auteur(s) | Licence / statut | Ce qu'on en reprend | Ce qu'on ne reprend pas |
|---|---|---|---|---|
| **Streamer Integration Framework (SIF)** | YebNFlo | Workshop 3387594292, tous droits réservés | L'**idée** du pont par fichier dans `Zomboid/Lua/` (lecture périodique, modules enregistrés, fichier vidé après traitement) | Aucun code, aucun fichier |
| **SIF — Example Events** | YebNFlo, ShavedTube | Workshop 3387596392 | Le **catalogue** de 48 modules déclenchables (spawns, météo, objets, véhicules, effets joueur) comme preuve de faisabilité | Aucun code |
| **Chaos Mod + Twitch/YouTube** | zoidxf (dépôt `clixff`) | MIT | L'**inventaire** des 462 effets (annexe `docs/design/fr/05-annexe-chaos-462.md`), utilisé pour cartographier ce qui est générable | Aucune implémentation d'effet recopiée : notre sous-ensemble est écrit à part |
| **log-extender** | outdead (dépôt `openzomboid`) | MIT | La **cartographie client/serveur** des signaux (craft via `ISHandcraftAction`, `PlayerLogger`, `CraftLogger`) | Aucun code |
| **zomboid-mod-analytics** | spoutnickgp | dépôt public | La **preuve** que `OnCharacterDeath`, `OnHitZombie`, `OnWeaponHitCharacter` et `OnEnterVehicle` sont exploitables côté serveur | Aucun code |
| **Zombie Spawn Manager** | El1oN (Elyon) | droits réservés par l'auteur (interdiction de modification) | La **documentation des limites** de B42 (pas de setters publics par zombie) | Aucun code, aucune ressource |
| **ZombieBuddy** | Andrey « Zed » Zaikin (`zed-0xff`) | licence permissive | Évaluation de la voie « agent Java » pour une version future | Non utilisé dans le MVP |
| **pzJBridge** | `rocky6777` | dépôt public | Évaluation de la voie pont Java (42.21) | Non utilisé |
| **PZHook** | Olipro | dépôt public | Référence historique sur le hooking Java | Non utilisé |
| **PZEventDoc** | `demiurgeQuantified` | documentation publique | La liste des 260 événements et leurs marquages `(Client)` / `(Server)` | — |
| **PZwiki** | communauté | CC / wiki | Versions, paramètres de lancement (`+connect`, `-adminpassword`), options serveur | — |
| **qrcode-generator** | Kazuhiko Arase | **MIT** | **Code embarqué** : `ui/vendor/qrcode.js` (seule dépendance de code tierce du projet) | — (voir `ui/vendor/qrcode.LICENSE.txt`) |

## Principes

1. **Aucun code de mod tiers n'est copié.** Ce qu'on reprend, ce sont des idées d'architecture, des listes de capacités et des noms d'API publiques du jeu.
2. **Toute référence est nominative** : nom du mod, auteur, lien. Si un auteur demande le retrait d'une mention, elle est retirée immédiatement.
3. **Licences respectées** : MIT pour le QR (texte conservé à côté du fichier) ; les mods « tous droits réservés » ne sont cités qu'à titre documentaire.
4. **Beaucoup de ces projets sont récents et parfois réalisés avec assistance IA.** Raison de plus pour être explicite : créditer les auteurs, ne pas aspirer leur travail, et dire clairement ce qui vient d'eux et ce qui vient de nous.

## Remerciements

À la communauté du modding PZ (forum The Indie Stone, PZ Modding Discord), aux auteurs ci-dessus, et à The Indie Stone pour une API Lua assez ouverte pour rendre ce genre de projet possible.
