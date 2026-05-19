"""Tests for src/io/paths.py — output-directory resolver.

The resolver fixes the v0.2.1 ``PermissionError [WinError 5]`` crash
that happened when the GUI was launched from a Start-Menu shortcut
into a ``C:\\Program Files\\...`` install: the previous default of
``Path("./output")`` resolved against CWD == install dir, and
``mkdir`` failed against the NTFS ACL.

These tests exercise the two-priority resolution (alongside-YAML
vs Documents-fallback), the filesystem-name sanitization, and the
defensive guarantees the rest of the codebase relies on (absolute
path returned, directory actually created).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.io.paths import (
    _is_writable,
    relativize_if_possible,
    resolve_output_dir,
    resolve_relative_to_yaml,
    sanitize_for_filesystem,
)


# ---------------------------------------------------------------------------
# sanitize_for_filesystem
# ---------------------------------------------------------------------------

class TestSanitizeForFilesystem:
    """Project names land in directory segments — guard the FS-illegal chars."""

    def test_replaces_every_illegal_windows_char(self):
        # Full Windows-illegal set: < > : " / \ | ? * — 9 chars total.
        raw = 'Kandla<>:"/\\|?*Run'
        out = sanitize_for_filesystem(raw)
        assert out == "Kandla_________Run"  # 9 underscores between stems
        assert "<" not in out and ">" not in out and ":" not in out
        assert "/" not in out and "\\" not in out and "|" not in out
        assert '"' not in out and "?" not in out and "*" not in out

    def test_collapses_whitespace_runs(self):
        assert sanitize_for_filesystem("Kandla   Run   2") == "Kandla_Run_2"

    def test_strips_leading_trailing_whitespace(self):
        assert sanitize_for_filesystem("  Kandla  ") == "Kandla"

    def test_empty_falls_back_to_default_stem(self):
        # Empty input must not produce "" (would make mkdir target the parent).
        assert sanitize_for_filesystem("") == "ffp_project"
        assert sanitize_for_filesystem("   ") == "ffp_project"

    def test_preserves_normal_chars(self):
        assert sanitize_for_filesystem("Kandla_v2-final.test") == "Kandla_v2-final.test"


# ---------------------------------------------------------------------------
# _is_writable
# ---------------------------------------------------------------------------

class TestIsWritable:
    """Tested in isolation — the read-only-NTFS case is hard to fake
    portably (Windows ACLs ≠ POSIX chmod), so we test what _is_writable
    can reliably distinguish: existent+writable, non-existent, file-not-dir."""

    def test_returns_true_for_writable_tmp_path(self, tmp_path):
        assert _is_writable(tmp_path) is True

    def test_leaves_no_probe_file_behind_on_success(self, tmp_path):
        _is_writable(tmp_path)
        # We touched-then-unlinked a probe file; tmp_path should be empty.
        assert list(tmp_path.iterdir()) == []

    def test_returns_false_for_nonexistent_directory(self, tmp_path):
        assert _is_writable(tmp_path / "does_not_exist") is False

    def test_returns_false_for_file_not_directory(self, tmp_path):
        # _is_writable expects a directory; a regular file should be False.
        f = tmp_path / "actually_a_file.txt"
        f.write_text("hi")
        assert _is_writable(f) is False


# ---------------------------------------------------------------------------
# resolve_output_dir — Priority 1: alongside YAML
# ---------------------------------------------------------------------------

class TestResolveOutputDirAlongsideYaml:
    def test_writable_yaml_parent_yields_yaml_sibling(self, tmp_path):
        yaml_path = tmp_path / "kandla_project.yaml"
        yaml_path.touch()
        out = resolve_output_dir(yaml_path, "Kandla Test")
        # Alongside the YAML, named "<stem>_output".
        assert out == (tmp_path / "kandla_project_output").resolve()
        assert out.exists()
        assert out.is_dir()

    def test_creates_directory_even_if_yaml_itself_doesnt_exist(self, tmp_path):
        # YAML path can be hypothetical (e.g. the GUI builds an
        # AnalysisJob before writing the YAML to disk in some flows).
        # As long as the parent is writable, that's enough.
        yaml_path = tmp_path / "project.yaml"  # not touched — won't exist
        out = resolve_output_dir(yaml_path, "Anything")
        assert out == (tmp_path / "project_output").resolve()
        assert out.exists()

    def test_returns_absolute_path_even_when_yaml_given_relative(
        self, tmp_path, monkeypatch,
    ):
        # CWD into tmp_path so a relative yaml path resolves there.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "rel.yaml").touch()
        out = resolve_output_dir(Path("rel.yaml"), "test")
        assert out.is_absolute()
        assert out == (tmp_path / "rel_output").resolve()


# ---------------------------------------------------------------------------
# resolve_output_dir — Priority 2: Documents fallback
# ---------------------------------------------------------------------------

class TestResolveOutputDirDocumentsFallback:
    """Steer Path.home() to a fake home dir via USERPROFILE so tests
    never pollute the real ~/Documents."""

    @pytest.fixture
    def fake_home(self, tmp_path, monkeypatch):
        h = tmp_path / "fake_home"
        h.mkdir()
        # Path.home() consults USERPROFILE on Windows and HOME on POSIX —
        # set both so this test passes on whichever runner picks it up.
        monkeypatch.setenv("USERPROFILE", str(h))
        monkeypatch.setenv("HOME", str(h))
        return h

    def test_yaml_path_none_routes_to_documents(self, fake_home):
        out = resolve_output_dir(None, "Kandla")
        expected = (fake_home / "Documents" / "Athena ILI FFP" / "Kandla").resolve()
        assert out == expected
        assert out.exists()

    def test_unwritable_yaml_parent_falls_through_to_documents(
        self, tmp_path, fake_home, monkeypatch,
    ):
        # Force every writability probe to fail — simulates an
        # install-dir YAML on a real Program Files deployment.
        monkeypatch.setattr("src.io.paths._is_writable", lambda d: False)
        yaml_path = tmp_path / "trapped.yaml"
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.touch()
        out = resolve_output_dir(yaml_path, "MyProject")
        expected = (
            fake_home / "Documents" / "Athena ILI FFP" / "MyProject"
        ).resolve()
        assert out == expected
        assert out.exists()

    def test_sanitizes_project_name_in_documents_path(self, fake_home):
        out = resolve_output_dir(None, "Has/Bad:Chars?")
        # Each illegal char becomes "_", trailing/leading collapse.
        expected = (
            fake_home / "Documents" / "Athena ILI FFP" / "Has_Bad_Chars_"
        ).resolve()
        assert out == expected
        assert out.exists()

    def test_creates_intermediate_documents_dir(self, fake_home):
        # fake_home/Documents/ doesn't exist yet — verify parents=True.
        assert not (fake_home / "Documents").exists()
        out = resolve_output_dir(None, "P1")
        assert (fake_home / "Documents" / "Athena ILI FFP").is_dir()
        assert out.exists()

    def test_existing_documents_dir_does_not_error(self, fake_home):
        # exist_ok=True semantics — re-resolving the same project
        # twice in a row must succeed.
        out1 = resolve_output_dir(None, "Repeat")
        out2 = resolve_output_dir(None, "Repeat")
        assert out1 == out2
        assert out1.exists()


# ---------------------------------------------------------------------------
# resolve_output_dir — invariants the rest of the codebase relies on
# ---------------------------------------------------------------------------

class TestResolveOutputDirInvariants:
    @pytest.fixture
    def fake_home(self, tmp_path, monkeypatch):
        h = tmp_path / "fake_home"
        h.mkdir()
        monkeypatch.setenv("USERPROFILE", str(h))
        monkeypatch.setenv("HOME", str(h))
        return h

    def test_always_returns_absolute_path_yaml_branch(self, tmp_path):
        yaml = tmp_path / "p.yaml"
        yaml.touch()
        assert resolve_output_dir(yaml, "p").is_absolute()

    def test_always_returns_absolute_path_documents_branch(self, fake_home):
        assert resolve_output_dir(None, "p").is_absolute()

    def test_directory_exists_after_resolve_yaml_branch(self, tmp_path):
        yaml = tmp_path / "p.yaml"
        yaml.touch()
        out = resolve_output_dir(yaml, "p")
        assert out.is_dir()

    def test_directory_exists_after_resolve_documents_branch(self, fake_home):
        out = resolve_output_dir(None, "p")
        assert out.is_dir()


# ---------------------------------------------------------------------------
# resolve_relative_to_yaml — v0.2.3 portable-YAML path resolution
# ---------------------------------------------------------------------------
#
# v0.2.0–v0.2.2 stored absolute paths in project YAMLs; moving a YAML
# between machines broke. v0.2.3 makes relative paths resolve against
# the YAML's parent dir while keeping absolute paths backward-compatible.

class TestResolveRelativeToYaml:
    """Load-side: YAML-stored file_path -> absolute Path on disk."""

    def test_none_value_returns_none(self):
        assert resolve_relative_to_yaml(Path("/x/p.yaml"), None) is None

    def test_empty_string_returns_none(self):
        assert resolve_relative_to_yaml(Path("/x/p.yaml"), "") is None

    def test_whitespace_only_returns_none(self):
        assert resolve_relative_to_yaml(Path("/x/p.yaml"), "   \t ") is None

    def test_absolute_path_passes_through_verbatim(self, tmp_path):
        # Backward compat: v0.2.0–v0.2.2 YAMLs with absolute paths
        # must continue to load identically.
        target = tmp_path / "absolute" / "run1.xlsx"
        target.parent.mkdir(parents=True)
        target.touch()
        result = resolve_relative_to_yaml(
            tmp_path / "elsewhere" / "project.yaml", str(target),
        )
        assert result == target

    def test_relative_path_resolves_against_yaml_parent(self, tmp_path):
        yaml_path = tmp_path / "project.yaml"
        run1 = tmp_path / "run1.xlsx"
        run1.touch()
        result = resolve_relative_to_yaml(yaml_path, "run1.xlsx")
        assert result == run1.resolve()
        assert result.exists()

    def test_relative_path_with_subdirectory(self, tmp_path):
        yaml_path = tmp_path / "project.yaml"
        (tmp_path / "data").mkdir()
        run1 = tmp_path / "data" / "run1.xlsx"
        run1.touch()
        result = resolve_relative_to_yaml(yaml_path, "data/run1.xlsx")
        assert result == run1.resolve()

    def test_relative_path_with_parent_traversal(self, tmp_path):
        # YAML in subdir, file one level up — ../foo.xlsx is valid.
        (tmp_path / "config").mkdir()
        yaml_path = tmp_path / "config" / "project.yaml"
        run1 = tmp_path / "run1.xlsx"
        run1.touch()
        result = resolve_relative_to_yaml(yaml_path, "../run1.xlsx")
        assert result == run1.resolve()

    def test_relative_path_with_windows_backslashes(self, tmp_path):
        yaml_path = tmp_path / "project.yaml"
        (tmp_path / "data").mkdir()
        run1 = tmp_path / "data" / "run1.xlsx"
        run1.touch()
        # Path("data\\run1.xlsx") is interpreted correctly on Windows;
        # on POSIX it's a single filename with a backslash. The contract
        # is: pathlib handles the separator that's native to the platform.
        import os
        sep = os.sep
        rel = "data" + sep + "run1.xlsx"
        result = resolve_relative_to_yaml(yaml_path, rel)
        assert result == run1.resolve()

    def test_relative_with_no_yaml_path_uses_cwd(self, tmp_path, monkeypatch):
        # Best-effort fallback when caller has no YAML context.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "rel.xlsx").touch()
        result = resolve_relative_to_yaml(None, "rel.xlsx")
        assert result == (tmp_path / "rel.xlsx").resolve()

    def test_nonexistent_relative_resolves_without_raising(self, tmp_path):
        # Existence is the caller's job — resolver returns the path it
        # WOULD have, so the caller can render "Resolved: ..." in errors.
        yaml_path = tmp_path / "project.yaml"
        result = resolve_relative_to_yaml(yaml_path, "missing.xlsx")
        assert result == (tmp_path / "missing.xlsx").resolve()
        assert not result.exists()


# ---------------------------------------------------------------------------
# relativize_if_possible — v0.2.3 portable-YAML path save-side
# ---------------------------------------------------------------------------

class TestRelativizeIfPossible:
    """Save-side: absolute on-disk path -> YAML-friendly string."""

    def test_empty_input_returns_empty(self):
        assert relativize_if_possible(Path("/x/p.yaml"), "") == ""
        assert relativize_if_possible(Path("/x/p.yaml"), None) == ""

    def test_same_directory_becomes_relative_filename(self, tmp_path):
        yaml_path = tmp_path / "project.yaml"
        run1 = tmp_path / "run1.xlsx"
        run1.touch()
        result = relativize_if_possible(yaml_path, run1)
        assert result == "run1.xlsx"

    def test_subdirectory_becomes_relative_with_forward_slash(self, tmp_path):
        yaml_path = tmp_path / "project.yaml"
        (tmp_path / "data").mkdir()
        run1 = tmp_path / "data" / "run1.xlsx"
        run1.touch()
        result = relativize_if_possible(yaml_path, run1)
        # Forward slashes on output regardless of platform.
        assert result == "data/run1.xlsx"

    def test_parent_traversal_within_max_levels(self, tmp_path):
        (tmp_path / "config").mkdir()
        yaml_path = tmp_path / "config" / "project.yaml"
        run1 = tmp_path / "run1.xlsx"
        run1.touch()
        result = relativize_if_possible(yaml_path, run1)
        assert result == "../run1.xlsx"

    def test_too_many_parent_levels_stays_absolute(self, tmp_path):
        # 4 levels up exceeds default max_levels_up=3 -> stay absolute.
        deep = tmp_path / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        yaml_path = deep / "project.yaml"
        run1 = tmp_path / "run1.xlsx"
        run1.touch()
        result = relativize_if_possible(yaml_path, run1)
        # 5 levels up exceeds 3 -> absolute kept.
        assert ".." not in result.split("/")[0:1]  # not starting with ..
        assert Path(result).is_absolute()

    def test_round_trip_load_then_save_stays_relative(self, tmp_path):
        # Critical scenario: load YAML with relative path -> save in
        # the same dir -> should stay relative (the whole point of v0.2.3).
        yaml_path = tmp_path / "project.yaml"
        run1 = tmp_path / "run1.xlsx"
        run1.touch()

        # Step 1: load the relative entry through the resolver.
        loaded = resolve_relative_to_yaml(yaml_path, "run1.xlsx")
        # Step 2: relativize back for save in the same location.
        out = relativize_if_possible(yaml_path, loaded)
        assert out == "run1.xlsx"

    def test_round_trip_save_to_different_drive_stays_absolute(
        self, tmp_path, monkeypatch,
    ):
        # When the YAML and the file are on different drives, must
        # stay absolute. We can't easily fake two drives in a tmp_path
        # test, but the helper's guard is `yaml_abs.drive !=
        # file_abs.drive`, so simulate by monkeypatching Path.resolve
        # to return synthetic drive letters. Simpler: just check the
        # ValueError fall-through directly via os.path.relpath:
        import os
        # On Windows, mocking different drives is possible. On POSIX,
        # there are no drives -> always same drive -> we test only
        # the "won't crash on missing target" path here.
        if os.name != "nt":
            pytest.skip("drive-letter test only meaningful on Windows")
        # Real cross-drive test on Windows:
        yaml_path = tmp_path / "project.yaml"
        # Synthesise a different-drive absolute path. If the test
        # machine doesn't have a D:\, os.path.relpath will raise
        # ValueError which our helper catches and returns absolute.
        cross_drive = Path("D:/some/other/run1.xlsx")
        result = relativize_if_possible(yaml_path, cross_drive)
        # Either becomes absolute (most likely) or relative if D: exists
        # AND happens to be the same drive (very unlikely). Either way:
        assert Path(result).is_absolute() or result.startswith("..")

    def test_no_existence_check_on_either_path(self, tmp_path):
        # Both yaml_path and file_path can point to not-yet-created
        # locations — the GUI calls this BEFORE writing the YAML.
        yaml_path = tmp_path / "future.yaml"   # doesn't exist yet
        run1 = tmp_path / "future_run1.xlsx"   # doesn't exist yet
        result = relativize_if_possible(yaml_path, run1)
        assert result == "future_run1.xlsx"


# ---------------------------------------------------------------------------
# Integration: YAML round-trip through Project.from_yaml
# ---------------------------------------------------------------------------

class TestYamlRoundTrip:
    """End-to-end: write a portable YAML, load it through Project.from_yaml,
    confirm the consumer-side resolve_relative_to_yaml lands at the right
    absolute path."""

    @pytest.fixture
    def portable_pack(self, tmp_path):
        """Create a yaml + two run files in the same dir."""
        import yaml as pyyaml
        (tmp_path / "run1.xlsx").touch()
        (tmp_path / "run2.xlsx").touch()
        yaml_path = tmp_path / "portable_project.yaml"
        data = {
            "project": {"project_name": "PortableTest"},
            "pipeline": {
                "diameter_mm": 273.0, "length_km": 50.0,
                "material_grade": "API 5L X52", "smys_mpa": 358.0,
            },
            "maop_zones": [{
                "wt_mm_min": 6.0, "wt_mm_max": 8.0,
                "design_factor": 0.72, "maop_kgcm2": 70.0,
            }],
            "runs": {
                "run_1": {"file_path": "run1.xlsx",
                          "inspection_date": "2018-12-15"},
                "run_2": {"file_path": "run2.xlsx",
                          "inspection_date": "2023-03-15"},
            },
        }
        with yaml_path.open("w", encoding="utf-8") as f:
            pyyaml.safe_dump(data, f, sort_keys=False)
        return yaml_path

    def test_relative_paths_resolve_via_helper_at_load(self, portable_pack):
        # Project.from_yaml stores the RAW string; resolution is the
        # consumer's job (CLI / GUI worker call resolve_relative_to_yaml).
        from src.models import Project
        proj = Project.from_yaml(str(portable_pack))
        assert proj.run_1.file_path == "run1.xlsx"     # raw, unresolved
        assert proj.run_2.file_path == "run2.xlsx"
        # Resolve via the helper as the CLI/worker would:
        r1 = resolve_relative_to_yaml(portable_pack, proj.run_1.file_path)
        r2 = resolve_relative_to_yaml(portable_pack, proj.run_2.file_path)
        assert r1 == (portable_pack.parent / "run1.xlsx").resolve()
        assert r2 == (portable_pack.parent / "run2.xlsx").resolve()
        assert r1.exists() and r2.exists()

    def test_portable_pack_after_folder_rename(self, portable_pack, tmp_path):
        # Simulate "user renamed C:\tmp\portable\ -> C:\tmp\portable_moved\":
        # copy the pack to a new dir and re-run resolution. Must still work.
        import shutil
        moved = tmp_path / "moved_subdir"
        shutil.copytree(portable_pack.parent, moved)
        moved_yaml = moved / portable_pack.name

        from src.models import Project
        proj = Project.from_yaml(str(moved_yaml))
        r1 = resolve_relative_to_yaml(moved_yaml, proj.run_1.file_path)
        assert r1 == (moved / "run1.xlsx").resolve()
        assert r1.exists()

    def test_mixed_absolute_and_relative_loads(self, tmp_path):
        # One run absolute, one relative — both must resolve.
        import yaml as pyyaml
        (tmp_path / "rel_run.xlsx").touch()
        abs_run = tmp_path / "elsewhere" / "abs_run.xlsx"
        abs_run.parent.mkdir()
        abs_run.touch()

        yaml_path = tmp_path / "mixed.yaml"
        with yaml_path.open("w", encoding="utf-8") as f:
            pyyaml.safe_dump({
                "runs": {
                    "run_1": {"file_path": "rel_run.xlsx"},
                    "run_2": {"file_path": str(abs_run)},
                }
            }, f)

        import yaml as pyyaml2
        with yaml_path.open() as f:
            data = pyyaml2.safe_load(f)
        r1 = resolve_relative_to_yaml(yaml_path, data["runs"]["run_1"]["file_path"])
        r2 = resolve_relative_to_yaml(yaml_path, data["runs"]["run_2"]["file_path"])
        assert r1 == (tmp_path / "rel_run.xlsx").resolve()
        assert r2 == abs_run  # absolute pass-through
        assert r1.exists() and r2.exists()
