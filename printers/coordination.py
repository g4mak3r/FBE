from __future__ import annotations

import threading
from functools import wraps
from typing import Any, Callable, TypeVar, cast


_PRINT_LOCK = threading.RLock()
F = TypeVar("F", bound=Callable[..., Any])


def serialized_print_job(function: F) -> F:
    """Keep shared CSV files, virtual PDF printers and spool order coherent.

    FastAPI executes synchronous handlers concurrently. Most legacy print paths
    share one BarTender CSV, one Bullzip ``runonce.ini`` and the same physical
    printers, so overlapping calls can print the wrong row or steal another
    job's PDF. A re-entrant process-local lock preserves complete paired jobs
    while allowing decorated print functions to call one another.
    """

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _PRINT_LOCK:
            return function(*args, **kwargs)

    return cast(F, wrapped)
