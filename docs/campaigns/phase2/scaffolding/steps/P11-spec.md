# P11 — Génération du lanceur invité (R6)

commit: feat(phase2): P11 — Génération du lanceur invité (R6)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [service/guest_launcher.py, tests/test_guest_launcher.py, tests/test_secrets.py]
- **Description:** `build_launcher(address, port, password, mod_src) -> bytes`, fonction pure de ses entrées (D-G, D-H) : ASCII, CRLF, lignes ≤ 8000 octets, < 256 Kio. `LauncherRefused` ne cite jamais la valeur. Le `.bat` : `set "PZDUEL_SELF=%~f0"`, appel PowerShell, `pause` en cas d'échec, code de sortie propagé. Le PowerShell cherche PZ comme R1, demande le dossier si la recherche échoue, décode et vérifie chaque SHA-256 dans un dossier voisin, puis seulement remplace `%USERPROFILE%\Zomboid\mods\duelcore` avec retour arrière, et lance `ProjectZomboid64.exe -nosteam +connect <adresse>:<port B> +password <mot de passe>`. Le mod précédent reste en place en cas d'échec. Messages en anglais. Couvre AC30, AC31, AC32, AC33, AC34, AC35.
- **Dependencies:** [P6, P7]
- **Tests:** Sous Linux : AC30 (octets, CRLF, longueur, taille, déterminisme) ; AC33 (les 10 mots de passe sont refusés sans être cités ; l'hexadécimal jamais). Windows réel : AC30 (déterminisme), AC31 (`USERPROFILE` avec espace et `é`), AC32, AC33 (aucun `pwned.txt`), AC34 (entrée vide, dossier saisi, payload altéré), AC35. `tests/test_secrets.py` : mot de passe absent de tout message de refus.
- **Risks:** Non triviale (format livré à des tiers). PowerShell en mode de langage restreint ou bloqué par une stratégie : à déclarer dans `limites-connues`. Le `.bat` téléchargé porte le Mark of the Web : SmartScreen (P17). `%` et `!` dans `%~f0` : `set` avec guillemets et sans expansion retardée. `pause` sur entrée fermée rend aussitôt, ce dont AC34 dépend : à constater.

## Critères d'acceptation couverts (verbatim de la spec)

- AC30 (R6): Le `.bat` généré, pour le mod actuel, ne contient que des octets ASCII, des fins de ligne CRLF, aucune ligne de plus de 8000 octets, et pèse moins de 256 Kio. Deux générations aux mêmes entrées sont identiques octet pour octet, sous Linux comme sous `windows-latest`.
- AC31 (R6): Sur `windows-latest`, avec `USERPROFILE` contenant une espace et un `é`, et un double de `ProjectZomboid64.exe` dans un dossier `Program Files (x86)\Steam\steamapps\common\ProjectZomboid`, le `.bat` sort avec le code 0, `mod_fingerprint` du dossier installé égale `mod_fingerprint(MOD_SRC)`, et le double a reçu exactement `-nosteam`, `+connect`, `<adresse>:17271`, `+password`, `<mot de passe>`.
- AC32 (R6): Sur `windows-latest`, un dossier `mods/autremod` préexistant est inchangé après exécution, et une seconde exécution rend la même empreinte.
- AC33 (R6): Pour chacun des mots de passe `100%`, `a&b`, `a^b`, `a!b`, `a"b`, `a b`, `%PATH%`, `a|b>c`, `pässwörd` et `x&echo pwned>pwned.txt`, le générateur refuse la valeur sans la citer, ou bien, sur `windows-latest`, le double reçoit la valeur à l'identique. Dans les deux cas, aucun fichier `pwned.txt` n'existe et le mot de passe généré par défaut (hexadécimal) n'est jamais refusé.
- AC34 (R6): Sur `windows-latest`, sans PZ trouvable et sans saisie (entrée vide), le `.bat` sort avec un code non nul, le double n'est pas lancé et `mods/duelcore` reste tel qu'il était ; avec un dossier saisi valide, il réussit. Le même code non nul et le même mod intact valent pour un payload altéré.
- AC35 (R6): Sur `windows-latest`, PZ situé uniquement dans la deuxième bibliothèque d'un `libraryfolders.vdf` factice sur un autre dossier racine est trouvé sans saisie.

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
- Un seul commit, sujet : `feat(phase2): P11 — Génération du lanceur invité (R6)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
