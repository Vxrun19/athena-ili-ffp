"""Auxiliary feature readers — non-FFP read paths.

The primary :class:`src.io.ili_reader.ILIReader` filters out non-
metal-loss features (dents, welds, cracks) at parse time via the
``value_normalisations.feature_type_anomaly.skip`` list in
``config/column_synonyms.yaml``. That filter is intentional — it's
the Abu Road dent-leak guard (see ``ENGINE_REFERENCE.md §1.10``) and
must stay active for the FFP pipeline.

But the v0.3.1 dent-strain topic *needs* dent features. This module
provides a separate, lightweight read path that:

  * Loads Run-2 with a stripped-down skip-list (dents kept, other
    non-ML rows still skipped).
  * Returns ONLY features whose ``feature_identification`` resolves
    to ``DENT`` or ``DENT_WITH_METAL_LOSS``.
  * Bypasses ``features_for_assessment()`` (which also filters dents).

The FFP pipeline does NOT use this reader. The dent inventory it
produces is consumed only by ``_write_dent_strain_sheet`` in
``src/reports/annexure_writer.py``.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from src.models import Feature, FeatureIdentification


# Dent-style POF codes that the dent-strain topic wants to see. Kept
# in sync with ``src/core/ffp.py:_NON_ASSESSABLE_FIDS`` and
# ``src/io/ili_reader.py:_NON_METAL_LOSS_FIDS`` (which both still
# include DENT for the FFP pipeline's filter).
_DENT_FIDS: frozenset[FeatureIdentification] = frozenset({
    FeatureIdentification.DENT,
    FeatureIdentification.DENT_WITH_METAL_LOSS,
})

# Skip-list entries from ``column_synonyms.yaml`` that we PRESERVE
# (i.e., things that should still be filtered even when reading dents):
# girth welds, valves, bends, casings, supports, attachments, etc.
# Dent-related entries ("dent", "denp", "denk", "dens", "dend",
# "denb", "denc", "dent ...") are removed from the skip-list before
# parsing so dent rows land in run.features.
_DENT_KEYWORDS = ("dent", "den")


def read_dent_features(
    run_path: str | Path,
    *,
    synonyms_path: str | Path | None = None,
) -> list[Feature]:
    """Read Run-2 and return only dent-type features.

    Loads the vendor xlsx via a modified :class:`ILIReader` whose
    skip-list omits dent keywords. The reader still filters out other
    non-metal-loss rows (welds, valves, casings, …). After parsing,
    keeps only features whose ``feature_identification`` is in
    :data:`_DENT_FIDS`.

    Returns ``[]`` when:

      * ``run_path`` doesn't exist (caller renders empty inventory).
      * The reader raises (file not in NGP format).
      * No dent rows are present.

    Args:
        run_path: Path to the Run-2 pipe-tally xlsx.
        synonyms_path: Override for the column-synonyms YAML. Defaults
            to the bundled ``config/column_synonyms.yaml``.

    Returns:
        Fresh list of :class:`Feature` instances (not shared with the
        FFP pipeline's reader output).
    """
    p = Path(run_path)
    if not p.exists():
        return []

    try:
        from src.io.ili_reader import ILIReader, load_synonyms, _norm
    except Exception:                                            # noqa: BLE001
        return []

    # Build a synonyms dict with dent keywords stripped from the skip-list.
    syns = deepcopy(load_synonyms(synonyms_path))
    vn = syns.get("value_normalisations") or {}
    ft = vn.get("feature_type_anomaly") or {}
    skip = list(ft.get("skip") or [])
    pruned_skip = [
        s for s in skip
        # Keep entries that don't contain dent-related keywords.
        if not any(kw in _norm(s) for kw in _DENT_KEYWORDS)
    ]
    ft["skip"] = pruned_skip
    vn["feature_type_anomaly"] = ft
    syns["value_normalisations"] = vn

    # Spin up a fresh ILIReader with our modified synonyms. Stash the
    # dict to a temp file because ILIReader.__init__ takes a path.
    import tempfile
    import yaml as pyyaml
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8",
    ) as f:
        pyyaml.safe_dump(syns, f, sort_keys=False, allow_unicode=True)
        tmp_syn_path = f.name

    try:
        reader = ILIReader(synonyms_path=tmp_syn_path)
        run = reader.read(str(p), run_id="run_2_dent_inventory")
    except Exception:                                            # noqa: BLE001
        return []
    finally:
        try:
            Path(tmp_syn_path).unlink(missing_ok=True)
        except OSError:
            pass

    # Filter to dent fids. We walk `run.features` (NOT
    # features_for_assessment, which strips dents).
    return [
        f for f in run.features
        if f.feature_identification in _DENT_FIDS
    ]


def read_metal_loss_features(
    run_path: str | Path,
    *,
    synonyms_path: str | Path | None = None,
) -> list[Feature]:
    """Read Run-2 and return only metal-loss features.

    Thin wrapper around :meth:`ILIReader.read` +
    :meth:`ILIRun.features_for_assessment`. Equivalent to the existing
    FFP-pipeline read path; exposed here as the symmetric counterpart
    to :func:`read_dent_features` so callers can use either without
    knowing the underlying reader plumbing.
    """
    p = Path(run_path)
    if not p.exists():
        return []
    try:
        from src.io.ili_reader import ILIReader
        reader = ILIReader(synonyms_path=synonyms_path)
        run = reader.read(str(p), run_id="run_2_ml")
    except Exception:                                            # noqa: BLE001
        return []
    return run.features_for_assessment()


__all__ = ["read_dent_features", "read_metal_loss_features"]
