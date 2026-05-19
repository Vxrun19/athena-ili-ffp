# ILI FFP/CGR Tool — Claude Code Build Plan

This document contains step-by-step prompts to feed to Claude Code (or any agentic coding assistant) to build the complete tool. Each prompt is **self-contained** and builds on the previous one. Run them in order. After each prompt, the assistant should produce working code and pass the tests included in the prompt.

## How to use this document

1. Open a fresh Claude Code session in the project root directory
2. Paste **Prompt 1** verbatim and let it run to completion
3. Verify the deliverables listed at the end of each prompt
4. Move on to **Prompt 2**, paste, run, verify
5. Continue through all prompts

Each prompt is self-contained — Claude Code can pick up from where the previous one left off by reading the existing files. If a session crashes or hits a context limit, just start a new session and continue with the next unfinished prompt.

---

## Prompt 0 — Context briefing (paste this first, every new session)

```
You are helping build an in-house desktop application for Athena PowerTech LLP, an Indian pipeline integrity consultancy. The tool automates production of Fitness-For-Purpose (FFP) and Corrosion Growth Rate (CGR) reports from In-Line Inspection (ILI) data.

Domain in 60 seconds:
- A "smart pig" runs through an oil/gas pipeline measuring wall thickness; output is a "pipe tally" (Excel) listing every joint, every weld, and every metal-loss defect with location, depth, length, width, orientation
- Every 5-7 years a new pig run happens; comparing run1 vs run2 reveals corrosion growth
- For each matched defect: CGR = (depth_new − depth_old) / years
- For each defect: failure pressure (Psafe) from ASME B31G; ERF = MAOP / Psafe
- Project forward 10 years: when does ERF reach 1.0 or depth reach 80% WT → repair date
- Deliverable: a DOCX report with Excel annexures

The customer is the pipeline operator (GAIL, HMEL, HPCL); the ILI vendor is third-party (Athena/NGP, Rosen, Baker Hughes, etc.). Athena PowerTech is the integrity consultancy doing the analysis between vendor and operator.

Tech stack:
- Python 3.11+
- pandas, numpy, scipy for math
- openpyxl, xlrd for Excel I/O
- python-docx for report generation  
- PyQt6 for desktop GUI
- matplotlib for charts in reports
- PyInstaller for Windows .exe distribution

Project layout (already created):
- /src/io/      — file readers/writers
- /src/core/    — matching, CGR, FFP engines
- /src/models/  — dataclasses (Feature, Joint, Pipeline, etc.)
- /src/reports/ — DOCX/Excel report generation
- /src/gui/     — PyQt6 multi-screen app
- /src/validation/ — QA flag generators
- /config/      — YAML configs (column_synonyms.yaml, default_project.yaml already populated)
- /examples/    — real example files for regression testing
- /tests/       — pytest suite

Critical design principle: vendor formats vary widely even within one vendor over years. The parser MUST be config-driven (via column_synonyms.yaml) — never hardcode column names. The tool must work on files from 2018, 2023, future formats, and other vendors with only config changes, not code changes.

ERF convention: ERF = MAOP / Psafe (high ERF = bad). ERF action threshold typically 1.0 but project-configurable. Report features in bands: ≤0.85 / 0.85-0.90 / 0.90-0.95 / 0.95-1.0 / >1.0.

CGR has two modes, project-selectable:
- "feature_specific": CGR = max(0, (d_new − d_old) / years)
- "hybrid": CGR_used = max(feature_CGR, P95_of_population), with separate P95 for internal vs external defects. For unmatched run2 features, assume run1 depth was 10% WT (the tool detection threshold).

MAOP zones are wall-thickness based. A pipeline may have 1 zone (whole line same MAOP) or 3+ zones (different MAOPs for thicker/thinner sections).

FFP methods: ASME B31G Original is most common in India; B31G Modified used by some vendors; Kastner for circumferentially-oriented defects; RSTRENG and DNV-RP-F101 as cross-checks. The tool implements all of them and the user picks per project.

Repair date stop conditions (whichever fires first):
- depth ≥ 80% WT, OR
- Psafe ≤ MAOP (ERF ≥ 1.0)

Reference standards:
- POF 100 (2021) — content specifications
- POF 110 (2021) — UPT data format
- ASME B31G-2012 — strength calculation
- DNV-RP-F101 (2017) — alternative method
- API 1160 (liquid) / API 570 (in-service)

Now ready for the next prompt.
```

---

## Prompt 1 — Verify project skeleton and data models

```
Read the project files in /src/models/__init__.py and /config/default_project.yaml and /config/column_synonyms.yaml. Verify the structure is in place, then:

1. If src/models/__init__.py is empty or missing key classes, create the dataclasses for:
   - Surface enum (INTERNAL, EXTERNAL, MIDWALL, UNKNOWN)
   - DimensionClass enum (matching POF: GENE, PITT, PINH, AXSL, AXGR, CISL, CIGR)
   - FeatureIdentification enum (CORR corrosion, COCL corrosion cluster, MILL mill anomaly, DENT, etc.)
   - FFPMethod enum (B31G_ORIGINAL, B31G_MODIFIED, RSTRENG, DNV_RP_F101, KASTNER)
   - CGRMode enum (FEATURE_SPECIFIC, HYBRID, POPULATION_ONLY)
   - Feature dataclass (anomaly_id, joint_number, abs_distance_m, upstream_weld_dist_m, surface, depth_pct_wt, length_mm, width_mm, clock_hours, wt_mm, latitude, longitude, dimension_class, feature_type, erf_reported, psafe_reported)
   - Joint dataclass (joint_number, abs_distance_m, length_m, wt_mm, features list)
   - MAOPZone dataclass (wt_values list, design_factor, maop_kgcm2)
   - Pipeline dataclass (name, diameter_mm, smys_mpa, maop_zones list, joints list)
   - ILIRun dataclass (run_id, inspection_date, vendor, tool_type, pipeline, features list, source_filepath)
   - FeatureMatch dataclass (run1_feature, run2_feature, match_score, confidence)
   - JointMatch dataclass (run1_joint_number, run2_joint_number, length_match_quality)
   - MatchResult dataclass (joint_matches list, feature_matches list, unmatched_run1 list, unmatched_run2 list)
   - FFPResult dataclass (feature, method, psafe_kgcm2, erf, m_factor, flow_stress_mpa, area_metal_loss_ratio)
   - RepairPrediction dataclass (feature, cgr_mm_yr, repair_date, trigger, depth_at_repair, erf_at_repair, years_to_repair)
   - Project dataclass (matches the YAML in /config/default_project.yaml — load from there)

2. Use Python 3.11 dataclasses with @dataclass(frozen=False) and type hints throughout. Internal units strictly:
   - distance in metres
   - dimensions (length, width) in mm
   - depth as both percent_wt (float 0-100) AND mm (computed)
   - clock position as decimal hours in [0, 12)
   - pressure in kg/cm² (Indian standard, NOT bar or psi)
   - SMYS in MPa
   - wall thickness in mm

3. Add unit-conversion utility functions in src/models/units.py:
   - bar_to_kgcm2(p_bar) and reverse
   - psi_to_kgcm2(p_psi) and reverse  
   - mpa_to_kgcm2(p_mpa) and reverse
   - parse_clock(value) → decimal hours; handles "06:14:00", "6:14", "06:14", 6.233, integer minutes, "6 o'clock", None
   - parse_surface(value) → Surface enum; handles "INT"/"int."/"Internal"/"INTERNAL", same for external
   - parse_depth(value, wt_mm) → tuple of (depth_pct_wt, depth_mm); handles "28.5%", "28.5", 28.5, 0.285 (when value < 1.0 assume fraction)

4. Write pytest tests in tests/test_models.py covering:
   - Each enum's value normalisation (parse_surface returns INTERNAL for all of "INT","int.","Internal","internal","INTERNAL")
   - parse_clock for every format in the docstring
   - parse_depth correctly distinguishes 0-1 fraction from percentage  
   - Round-trip unit conversions are exact (kg/cm² ↔ bar ↔ MPa)
   - Feature dataclass validates: depth_pct_wt in [0, 100], clock_hours in [0, 12), latitude in [-90, 90]

5. Run pytest and ensure all tests pass.

Deliverables:
- src/models/__init__.py with all dataclasses and enums
- src/models/units.py with conversion + parsing functions
- tests/test_models.py with at least 25 tests, all passing
```

---

## Prompt 2 — Build the flexible ILI file reader

```
Build the ILI file reader module at src/io/ili_reader.py. This is the most critical and most-used component of the tool — it must handle any vendor format gracefully.

The reader's contract:
- Input: path to an Excel file (.xls or .xlsx) and a project config (Pipeline + reader settings)
- Output: an ILIRun object populated with all defect features, joints, and basic pipeline-component records (welds, valves, supports, casings, dents) suitable for matching and reporting

Implementation requirements:

1. Sheet discovery
   - For multi-sheet files, scan ALL sheets and detect which one(s) contain defect data
   - A sheet is a "defect sheet" if its column headers (after header-row detection — see below) contain at least 4 of the canonical fields: abs_distance_m, depth_pct_wt, length_mm, width_mm, surface, feature_type, clock_position, joint_number, wt_mm
   - For files with explicit Defects / Metal Loss List sheets, prefer those over All features sheets
   - For older formats with sheets like "Pipeline Tally" and a separate "Metal Loss List", read from Metal Loss List as the canonical defect source
   - For single-sheet files where defects are mixed with welds/valves/etc., filter rows by Feature Type column (only "Anomaly" / "Metal Loss" / "Corrosion" rows are defects)

2. Header row detection  
   - Excel files from ILI vendors often have title rows, sub-title rows, blank rows before the actual column headers
   - Scan first 10 rows of each sheet looking for a row containing at least 4 known column synonyms (load these from /config/column_synonyms.yaml)
   - The row with the most synonym hits is the header row

3. Column mapping
   - Load /config/column_synonyms.yaml — it's the source of truth for "real column name → canonical internal field name"
   - For each canonical field, try each synonym in turn (case-insensitive, whitespace-stripped, punctuation-tolerant)
   - Build a column-index map: canonical_field → column_index_in_sheet
   - REQUIRED fields (file is rejected if missing): abs_distance_m, joint_number, depth_pct_wt, length_mm, width_mm, surface, wt_mm
   - OPTIONAL fields: clock_position, feature_type, dimension_class, anomaly_id, erf, psafe, latitude, longitude, altitude, upstream_weld_dist_m
   - If a required field is missing, raise a clear ValueError listing what columns were available and what synonyms were tried

4. Value normalisation (use the parsers from src/models/units.py)
   - parse_surface for surface column
   - parse_clock for clock_position column
   - parse_depth for depth column (auto-detect %-of-WT vs absolute mm vs fraction)
   - Handle empty cells, "N/A", "-", "·", "--" → None
   - Handle European decimal commas if needed
   - Strip whitespace, normalise unicode dashes

5. Row filtering
   - Skip rows where joint_number is missing (these are valves, supports, etc. mixed in)
   - Skip rows where Feature Type is in the "skip list" defined in column_synonyms.yaml (Weld, Valve, T-piece, Bend, Support, Casing, Flange, Tap, Attachment, Magnet, Reference point, Marker, Data recording begin/end)
   - Keep rows where Feature Type matches the "anomaly list" (Anomaly, Metal Loss, Corrosion, Corrosion Cluster, COCL, CORR, Pinhole, Pitting, etc.)
   - When Feature Type column is missing, fall back to: any row with non-null depth IS a defect

6. Joint extraction
   - For each unique joint_number, derive: abs_distance_m (minimum across all rows in that joint = upstream weld position), wt_mm (mode WT across joint), length_m (where available, or compute from upstream-weld + length-to-feature columns)
   - Build the list of Joint objects

7. Coordinate handling
   - Latitude / Longitude columns when present
   - Some files have them swapped — if all lat values look like longitudes (>>30°) and all lng look like latitudes (<30° for India), swap them and emit a warning

8. Cluster-aware reading
   - Some files report both individual features AND cluster summaries (Athena/NGP 1ZSV format has cluster rows like "GOCL-01" parent and child rows "GOCL-01-01")
   - Detect parent-cluster rows (anomaly_id without dot) vs child rows (with dot or hyphen)
   - Mark cluster parents with a `is_cluster_parent=True` flag, children with `cluster_parent_id` reference
   - The tool defaults to using cluster-parent rows for FFP (matching vendor's clustering decision)

9. Audit trail
   - The ILIRun object must store: source_filepath, sheet_name used, row count read, column mapping used, count of features filtered out and why
   - This audit info appears in QA flags and in the final report's traceability appendix

10. Tests in tests/test_ili_reader.py
   - Read the 4 example files I'll list below — each must succeed
   - Verify feature counts are within ±2% of what the vendor's report PDF says
   - Verify clock position is correctly parsed for both "06:14:00" string format and 6.233 decimal format
   - Verify surface normalisation handles all 4 known variants
   - Verify column mapping correctly identifies all canonical fields for each file
   - Test with deliberately-malformed file (random columns) → raises clear ValueError

Example files for testing (place in /examples/):
- 8-9100-13964_28IP1IP2_Pipe_Tally_run1_.xlsx  (NGP 2019 single-sheet format)
- 1YCF_Pipeline_Listing__run2_.xlsx (NGP 2025 multi-sheet, Defects sheet)
- 1ZSV_Pipeline_Listing.xlsx (NGP 2023 multi-sheet)
- 10_inch_Kandla_to_Samakhiali__58_2_km_FR_Pipe_Tally_Rev_0.xlsx (older Athena 2018 multi-sheet)
- FFP_Report_IPS_1_to_IPS2_Annexure_E__F___r4.xlsx (output format example for later)
- FFP_Report_COT_to_IPS_1_Annexure_E__F.xlsx
- IPS__Samakhiali_to_IP_01_-_Annexure_B__C__D_FFP_.xls (older Annexure B/C/D format)

For each example, write a fixture that loads and asserts:
  feature_count == EXPECTED_VALUE  (from the vendor report's executive summary)
  joint_count >= MINIMUM_VALUE
  no None values in required fields after parsing

Deliverables:
- src/io/ili_reader.py with class ILIReader having method read(filepath, project) → ILIRun
- tests/test_ili_reader.py with at least one passing test per example file
- A docstring at the top of ili_reader.py explaining: "Add a new vendor format by adding column synonyms to /config/column_synonyms.yaml — no code changes needed."
```

---

## Prompt 3 — Joint alignment between runs

```
Build src/core/joint_alignment.py.

The problem: Run 1 (2019) might have 4899 joints. Run 2 (2025) might have 4904 joints. Joint numbers may not match between runs (vendors may renumber). Some joints may have been replaced (different lengths). The matcher must find a robust correspondence between the two joint sequences.

Algorithm: Needleman-Wunsch global sequence alignment on joint length signatures.

1. Build joint length sequence for each run: ordered list of (joint_number, length_m, abs_distance_m, wt_mm)

2. Run Needleman-Wunsch with these scoring rules:
   - Match score: similarity = 1.0 - |len1 - len2| / max(len1, len2); only consider a "match" if similarity ≥ 0.85 (i.e. lengths within 15%)
   - Gap penalty: -0.5 (introducing a gap is moderately costly)
   - Mismatch penalty: -1.0 (forcing a bad match is worse than gaps)
   - Bonus for WT agreement: +0.2 if WT matches within ±0.5mm
   - Bonus for abs_distance agreement: +0.3 if positions within 20m (helps anchor early)

3. Output JointMatch list:
   - For each aligned pair, length_match_quality = the similarity score
   - Joints aligned to gaps are reported as unmatched_run1 or unmatched_run2
   - Aim for ≥90% match rate; if below, emit a warning with diagnostic info

4. Post-alignment validation:
   - The cumulative abs_distance of matched joints should increase monotonically in both runs; if there are local reversals (joint i+1 maps to a smaller distance than joint i), flag those as suspicious alignments
   - The total length of matched run1 joints vs matched run2 joints should agree within 1% (pipelines don't shrink)

5. Tests in tests/test_joint_alignment.py:
   - Synthetic test: identical sequence → 100% match, all in order
   - Synthetic test: insert 3 joints in middle of one sequence → those report as unmatched, all others still align
   - Synthetic test: shuffle 5% of lengths within ±10% → still ≥95% match
   - Real test: load run1 and run2 from Kandla-Samakhiali examples, run alignment, assert ≥95% of joints align (the actual reports say 4899 vs 4904 joints, so this is a realistic test)

Deliverables:
- src/core/joint_alignment.py with class JointAligner with method align(run1, run2, config) → list of JointMatch
- tests/test_joint_alignment.py with passing tests
- Brief docstring explaining when to fall back to nearest-distance matching (if NW fails because joint lengths are missing in either run)
```

---

## Prompt 4 — Defect matching within joints

```
Build src/core/defect_matcher.py.

The problem: after joints are aligned, within each matched joint pair, find the defect-to-defect correspondence. A single 12m joint might have 50 defects in run 1 and 200 defects in run 2 (better tool resolution). Not every run-1 defect has a run-2 counterpart and vice versa.

Algorithm: Hungarian assignment (scipy.optimize.linear_sum_assignment) within each joint, with iterative tolerance relaxation.

1. For each matched joint pair:
   a) Build cost matrix [n_run1 × n_run2] where cost[i][j] = distance between defect_i and defect_j
   b) Distance metric (configurable in /config/default_project.yaml under matching.distance_metric):
      - axial_distance_weight × |upstream_weld_dist_run1 - upstream_weld_dist_run2| (in meters; default weight 1.0)
      - clock_distance_weight × min(|clock1-clock2|, 12-|clock1-clock2|) (in hours, wraps around; default weight 0.3 per hour)
      - surface_mismatch_penalty: if int vs ext, add 10.0 (essentially forbids cross-surface matching unless no alternatives)
      - depth_shrinkage_penalty: if depth_run2 < depth_run1 × 0.5, add 1.0 (real corrosion doesn't shrink that much)
   c) Apply Hungarian assignment → optimal one-to-one matching
   d) Reject matches where total cost > tolerance threshold (default 0.5)

2. Iterative tolerance relaxation:
   - Pass 1: tight tolerances (default 0.10m axial, 0.5h clock)
   - Pass 2: medium tolerances on remaining unmatched (0.25m, 1.0h)
   - Pass 3: loose tolerances (0.50m, 1.5h)
   - Each pass operates only on remaining unmatched defects from previous passes
   - This catches the easy matches first, then progressively more uncertain ones

3. Cluster-aware mode:
   - If features are clustered (have is_cluster_parent / cluster_parent_id), match clusters-to-clusters first
   - A cluster matches another cluster if their bounding boxes overlap or are within tolerance
   - Then match unmatched individual features

4. Output FeatureMatch list:
   - Each match has score (cost), confidence (0-1 based on score), and the run1/run2 features
   - Unmatched run1 features (likely repaired or below run-2 detection threshold) listed separately
   - Unmatched run2 features (new defects OR below run-1 detection threshold) listed separately — these are the ones that get the "10% depth assumption" downstream

5. Tests in tests/test_defect_matcher.py:
   - Synthetic test: 5 defects in joint, run2 has same 5 with depth+1% each → all 5 match with high confidence
   - Synthetic test: 5 run1 defects, run2 has those 5 plus 100 new ones → all 5 match, 100 are unmatched
   - Synthetic test: 5 defects but 1 is on different surface (int vs ext) → 4 match, 1 left unmatched on each side
   - Real test: load Kandla-Samakhiali run1 (78 metal loss features) and run2 (333), match within joint 6410 — should get the specific feature #125 (28.8% int) matching feature #125 from run1 (which was 12%); both reports document this exact pair as the highest-CGR defect (0.2522 mm/yr)

Deliverables:
- src/core/defect_matcher.py with class DefectMatcher with method match(run1, run2, joint_matches, config) → MatchResult
- tests/test_defect_matcher.py with passing tests including the validation against the Kandla report's documented feature #125
```

---

## Prompt 5 — Corrosion Growth Rate calculation

```
Build src/core/cgr.py.

Implements three CGR methodologies, selectable per-project:

1. FEATURE_SPECIFIC mode:
   - For each matched defect: cgr_mm_yr = max(0, (depth_new_mm - depth_old_mm) / years_between_runs)
   - For unmatched run2 defects: assume depth_old = unmatched_depth_assumption (default 10% WT = tool POD threshold)
   - For unmatched run1 defects: not used in projection (they don't exist in run 2)

2. POPULATION_ONLY mode:
   - Pool all matched defect CGRs
   - Compute the 95th percentile (configurable), separately for internal and external defects
   - Every defect gets the same CGR (its surface's P95)
   - Useful for very sparse matching where individual rates aren't reliable

3. HYBRID mode (recommended default for Indian projects):
   - Compute feature-specific CGRs as in mode 1
   - Compute P95 of internal CGRs and P95 of external CGRs separately
   - For each defect: cgr_used = max(feature_cgr, p95_for_its_surface)
   - This applies a population-level floor without forcing every defect to the floor

QA flags emitted by CGR:
- NEGATIVE_GROWTH: feature got shallower (likely re-measurement error); CGR set to 0
- EXTREME_CGR: feature CGR > 1.0 mm/yr (very fast, deserves attention)
- POPULATION_FLOOR_APPLIED: feature CGR was below the P95 floor and was raised to it
- UNMATCHED_RUN2: depth_old assumed = 10% WT
- DEPTH_BELOW_TOL: depth difference smaller than tool's depth-sizing tolerance (e.g., for general corrosion at 80% confidence the tolerance is ±0.10t, so if depth_diff_mm < 0.10 × wt_mm, the measured "growth" is within tool noise and CGR is unreliable)

Output: list of (Feature, CGRResult) where CGRResult has:
- cgr_mm_yr
- mode_used (feature_specific or population_floor)
- depth_old_used_mm (actual or assumed)
- years_between
- qa_flags list

Tests:
- Synthetic: 100 matched defects, depths grow linearly 1mm over 5yrs → all CGRs ≈ 0.2 mm/yr, P95 ≈ 0.2
- Synthetic: 100 matched + 100 unmatched, hybrid mode → unmatched defects all get P95 of matched population
- Real: Kandla-Samakhiali — pipeline report gives external P95 = 0.0339 mm/yr and internal P95 = 0.0625 mm/yr. Loading run1 and run2, matching them, then running CGR in hybrid mode should reproduce these numbers within ±10% (some methodological wiggle expected because the report combined 23 matched + 310 unmatched with the 10% assumption).

Deliverables:
- src/core/cgr.py with class CGRCalculator with method compute(match_result, years_between, config) → list of CGRResult
- tests/test_cgr.py validating against the Kandla numbers
```

---

## Prompt 6 — FFP engine (B31G + Kastner + RSTRENG + DNV)

```
Build src/core/ffp.py with all four FFP methods.

CRITICAL: every method must be implemented from primary references (ASME B31G-2012, DNV-RP-F101-2017, original Kastner 1986 paper). Add inline references in docstrings.

Methods to implement:

A) B31G_ORIGINAL (most common in India for liquid pipelines)
   - z = L² / (D × t)
   - Sflow = 1.1 × SMYS
   - For z ≤ 20: A/A0 = (2/3)(d/t),  M = sqrt(1 + 0.8z),  Psafe = 1.1 × P × (1 - Q/(1 - Q/M)) where Q = (2/3)(d/t)
   - For z > 20: A/A0 = d/t,  Psafe = 1.1 × P × (1 - d/t)
   - P = 2 × SMYS × Fd × t / D (intact pipe pressure)
   - Returns FFPResult with psafe, erf, m_factor, area_ratio, branch_used (low_z / high_z)

B) B31G_MODIFIED (used by Athena for HMEL projects)
   - Same flow stress as Original but Sflow = SMYS + 69 MPa (≈ SMYS + 10 ksi)
   - z = L² / (D × t) (same)
   - For z ≤ 50: M = sqrt(1 + 0.6275z - 0.003375z²)
   - For z > 50: M = 0.032z + 3.3
   - SF (estimated failure stress) = Sflow × [(1 - 0.85(d/t)) / (1 - 0.85(d/t)/M)]
   - PF (failure pressure) = 2 × SF × t / D
   - Psafe = PF / safety_factor = PF × Fd
   - ERF = MAOP / Psafe

C) RSTRENG (effective area)
   - Requires the actual depth profile of the defect (river-bottom profile)
   - For features without a profile (most ILI data), fallback to "0.85dL approximation":
     - A = 0.85 × d × L (instead of (2/3)dL for B31G or dL for full-length)
     - Otherwise same formula structure as B31G Modified
   - Mark result with `using_approximate_profile=True` since we don't have detailed profiles
   - Used as cross-check method only, not primary

D) DNV-RP-F101 Part B (ASD format — matches design-factor approach used in India)
   - Psafe = (2 × UTS × t / (D - t)) × [(1 - d/t) / (1 - (d/t)/Q)] × Fd_DNV
   - Q = sqrt(1 + 0.31 × (L / sqrt(D × t))²) (this is DNV's "length correction factor", different from B31G's M)
   - UTS is the ultimate tensile strength; if user only knows SMYS, the tool computes UTS = SMYS + 110 MPa (standard estimate for line pipe steels) and warns
   - Fd_DNV is the design factor (same as input Fd)

E) KASTNER (for circumferentially-oriented defects)
   - Used when dimension_class is "Circumferential Slotting" (CISL) or "Circumferential Grooving" (CIGR)
   - σ_failure = (2 × Sflow / π) × [sin(β) - 0.5 × (d/t) × sin(β_d)]
   - β = π × W / (π × D) = W / D (half-angle of defect circumferentially, with W = defect width in mm and D in mm)
   - β_d = β  (for shallow defects, can use simplified form)
   - Failure pressure (longitudinal direction): P_fail_long = (4 × t / D) × σ_failure
   - Note: Kastner gives FAILURE in axial direction, B31G gives failure in hoop direction; for circumferential defects, the lower of the two is the controlling case
   - The tool should compute BOTH B31G and Kastner for circumferential defects and report the lower Psafe

F) Coordinator function ffp_assess(feature, pipeline, project) that:
   - Looks up the right MAOP zone based on feature's WT
   - Runs the primary method specified in project config
   - Runs cross-check methods if specified
   - For circumferential-oriented features, automatically runs Kastner as well
   - Returns list of FFPResult (one per method run); first is the primary

Validation tests:

1. Reproduce known B31G textbook example:
   - 24" pipeline, D=610mm, t=11.91mm, SMYS=358 MPa (X52), MAOP=70 kg/cm²
   - Defect: L=200mm, d=3mm (~25% wt)
   - Expected Psafe (B31G Original) ≈ 165 kg/cm² (textbook value, look up)
   - Assert your implementation produces within ±2%

2. Reproduce Kandla-Samakhiali highest-ERF defect:
   - Feature #125, joint 6410, abs_dist 7453.05m
   - WT=6.4mm, depth=28.75% → d=1.84mm, L=9mm, W=9mm, circumferential pinhole (PINH)
   - MAOP=70 kg/cm², SMYS=358 MPa, Fd=0.72
   - Vendor report says Psafe (B31G Original) = 132.4 kg/cm², ERF = 0.519
   - Your implementation must match these within ±1%

3. Reproduce HMEL IPS1-IPS2 highest-ERF defect (B31G Modified):
   - Feature #209581, joint 77910, abs_dist 94343.7m  
   - WT=8.7mm, depth=25.8% → d=2.24mm, L=1235mm, W=234mm, general corrosion (GENE)
   - MAOP=96.7 kg/cm² (zone 1), SMYS=482 MPa (X70), Fd=0.72
   - Vendor report says ERF = 1.022, Psafe = 78.9 kg/cm²
   - Your B31G_Modified must match within ±2%

4. For each method, test a feature at d/t = 0.80 → expect ERF very close to 1.0 with z_high regime

Deliverables:
- src/core/ffp.py with all five methods + coordinator
- tests/test_ffp.py with all four validation cases + edge cases (d=0, d≈WT, very long defects, very short defects)
- A docs/FFP_METHODS.md explaining each method, source paper, when to use, sample calculation
```

---

## Prompt 7 — Repair date prediction

```
Build src/core/repair_predictor.py.

Project each defect forward in time using its CGR and find when either:
- depth ≥ 80% WT, OR
- Psafe ≤ MAOP (ERF ≥ 1.0)

Whichever fires first is the predicted repair date. Year-by-year simulation, not continuous.

1. For each defect with a CGR:
   year = 0
   current_depth_mm = current_depth_mm
   while year <= max_horizon_years (default 10):
       current_depth_mm += cgr_mm_yr × 1.0
       depth_pct = 100 × current_depth_mm / wt_mm
       if depth_pct >= 80:
           return RepairPrediction(year, depth_pct, trigger="DEPTH_80")
       ffp_result = ffp_assess(feature_with_new_depth, pipeline, project)
       if ffp_result.erf >= 1.0:
           return RepairPrediction(year, depth_pct, trigger="ERF_1.0")
       year += 1
   return RepairPrediction(year=None, trigger="NONE_WITHIN_HORIZON")

2. For long horizons or when no trigger fires:
   - Report "After [horizon]" (e.g., "After March 2033") matching the wording in the example FFP reports
   - Compute final-year depth and ERF for the table

3. For unmatched run-2 defects (those with the 10% assumption):
   - These get the SAME treatment but with the population-floor CGR (since they have no measured rate)
   - Tag them with the QA flag UNMATCHED_RUN2 in the output

4. Two output formats:
   - Annexure E format: matched defects with CGR, current depth, future depth, repair year
   - Annexure F format: all defects ranked by predicted-repair-date, with full pipe-data context

5. Cluster handling:
   - For clustered defects, the cluster's CGR is the max of any child defect's CGR (the cluster ages as fast as its fastest-growing component)
   - The cluster's dimensions stay the same (clustering boundary doesn't grow); only depth grows

6. Tests:
   - Synthetic: defect at 10%, CGR 1mm/yr (very fast), WT 10mm — should hit 80% in year 7
   - Synthetic: defect at 30%, CGR 0.05mm/yr (very slow) — should hit neither trigger in 10 years
   - Real validation against Kandla-Samakhiali Table 6a/6b: "no defects require repair in next 10 years" → run your tool on the same data, all defects should report "After March 2033"
   - Real validation against HMEL: 7 features already have ERF > 1.0 → these should report year=0 (immediate repair)

Deliverables:
- src/core/repair_predictor.py
- tests/test_repair_predictor.py with both synthetic and real validation cases
```

---

## Prompt 8 — QA flag generation

```
Build src/validation/qa_flags.py.

A comprehensive QA flag system. Each flag has:
- code (uppercase identifier, e.g., NEGATIVE_GROWTH)
- severity (INFO, WARN, ERROR)
- feature_id (which defect it applies to, or None for run-level flags)
- message (human-readable)
- context (dict of relevant numbers)

Flag categories:

A) Reader flags (raised by ili_reader):
   - SHEET_NOT_DETECTED: couldn't find a defect sheet
   - HEADER_ROW_AMBIGUOUS: multiple plausible header rows
   - MISSING_COLUMN: a required canonical field has no synonym match
   - SURFACE_VALUE_UNKNOWN: surface value couldn't be parsed (defaulted to UNKNOWN)
   - CLOCK_VALUE_UNKNOWN: clock couldn't be parsed
   - COORDINATES_SWAPPED: lat/lng appear swapped (auto-corrected)

B) Matching flags (raised by joint_alignment, defect_matcher):
   - LOW_JOINT_MATCH_RATE: <90% of joints aligned
   - REVERSAL_DETECTED: matched joints have non-monotonic positions
   - LENGTH_MISMATCH_RUN: total matched length differs >1% between runs
   - LOW_DEFECT_MATCH_RATE: <90% of run1 features have run2 counterparts

C) CGR flags (raised by cgr):
   - NEGATIVE_GROWTH: feature got shallower (CGR clamped to 0)
   - EXTREME_CGR: CGR > 1.0 mm/yr (very high)
   - DEPTH_BELOW_TOL: depth diff < tool tolerance (CGR unreliable, set to 0 with warning)
   - POPULATION_FLOOR_APPLIED: feature CGR raised to P95
   - UNMATCHED_RUN2: depth_old assumed = 10% WT

D) FFP flags (raised by ffp):
   - ERF_EXCEEDS_1: ERF > 1.0 (immediate action required)
   - DEPTH_EXCEEDS_80: depth ≥ 80% WT (mandatory repair)
   - LONG_DEFECT_OUTSIDE_B31G: z >> 50 in B31G Original (use B31G Modified or RSTRENG)
   - VERY_SHORT_DEFECT: L < t (pinhole, ensure method is appropriate)
   - MAOP_ZONE_NOT_FOUND: feature WT doesn't match any MAOP zone (defaulted to most-conservative zone)

E) Pipeline-level flags:
   - REPAIR_PREDICTED_WITHIN_HORIZON: at least one defect needs repair in next N years
   - HIGH_CGR_POPULATION: pipeline-wide median CGR > 0.2 mm/yr (suggests active corrosion)
   - GEOMETRY_DEFECTS_WITH_METAL_LOSS: at least one dent has overlapping metal loss

Each flag should also be exportable to:
- A QA section in the DOCX report
- An "Issues" tab in the Excel deliverable
- A summary count in the GUI dashboard

Tests:
- For each flag, write a test that constructs the triggering condition and verifies the flag fires
- For each flag, write a "negative" test that verifies the flag does NOT fire under normal conditions

Deliverables:
- src/validation/qa_flags.py with all flag classes
- src/validation/flag_aggregator.py that collects flags from all modules and produces a unified report
- tests/test_qa_flags.py
```

---

## Prompt 9 — Annexure E/F writer (Excel output)

```
Build src/reports/annexure_writer.py.

Generate the exact Excel format that Athena PowerTech delivers to clients. Two main format variants:

Format 1 — Annexure E/F (HMEL, modern projects):
- Annexure E sheet: "Run to Run Comparison" with columns:
  S.N. | Anomaly ID | Wall Thickness (mm) | Joint Number | Abs Distance ILI {year_new} | Abs Distance ILI {year_old} | Anomaly Depth (%) ILI {year_new} | Anomaly Depth (%) ILI {year_old} | Anomaly Orientation ILI {year_new} | Anomaly Orientation ILI {year_old} | Anomaly Location ILI {year_new} | Anomaly Location ILI {year_old} | CGR (mm/yr)

- Annexure F sheet: "Metal Loss Anomalies" with columns:
  S.N. | Feature ID | Absolute Distance (m) | Latitude | Longitude | Joint No. | Joint Length (m) | Distance to closest weld (m) | Event | Surface | Wall Thickness (mm) | Orientation (hh:mm) | Reported Depth (%WT) | Length (mm) | Width (mm) | Predicted Repair Year - Effective Repair Date

Both sheets get:
- Title row with project name (merged across columns, bold)
- Sub-title with section name (merged, bold)
- Two-row header (group headers above field headers, with merging where the groups span multiple columns)
- Header row formatted: bold, centered, light blue fill, thin black borders
- Data rows: thin black borders, alternating row colors optional
- Specific cells formatted: dates as DD-MM-YYYY, percentages with 2 decimals, distances with 3 decimals (matching what Athena uses)

Format 2 — Annexure B/C/D (older GAIL projects):
- Annexure B: matched features
- Annexure C: circumferential defects (Kastner)
- Annexure D: all defects (B31G)
- Slightly different column layouts (refer to the IPS_Samakhiali example xls)

Implementation:
- Use openpyxl for write-side
- Build a class AnnexureWriter with method write(match_result, ffp_results, cgr_results, repair_predictions, project, output_path, format="E_F")
- The format flag selects which annexure style to produce
- Borders, fonts, fills, merges all match the example files within visual inspection

Tests:
- Generate output for the Kandla-Samakhiali data and validate:
  - Sheet names match expected
  - Header row content matches expected
  - All defects from the input appear in output
  - Sort order is by absolute distance ascending
  - The actual computed CGR matches what the validated cgr.py produced
- Compare via Python's diff a generated annexure with the example annexure (allow whitespace/format differences but require all data values to be present)

Deliverables:
- src/reports/annexure_writer.py
- tests/test_annexure_writer.py
- Reference screenshots / cell-styling specs in docs/REPORT_FORMATS.md
```

---

## Prompt 10 — Main report (DOCX) writer

```
Build src/reports/main_report_writer.py.

Produces a full Fitness-For-Purpose report DOCX matching the structure of the Athena/HMEL examples we have:
1. Cover page (client logos, project title, document number, revision history)
2. Executive Summary (auto-populated bullets: feature counts, key findings, repair count, max ERF, max depth)
3. Abbreviations
4. Table of Contents (auto-generated)
5. 1. Introduction (Background, Scope, Pipeline Details table)
6. 2. ILI Results (Defect Distributions plots, Discussion, Shape Categorization)
7. 3. Fitness for Purpose Analysis (charts at each WT, narrative)
8. 4. ILI Reports Comparison / CGR Analysis / Repair Prediction
   - 4.1 ILI Reports Assessment
   - 4.2 Repair Prediction Methodology (the standardized text we extracted from the Kandla report)
   - 4.3 Response to pipeline ILI results (API 1160 categories)
   - 4.4 Recommended Repair Methods
   - 4.5 Integrity Management
   - 4.6 Conclusions & Recommendations
9. Disclaimer
10. Annexure A: Guidelines & Formulas (boilerplate, mostly fixed)

Each section has:
- Boilerplate text stored in /templates/ (one .docx file per section, with placeholder tokens like {{PIPELINE_NAME}}, {{TOTAL_DEFECTS}}, {{MAX_ERF}})
- A render function in main_report_writer.py that fills the placeholders

Charts to generate via matplotlib and embed:
- Metal loss defect depth distribution (histogram)
- Metal loss defect length distribution
- Metal loss defect width distribution
- Metal loss orientation (polar / clock position)
- Defect distance from upstream girth weld (histogram)
- Pipeline elevation profile with internal corrosion density overlay
- ERF acceptance chart per WT zone (length vs depth scatter with ERF=1 curve)
- Repair prediction timeline (defects requiring repair plotted by year)

The chart files go in a temporary directory and are embedded with python-docx's add_picture.

Tables to embed:
- Pipeline details table
- Analysis parameters table
- Defect shape categorization (rows: defect class; cols: internal count/%, external count/%, total, %)
- ILI comparison summary (the "ILI 2018 vs ILI 2023" table)
- Corrosion Growth Assessment (upper-bound CGRs by surface)
- Top-20 defects by ERF (Table 6a equivalent)
- Top-20 defects by depth (Table 6b equivalent)

Implementation:
- Use python-docx; build the document by reading a /templates/main_report_template.docx that has the Athena letterhead, fonts, page numbers, headers/footers pre-set
- Then programmatically insert each section's content
- For tables, use a helper that converts pandas DataFrames to nicely-styled docx tables
- Save to {project_name}_FFP_Report_{date}.docx

Tests:
- Generate the full report for Kandla-Samakhiali and HMEL projects
- Verify: file opens in Word without errors, all placeholders are replaced, all charts embedded, page count is roughly 15-30 pages

Deliverables:
- src/reports/main_report_writer.py
- /templates/main_report_template.docx (placeholder; you'll customize this with Athena's actual letterhead later)
- /templates/sections/*.docx with boilerplate text for each section
- tests/test_main_report_writer.py
```

---

## Prompt 11 — PyQt6 desktop GUI

```
Build src/gui/app.py — the main desktop application.

Multi-screen workflow GUI using PyQt6 + qdarktheme for a modern look:

Screen 1: Project Setup
- File browser to load /config/default_project.yaml or any project YAML
- Form fields for: project name, client, pipeline name, diameter, length, material/SMYS, wall thicknesses (list), MAOP zones (with WT/Fd/MAOP per zone), ERF convention, ERF action threshold, CGR mode, FFP primary method, FFP cross-check methods, repair horizon years
- Save / load project config to/from YAML
- All fields validate inline (e.g., diameter must be positive; MAOP zones must cover all WT values)

Screen 2: Data Loading
- Drag-drop or browse for run 1 file
- Drag-drop or browse for run 2 file
- Per-file preview: detected vendor, sheet used, feature count, column mapping (with green checks for matched canonical fields and red Xs for missing)
- Run inspection-date input (for each run)
- Column-mapping override panel: if the auto-detected mapping is wrong, user can manually map any column to any canonical field
- Save the column-mapping override to /config/column_synonyms_override.yaml so it's remembered for next time

Screen 3: Matching
- Run joint alignment + defect matching with progress bar
- Show stats: joint match rate, defect match rate, unmatched counts
- Visualize: a chainage-axis plot showing run1 features (above axis) and run2 features (below axis) with lines connecting matches
- Allow user to manually override a match: select a feature on run1 side, select a feature on run2 side, click "force match"
- Save match overrides

Screen 4: CGR & FFP
- Run CGR + FFP computation with progress bar
- Display QA flags grouped by severity
- Show top-20 most critical features in a table
- Plot: ERF distribution histogram, CGR distribution, defect depth scatter
- Show pipeline-wide statistics: total features, max ERF, max CGR, mean CGR (int/ext)

Screen 5: Reports
- Output directory selector
- Checkboxes for what to generate: Annexure E/F (Excel), Annexure B/C/D (Excel), Main DOCX report, QA Excel
- Generate button with progress bar
- After generation: "Open output folder" button

Sidebar persistent on all screens:
- Project name + client at top
- Run 1 / Run 2 status indicators
- "Save project state" button (saves all overrides, matches, manual corrections to a .pkl file for resume-later)

Implementation notes:
- Use PyQt6.QtWidgets, QtCore signals/slots for inter-screen state
- Theme via qdarktheme (light or dark, user-selectable)
- Long-running operations (matching, FFP) run in a QThread to keep UI responsive
- File reading is also async to handle large files
- All errors go to a status bar + a "View Log" dialog showing full traceback

Tests:
- Use pytest-qt for basic widget existence and signal-emission tests
- Don't test full UI workflows (manual smoke testing for that)

Deliverables:
- src/gui/app.py with the main window
- src/gui/screens/*.py with one file per screen
- src/gui/widgets/*.py with reusable widgets (file drop zone, mapping table, etc.)
- tests/test_gui.py with basic widget tests
- A bin/run_gui.py script to launch the app
```

---

## Prompt 12 — Packaging for Windows

```
Build the Windows distribution pipeline.

1. PyInstaller spec file at packaging/build_windows.spec:
   - One-folder distribution (faster startup than one-file)
   - Include /config/, /templates/ as data files (read-only at runtime)
   - Bundle all PyQt6 dependencies
   - Custom .ico file for the app
   - Hidden imports list for things PyInstaller misses (pandas extension modules, openpyxl, xlrd)
   - Console-less by default (windowed)

2. Inno Setup script at packaging/installer.iss:
   - Installer that creates Start Menu shortcuts, desktop shortcut
   - Default install location C:\Program Files\Athena ILI FFP Tool
   - Per-user config storage at %APPDATA%\Athena\ILI_FFP_Tool\
   - Uninstaller cleans up correctly
   - Version info, publisher (Athena PowerTech LLP), license

3. Build script at packaging/build.bat:
   - Cleans previous build
   - Runs PyInstaller
   - Runs Inno Setup
   - Outputs Setup_AthenaIliFfp_v0.1.0.exe to /dist/

4. Continuous integration via GitHub Actions at .github/workflows/build_windows.yml:
   - Triggers on tag push (v0.x.x)
   - Runs on windows-latest
   - Installs Python, PyInstaller, Inno Setup
   - Builds installer
   - Uploads as release artifact

5. Code-signing hook in installer.iss (commented; the user will add their actual cert later)

Deliverables:
- packaging/ directory with all of the above
- docs/BUILD_INSTRUCTIONS.md explaining how to produce a Windows installer locally
```

---

## Prompt 13 — Regression test suite

```
Build tests/test_regression.py — the integration test suite that runs the full tool against all 4 example projects and validates outputs match the documented FFP results.

For each example project:

1. GAIL Samakhiali → IP-01 (Annexure B/C/D format)
   - Inputs: pipe tally files (when available; if only the FFP output is available, use that for validation only)
   - Expected outputs to validate:
     - Match rate ≥ 90%
     - Top features by ERF match the published values within ±3%
     - Annexure B/C/D output passes openpyxl-load + sheet name check

2. HMEL COT → IPS-01 (Annexure E/F format)
   - Expected: 15,572 matched features (from the FFP report)
   - Mean CGR ≈ 0.084 mm/yr
   - Top ERF features match published ±3%

3. HMEL IPS-1 → IPS-2 (Annexure E/F format, 3 MAOP zones)
   - Inputs: run1 (2019), run2 (2025)
   - Expected: 7 features with ERF > 1.0 (per main report executive summary)
   - Top 10 features by ERF match the severity table in the main report ±2%
   - 3 MAOP zones correctly assigned by WT

4. GAIL Kandla → Samakhiali LPG (Annexure C/D format, hybrid CGR)
   - Inputs: run1 (2018), run2 (2023)
   - Expected: 0 defects require repair in 10 years
   - External P95 CGR ≈ 0.0339 mm/yr
   - Internal P95 CGR ≈ 0.0625 mm/yr
   - Crack-like feature at joint 30490 detected and flagged

Each test:
- Loads the inputs
- Runs the full pipeline (read → align → match → CGR → FFP → predict → report)
- Validates the documented numbers within tolerances
- Asserts that the output Excel and DOCX files exist and are openable

Deliverables:
- tests/test_regression.py with one test_method per project
- /examples/expected_results/*.yaml with the documented values to validate against
- A make target / batch script that runs the full regression suite
```

---

## After all prompts complete

You will have:
- A complete, working desktop tool
- 4 validated regression projects
- A Windows installer
- Documentation
- Tests at every layer

To customize for a new project:
1. Open the GUI, create a new project config (or copy an existing one)
2. Set MAOP zones, FFP method, CGR mode per the client's specification
3. Load run 1 and run 2 files (any format — column synonyms will detect)
4. Run the full pipeline through the GUI
5. Output annexures and DOCX go to your chosen folder

To add support for a new vendor's format:
1. Open one of their files, note the column names
2. Add the column-name variants to /config/column_synonyms.yaml under the appropriate canonical field
3. Re-run — no code changes needed

To add a new FFP method:
1. Add the method to FFPMethod enum
2. Implement the function in src/core/ffp.py
3. Register it in the coordinator
4. Add to FFP method choice in GUI
