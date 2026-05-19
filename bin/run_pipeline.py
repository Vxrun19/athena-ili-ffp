#!/usr/bin/env python3
"""
Run the full FFP pipeline from the command line.

Usage:

    python bin/run_pipeline.py \\
        --config examples/kandla_project.yaml \\
        --output-dir ./output/

    # The --run1 / --run2 / --years overrides take precedence over the
    # YAML's `runs` block when supplied:
    python bin/run_pipeline.py \\
        --config examples/hmel_ips1_ips2_project.yaml \\
        --run1 examples/8-9100-13964_28IP1IP2_Pipe_Tally_run1_.xlsx \\
        --run2 examples/1YCF_Pipeline_Listing__run2_.xlsx \\
        --years 5.42 \\
        --output-dir ./output/

Exit codes:
    0 — pipeline ran cleanly
    1 — any QA flag landed in the ERROR severity bucket
    2 — unrecoverable error (missing input, bad config, …)

The script prints a one-page summary to stdout. Full outputs (annexure
.xlsx + report .docx) are written to `--output-dir`.

This is the non-GUI entry point. The PyQt6 GUI (Prompt 12) wraps the
same chain.
"""
from __future__ import annotations

import argparse
import io
import sys
import time
from datetime import date
from pathlib import Path

# Reconfigure stdout to UTF-8 so the summary lines (which use ≥ / →
# characters) survive Windows' default 'charmap' codec. Available since
# Python 3.7; the .reconfigure() call is safe to no-op on non-TTY pipes.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, io.UnsupportedOperation):  # pragma: no cover
    pass

# Make sure 'src' is importable when this script is run directly.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src._version import version_string                         # noqa: E402
from src.core.cgr import CGRCalculator                          # noqa: E402
from src.core.defect_matcher import DefectMatcher                # noqa: E402
from src.core.ffp import ffp_assess                              # noqa: E402
from src.core.joint_alignment import JointAligner                # noqa: E402
from src.core.repair_predictor import RepairPredictor            # noqa: E402
from src.io.ili_reader import ILIReader                          # noqa: E402
from src.io.paths import resolve_output_dir, resolve_relative_to_yaml  # noqa: E402
from src.models import Project, FFPMethod                        # noqa: E402
from src.reports.annexure_writer import AnnexureWriter           # noqa: E402
from src.reports.main_report_writer import MainReportWriter      # noqa: E402
from src.validation.flag_aggregator import FlagAggregator        # noqa: E402


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_pipeline",
        description="Run the full FFP pipeline (read → align → match → "
                    "CGR → FFP → predict → reports) on a project config.",
    )
    p.add_argument(
        "--version", action="version", version=version_string(),
        help="print version banner and exit",
    )
    p.add_argument(
        "--config", required=False, type=Path, default=None,
        help="path to project YAML (see examples/kandla_project.yaml)",
    )
    p.add_argument(
        "--run1", type=Path, default=None,
        help="path to run-1 pipe tally (overrides config's runs.run_1.file_path)",
    )
    p.add_argument(
        "--run2", type=Path, default=None,
        help="path to run-2 pipe tally (overrides config's runs.run_2.file_path)",
    )
    p.add_argument(
        "--years", type=float, default=None,
        help="interval between runs in years (overrides date arithmetic from config)",
    )
    p.add_argument(
        "--output-dir", type=Path, default=None,
        help="directory to write annexure + DOCX (default: alongside the "
             "YAML, or ~/Documents/Athena ILI FFP/<project>/ as fallback)",
    )
    p.add_argument(
        "--annexure-format", default=None, choices=("E_F", "B_C_D"),
        help="legacy preset (v0.2.0–v0.2.4). When set, overrides the "
             "project YAML's `report.annexures` topic list. Default: "
             "honour the YAML (falling back to the v0.2.x E_F-preset "
             "equivalent when the YAML has no `report.annexures` block).",
    )
    p.add_argument(
        "--no-docx", action="store_true",
        help="skip the DOCX report (annexure only)",
    )
    p.add_argument(
        "--quiet", action="store_true",
        help="suppress per-stage progress lines (keep the final summary)",
    )
    return p


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, flush=True))

    # ----- Load project config + resolve run paths
    if args.config is None:
        print("ERROR: --config is required (try --help)", file=sys.stderr)
        return 2
    config_path = args.config.resolve()
    if not config_path.exists():
        print(f"ERROR: config not found: {config_path}", file=sys.stderr)
        return 2
    project = Project.from_yaml(str(config_path))

    runs_cfg = (project.config.get("runs") or {})
    run1_path = args.run1 or resolve_relative_to_yaml(
        config_path, runs_cfg.get("run_1", {}).get("file_path"),
    )
    run2_path = args.run2 or resolve_relative_to_yaml(
        config_path, runs_cfg.get("run_2", {}).get("file_path"),
    )
    # v0.2.3: error messages mention BOTH the YAML location and the
    # resolved path. Relative-resolving against the YAML's parent
    # makes the resolved path non-obvious from the raw YAML text alone.
    if not run1_path or not Path(run1_path).exists():
        print(
            f"ERROR: run-1 file not found.\n"
            f"  YAML:     {config_path}\n"
            f"  Resolved: {run1_path}\n"
            f"  Raw YAML value: "
            f"{runs_cfg.get('run_1', {}).get('file_path')!r}",
            file=sys.stderr,
        )
        return 2
    if not run2_path or not Path(run2_path).exists():
        print(
            f"ERROR: run-2 file not found.\n"
            f"  YAML:     {config_path}\n"
            f"  Resolved: {run2_path}\n"
            f"  Raw YAML value: "
            f"{runs_cfg.get('run_2', {}).get('file_path')!r}",
            file=sys.stderr,
        )
        return 2

    pipeline = project.pipeline

    # ----- Read
    log(f"Reading run-1: {run1_path}")
    t0 = time.time()
    reader = ILIReader()
    run1 = reader.read(str(run1_path), run_id="run_1")
    if project.run_1.inspection_date:
        run1.inspection_date = project.run_1.inspection_date

    log(f"Reading run-2: {run2_path}")
    run2 = reader.read(str(run2_path), run_id="run_2")
    if project.run_2.inspection_date:
        run2.inspection_date = project.run_2.inspection_date

    project.run_1 = run1
    project.run_2 = run2
    log(f"  read: {time.time() - t0:.1f}s — "
        f"run1={len(run1.features_for_assessment())} features, "
        f"run2={len(run2.features_for_assessment())} features")

    # ----- Years between
    if args.years is not None:
        years_between = float(args.years)
    elif run1.inspection_date and run2.inspection_date:
        years_between = (run2.inspection_date - run1.inspection_date).days / 365.25
    else:
        print("ERROR: --years not supplied and inspection dates missing from config",
              file=sys.stderr)
        return 2
    log(f"  years between runs: {years_between:.3f}")

    # ----- Joint alignment
    log("Joint alignment...")
    t0 = time.time()
    ja = JointAligner().align(run1, run2)
    log(f"  align: {time.time() - t0:.1f}s — "
        f"{len(ja.matches)} joint pairs, match_rate={ja.match_rate:.1%}")

    # ----- Defect matcher
    log("Defect matching...")
    t0 = time.time()
    mr = DefectMatcher().match(run1, run2, ja.matches)
    log(f"  match: {time.time() - t0:.1f}s — "
        f"{len(mr.feature_matches)} matched, "
        f"{len(mr.unmatched_features_old)}/{len(mr.unmatched_features_new)} unmatched")

    # ----- CGR
    cgr_mode = (project.config.get("cgr") or {}).get("mode", "hybrid")
    log(f"CGR ({cgr_mode})...")
    t0 = time.time()
    cgrs = CGRCalculator({"mode": cgr_mode}).compute(mr, years_between=years_between)
    log(f"  cgr: {time.time() - t0:.1f}s — {len(cgrs)} results")

    # ----- FFP
    primary = (project.config.get("ffp") or {}).get("primary_method", "B31G_Original")
    log(f"FFP ({primary})...")
    t0 = time.time()
    ffps_by_id: dict[str, object] = {}
    for c in cgrs:
        try:
            fl = ffp_assess(c.feature, pipeline, config={"primary_method": primary})
            ffps_by_id[c.feature.anomaly_id] = next(
                (f for f in fl if f.is_controlling), fl[0]
            )
        except ValueError:
            continue
    log(f"  ffp: {time.time() - t0:.1f}s — {len(ffps_by_id)} assessed")

    # ----- Repair predictor
    log("Repair prediction...")
    t0 = time.time()
    horizon = int(
        (project.config.get("repair_prediction") or {}).get("horizon_years", 10)
    )
    preds = RepairPredictor({"horizon_years": horizon}).predict(
        cgrs, ffps_by_id, pipeline,
        run2_inspection_date=run2.inspection_date,
    )
    log(f"  predict: {time.time() - t0:.1f}s")

    # ----- QA aggregator
    log("QA aggregation...")
    flag_report = FlagAggregator().aggregate(
        run1=run1, run2=run2, joint_alignment=ja, match_result=mr,
        cgr_results=cgrs, ffp_results=list(ffps_by_id.values()),
        predictions=preds,
    )

    # ----- Reports
    if args.output_dir is not None:
        out_dir = args.output_dir
    else:
        # No --output-dir supplied: write alongside the YAML when
        # possible, fall back to ~/Documents. Avoids the v0.2.1 bug
        # where the implicit Path("./output") crashed under a Start-
        # Menu launch with CWD = ACL-protected install dir.
        out_dir = resolve_output_dir(
            config_path, project.project_name or config_path.stem,
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = project.project_name or "ffp_project"

    annex_path = out_dir / f"{stem}_annexure.xlsx"
    # v0.2.5: prefer topic-list mode (from YAML's `report.annexures`).
    # `--annexure-format` is now a backward-compat override.
    if args.annexure_format:
        topics_arg = None       # legacy preset wins
        log(f"Writing annexure (legacy preset {args.annexure_format}) -> {annex_path}")
    else:
        topics_arg = project.report_annexures
        log(f"Writing annexure ({len(topics_arg)} topics from YAML) -> {annex_path}")
    t0 = time.time()
    AnnexureWriter().write(
        cgr_results=cgrs, ffp_results=list(ffps_by_id.values()),
        repair_predictions=preds, flag_report=flag_report,
        project=project, pipeline=pipeline,
        output_path=str(annex_path),
        topics=topics_arg,
        years_between=years_between,
        format=args.annexure_format,
    )
    log(f"  annexure: {time.time() - t0:.1f}s")

    docx_path: Path | None = None
    if not args.no_docx:
        docx_path = out_dir / f"{stem}_report.docx"
        log(f"Writing DOCX -> {docx_path}")
        t0 = time.time()
        MainReportWriter().write(
            project=project, match_result=mr, joint_alignment=ja,
            cgr_results=cgrs, ffp_results=list(ffps_by_id.values()),
            repair_predictions=preds, flag_report=flag_report,
            output_path=str(docx_path),
        )
        log(f"  docx: {time.time() - t0:.1f}s")

    # ----- Summary
    _print_summary(
        project=project, ja=ja, mr=mr, cgrs=cgrs,
        ffps=list(ffps_by_id.values()), preds=preds,
        flag_report=flag_report,
        annex_path=annex_path, docx_path=docx_path,
    )

    return 1 if flag_report.has_critical else 0


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _print_summary(*, project, ja, mr, cgrs, ffps, preds, flag_report,
                   annex_path, docx_path) -> None:
    line = "=" * 70
    pl = project.pipeline
    print()
    print(line)
    print(f"  {project.project_name}")
    print(line)
    print(f"  Pipeline:           {pl.pipeline_name} ({pl.diameter_mm:.0f} mm OD, "
          f"{pl.length_km:.1f} km, {pl.material_grade})")
    print(f"  Joints aligned:     {len(ja.matches)}  (match rate "
          f"{ja.match_rate:.1%}, {len(ja.monotonicity_violations)} reversal(s))")
    print(f"  Defects matched:    {len(mr.feature_matches)} "
          f"(of {len(mr.feature_matches) + len(mr.unmatched_features_new)} run-2 features)")

    n_erf = sum(1 for r in ffps if r.erf >= 1.0)
    n_depth = sum(1 for r in ffps if r.depth_pct_wt >= 80.0)
    n_repair = sum(1 for p in preds if p.repair_trigger != "NONE_WITHIN_HORIZON")
    print(f"  ERF ≥ 1.0:          {n_erf}")
    print(f"  Depth ≥ 80% WT:     {n_depth}")
    print(f"  Repair within {preds[0].horizon_years if preds else '—'} yr: {n_repair}")
    print()
    print(f"  QA: {flag_report.summary}")
    print()
    print(f"  Annexure:   {annex_path}")
    if docx_path:
        print(f"  DOCX:       {docx_path}")
    print(line)


# NOTE: v0.2.3 removed `_resolve_under` in favour of
# `src.io.paths.resolve_relative_to_yaml`. The old helper resolved
# relative paths against the source-tree project root first
# (`_PROJECT_ROOT / p`) — which works in a `python bin/run_pipeline.py
# ...` source-tree invocation but is meaningless inside a PyInstaller
# bundle (where `_PROJECT_ROOT` points at the install dir). The new
# helper resolves against the YAML's own parent only, so a hand-curated
# YAML + xlsx folder is portable across machines.


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return run(args)
    except SystemExit:
        raise
    except Exception as e:                                 # noqa: BLE001
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
