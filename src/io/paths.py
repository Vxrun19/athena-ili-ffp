"""Output-path resolution for the FFP tool.

Why this lives here:

PyInstaller-bundled installs land in ``C:\\Program Files\\Athena ILI
FFP Tool\\`` (per the installer.iss ``DefaultDirName={autopf}\\...``).
That directory is ACL-protected on Windows — non-admin users can't
``mkdir`` inside it.

v0.2.1 defaulted the output directory to ``Path("./output")``, which
resolves against the process CWD. When the GUI is launched from a
Start-Menu shortcut, CWD == the install dir, so the worker's
``out_dir.mkdir(parents=True, exist_ok=True)`` call raised
``PermissionError [WinError 5]`` ~3 stages into the pipeline.

``resolve_output_dir`` always returns a path the current user can
actually write to: either the YAML's own parent dir (so outputs
co-locate with the project — the natural Windows-user mental model)
or a stable spot under the user's Documents folder.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path


# Windows-illegal filename chars (per MSDN: <>:"/\|?*). On POSIX
# only `/` is technically reserved, but we strip the full set so
# directory names look identical across platforms.
_ILLEGAL_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def sanitize_for_filesystem(name: str) -> str:
    """Replace filesystem-illegal chars in `name` with underscores.

    Collapses whitespace runs to a single underscore too, so
    ``"Kandla   Run 2"`` becomes ``"Kandla_Run_2"`` — partly cosmetic,
    partly defensive against trailing-space paste-ins that produce
    a directory whose name Explorer can't display cleanly.

    Empty / whitespace-only input falls back to ``"ffp_project"`` so
    we never call ``mkdir`` on the parent directory itself.
    """
    cleaned = _ILLEGAL_NAME_CHARS.sub("_", name).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or "ffp_project"


def _is_writable(directory: Path) -> bool:
    """Test write-access by creating + deleting a tmp file in `directory`.

    ``os.access(p, os.W_OK)`` is unreliable on Windows: it reflects
    the legacy DOS read-only bit, not the NTFS ACL. ``C:\\Program
    Files\\`` reports writable for non-admin users even though
    ``open()`` immediately fails. The only reliable probe is to
    actually attempt a write.

    Returns False if the directory doesn't exist; callers fall
    through to the Documents path.
    """
    if not directory.exists() or not directory.is_dir():
        return False
    probe = directory / f".athena_writable_probe_{uuid.uuid4().hex}.tmp"
    try:
        probe.touch()
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        # The dir IS writable — we just touched it. Leaving an
        # orphan probe is a better outcome than misreporting the
        # location as unwritable and steering the user elsewhere.
        pass
    return True


def resolve_output_dir(
    project_yaml_path: Path | None,
    project_name: str,
) -> Path:
    """Resolve the output directory in a user-writable location.

    Priority:

      1. If ``project_yaml_path`` is given AND its parent dir is
         writable: ``<yaml_parent>/<yaml_stem>_output/``.
      2. Else: ``~/Documents/Athena ILI FFP/<sanitized project_name>/``.

    Always ``mkdir(parents=True, exist_ok=True)`` the chosen path.
    Returns an absolute :class:`pathlib.Path`.
    """
    # ---- Priority 1: alongside the YAML ----------------------------
    if project_yaml_path is not None:
        yaml_path = Path(project_yaml_path)
        yaml_parent = yaml_path.parent
        if _is_writable(yaml_parent):
            out = yaml_parent / f"{yaml_path.stem}_output"
            out.mkdir(parents=True, exist_ok=True)
            return out.resolve()

    # ---- Priority 2: Documents fallback ----------------------------
    # ``Path.home()`` resolves ``%USERPROFILE%`` on Windows (e.g.
    # ``C:\\Users\\varun``). The "Documents" subdirectory exists
    # out-of-the-box on every Windows user profile; we don't need
    # SHGetKnownFolderPath for the standard case. Users who
    # redirected Documents via Group Policy will see the folder at
    # the redirected location — that's the correct behavior.
    base = Path.home() / "Documents" / "Athena ILI FFP"
    out = base / sanitize_for_filesystem(project_name)
    out.mkdir(parents=True, exist_ok=True)
    return out.resolve()


# ---------------------------------------------------------------------------
# YAML-stored path resolution (v0.2.3)
# ---------------------------------------------------------------------------
#
# v0.2.0–v0.2.2 stored absolute paths in project YAMLs for the Run-1 and
# Run-2 ILI files. That breaks the moment a YAML moves between machines
# (dev → customer, operator → office, or even just a different folder).
# v0.2.3 makes paths YAML-relative: relative entries resolve against the
# YAML's own parent directory; absolute entries pass through unchanged
# (backward compat).
#
# The save-side counterpart (`relativize_if_possible`) renders an
# absolute on-disk path back as a relative string when both the YAML
# and the file share a common ancestor — so hand-curated YAML+xlsx
# folders stay portable.


def resolve_relative_to_yaml(
    yaml_path: Path | str | None,
    value: str | Path | None,
) -> Path | None:
    """Resolve a YAML-stored file path.

    Semantics:

      * ``None`` / empty / whitespace-only ``value`` -> ``None``.
      * Absolute ``value`` -> returned verbatim (no resolve, no exist
        check). Backward-compat for v0.2.0–v0.2.2 YAMLs.
      * Relative ``value`` AND ``yaml_path`` provided -> resolved
        against ``Path(yaml_path).parent``.
      * Relative ``value`` AND ``yaml_path`` is ``None`` -> resolved
        against the current working directory (best-effort fallback —
        callers normally have a YAML location).

    Existence is **not** checked here. Callers raise their own
    user-facing errors with whatever context they have (CLI vs GUI
    show different things).

    Why Path.is_absolute() and not heuristics: on Windows
    ``"C:/foo/bar"`` and ``"C:\\foo\\bar"`` and ``"//server/share/x"``
    are all absolute; ``"foo\\bar.xlsx"``, ``"./foo/bar.xlsx"``, and
    ``"../foo.xlsx"`` are all relative. ``Path.is_absolute()`` gets
    this right on both Windows and POSIX without our intervention.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        value = stripped

    p = Path(value)
    if p.is_absolute():
        return p

    if yaml_path is None:
        return (Path.cwd() / p).resolve()

    return (Path(yaml_path).parent / p).resolve()


def relativize_if_possible(
    yaml_path: Path | str,
    file_path: Path | str,
    *,
    max_levels_up: int = 3,
) -> str:
    """Render `file_path` for storage in `yaml_path`'s YAML body.

    Returns a **relative** path string iff all of:

      * Both ``yaml_path`` and ``file_path`` exist on the same drive
        (relevant on Windows; ``os.path.relpath`` raises ``ValueError``
        across drives anyway).
      * The relative result needs ``<=`` ``max_levels_up`` ``..``
        segments (avoids storing weird ``../../../../../foo`` strings
        that aren't really "portable").

    Otherwise returns an **absolute** path string.

    Output always uses forward slashes — YAML stores them literally and
    Windows handles either separator, so this gives one consistent
    portable form. (Same convention as ``examples/kandla_project.yaml``
    which used ``C:/Users/...`` even on Windows.)

    The caller is expected to write whatever string we return into the
    YAML verbatim. Empty input -> empty string (the load path treats
    empty as "no file specified" via ``resolve_relative_to_yaml``).
    """
    if file_path in (None, ""):
        return ""

    yaml_abs = Path(yaml_path).resolve()
    file_abs = Path(file_path).resolve()

    # Different drives on Windows -> can't make relative (os.path.relpath
    # raises ValueError on this case). Bail to absolute.
    if yaml_abs.drive != file_abs.drive:
        return str(file_abs).replace("\\", "/")

    try:
        rel = os.path.relpath(file_abs, yaml_abs.parent)
    except ValueError:
        # Defensive — relpath also raises if either path is malformed
        # in some unusual ways on Windows (UNC vs drive-letter mixes).
        return str(file_abs).replace("\\", "/")

    # Count the leading ``..`` segments. If we'd need to go up more
    # than ``max_levels_up`` levels, the file isn't really co-located
    # with the YAML and we shouldn't pretend otherwise.
    parts = rel.replace("\\", "/").split("/")
    n_up = 0
    for part in parts:
        if part == "..":
            n_up += 1
        else:
            break
    if n_up > max_levels_up:
        return str(file_abs).replace("\\", "/")

    return rel.replace("\\", "/")
