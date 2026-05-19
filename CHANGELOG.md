# Changelog

All notable changes to the Athena ILI FFP Tool.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project loosely follows semantic versioning. Until v1.0 the
project is treated as MINOR-versioned (0.2 → 0.3 is allowed to break
on-disk YAML / annexure schemas; a PATCH bump like 0.2.0 → 0.2.1 will not).

## [0.3.3] - 2026-05-19

### Fixed
- `estimated_erf_circ` (Kastner) annexure topic now produces one row per
  circumferentially-classified defect instead of only those where Kastner
  was the controlling (lower Psafe) method. Previously the sheet's
  per-feature filter required `ffps_by_id[id].method is FFPMethod.KASTNER`,
  but `ffps_by_id` only stores the controlling result per feature — and
  for typical Indian metal-loss populations B31G's Psafe is the lower
  value, so the writer found zero matching rows. Surfaced during the
  BPCL Mathura-Piyala 1ZYT validation: engine produced 0 Kastner
  features vs the reference deliverable's ~325. The fix replaces the
  controlling-only filter with the new `is_kastner_eligible(feature)`
  multi-signal classifier (POF dimension_class enum authoritative; raw-
  description substring and geometric-proxy `W > L` as fallbacks when
  POF is `UNDEFINED`), then dispatches `kastner()` per eligible feature
  directly. BPCL Mathura-Piyala now produces 323 rows; the
  POF-enum-authoritative priority order avoids the iteration-1 overshoot
  to 3318 (which mis-classified PITTING/GENERAL/PINHOLE features whose
  width happened to exceed their length).

### Added
- `src.core.ffp.is_kastner_eligible(feature) -> bool` — the new
  three-signal classifier exposed for reuse outside the topic writer.
  See `docs/ENGINE_REFERENCE.md §9` for the priority rules.

## [0.3.2] - 2026-05-19

### Changed
- GUI Results-screen ERF bucket counter now uses full-precision ERFs
  without 3dp rounding, aligning to the per-feature ERFs reported in
  Annexure D. The GUI bucket counts now exactly match what a downstream
  consumer would get by re-bucketing the annexure file's ERF column
  directly. The 3dp-rounded classification mode introduced in v0.2.6
  remains available in `src/reports/erf_buckets.py` for callers that
  want it (pass `dp=3`), but is no longer the default. Note: published
  Athena report PDFs may show slightly different bucket counts (16
  features on BPCL Malarna, 41 on BPCL Mathura-Piyala) due to
  display-precision conventions in the original analysis; this is
  expected and documented in ENGINE_REFERENCE.md §11.2.
- Dent strain Ei/Eo sign convention refined to match the BPCL Annexure
  E reference values to 4 significant figures. Engine now adds an
  internal transverse quadratic curvature term `ε_3,W = (1/2)(d/W)²`
  that pairs with ε_1 on the circumferential axis and flips sign
  between surfaces — the asymmetry is what produces BPCL's
  `|Eo| > |Ei|` separation that v0.3.1's symmetric-membrane
  interpretation could not reproduce. BPCL row 4 reconciliation: Ei
  matches within 5e-5 absolute (was 4e-4 in v0.3.1); Eo within 1.7e-5
  (was 1e-4); Resultant within 0.0017 % absolute (was 0.0106 %).
  E1/E2/E3 components were already bit-exact in v0.3.1 and remain
  unchanged.

## [0.3.1] - 2026-05-18

### Added
- Full ASME B31.8 §851.4.1 dent strain analysis. The dent_strain_b318 topic
  now computes circumferential bending (E1), longitudinal bending (E2),
  membrane (E3), inside/outside surface (Ei/Eo) and resultant strain for each
  dent feature in Run-2. Replaces the v0.2.5 placeholder.
- Separate dent feature read path (`src/io/feature_reader.py:read_dent_features`)
  bypasses the metal-loss skip-list specifically for the dent strain topic.
  FFP pipeline continues to filter dents normally via the existing read path
  (Abu Road dent-leak guard unchanged).
- HIGH_STRAIN_REJECT_CRITERIA flag fires when resultant strain ≥ 6%
  (ASME B31.8 dent rejection threshold). Over-threshold rows are highlighted
  yellow in the Annexure E XLSX output.
- Topic registry: `dent_strain_b318.implemented` flipped from False to True.
  All seven topics now produce real output (no placeholders).

### Note
- The formulas were reverse-engineered against BPCL Malarna-Karwadi's
  published Annexure E (the ASME standard itself is paywalled). E1/E2/E3
  match BPCL bit-exactly on the calibration sample; Ei/Eo agree within
  ~0.5% relative on the resultant strain — within the "3-4 significant
  figures" tolerance the customer specified for the v0.2.5 placeholder.
  The full derivation lives in `docs/ENGINE_REFERENCE.md §11.5`.

## [0.3.0] - 2026-05-18

### Added
- MAOP zones can now be defined by chainage instead of wall thickness. Set
  `maop_zoning_mode: chainage` at the pipeline level in the project YAML to
  enable. Each zone then declares `chainage_m_min` and `chainage_m_max` (instead
  of `wt_mm_min` / `wt_mm_max`). This matches real pipeline operator practice
  (e.g., section valves at fixed chainages bounding distinct pressure regimes)
  and is the recommended mode for multi-section pipelines like HMEL.
- New `Pipeline.maop_for_chainage(chainage_m)` and dispatcher
  `Pipeline.maop_for_feature(feature)` methods route MAOP lookups based on the
  declared zoning mode.

### Backward compatibility
- YAMLs without `maop_zoning_mode` continue to use WT-based zoning, unchanged.
  All existing project YAMLs (Kandla, BPCL, TP1/TP2/TP3) load and run
  bit-identically to v0.2.6 with no modification.

## [0.2.6] - 2026-05-18

### Added
- Annexure B (results_ili_comparison topic) now writes a "CGR raw (mm/yr)" column
  showing the pre-floor per-feature CGR (depth_run2 − depth_unmatched) / Δt
  alongside the existing post-floor "CGR (mm/yr)" column. In hybrid CGR mode the
  two columns may differ; in feature_specific or population_only modes they are
  identical. The post-floor value remains what FFP projection consumes; the raw
  column lets engineers verify per-feature rates against hand-calculation and
  published reference reports.

### Changed
- ERF bucket classification (Results screen ERF distribution + any annexure-level
  bucket summaries) now applies 3-decimal-place rounding before comparing to
  boundaries (0.85, 0.90, 1.00). This matches the convention used in published
  Athena reports — a feature with raw ERF 0.8504 displays as "0.850" and now
  classifies as "≤ 0.85" rather than splitting at the underlying float boundary.
  QA flag thresholds (ERF_EXCEEDS_1) and repair triggers continue to use raw
  float comparison — only display-purpose bucket counts are affected.

## [0.2.5] - 2026-05-18

### Changed
- **Report generation switched from a 2-option preset selector
  ("E_F format" / "B_C_D format") to a topic-based multi-select.**
  Each topic becomes one sheet in the output XLSX. The user-facing
  annexure letter (A, B, C…) is per-topic and engineer-overridable
  to fit each customer's requirements. Topic registry lives in
  `src/reports/topic_registry.py`; YAML schema is the new
  `report.annexures` block (see ENGINE_REFERENCE.md §11).

### Added
- New writer **Guidelines & Formulas Used** (default annexure A) —
  9-section reference sheet documenting the project, pipeline,
  MAOP zones, ILI runs, FFP method + formulas, CGR settings,
  repair-prediction settings, critical constants, and references.
- New writer **Estimated Strain in Dents per ASME B31.8**
  (default annexure F) — **PLACEHOLDER** in v0.2.5. Lists dent
  features identified in Run-2 with a note that full strain
  computation per B31.8 §851.4.1 is a future addition.
- GUI multi-select panel on Project Setup with per-topic checkboxes
  and letter overrides. Duplicate letters highlighted red; Save /
  Proceed disabled until at least one topic is selected and
  letters are unique.
- `parse_report_annexures` / `serialize_report_annexures` helpers
  in `src.models` for YAML I/O. Unknown topic IDs and duplicate
  letters raise `ValueError` at load time with the offending YAML
  path + list index.

### Note
- **Backward compatibility:** YAMLs without a `report.annexures`
  block load with the v0.2.0–v0.2.4 "E_F preset" equivalent
  (results_ili_comparison + metal_loss_anomalies + qa_findings).
  The bundled `examples/kandla_project.yaml` continues to work
  unchanged.
- The legacy `--annexure-format E_F | B_C_D` CLI flag is retained
  as an override (writes the legacy preset rather than honouring
  the YAML's topic list). Default behaviour now reads the YAML.
- Annexure B (matched-only run-to-run) is retired as a selectable
  topic — its content is fully subsumed by
  `results_ili_comparison` (Annexure E equivalent: matched +
  unmatched). The `_write_annexure_b` function stays in the
  codebase for legacy `format="B_C_D"` callers.

## [0.2.4] - 2026-05-18

### Fixed
- MAOP-vs-WT design sanity warning banner now correctly checks each
  MAOP zone against the thinnest wall IN THAT ZONE, not against the
  globally thinnest wall. v0.2.0-v0.2.3 produced false-positive
  warnings on multi-zone pipelines where thicker-pipe zones carried
  higher MAOPs (the physically correct setup). The banner now only
  fires when a zone's MAOP genuinely exceeds the Barlow design limit
  for its own thinnest pipe.

## [0.2.3] - 2026-05-18

### Fixed
- Project YAML file_path entries can now be specified as paths relative
  to the YAML's own location. Absolute paths still work (backward compat).
- The GUI's Save YAML now writes relative paths when the YAML and ILI
  files share a common folder, making projects portable between machines.

### Note
- Existing absolute-path YAMLs from v0.2.0–v0.2.2 continue to work
  unchanged. No migration needed.

## [0.2.2] - 2026-05-17

### Fixed
- Output directory is now resolved to a user-writable location (alongside
  the project YAML, or in `~/Documents/Athena ILI FFP/<project>/` as a
  fallback). v0.2.1 attempted to write outputs into the installation
  directory, which fails with PermissionError when installed to
  Program Files (the standard install location).

## [0.2.1] — Vendor-format hardening release

### Added
- **Non-UTF-8 vendor CSV support.** Encoding cascade (`utf-8` →
  `utf-8-sig` → `latin-1` → `cp1252` → `utf-16`) plus BOM sniffing in
  `src/io/format_converter/csv_input.py`. Unblocks Athena 2018 CSV
  exports whose column headers carry the `°` symbol as byte 0xb0
  (latin-1). Bare `pd.read_csv()` under utf-8 default chokes on this.
- **MAOP-vs-WT design sanity warning** at Validate time. Computes the
  Barlow design pressure `P = 2·SMYS·t / OD · Fd` for the thinnest
  WT actually present in Run-2, per zone, with a 5% tolerance for
  vendor sub-nominal WT readings. Surfaces a yellow inline banner
  under the MAOP-zones table naming the specific offending zone(s).
  Banner is advisory — does not block Proceed.
- **PDF auto-fill overwrite-confirm dialog.** When the user clicks
  "Auto-fill from Final Report PDF…" while the form already has
  data, a confirm dialog appears (Cancel as the default button) so
  a stray Enter can't clobber user input. Empty placeholders /
  whitespace-only fields don't trigger the prompt.
- **PDF auto-fill — product + installation year extraction.** Pulls
  "Pipeline medium during inspection X" and "Year of construction"
  lines off page 6, maps product → service_class via a curated table
  (LPG/Crude → liquid; Natural Gas → gas; Multiphase).
- **Pipeline-name regex hardening.** Three new fallback patterns
  (smart-quote variants, explicit `PIPELINE:` cover-page label,
  `Pipeline name:` / `Pipeline section:` labelled lines) plus
  truncation of geometry suffixes (`, OD 406 mm`, `, L 144.4 km`).
  Closes the IP-2 → Nasirabad blank-fields bug.
- **MAOP zone merging on auto-fill.** Single-MAOP case (most NGP
  projects) collapses into ONE zone spanning min(WTs)−0.5 to
  max(WTs)+0.5 mm — avoids the v0.2.0 overlapping-zones bug. Per-WT-
  MAOP case uses midpoint cuts between consecutive WTs.
- **CHANGELOG.md** (this file).

### Changed
- **`FormatConverter.read_source` / `read_pipe_source` are now
  polymorphic by file extension.** `.csv` routes through the
  encoding-fallback helper; everything else through `pd.read_excel`.
  Sidesteps the cryptic "Excel file format cannot be determined"
  error when the export path was given a `.csv` source.
- **`FormatConverter.convert()` accepts `source_df=` / `pipe_df=`
  cache overrides.** GUI passes its already-read DataFrames at export
  time so the converter doesn't redundantly re-read the disk. ~halves
  wall-clock on big files.
- **QSS label visibility hardening (four defense layers).** Explicit
  `color:` rules on `QLabel` / `QCheckBox` / `QGroupBox` / table
  `::item` selectors; app-wide `QPalette` set via
  `apply_application_palette()`; per-table `apply_table_palette()` +
  `themed_item()` helpers for QTableWidgetItem foreground brushes;
  inline `<span color:#2C3E50>` on required-field labels. Closes the
  invisible-text class of regressions.

### Fixed
- Auto-fill from PDF no longer leaves Product / Installation year
  blank when those values are present on page 6 (regression from
  v0.2.0's "metadata Out of Scope" assumption).

### Test count
555 passing, 3 skipped (optional vendor files: real Abu Road PDF,
1ZYC Run-2 xlsx, real HMEL PDF).

## [0.2.0] — Initial v0.2 release

### Added
- **PyQt6 GUI** with five sidebar screens: Project Setup, Convert
  Format, Run Analysis, Results, Output. Replaces the v0.1 CLI as
  the primary entry point. CLI (`athena_ili_ffp.exe`) still ships
  alongside as `bin/run_pipeline.py`.
- **Format Converter screen.** Drag-and-drop column mapping between
  any vendor file and the NGP/Athena canonical layout. Built-in
  starter profiles for Rosen 2018, Baker Hughes / PII (DRAFT), NDT
  Global (DRAFT), Onstream (DRAFT), plus a generic template.
- **PDF auto-fill** from vendor Final Reports. Extracts ~12 fields
  (project / pipeline / client / vendor / OD / length / wall
  thicknesses / material grade / SMYS / MAOP / design factor / Run-2
  date / pipeline section code) from the Athena/NGP report template.
- **Vendor-report PDF parser** (`src/io/vendor_report_parser.py`)
  with per-field confidence scoring and graceful handling of
  image-only / corrupt / non-Athena-template PDFs.
- **Welcome banner + sidebar reordering.** Project Setup is now the
  default landing screen. Convert Format moves below it as a tool
  for the minority case (Run-1 in foreign vendor format).
- **NGP-validation banner on Project Setup.** Clicking Browse for
  Run-1 runs `ILIReader().read()` against the file; failure shows
  a yellow "Convert it →" banner that jumps to Convert Format.
- **Two-EXE installer.** Same `dist/athena_ili_ffp/` folder ships
  both `athena_ili_ffp.exe` (CLI, console=True) and
  `AthenaIliFfp.exe` (GUI, windowed). Single Inno Setup installer
  drops both into `%LOCALAPPDATA%\Programs\Athena ILI FFP Tool\`.
- **HMEL MAOP zone correction.** The published WT→MAOP mapping for
  HMEL IPS1-IPS2 is counterintuitive (thicker WT = higher pressure
  section). Project YAML and expected_results updated to match;
  #209581 now reconciles exactly to Psafe=78.9 kg/cm², ERF=1.022.
- **Dent-leak guard.** Three-layer defense (synonym table + reader
  parse warning + `ffp_assess` ValueError guard) ensures dent
  features never reach FFP assessment. Triggered by the Abu Road
  #1637 dent that had its 0.9 %OD depth treated as 90 %WT under
  v0.1, producing a bogus ERF=8.57.

### Changed
- **`ILIRun.features_for_assessment()` now filters non-metal-loss
  features in-method** (DENT / DEML / CRAC / GWAN / SWAN / LWAN) so
  `run.features` keeps the raw row count for vendor-total
  cross-checks while only assessable features reach the FFP engine.

### Migrated from v0.1.0
- CLI behaviour and on-disk YAML / annexure / DOCX schemas are
  unchanged. Existing v0.1.0 project YAMLs load directly.

[0.2.1]: https://github.com/athena-powertech/ili-ffp-tool/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/athena-powertech/ili-ffp-tool/releases/tag/v0.2.0
