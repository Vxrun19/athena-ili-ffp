"""Pytest bootstrap — put the project root on sys.path so `import src...` works."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def pytest_configure(config):
    """Register custom markers.

    `perf` — wall-clock performance smoke tests. They assert a full
    chain finished inside a (deliberately generous) time budget. They
    are INFORMATIONAL, not release-gating: a loaded machine can blow a
    wall-clock budget with no code regression at all. Deselect for a
    release-gating run with ``pytest -m "not perf"``; the substantive
    correctness tests and the sacred-pin regressions stay gating.
    """
    config.addinivalue_line(
        "markers",
        "perf: wall-clock performance smoke test (informational, "
        "non-release-gating; deselect with -m 'not perf').",
    )
