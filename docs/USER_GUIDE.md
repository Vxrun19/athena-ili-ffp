# Athena ILI FFP Tool — User Guide (v0.3.6)

## What this tool does

The Athena ILI FFP Tool reads two in-line inspection (ILI) pipe-tally Excel files (an earlier "run 1" and a later "run 2") and produces a Fitness-For-Purpose (FFP) report assessing every metal-loss defect on the pipeline. The tool computes a per-feature corrosion growth rate from the two runs, evaluates the safe operating pressure at the run-2 state using one of five standard methods (ASME B31G Original / Modified, RSTRENG, DNV-RP-F101, Kastner), projects each defect forward in time to predict the year it will reach the repair threshold, and emits both an Excel annexure (matching Athena's standard E/F or B/C/D format) and a Word DOCX main report. A QA flag system surfaces any data-quality or methodology concerns. All thresholds, MAOP zones, and methodology choices are controlled by a single project YAML file.

---

## Quick Start: from 3 files to FFP report

When Athena receives a project, you typically get three files from the vendor:

1. **Run-1 ILI file** — older inspection (Excel pipe tally)
2. **Run-2 ILI file** — newer inspection (Excel pipe tally; usually NGP format)
3. **Final Report PDF** — the vendor's deliverable, with the pipeline metadata in standard tables

The tool's Quick Start uses all three:

### Step 1 — Browse Run-1
Project Setup → click **Browse…** next to *Run 1 (older / baseline)*. Pick the older pipe-tally file. If it's already in NGP/Athena format the reader accepts it silently. If it's in a foreign vendor format (Rosen, Baker Hughes, NDT Global, Onstream, …) a yellow banner appears with a **Convert it →** button — that jumps you to the Convert Format screen, where you map the columns once and save a profile for future re-use.

### Step 2 — Browse Run-2
Same as Step 1, for the newer inspection file. Run-2 is almost always in NGP format already (Athena runs the inspection), so this is usually a silent accept.

### Step 3 — Auto-fill from Final Report PDF
Click the primary blue **⤵ Auto-fill from Final Report PDF…** button in the toolbar. Pick the vendor's Final Report PDF. The tool reads the standard Pipeline-Data table (page ~6) and Strength-Calculation Summary (page ~20) and extracts ~12 fields automatically:

- Project / pipeline / client / vendor / inspection technology
- Outer diameter, pipeline length
- Wall thicknesses (one MAOP zone is created per WT)
- Material grade, SMYS
- MAOP (kg/cm²), design factor
- Run-2 inspection date (full or year-only)

A preview dialog shows everything found, with a confidence score per field. **Apply to form** populates the fields; **Cancel** leaves the form untouched. Low-confidence fields (confidence < 0.7) are highlighted yellow in the form so you can sanity-check them against the PDF.

### Step 4 — Verify Run-1 inspection date
The Final Report PDF describes the *Run-2* inspection. Run-1's date isn't in the PDF, so it's the one field you still have to set by hand. Pick the date from the Run-1 calendar widget — the **Years between runs (auto-calculated):** label updates live.

### Step 5 — Validate → Proceed → Run
Click **Validate** (errors surface at the bottom of the form). Then **Proceed to Analysis →**. On the Run Analysis screen, click **Run analysis**. ~10 seconds later you'll have an Excel annexure + DOCX report in `./output/` (or wherever you point the output dir).

**Net result:** what used to be a 15-field manual form is now 1 button + 1 calendar pick.

### What the PDF parser does NOT extract

- **Run-1 inspection date** — not in the Run-2 vendor's Final Report
- **Run-1 vendor / tool type** — likewise
- **Service class (gas/liquid)** — sometimes implied by product type but not always; leave the default or set explicitly
- **Per-zone MAOP variations** — the parser creates one MAOP zone per wall thickness with all the same MAOP / design factor. If different WTs have different MAOPs, edit the table manually after auto-fill.

If your file isn't Athena/NGP format (e.g. a Rosen or PII Final Report), the parser falls back to "nothing recognised" and logs a note. Use the manual form in that case — a future release will add per-vendor PDF templates.

---

## Project YAML — annotated example

Save the following as `kandla_project.yaml` (the installer puts a copy at `%APPDATA%\Athena\ILI_FFP_Tool\projects\kandla_project.yaml`):

```yaml
project:
  project_name: "FFP_Kandla_Samakhiali_10in_LPG"   # used in output filenames
  pipeline_name: 'Kandla-Samakhiali 10" LPG'       # appears on the DOCX cover page
  client_name:   "GAIL (India) Limited"            # also on cover page
  report_number: "ATH-KS-2023-001"
  report_revision: "00"
  prepared_by:   "Athena PowerTech LLP"
  reviewed_by:   ""                                # left empty until reviewed
  approved_by:   ""

pipeline:
  diameter_mm:    273.0                            # 10" nominal OD; use 711 for 28", etc.
  length_km:      58.5
  install_year:   2011
  material_grade: "API 5L X52"                     # determines SMYS via the lookup table below
  smys_mpa:       358.0                            # X52 = 358 MPa; X70 = 482 MPa; etc.
  product:        "LPG"                            # crude oil / refined product / LPG / natural gas / water
  service_class:  "liquid"                         # liquid (B31.4) or gas (B31.8)

maop_zones:                                        # list one zone per WT-range bucket
  - wt_mm_min:      6.0                            # this zone applies to features with WT in [6.0, 8.0]
    wt_mm_max:      8.0
    design_factor: 0.72                            # B31.4 liquid default
    maop_kgcm2:    70.0                            # operator's MAOP for this zone

  # For HMEL-style 3-zone pipelines, add more entries:
  # - {wt_mm_min: 10.3, wt_mm_max: 11.1, design_factor: 0.60, maop_kgcm2: 84.1}
  # - {wt_mm_min: 11.9, wt_mm_max: 14.3, design_factor: 0.50, maop_kgcm2: 80.6}

runs:
  run_1:
    file_path:       "examples/10_inch_Kandla...Rev_0.xlsx"
    inspection_date: "2018-12-15"                  # ISO 8601 — used to compute years_between
    vendor:          "Athena PowerTech / NGP"
    tool_type:       "MFL-A"
  run_2:
    file_path:       "examples/1ZSV_Pipeline_Listing.xlsx"
    inspection_date: "2023-03-15"
    vendor:          "GAIL / NGP"
    tool_type:       "MFL-A + MFL-C"

cgr:
  mode: "hybrid"                                   # feature_specific / hybrid / population_only

ffp:
  primary_method: "B31G_Original"                  # the method whose output drives Annexure E + the
                                                   # repair-prediction projection
  cross_check_methods: []                          # optional list, e.g. ["B31G_Modified", "DNV_RP_F101"]
  kastner_for_circumferential: true                # auto-run Kastner for CISL/CIGR defects

repair_prediction:
  horizon_years: 10                                # projection horizon for the repair-year calc
```

### Picking the right CGR mode

| Mode | When to use |
| --- | --- |
| `feature_specific` | High-quality matched-pair data (~70 %+ of run-1 features have run-2 matches). Each defect uses its own measured growth rate. |
| `hybrid` (default for Indian projects) | Most realistic. Floors each defect's CGR at the population P95 — keeps slow-looking defects from escaping the typical population growth. |
| `population_only` | Very sparse matched data (<20 %). Every defect gets the surface's P95. |

### Picking the FFP method

| Method | Project type |
| --- | --- |
| `B31G_Original` | Indian liquid lines (B31.4), default historic. Used by GAIL Kandla-Samakhiali. |
| `B31G_Modified` | Long defects (z > 50) or HMEL-style. Default for HMEL IPS1-IPS2. |
| `RSTRENG` | When a measured river-bottom depth profile is available (rare in POF deliveries; the tool falls back to 0.85·dL = B31G Modified otherwise). |
| `DNV_RP_F101` | Cross-check / European jurisdiction. Uses UTS instead of flow stress. |
| `Kastner` | Auto-run for circumferentially-oriented defects (dimension_class CISL/CIGR). |

---

## Running an analysis

From any CMD window with the tool on PATH (the Start Menu's "Run FFP Analysis (CMD)" shortcut does this for you):

```cmd
athena_ili_ffp --config "C:\path\to\kandla_project.yaml" --output-dir "C:\Users\<you>\Desktop\out"
```

Useful flags:

| Flag | Effect |
| --- | --- |
| `--version` | Print version banner and exit |
| `--config <path>` | Required: path to the project YAML |
| `--output-dir <path>` | Where the annexure + DOCX land (default `./output`) |
| `--run1 <path>` / `--run2 <path>` | Override the YAML's file paths (handy for one-off runs) |
| `--years <N>` | Override the date-derived years between runs |
| `--annexure-format E_F` or `B_C_D` | Annexure layout (default E/F = modern HMEL style) |
| `--no-docx` | Skip the DOCX report (annexure only — faster) |
| `--quiet` | Suppress per-stage progress lines |

Typical Kandla run takes ~5–10 seconds. HMEL-scale projects (~100k features) take ~2 minutes.

---

## Interpreting the output

The tool writes two files to `--output-dir`:

```
<project_name>_annexure.xlsx
<project_name>_report.docx
```

### Annexure Excel — sheets

| Sheet | Purpose |
| --- | --- |
| **Annexure E** | Run-to-run defect comparison. One row per run-2 defect with: anomaly ID, joint, WT, abs distance + depth + orientation + surface for BOTH runs, and the computed CGR. Sorted by abs distance. |
| **Annexure F** | The per-defect deliverable. Feature ID, abs distance, lat/lon, joint, joint length, distance to closest weld, event class, surface, WT, orientation, depth, length, width, predicted repair date. |
| **QA Issues** | Every flag the tool raised, sorted ERROR → WARN → INFO. See "How to read the QA flags" below. |

For older GAIL projects, use `--annexure-format B_C_D` instead — produces three sheets (B, C, D) matching the Samakhiali deliverable format.

### DOCX main report — sections

```
Cover page (project metadata + revision history)
Executive Summary
  - feature counts
  - max ERF / max depth
  - "Overall verdict: No defects require repair…" OR "Defects flagged for action."
Abbreviations
Table of Contents          (right-click → Update Field in Word to refresh)
1. Introduction
2. ILI Results              (charts: depth/length/orientation distributions)
3. Fitness-For-Purpose      (ERF-acceptance scatter charts per WT zone)
4. CGR + Repair Prediction  (CGR P95 table, top-20 by ERF, top-20 by depth, repair timeline)
Disclaimer
Annexure A — Guidelines and Formulas
```

### Key numbers to look at first

1. **Executive summary → "Features with ERF ≥ 1.0 today"** — anything > 0 here is an immediate-action defect. Cross-check against the Top-20 by ERF table in §4.
2. **Executive summary → "Features requiring repair within the 10-year horizon"** — the cumulative count over the projection. 0 = continue operation; non-zero = see §4.3 for the response breakdown.
3. **§4 Table 5 — Upper-bound Corrosion Growth Rates** — the internal/external P95 CGR. Compare to historical baselines for this pipeline (typical range 0.02–0.10 mm/yr for Indian crude/LPG lines).
4. **§4 Table 6a — Top-20 by ERF** — the worst-case defects. The first row's feature ID is the one the engineering team will look at first.

---

## How to read the QA flags

Flags appear in three places:

1. **QA Issues sheet** in the annexure Excel — full list, sortable.
2. **Executive Summary** in the DOCX — one-line `QA: …` summary.
3. **CLI exit code** — `0` if no ERROR-severity flags, `1` if any.

### Severity

| Severity | Meaning | Action |
| --- | --- | --- |
| **ERROR** | A critical condition is already true (ERF ≥ 1.0, depth ≥ 80 % WT, required column couldn't be parsed). | Review before issuing the report. The CLI exit code will be 1 — your build pipeline / Slack bot / etc. should treat this as a failure. |
| **WARN** | A value is at the edge of its calibration or a methodology assumption was loosened. | Worth reviewing before finalising. |
| **INFO** | Expected behaviour, documented for the audit trail. | No action — just part of the record. |

### Common flags and what they mean

| Code | Severity | What it means |
| --- | --- | --- |
| `ERF_EXCEEDS_1` | ERROR | The defect's predicted Psafe ≤ MAOP. Immediate engineering review required. |
| `DEPTH_EXCEEDS_80` | ERROR | Mandatory repair per the depth-only criterion (80 % WT). |
| `LOW_DEFECT_MATCH_RATE` | WARN | Fewer than 90 % of run-1 features have run-2 partners. Common for pipelines with sparse run-1 detection; review match parameters. |
| `EXTREME_CGR` | WARN | A defect grew faster than 1.0 mm/yr. Could be real, could be a re-measurement issue — spot-check. |
| `LONG_DEFECT_OUTSIDE_B31G` | WARN | A B31G Original defect with z > 50 — outside its calibration. Switch to B31G Modified or RSTRENG. |
| `MAOP_ZONE_NOT_FOUND` | WARN | A defect's WT doesn't match any explicit MAOP zone — tool used the nearest zone. Add the missing range to the YAML. |
| `COORDINATES_SWAPPED` | INFO | Lat/lon were swapped in the source file; tool auto-corrected. |
| `RECONSTRUCTED_JOINT_CONTEXT_FROM_CHAINAGE` | INFO | The source rows weren't chainage-monotonic; tool used binary search against weld anchors. |
| `UNMATCHED_RUN2` | INFO | Run-2 features assumed to have been at 10 % WT in run-1 (the tool POD threshold) — one flag per unmatched feature. |
| `NEGATIVE_GROWTH` | INFO | A matched defect appears shallower in run-2 than run-1. CGR clamped to 0; the apparent shrinkage is within tool tolerance. |
| `POPULATION_FLOOR_APPLIED` | INFO | A defect's CGR was below the surface P95; the population floor was applied (hybrid mode). |

The full taxonomy is in `src/validation/qa_flags.py`.

---

## Troubleshooting

### "ERROR: no MAOP zone matches WT X.X mm"

The defect's measured WT doesn't fall in any `maop_zones` entry. The tool uses a nearest-zone fallback when configured zones overlap or cover most of the range, but if your YAML has a gap between zones, defects in the gap will hit this. Add the missing WT range to the YAML.

### "ERROR: column missing: depth_pct_wt"

The reader couldn't find a depth column in the pipe-tally Excel. Check `config/column_synonyms.yaml` — if your vendor uses a column name we haven't seen yet, add it to the `depth_pct_wt.synonyms` list. The reader is config-driven; no code change should be needed for new vendor formats.

### "RECONSTRUCTED_JOINT_CONTEXT_FROM_CHAINAGE" on every run

This isn't an error — it's an INFO flag. It means your pipe tally has anomaly rows in non-chainage order (common in NGP single-sheet 2019 files). The reader uses binary search against weld anchors instead of forward-fill. The data is correct.

### Annexure E shows 333 rows but I expected 23

That's the matched + unmatched count. Annexure E lists every run-2 defect, including those without a run-1 partner (which get the 10 % WT depth assumption). To see only matched-pair rows, use `--annexure-format B_C_D` — Annexure B in that format is matched-only.

### DOCX report shows `[KEY not set]` in the body

A template placeholder wasn't filled. Typically because a required field was missing from the project YAML (e.g. `material_grade` was left empty). Open the YAML, fill the field, re-run.

### Charts in the DOCX look blurry

Open `Setup_AthenaIliFfp_v0.1.0.exe` was built without the matplotlib font cache; the bundled DOCX rendering uses the same fonts as Office on the target machine. If the charts are off, install the Calibri font (default on Windows) and re-open in Word.

### "ModuleNotFoundError: No module named '<x>'" at first launch

PyInstaller missed a dynamic import. Add the module name to the `hiddenimports` list in `packaging/build_windows.spec` and rebuild. The full build process is in `docs/BUILD_INSTRUCTIONS.md`.

### Annexure has the wrong year in the column headers

Annexure E group headers use the year from `runs.run_1.inspection_date` and `runs.run_2.inspection_date` in the project YAML. Check both dates are set in ISO format (e.g. `"2018-12-15"`).

### Tool runs but uses 4+ GB of RAM on HMEL-scale data

Expected — the 1YCF Excel sheet has 136 k rows. Cumulative memory peak is ~600 MB during read, dropping back after. If your machine has <4 GB free, close other applications first.

---

## Next steps

* Read `docs/REPORT_FORMATS.md` for the Excel column-by-column structure and styling.
* Read `docs/FFP_VALIDATION.md` for the validation audit trail and known limitations.
* Edit the boilerplate text in `templates/sections/*.txt` to match Athena's standard wording — those files are plain text and survive across version updates (the installer doesn't overwrite them after the first install if you've edited them).
