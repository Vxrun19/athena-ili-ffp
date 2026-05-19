"""User-data path helpers for the GUI.

Centralised so the converter screen, the project-setup screen, and any
future GUI code agree on where on-disk artefacts live (vendor profiles,
saved project YAMLs, log files, …).

Layout::

    Windows:  %APPDATA%\\Athena\\ILI_FFP_Tool\\
    Linux:    ~/.config/Athena/ILI_FFP_Tool/
    macOS:    ~/Library/Application Support/Athena/ILI_FFP_Tool/

These directories are created on demand. We don't probe at import-time
so the module is cheap to load.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


_ORG = "Athena"
_APP = "ILI_FFP_Tool"


def user_data_dir() -> Path:
    """Return the OS-conventional per-user app data directory.

    Always returns a real path (creating parent directories if needed
    when callers go on to write into it). The path itself is not
    auto-created here — callers do that when they actually write.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"))
    return base / _ORG / _APP


def user_vendor_profiles_dir() -> Path:
    """Directory for user-saved :class:`VendorProfile` JSONs."""
    return user_data_dir() / "vendor_profiles"


def bundled_vendor_profiles_dir() -> Path:
    """Directory for the shipped starter profiles (read-only from the GUI)."""
    return (
        Path(__file__).resolve().parents[1]
        / "io" / "format_converter" / "profiles"
    )


def ensure_dir(path: Path) -> Path:
    """Create the directory if it doesn't exist; return the path."""
    path.mkdir(parents=True, exist_ok=True)
    return path
