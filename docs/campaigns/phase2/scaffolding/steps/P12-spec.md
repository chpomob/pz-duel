# P12 — Modèle d'invitation (R7)

commit: feat(phase2): P12 — Modèle d'invitation (R7)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [service/engine.py, service/api.py, tests/test_share.py, tests/test_api_security.py]
- **Description:** Une seule source, `_build_invite()`, remplace les deux blocs (`engine.py:1928`, `:2046`). `is_usable_address` (H7) : IPv4 ou nom d'hôte, sans `127.0.0.0/8`, `localhost` (casse et point final compris), `0.0.0.0`, `169.254.0.0/16` ; un nom dont le dernier label est numérique doit être une IPv4 complète, car `127.1` ou `2130706433` désignent la boucle. `local_ip()` n'est appelé qu'à l'ouverture du lobby et son résultat est gardé : il fait un `connect` vers `8.8.8.8`, qu'AC58 interdit pendant la saisie et la génération. L'invitation garde `ip`, `port`, `password`, `code` (tests existants) et ajoute `address`, `link`, `qr_text`, `scope`, `scope_text`, `share_available`, `needs_address`, `rematch_note`. En `mock`, ni lien ni `.bat`, et `rematch_note` est absent (AC60). Sans adresse utilisable, ni lien ni `.bat`, lobby ouvert. Portée et ports (`ports.B.game`, `ports.B.udp`, `share_port`) viennent de `self.runtime`. `POST /api/invite/address` : refus en gardant l'adresse précédente, 409 hors lobby ; l'adresse saisie reste en mémoire pour la revanche. `address` est classé dans `NOT_IDENTIFIERS`. `GET /api/invite/launcher` : `attachment`, `no-store`, 409 en `mock` et hors lobby ; `.bat` généré à la demande, jamais écrit. Un mot de passe saisi hors liste blanche donne une raison en anglais dans `invite` et aucun `.bat`. Couvre AC36, AC38, AC43, AC44, AC57, AC58, AC59, AC60.
- **Dependencies:** [P9, P10, P11]
- **Tests:** `tests/test_share.py` : AC43 (valides et refusés, l'adresse précédente reste) ; AC44 (3 cas puis saisie) ; AC57 (`runtime.json` de test aux ports modifiés) ; AC58 (sockets espionnées, 0 connexion) ; AC36 (409, aucun fichier ne contient le mot de passe, en-têtes) ; AC38 (`+connect` et `+password` égaux après `rematch`, `Password` du `.ini` de B) ; AC59 (`gen_player_password` injecté à 2 valeurs ; `start` qui ressaisit le mot de passe) ; AC60.
- **Risks:** `tests/test_match.py:335` vérifie que `host_password` n'est pas dans `invite.values()` : aucun champ ajouté ne doit le porter. L'état publié sur la boucle locale porte le lien et le jeton : il ne doit pas aller au journal (`_record_state` n'enregistre que `phase` et `server`, à garder). Les deux formes de lobby (réel, `mock`) doivent rester compatibles avec `test_modes.py:156`.

## Critères d'acceptation couverts (verbatim de la spec)

- AC36 (R6): La génération répond 409 en mode `mock` et hors du lobby. Après une génération en mode réel, aucun fichier sous `matches_dir` ni sous le dossier de données ne contient le mot de passe, et la réponse porte `Content-Disposition: attachment` et `Cache-Control: no-store`.
- AC38 (R7): Le jeton compte au moins 128 bits (32 caractères hexadécimaux ou équivalent) et 2 lobbys successifs, revanche comprise, ont des jetons différents. Cas vérifié ici : la revanche, sans nouvelle adresse saisie. Après `rematch`, le `+connect` et le `+password` du `.bat` du lobby précédent égalent ceux du `.bat` du nouveau lobby, et ce mot de passe est le `Password` écrit dans le `.ini` du serveur B. Le cas du `start` est celui d'AC59.
- AC43 (R7): Une adresse publique `82.64.1.2` ou `duel.example.net` saisie se retrouve à l'identique dans le lien, dans le QR et dans le `+connect` du `.bat`. Une adresse vide, contenant un espace, `&`, `%`, `/`, `:` ou `"`, de plus de 253 caractères, ou égale à `127.0.0.1`, `127.1.2.3`, `localhost`, `0.0.0.0` ou `169.254.1.1`, est refusée, et l'adresse précédente reste en vigueur.
- AC44 (R7): Avec `local_ip` rendant `127.0.0.1`, `0.0.0.0` ou `169.254.1.1` (3 cas) et sans adresse saisie, aucun lien ni `.bat` n'est produit, le lobby reste ouvert et l'UI demande l'adresse ; après saisie, les deux le sont.
- AC57 (R7): Avec `local_ip` rendant `192.168.1.20` et sans adresse saisie, le lien et le `.bat` portent cette adresse et `invite` annonce en anglais une portée limitée au réseau local. Avec `82.64.1.2` saisie, `invite` annonce une adresse non vérifiée et nomme les ports `17271` et `17272` en UDP et le port de partage de `runtime.json` en TCP. Les 2 valeurs de ports viennent de `runtime.json` : un `runtime.json` de test aux ports modifiés change le message.
- AC58 (R7): Une adresse saisie sans machine derrière elle (`192.0.2.1`) produit le lien et le `.bat` sans attente, et 0 connexion n'est tentée vers cette adresse ni vers un tiers pour la valider (sockets espionnées pendant la saisie et la génération).
- AC59 (R7): Cas vérifié ici : le `start` neuf, qu'AC38 ne couvre pas. Après un lobby réel, un `start` sans mot de passe saisi, `gen_player_password` injecté rendant 2 valeurs distinctes, écrit dans le `.ini` du serveur B un `Password` différent du `+password` du `.bat` précédent : ce `.bat` est périmé. Un `start` qui saisit le mot de passe du lobby précédent garde ce `Password`.
- AC60 (R7): Dans un lobby réel, `invite` annonce en anglais qu'un `.bat` déjà envoyé reste valide pour une revanche et qu'un nouveau duel peut le périmer, et `ui/app.js` affiche ce texte dans l'écran d'invitation. Hors lobby réel, `invite` ne porte pas cette annonce.

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
- Un seul commit, sujet : `feat(phase2): P12 — Modèle d'invitation (R7)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
