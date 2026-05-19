#!/usr/bin/env python3
"""Launch the Athena ILI FFP Tool desktop GUI.

This is the GUI counterpart to ``bin/run_pipeline.py``. It just spins up
the PyQt6 QApplication; all the real work happens inside ``src/gui/``.

    python bin/run_gui.py

Exit code is forwarded from Qt's event loop (0 on a clean shutdown).
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

# Reconfigure stdout/stderr so any print() that escapes the GUI (e.g.
# from a worker exception's traceback) survives Windows' default cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, io.UnsupportedOperation):  # pragma: no cover
    pass

# Make 'src.gui' importable when this script is run directly.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.gui.main_window import launch                          # noqa: E402


if __name__ == "__main__":
    sys.exit(launch(sys.argv))
