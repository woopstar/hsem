"""One-time scipy availability probe for the MILP optimiser.

Extracted from ``milp_optimizer.py`` to satisfy the repository's 30 KB /
1000-line file limit. Pure move: the result is still cached once at import
time, and ``is_scipy_available`` is re-exported from ``milp_optimizer``.
"""

from __future__ import annotations


def is_scipy_available() -> bool:
    """Return ``True`` if scipy is importable in the current environment.

    The import result is cached at module level so that the blocking
    ``import scipy.optimize`` happens exactly once at import time rather
    than on every planner run inside the Home Assistant event loop.
    """
    return _SCIPY_AVAILABLE


# --- Module-level cache: computed once at import time --------------------
def _check_scipy() -> bool:
    """Check whether scipy is importable.  Called once at module load."""
    try:
        import scipy.optimize  # noqa: F401

        return True
    except ImportError:
        return False


_SCIPY_AVAILABLE: bool = _check_scipy()
