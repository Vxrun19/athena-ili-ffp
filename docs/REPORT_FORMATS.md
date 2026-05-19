# Excel annexure formats — structure + styling reference

This document captures the *exact* structure of the two Excel annexure
formats Athena PowerTech delivers, read off the published reference
workbooks in `/examples/`. `src/reports/annexure_writer.py` implements
both formats per the tables below; `tests/test_annexure_writer.py` pins
every assertion.

The reference workbooks:

| Format | Reference file | Sheets | Notes |
| --- | --- | --- | --- |
| **E/F** | `FFP_Report_IPS_1_to_IPS2_Annexure_E__F___r4.xlsx` | Annexure E, Annexure F | Modern HMEL deliverable. Primary format. |
| E/F | `FFP_Report_COT_to_IPS_1_Annexure_E__F.xlsx` | Annexure E, Annexure F | Second reference; same structure. |
| **B/C/D** | `IPS__Samakhiali_to_IP_01_-_Annexure_B__C__D_FFP_.xls` | Annexure B, C, D | Older GAIL Samakhiali-style deliverable. File is XLSX-by-magic-bytes despite the `.xls` extension. |

---

## Format E/F (modern HMEL)

### Annexure E — Run to Run Comparison

Sheet name: `Annexure E`. **13 columns** (A–M).

#### Row layout

| Row | Purpose | Merges | Notes |
| --- | --- | --- | --- |
| 1 | Title | `A1:M1` | "Annexure E: Run to Run Comparison" |
| 2 | Group headers | `A2:D2`, `E2:F2`, `G2:H2`, `I2:J2`, `K2:L2` | 5 group spans + unmerged M |
| 3 | Field headers | none | Single-row labels |
| 4+ | Data | none | One row per run-2 feature, sorted by abs_dist ascending |

#### Row 2 group-header text

| Merge | Cell value |
| --- | --- |
| `A2:D2` | `Feature Detail as per ILI {year_new}` |
| `E2:F2` | `Abs. Distance, (m)` |
| `G2:H2` | `Anomaly Depth, (%)` |
| `I2:J2` | `Anomaly Orientation` |
| `K2:L2` | `Anomaly Location` |
| `M2` | (blank — CGR header lives on row 3 only) |

`{year_new}` is the 4-digit year of `Project.run_2.inspection_date`
(e.g. `2023` for Kandla). `{year_old}` is `Project.run_1.inspection_date.year`.

#### Row 3 field headers

| Col | Header | Source field | Format |
| --- | --- | --- | --- |
| A | `S.N.` | row index (1..N) | `0` |
| B | `Anomaly ID` | `Feature.anomaly_id` | `@` (text) |
| C | `Wall Thickness, (mm)` | `Feature.wt_mm` | `0.0` |
| D | `Joint Number` | `Feature.joint_number` | `0` |
| E | `ILI {year_new}` | run-2 `abs_distance_m` | `0.000` |
| F | `ILI {year_old}` | run-1 `abs_distance_m` (`None` if unmatched) | `0.000` |
| G | `ILI {year_new}` | run-2 `depth_pct_wt` | `0.00` |
| H | `ILI {year_old}` | run-1 `depth_pct_wt`, falls back to the 10 % assumption when unmatched | `0.00` |
| I | `ILI {year_new}` | run-2 orientation as `hh:mm:ss` text | `@` |
| J | `ILI {year_old}` | run-1 orientation, blank when unmatched | `@` |
| K | `ILI {year_new}` | run-2 surface as `int.` / `ext.` | `@` |
| L | `ILI {year_old}` | run-1 surface, blank when unmatched | `@` |
| M | `CGR (mm/yr)` | `CGRResult.cgr_mm_yr` | `0.0000` |

### Annexure F — Metal Loss Anomalies

Sheet name: `Annexure F`. **16 columns** (A–P).

#### Row layout

| Row | Purpose | Merges | Notes |
| --- | --- | --- | --- |
| 1 | Title | `A1:P1` | "Annexure F: Metal Loss Anomalies" |
| 2 | Pipeline name | `A2:P2` | e.g. "GAIL (India) Limited" |
| 3 | Section name | `A3:P3` | e.g. "Kandla-Samakhiali 10\" LPG" |
| 4 | Field headers | none | Single-row labels |
| 5+ | Data | none | One row per run-2 feature, sorted by abs_dist ascending |

#### Row 4 field headers (left-to-right)

`S.N.` · `Feature ID` · `Absolute Distance [m]` · `Latitude` · `Longitude` · `Joint No.` · `Joint Length (m)` · `Distance to closest weld (m)` · `Event` · `Surface` · `Wall Thickness [mm]` · `Orientation (hh:mm)` · `Reported Depth [% WT]` · `Length (mm)` · `Width (mm)` · `Predicted Repair year- Effective Repair Date`

Note: the reference files contain the typo `closet weld` instead of
`closest weld`. We use the correct spelling in our output.

#### Repair date column rendering

* Trigger fired within horizon → `dd-mm-yyyy` of `predicted_repair_date`
* `NONE_WITHIN_HORIZON` → `After {Month YYYY}` of the horizon end
* No `Project.run_2.inspection_date` set → `After horizon`

---

## Format B/C/D (older GAIL Samakhiali)

### Annexure B — Matched defects only

| Row | Purpose | Notes |
| --- | --- | --- |
| 1 | Title `B1:I1` merged | "ANNEXURE -  B" |
| 2-3 | Subtitle | "Results of ILI Comparison ({section})" |
| 4-5 | Notes block | Free-text |
| 6 | Group headers | `F6:G6` (Feature Depth %), `H6:I6` (Wall side) |
| 7 | Sub-header | Year labels under depth and wall-side groups |
| 8+ | Data | Matched run-2 features only |

#### Columns (A–J)

`S.N.` · `Feature ID` · `Joint number` · `Absolute distance` · `Wall Thickness` · `Feature Depth %` (ILI new) · `Feature Depth %` (ILI old) · `Wall side` (ILI new) · `Wall side` (ILI old) · `CGR (mm/yr)`

### Annexure C — B31G (all defects, current + 10-year)

| Row | Purpose | Merges |
| --- | --- | --- |
| 1 | Title | `A1:M1` |
| 2 | Subtitle | `A2:M2` |
| 3 | Group headers | `F3:G3` (Feature Depth), `H3:I3` (SOP), `J3:K3` (ERF) |
| 4 | Field headers — uses ISO date strings of run-2 and horizon-end as the year-now / +10 labels |
| 5+ | Data |

#### Columns (A–M)

`S.N.` · `Feature ID` · `Joint No.` · `Chainage-(mtrs)` · `Surface` · `Depth now` · `Depth +10` · `SOP now` · `SOP +10` · `ERF now` · `ERF +10` · `CGR (mm/yr)` · `Repair Date`

Note: in the published reference file Annexure C uses **B31G Original**,
not Kastner. The user-spec wording had this flipped; the tool follows
the *file*, not the spec wording, because the audit chain matters.

### Annexure D — Kastner (circumferential defects only)

Same structure as Annexure C. Subtitle reads `Kastner Approach`.
Population filtered to features whose controlling FFP method is
`FFPMethod.KASTNER` (i.e. `dimension_class ∈ {CISL, CIGR}`).

---

## Cell styling (both formats)

| Element | Font | Fill | Alignment | Border |
| --- | --- | --- | --- | --- |
| Title row | bold, 12pt | `FFC000` (yellow/orange) | center, wrap | thin |
| Sub-title row | bold, 11pt | none | center, wrap | thin |
| Group header row | bold, 10pt | `BDD7EE` (light blue) | center, wrap | thin |
| Field header row | bold, 10pt | `BDD7EE` | center, wrap | thin |
| Data row | regular, 10pt | none | center | thin |

Borders are `Side(style='thin', color='000000')` on all four sides of
every styled cell.

Freeze panes: data area is frozen so the headers stay visible while
scrolling (`A4` for Annexure E, `A5` for Annexure F).

---

## QA Issues sheet

Appended when a `FlagReport` is passed to `AnnexureWriter.write(...)`.

| Row | Content |
| --- | --- |
| 1 | Title `A1:E1` merged — "QA Issues" (title styling) |
| 2 | `FlagReport.summary` `A2:E2` merged (subtitle styling) |
| 3 | Column headers: Severity / Code / Feature / Source row / Message |
| 4+ | Flag rows, sorted ERROR → WARN → INFO, then by code |

---

## Validation against the published reference (Kandla #125)

The canonical pinning test in `test_annexure_writer.py`. After the full
chain runs on the Kandla-Samakhiali pair, Annexure E row for feature
#125 contains:

| Column | Value |
| --- | --- |
| WT | 6.4 |
| Joint No. | 6410 |
| Abs Distance ILI 2023 | 7453.053 |
| Abs Distance ILI 2018 | 7426.979 |
| Depth (%) ILI 2023 | 28.75 |
| Depth (%) ILI 2018 | 12 |
| Orientation ILI 2023 | 05:08:00 |
| Orientation ILI 2018 | 05:18:00 |
| Surface ILI 2023 | int. |
| Surface ILI 2018 | int. |
| CGR (mm/yr) | **0.2522** |

This matches the published Athena report exactly (CGR 0.2522 mm/yr was
the canonical reconciliation target from Prompt 5).
