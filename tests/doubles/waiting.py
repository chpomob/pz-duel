"""Attente bornée d'un état produit ailleurs (un fil, une passe du moteur) : on attend une
condition, jamais une durée.

Un test qui lit un tel état juste après l'avoir provoqué photographie un état en vol. Ici, la
condition est relue jusqu'à tenir ; à l'expiration, l'échec nomme ce qui était attendu et ce qui
a été vu à la place — une attente ne doit jamais masquer un état qui n'arrive pas.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

POLL_S = 0.01


def wait_until(
    condition: Callable[[], Any],
    timeout_s: float = 2.0,
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
    avalée : elle dit une panne, pas un état en retard.
    """
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
