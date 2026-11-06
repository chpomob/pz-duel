# P14 — Interface (R5, R7)

commit: feat(phase2): P14 — Interface (R5, R7)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [ui/app.js, ui/index.html, ui/style.css, tests/test_setup_flow.py, tests/test_share.py]
- **Description:** Écran de premier lancement : état de PZ, du mod, de la RAM et des ports, une action par défaut (`install_mod`, `choose_folder`, `retry`). `canLaunch(setup)` désactive « Launch duel » tant que `ok` est faux. Bouton « Quit » et écran de fin. Écran d'invitation : lien, QR construit depuis `invite.qr_text`, enregistrement du `.bat`, champ d'adresse publique, portée, ports à ouvrir, `rematch_note`, partage indisponible, demande d'adresse. `pzduel://` disparaît (`ui/app.js:346`, `:379`). Le bloc `ip:port` et mot de passe en clair reste pour le repli manuel de l'invité Linux (H2). Textes ajoutés en anglais. Couvre AC26 (UI), AC37 (absence de `pzduel://`), AC60 (UI), R5, R7.
- **Dependencies:** [P10, P13]
- **Tests:** Lecture statique de `ui/app.js`, sur le modèle de `tests/test_match.py:2488`, sans dépendance à `node` : AC26 (`canLaunch` commande l'attribut `disabled`), AC37, AC60. Vérification à la main dans un navigateur en mode réel double : les écrans s'affichent, « Quit » arrête le service.
- **Risks:** Le contrôle statique prouve la présence du lien logique, pas le rendu : la vérification à la main doit être rapportée telle quelle. Le QR a une capacité bornée : lien long avec un nom d'hôte de 253 caractères, à tester à cette taille.

## Critères d'acceptation couverts (verbatim de la spec)

- AC26 (R5): Un `start` refusé par le préflight rend le même défaut que la route d'état, et un test de l'UI vérifie que le bouton « Launch duel » reste désactivé tant que `ok` est faux.
- AC37 (R7): Le lien exposé par `invite` et le texte du QR sont identiques, commencent par `http://`, et le `GET` de ce lien rend les mêmes octets que l'enregistrement depuis l'UI. `pzduel://` n'apparaît plus dans `ui/`.
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
- Un seul commit, sujet : `feat(phase2): P14 — Interface (R5, R7)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
