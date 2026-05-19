"""PyQt6 desktop GUI for the Athena ILI FFP Tool.

The GUI is a thin wrapper around the same pipeline that
``bin/run_pipeline.py`` exercises. Other modules in ``src/`` do not import
this package, so the rest of the codebase remains importable on machines
without PyQt6.

Entry point: ``from src.gui.main_window import launch``.
"""
from __future__ import annotations

from .main_window import launch

__all__ = ["launch"]
