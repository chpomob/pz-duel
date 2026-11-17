"""Attente bornée d'un état produit ailleurs (un fil, une passe du moteur) : on attend une
condition, jamais une durée.

Un test qui lit un tel état juste après l'avoir provoqué photographie un état en vol. Ici, la
condition est relue jusqu'à tenir ; à l'expiration, l'échec nomme ce qui était attendu et ce qui
a été vu à la place — une attente ne doit jamais masquer un état qui n'arrive pas.

P9F29 : la patience des tests se décide ici, et nulle part ailleurs. `WAIT_S` est lu dans
`PZDUEL_TEST_WAIT_S` (la CI pose 60 s : un runner partagé est trois à cinq fois plus lent sur les
opérations de processus) ; sans la variable, il vaut le défaut de la machine de développement.
Un budget en dur ne reste dans `tests/` que s'il mesure une durée voulue (une fenêtre de
contrat, une borne qu'on affirme), jamais une patience machine.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable, Mapping
from typing import Any

POLL_S = 0.01
WAIT_ENV = "PZDUEL_TEST_WAIT_S"
DEFAULT_WAIT_S = 10.0


def wait_budget_s(environ: Mapping[str, str] | None = None) -> float:
    """Le budget d'attente des tests, en secondes : `PZDUEL_TEST_WAIT_S`, sinon le défaut.

    Une valeur illisible, nulle, négative ou infinie est refusée : un budget mal écrit ne doit
    pas devenir en silence le défaut, ni une attente sans fin.
    """
    raw = (os.environ if environ is None else environ).get(WAIT_ENV, "").strip()
    if not raw:
        return DEFAULT_WAIT_S
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{WAIT_ENV}={raw!r} : un nombre de secondes est attendu") from None
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"{WAIT_ENV}={raw!r} : un nombre de secondes fini et positif est attendu")
    return value


WAIT_S = wait_budget_s()


def wait_until(
    condition: Callable[[], Any],
    timeout_s: float | None = None,
    *,
    what: str,
    observe: Callable[[], Any] | None = None,
    poll_s: float = POLL_S,
) -> Any:
    """Rend la valeur de `condition()` dès qu'elle est vraie ; sinon échoue en la nommant.

    `what` dit le fait attendu, en clair : il est obligatoire, une attente sans nom ne se
    diagnostique pas. `observe` rend l'état à citer dans l'échec ; sans lui, c'est la dernière
    valeur rendue par la condition. La condition est relue une dernière fois au délai : un état
    atteint pendant la dernière pause n'échoue pas. Une exception de la condition n'est pas
    avalée : elle dit une panne, pas un état en retard. Sans `timeout_s`, le budget est `WAIT_S`.
    """
    if timeout_s is None:
        timeout_s = WAIT_S
    if not what.strip():
        raise ValueError("wait_until : `what` doit nommer la condition attendue")
    deadline = time.monotonic() + timeout_s
    while True:
        expired = time.monotonic() >= deadline
        value = condition()
        if value:
            return value
        if expired:
            break
        time.sleep(max(0.0, min(poll_s, deadline - time.monotonic())))
    observed = observe() if observe is not None else value
    raise AssertionError(f"attendu en {timeout_s:g} s : {what} ; observé : {observed!r}")
