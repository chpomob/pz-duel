# P13 — Écouteur de partage (R7)

commit: feat(phase2): P13 — Écouteur de partage (R7)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [service/share.py, service/engine.py, tests/test_share.py, tests/test_api_security.py, tests/test_secrets.py]
- **Description:** `ShareServer` : premier écouteur hors boucle locale (H1), lié à toutes les interfaces sur `share_port`, avec son propre `Handler` minimal sur `BoundedHTTPServer` (plafond 32, `REQUEST_READ_S`). Un seul chemin `/<jeton>/pz-duel-join.bat` en `GET`, comparé par `hmac.compare_digest`. Tout le reste : 404 ou 405 à corps vide, sans citer la requête ; `log_message` muet, `send_error` remplacé pour que le chemin ne sorte jamais. Il appelle `engine.guest_launcher()` à chaque requête, sans cache. Jeton `secrets.token_hex(16)` tiré à chaque `_open_lobby`, revanche comprise. Fermeture idempotente `_close_share()` à **toute** sortie du lobby : compte à rebours, `cancel`, panne, `shutdown`, donc dans `_finish_match`, `_cancel_state`, `_fail` et au passage de phase. Échec de liaison : `share_available: false`, texte anglais, lobby ouvert, enregistrement du `.bat` encore offert. `make_server` reste sur `127.0.0.1`. Couvre AC37, AC39, AC40, AC41, AC42.
- **Dependencies:** [P12]
- **Tests:** `tests/test_share.py` : AC37 (lien = `qr_text`, `http://`, mêmes octets que l'API) ; AC39 (les 6 refus, API toujours sur `127.0.0.1`) ; AC40 (non lié avant le lobby, refus sous 2 s pour chaque sortie, paramétré ; plafond 32) ; AC41 (jeton absent des logs, de l'état journalisé, des fichiers de match) ; AC42 (port occupé). `tests/test_api_security.py` et `tests/test_secrets.py` : balayage de l'écouteur.
- **Risks:** Non triviale (surface réseau nouvelle, en clair). `serve_forever` scrute toutes les 0,5 s : `shutdown` + `server_close` tiennent les 2 s d'AC40, à condition de ne pas attendre les fils en cours (`daemon_threads`). Le fil d'arrêt ne doit pas être celui de l'écouteur. Une sortie de lobby oubliée laisserait le port ouvert avec le secret derrière : d'où le test paramétré sur toutes les sorties. Rebind rapide après revanche sous Windows, sans `SO_REUSEADDR` : à constater en CI.

## Critères d'acceptation couverts (verbatim de la spec)

- AC37 (R7): Le lien exposé par `invite` et le texte du QR sont identiques, commencent par `http://`, et le `GET` de ce lien rend les mêmes octets que l'enregistrement depuis l'UI. `pzduel://` n'apparaît plus dans `ui/`.
- AC39 (R7): Sur l'écouteur de partage, `/`, `/api/state`, `/api/action`, un jeton faux, un jeton juste avec un autre chemin et toute méthode autre que `GET` répondent 404 ou 405, sans corps contenant le jeton, le mot de passe ou une ressource de l'UI. Le port de l'API reste lié à `127.0.0.1`.
- AC40 (R7): L'écouteur refuse la connexion dans les 2 s qui suivent la sortie du lobby, et le port de partage n'est pas lié avant l'ouverture du lobby. Le nombre de connexions simultanées reste plafonné comme sur l'API (32).
- AC41 (R7): Après un téléchargement réussi et un refus, ni les logs du service, ni l'état journalisé, ni les fichiers de match ne contiennent le jeton.
- AC42 (R7): Avec le port de partage occupé, le lobby s'ouvre, `invite` signale le partage indisponible en anglais, et l'enregistrement du `.bat` fonctionne.

## Règles

- Ne modifie QUE les fichiers listés dans `Files` de cette étape, plus les fichiers de test
  qu'elle nomme. Si une dépendance manque, tu la constates dans ta réponse finale, tu ne sors
  pas du périmètre.
- Python 3.11+ ; `service/` reste **stdlib uniquement** (aucun `import` de PyInstaller, de
  `winreg` au chargement sous Linux, ni de dépendance tierce). Tests : `/home/chpo/pz-duel/.venv/bin/python -m pytest -q`.
- Rien de spécifique à Windows ne doit être importé au chargement de `service/` sous Linux :
  les modules réservés passent par `service/platforms.py` et sont injectés dans les tests.
- Les messages **ajoutés** par cette phase sont en anglais ; l'UI existante et les documents
  de campagne restent en français.
- Aucun secret (mot de passe admin, mot de passe client, jeton d'invitation) dans un log, un
  journal, un état exposé ou un fichier de match.
- Ne touche ni à `docs/campaigns/phase1/`, ni à `spec.md`, ni à `plan.md`, ni au protocole,
  ni aux règles du duel, ni à `mods/duelcore/`.
- Toute valeur numérique vient de `service/runtime.json` : aucune en dur.

## Definition of done

- Les tests de l'étape existent, portent exactement les noms cités, et passent.
- `/home/chpo/pz-duel/.venv/bin/python -m pytest -q` est vert, et le nombre de skips reste 5 sous Linux (les skips propres à Windows
  nomment le test Windows qui les remplace).
- Un seul commit, sujet : `feat(phase2): P13 — Écouteur de partage (R7)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
