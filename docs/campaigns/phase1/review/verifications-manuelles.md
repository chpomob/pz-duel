# Vérifications manuelles (hors périmètre de lecture des modèles)

Ces constats sont consignés ici parce qu'ils ont été tranchés par une expérience, pas par une
relecture. Le vérificateur, restreint à `cat`, ne pouvait ni exécuter les tests ni lire certains
fichiers : ses PARTIAL correspondants ne sont pas des défauts du code.

## A8 — mot de passe joueur : RESOLVED

`service/servers.py:181` : `gen_player_password()` rend `secrets.token_hex(PLAYER_PASSWORD_BYTES)`.
Générateur cryptographique de la bibliothèque standard, sortie hexadécimale — ni prévisible, ni
problématique à journaliser. Le PARTIAL de la deuxième vérification venait de ce que
`service/servers.py` ne figurait pas dans les chemins autorisés.

## MINI_LUA — le balayage de surface a des dents : RESOLVED

Le vérificateur doutait du mécanisme (`tests/test_mini_lua.py:68`) sans pouvoir l'exécuter.
Mutation faite sur `tests/doubles/mini_lua.py` : suppression de la ligne 521 (`remove=_remove,`),
puis `pytest -q tests/test_mini_lua.py`.

Résultat : **10 failed, 46 passed**, dont `test_double_provides_everything_the_mod_calls`.
Fichier restauré ensuite (`git status` propre).

Le test échoue donc bien quand le double perd une fonction que le mod appelle : la classe est
fermée par un mécanisme, pas par une déclaration d'intention.

## A6 — résidu réel, devenu NEW major

Le correctif F6 ferme bien les identifiants et l'encodage, mais la famille « entrée textuelle qui
fait lever la réponse » se referme sur un autre membre : `Content-Length: 9223372036854775808`
passe `int`, puis `read` lève `OverflowError` avant le contrôle `Host`. Traité en F7.

## F15 — le test croise-t-il vraiment table, hash et effets ? RESOLVED

Le vérificateur jugeait A2 PARTIAL : à ses yeux, `tests/test_match.py:2353`
(`test_table_hash_and_applied_effects_agree`) passerait si une règle était journalisée comme
déclenchée sans qu'aucun effet ne soit émis. Il ne pouvait pas exécuter la mutation.

Mutation faite dans `service/rules.py::_apply_rule` : `rule_fired` journalisé, puis la boucle
`_enqueue` remplacée par `pass`. Résultat :

    FAILED test_table_hash_and_applied_effects_agree[duel_rapide-failed_sides0..3]
    4 failed, 146 deselected

Les quatre paramétrages échouent : une règle déclenchée sans effet est bien attrapée. Le code de
F15 dérive par ailleurs hash, scellement, publication et cœur de `_played_table`
(`engine.py:481,1300,1331,1434`), et l'UI lit cette publication (`ui/app.js:24,235`).
Le PARTIAL était donc un artefact de lecture, pas un défaut. Fichier restauré ensuite.
