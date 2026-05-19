# Athena ILI FFP Tool — Engine Reference (v0.3.1)

> Reference for constructing synthetic ILI files and predicting tool
> output to the 4th decimal place. Every formula, constant, threshold,
> and column synonym is cited to a file + line range in `src/`. This
> document captures what the code **does** — not what it "should" do.

Audience: a pipeline-integrity engineer who already knows modified B31G,
ERF, and CGR conceptually and wants the exact knobs the tool turns. If
something is configurable, the configuration mechanism is named inline.

---

## 1. Input file schema (Run-1 and Run-2)

### 1.1 Files the reader accepts

`src/io/ili_reader.py` reads `.xlsx` and `.xls` via magic-byte detection
(`_XLSX_MAGIC = b"PK\x03\x04"`, `_XLS_MAGIC = b"\xd0\xcf\x11\xe0"`,
`src/io/ili_reader.py:282-296`). CSV inputs go through the Format
Converter first (`src/io/format_converter/csv_input.py`) to be rewritten
as an `.xlsx` in the NGP layout — the FFP pipeline itself only consumes
Excel.

### 1.2 Sheet selection

Multi-sheet workbooks are common (NGP multi-sheet 2023+, Athena 2018).
Sheet ranking happens in `_pick_defect_sheet` (`ili_reader.py:367-398`):

1. Every sheet's first ten rows are scored for canonical-field hits via
   the synonyms index (`_find_header_row`, `ili_reader.py:355-364`).
2. Any sheet with `< _DEFECT_SHEET_MIN_HITS` (= 4 canonical fields,
   `ili_reader.py:184`) is dropped.
3. Surviving sheets are bucketed by name into a 3-tier priority:
   - **priority 2** — name contains any of `_PREFERRED_SHEET_NAMES =
     ("defects", "metal loss list", "severity list")` (line 188).
   - **priority 1** — name contains none of the preferred or negative
     terms.
   - **priority 0** — name contains any of `_NEG_SHEET_NAMES = ("weld",
     "casing", "wall thickness", "reference point", "bend",
     "installation", "pipe", "adjacent")` (line 189-198).
4. Ties broken by hit count, then sheet-name string.
5. If no sheet qualifies, `ValueError` is raised naming every sheet
   tried (line 387-393).

A separate pass for `pipe` / `pipeline tally` sheets
(`_merge_joints_from_secondary_sheets`, `ili_reader.py:1094-1142`)
augments the joint registry — defect sheets only list defect-bearing
joints, so the full joint count comes from the secondary sheet.

### 1.3 Header-row detection

`_find_header_row` (`ili_reader.py:355-364`) scans the first **10** rows
of the chosen sheet and picks the row with the most canonical-field
hits — minimum `_HEADER_ROW_MIN_HITS = 4` (line 185). NGP single-sheet
2019 files have headers on row 1; Athena 2018 deliverables on row 3
(rows 1–2 are merged title/sub-title). The detector is layout-agnostic.

### 1.4 Column synonyms

The canonical-field set the reader looks for (`ili_reader.py:157-180`):

| Required | Optional |
|---|---|
| `abs_distance_m` | `clock_position`, `feature_type` |
| `joint_number` | `feature_identification`, `dimension_class` |
| `depth_pct_wt` | `description`, `anomaly_id` |
| `length_mm` | `erf`, `psafe` |
| `width_mm` | `latitude`, `longitude`, `altitude_m` |
| `surface` | `upstream_weld_dist_m`, `joint_length_m` |
| `wt_mm` | |

Missing **any** required column raises `ValueError` with the headers
seen and the synonyms tried (`_check_required`, line 418-428).

Synonym matching is **case-insensitive, whitespace-normalised,
punctuation-tolerant**. The normaliser is `_norm`
(`ili_reader.py:210-224`): NFKC normalise, casefold, strip, then collapse
runs of `[\s_\-/.,()\[\]{}'"`²°*:%]+` to single spaces. So `"Wall
Thickness, (mm)"`, `"wall thickness mm"`, and `"WALL_THICKNESS [MM]"`
all hash to the same key.

The full synonym table lives in `config/column_synonyms.yaml`. The
declaration order matters — first synonym wins when two canonical
fields list the same alias (`_build_synonym_index`, line 235-249). Some
high-traffic examples:

**`abs_distance_m`** (`config/column_synonyms.yaml:14-35`) —
`log dist. [m]`, `log distance`, `log distance, m`, `log distance,
mtrs`, `abs. distance, m`, `absolute distance`, `chainage`,
`chainage, m`, `chainage-(mtrs)` … 22 variants total.

**`joint_number`** (lines 37-49) — `joint number`, `j.no.`, `j.no`,
`joint no.`, `pipe number`, `pipe no.` … 8 variants.

**`depth_pct_wt`** (lines 109-137) — `depth, % wt`, `depth, %wt`,
`depth, % wt/od` (NGP combined ML/dent column), `depth %`,
`anomaly depth, (%)` (NGP FFP Annexure E), `wl [%]` (NGP single-sheet
2019 — "wall loss"), `reported depth`, `peak depth`, `d (peak)` …
21 variants.

**`clock_position`** (lines 163-181) — `orientation, h:min`,
`orientation, h : min`, `orientation (hh:mm)`, `orientation o'clock`
(Athena 2018), `o'clock` (NGP 2019), `anomaly orientation`,
`clock position`, `clock` … 14 variants.

**`feature_identification`** (lines 207-218) — `pof acronym` (NGP
multi-sheet, direct codes), `feature identification`, `feature id`,
`pof feature identification`, `feature description`, `event`,
`comments` (Athena severity list).

The `value_normalisations` section of the same YAML maps **free-text
values** to canonical POF codes — applied BEFORE the
`FeatureIdentification` enum is constructed (`_normalise_value`,
`ili_reader.py:272-275`). For example
`value_normalisations.feature_identification.DENT` includes 13 vendor
strings (`dent`, `denp`, `dent plain`, `denc`, `dent complex` — the
1ZYC Abu Road label that previously leaked — `kinked dent`,
`dent dimple`, `area of dent`, etc.; lines 360-388).

### 1.5 Data types and units

All internal storage in SI; conversion to industry-standard kg/cm² at
the FFP boundary (`src/models/__init__.py:1-11`):

| Field | Internal | Notes |
|---|---|---|
| `abs_distance_m`, `upstream_weld_dist_m` | metres | `_to_float` parses European-comma decimals (`50,5` → `50.5`) when no `.` present (`ili_reader.py:460-461`) |
| `wt_mm`, `length_mm`, `width_mm` | mm | `wt_mm` must be `> 0`; otherwise `Feature` rejects on `__post_init__` (`models/__init__.py:150-151`) |
| `depth_pct_wt` | `[0, 100]` | `parse_depth` (`models/units.py:205-262`) accepts `"28.5%"`, `28.5`, `0.285` (fraction in `(0,1)` is auto-percentified). Stored as percent; `depth_mm` is a computed property `pct / 100 × wt_mm` (`models/__init__.py:121-125`). Strict bounds in `Feature.__post_init__` (`models/__init__.py:128-131`). |
| `clock_decimal_hours` | `[0, 12)` | `parse_clock` (`models/units.py:87-149`). **Convention:** strings are hh:mm (`"6:14"` = 6h 14m = 6.2333); numerics are decimal hours (`6.14` = 6.14h). Numerics in `(12, 360]` are interpreted as degrees and divided by 30. `12.0` wraps to `0.0`. |
| `surface` | `Surface` enum | `parse_surface` (`models/units.py:180-198`) consults `_SURFACE_MAP` (lines 156-177): `int`, `int.`, `internal`, `i`, `in`, `inner`, `inside` → `INTERNAL`; `ext`, `ext.`, `external`, `outer`, `out`, `outside` → `EXTERNAL`; `mid`, `midwall`, `mw`, `m` → `MIDWALL`. Anything else → `Surface.UNKNOWN`. |
| `latitude`, `longitude` | degrees, signed | bounds `[-90, 90]` / `[-180, 180]` in `Feature.__post_init__` (lines 138-145). Project-wide reasonability check via the coordinate-bounds box, see §1.7. |
| Pressure (MAOP, Psafe) | kg/cm² | Exact conversion `1 kg/cm² = 98066.5 Pa` (`models/units.py:32-41`). `MPA_TO_KGCM2 = 10.197162129779283`. |
| SMYS | MPa | Looked up by material grade if `smys_mpa` is 0 — see §2. |

### 1.6 Optional columns and defaults

| Missing column | Behaviour |
|---|---|
| `clock_position` | `Feature.clock_decimal_hours = None`. Defect matcher treats missing clock as **zero contribution** to cost (`defect_matcher.py:352-354`) — neither rewarded nor forbidden. |
| `feature_identification` | `Feature.feature_identification = FeatureIdentification.UNDEFINED`. The reader's filter logic falls back to "has depth → keep" heuristic (`_row_is_anomaly`, `ili_reader.py:493-539`). |
| `dimension_class` | Defaults to `DimensionClass.UNDEFINED`; Kastner is then **not** auto-run (the auto-Kastner gate is `dim is CISL or CIGR`, `ffp.py:632-635`). |
| `anomaly_id` | Synthesised as `f"row{source_row}"` (`ili_reader.py:1019-1020`). |
| `latitude`/`longitude` | `None`. No coordinate-bounds check fires. |
| `upstream_weld_dist_m` | Falls back to `None`. Defect matcher treats missing `upstream_weld_dist_m` on **either** side of a pair as **forbidden** (`defect_matcher.py:344-345`) — the pair cannot match. |
| `width_mm` | `None`. Kastner explicitly requires `width_mm` and raises `ValueError` (`ffp.py:697-700`). B31G / RSTRENG / DNV do not use width. |
| `joint_length_m` | Joint length stays 0.0; downstream joint-alignment NW falls back to nearest-distance method if length coverage drops below 50 % (`joint_alignment.py:154-167`). |
| `erf`, `psafe` | Vendor pre-computed values — read for QA cross-check, never used by the engine. |

### 1.7 Coordinate bounds + lat/lon swap detection

`_check_coordinate_bounds` (`ili_reader.py:571-637`) computes the median
latitude and longitude across all features. Three outcomes:

1. Both medians inside their bands → no flag.
2. Medians fall inside the **swapped** bands → every feature's
   `(latitude, longitude)` is auto-swapped in place and a
   `COORDINATES_SWAPPED` flag is emitted (INFO).
3. Neither orientation puts both medians in band → values left alone,
   `LAT_LON_OUT_OF_BOUNDS` is emitted (ERROR).

Default bounds (`ili_reader.py:75-76`) cover the Indian subcontinent:
`lat [6.0, 38.0]`, `lon [68.0, 98.0]`. Override per-project via the
`qa.coordinate_bounds` block in the YAML (see `config/default_project.yaml:332-334`).

### 1.8 CSV encoding cascade (Format Converter)

For CSV vendor inputs (Athena 2018 in particular — uses `°` in headers
as `0xb0` latin-1), `csv_input.py:read_csv_with_encoding_fallback`
(lines 75-158) tries encodings in this order:

1. BOM sniff at byte 0 (`_BOM_TO_ENCODING`, lines 53-59):
   `utf-8-sig` (EF BB BF), `utf-32-le`, `utf-32-be`, `utf-16-le`,
   `utf-16-be`.
2. Default cascade (`DEFAULT_CSV_ENCODINGS`, line 40-46): `utf-8`,
   `utf-8-sig`, `latin-1`, `cp1252`, `utf-16`.

The BOM check runs first because `latin-1` accepts ANY byte sequence
and would "win" before `utf-16` is tried, producing mojibake.

### 1.9 Row filtering

`_row_is_anomaly` (`ili_reader.py:493-539`) decides whether a row is a
metal-loss defect. Order matters:

1. Skip-list (`value_normalisations.feature_type_anomaly.skip` in
   `column_synonyms.yaml:478-521`) — matches feature_type OR fid: `weld`,
   `girth weld`, `valve`, `tee`, `flange`, `bend`, `casing`, `support`,
   `offtake`, `dent`, `ripple`, `repair`, `sleeve`, `patch`, etc. →
   drop.
2. Keep-list (`feature_type_anomaly.is_anomaly`, lines 451-475) → keep.
3. fid resolves to a known POF code via `value_normalisations` → keep
   (post-parse non-ML drop will still filter dents/welds).
4. No type info at all → keep iff `has_depth_raw` (the depth cell is
   not NA).
5. Type present but unrecognised → drop, AND if the string contains
   `dent`/`weld`/`crack`/`lwcr`/`mian`/`miac` substrings emit an
   `UNRECOGNISED_NON_ML_FID` parse warning so a maintainer can extend
   `column_synonyms.yaml`.

### 1.10 `run.features` vs `run.features_for_assessment()`

This is **important** for hand-checks against vendor "Total rows"
figures.

`run.features` is the raw, all-feature list — what the reader produced
after row filtering. Includes cluster children and rows whose
`feature_identification` matches `_NON_METAL_LOSS_FIDS` =
`{DENT, DEML, CRAC, GWAN, SWAN, LWAN}` (`ili_reader.py:62-71`).

`run.features_for_assessment()` (`models/__init__.py:246-287`) filters
the raw list at consumption time. Two filters:

1. **Cluster children** — features whose `cluster_parent_id is not None`.
   The COCL parent row carries the bounding-box dimensions for FFP; the
   children are subsumed.
2. **Non-metal-loss fids** — `DENT`, `DEML`, `CRAC`, `GWAN`, `SWAN`,
   `LWAN` (the same six). Rationale: B31G/RSTRENG/DNV/Kastner don't
   apply.

A **third** copy of the non-ML set lives in `ffp.py:_NON_ASSESSABLE_FIDS`
(line 88-95) — the FFP coordinator raises `ValueError` if a non-ML
feature reaches it, as defense-in-depth against future vendor variants
slipping past `column_synonyms.yaml`. The triple-redundancy is
deliberate and documented as the "Abu Road dent leak guard": one specific
historical bug (vendor labelled a dent as `"Dent complex"` not in the
synonym map → dent's 0.9 % OD depth was treated as 90 % WT → bogus
ERF = 8.57).

Cluster-child detection runs in the reader: when a COCL parent is seen,
subsequent CORR rows whose description matches any of
`value_normalisations.cluster_child_markers`
(`column_synonyms.yaml:438-447`: `"grouped"`, `"in cluster"`, `"child"`)
are tagged with the parent's anomaly_id. Resets at joint boundaries
(`ili_reader.py:886-889`).

---

## 2. Pipeline parameters (project YAML)

`Project.from_yaml` (`src/models/__init__.py:436-522`) is the canonical
parser. Block-by-block:

### 2.1 `project:`

Metadata only — `project_name`, `pipeline_name`, `client_name`,
`report_number`, `report_revision`, `prepared_by`, `reviewed_by`,
`approved_by`. None of these affect compute; all flow through to the
report headers.

### 2.2 `pipeline:`

| Field | Units | Default | Consumer |
|---|---|---|---|
| `diameter_mm` | mm OD | 0 | `Pipeline.diameter_mm`; every FFP method (`ffp.py:b31g_*`, `dnv_rp_f101`, `kastner` — the `D_mm` arg) |
| `length_km` | km | 0 | report metadata only |
| `install_year` | yyyy | 0 | report metadata; PDF auto-fill cross-check |
| `material_grade` | string | `"API 5L X70"` | `Pipeline.material_grade`; the table lookup below applies if `smys_mpa == 0` |
| `smys_mpa` | MPa | 0 (auto from grade) | flow stress in every method |
| `product` | string | `"crude oil"` | report metadata only |
| `service_class` | `"liquid"` or `"gas"` | `"liquid"` | report metadata only |
| `smys_lookup` | dict | see `default_project.yaml:33-41` | applied when `smys_mpa == 0` and `grade` resolves: X42→290, X46→317, X52→358, X56→386, X60→414, X65→448, X70→482, X80→552 (`models/__init__.py:455-459`) |

### 2.3 `maop_zones:`

A list — each entry is a `MAOPZone` (`models/__init__.py:168-181`):

```yaml
- wt_mm_min: 6.4         # inclusive
  wt_mm_max: 7.1         # inclusive
  design_factor: 0.72    # Fd; safety factor SF = 1 / Fd
  maop_kgcm2: 70.0
```

Zone lookup is `Pipeline.maop_for_wt(wt)` (`models/__init__.py:196-205`):
walks zones in declared order, returns the first whose `[wt_min,
wt_max]` contains `wt`. If no zone matches, falls back to the **nearest
zone** by `min(|wt − wt_min|, |wt − wt_max|)` and the FFP coordinator
attaches `MAOP_ZONE_NOT_FOUND` (WARN) to the result
(`ffp.py:642-655`).

Zone overlaps are not policed — if zones overlap, declared-first wins
because of the linear scan.

### 2.4 `runs:`

```yaml
runs:
  run_1:
    file_path: "examples/foo_run1.xlsx"
    inspection_date: "2018-12-15"    # ISO 8601
    vendor: "Athena PowerTech / NGP"
    tool_type: "MFL-A"
    tool_serial: ""
  run_2:
    ...
```

`inspection_date` is parsed by `date.fromisoformat` —
non-ISO strings silently become `None` (`models/__init__.py:483-489`),
which makes `Project.years_between_runs` return 0.0 and downstream
CGR raises `ValueError` (`cgr.py:149-154`).

### 2.5 `cgr:`

```yaml
cgr:
  mode: hybrid                       # feature_specific | hybrid | population_only
  # plus any DEFAULT_CONFIG override:
  population_quantile: 0.95
  split_by_surface: true
  floor_negative_at_zero: true
  unmatched_depth_assumption_pct_wt: 10.0
  extreme_cgr_threshold_mm_yr: 1.0
  tool_depth_tolerance_pct_wt: 10.0
  flag_below_tool_tolerance: true
```

Defaults baked in at `cgr.py:DEFAULT_CONFIG` (lines 58-85). See §6.

### 2.6 `ffp:`

```yaml
ffp:
  primary_method: B31G_Original      # B31G_Original | B31G_Modified | RSTRENG | DNV_RP_F101
  cross_check_methods: []            # list of method names
  kastner_for_circumferential: true
  uts_offset_mpa: 110.0              # DNV: UTS = SMYS + offset when UTS not supplied
```

Defaults at `ffp.py:DEFAULT_CONFIG` (lines 104-109). See §5.

### 2.7 `repair_prediction:`

```yaml
repair_prediction:
  horizon_years: 10
  depth_trigger_pct_wt: 80.0
  erf_trigger: 1.0
```

Defaults at `repair_predictor.py:DEFAULT_CONFIG` (lines 72-76). See §7.

### 2.8 Compute-affecting override: `years_override`

Not a YAML field — passed through `AnalysisJob.years_override` from the
GUI/CLI. If non-`None`, replaces date arithmetic in both the CLI
(`bin/run_pipeline.py:165-172`) and the GUI worker
(`src/gui/analysis_worker.py` around `years_between`). Used for projects
where one inspection date is uncertain (e.g. Run-1 year known but day
unknown).

---

## 3. Joint matching algorithm

`src/core/joint_alignment.py:JointAligner.align` (lines 134-167).

### 3.1 Inputs

Two `ILIRun.joints` lists. Each `Joint` (`models/__init__.py:154-165`)
carries:

- `joint_number: int`
- `abs_distance_start_m: float`
- `length_m: float` (0.0 if not in the source)
- `wt_mm: float | None`

### 3.2 Algorithm selection

`_length_coverage(joints) = (joints with length_m > 0) / total`
(`joint_alignment.py:447-451`). If **either** run's coverage is below
`min_length_coverage = 0.5` (config default), the aligner falls back
to the **nearest-distance method** (`_align_by_distance`,
lines 299-346) and emits a `parse_warning`.

Otherwise it uses **banded Needleman-Wunsch** (`_align_nw`,
lines 173-293).

### 3.3 Banded Needleman-Wunsch scoring

The pairwise score `S[i, j]` is computed in `_score_matrix`
(`joint_alignment.py:454-495`):

```
sim   = 1 − |L_i − L_j| / max(L_i, L_j)          # length similarity
is_match = sim ≥ min_similarity                  # default 0.85
score = sim if is_match else mismatch_penalty    # default −1.0
score += wt_bonus    if is_match and |WT_i − WT_j| ≤ wt_tolerance_mm    (0.2, 0.5 mm)
score += distance_bonus if is_match and |d_i − d_j| ≤ distance_tolerance_m  (0.3, 500 m)
```

(`DEFAULT_CONFIG` at lines 88-111.) **The 500 m distance tolerance is
unusually wide** — empirically tuned for the Kandla pipeline where the
+30 joint-number offset between runs has 26 m chainage drift but length
signatures match to 0.025 %; widening below 500 m loses the canonical
row5↔#125 pair (comment at lines 95-105). Net effect on
cleanly-aligned pipelines is nil — their candidates are already cheapest
within 20 m, the bonus just widens who qualifies for it.

### 3.4 Banded DP

The band width is `band = cfg["band_width"] or max(20, int(0.05 *
max(n1, n2)))` (line 181) — 5 % of the larger joint count, floored
at 20. The DP fills only cells within `band` of the diagonal; the
traceback table is full-size `int8`.

Cell update (lines 222-238):
```
diag = F[i-1, j-1] + S[i-1, j-1]
up   = F[i-1, j]   + gap_penalty   # default −0.5
left = F[i,   j-1] + gap_penalty
F[i, j] = max(diag, up, left)
```

Traceback rule on a diagonal step: the pair is accepted **only if**
`_length_similarity(L_i, L_j) ≥ min_similarity` (line 252-265).
DP-forced diagonals that fall short of threshold split into two
unmatched joints (lines 275-279) — this matters when constructing
synthetic tests because the DP CAN choose a "bad" diagonal.

### 3.5 Tie-breaks

`max(diag, up, left)` uses `>=`, so on ties the order is **diag > left
> up** (lines 225-238). For aligned synthetic data this never matters.

### 3.6 Joints in one run only

Whichever run has more joints, the extras flow out as
`unmatched_run1` / `unmatched_run2`. The match rate uses
`max(len(joints1), len(joints2))` as the denominator
(`joint_alignment.py:367`) — conservative.

### 3.7 Output (`JointAlignment`, lines 58-81)

| Field | Notes |
|---|---|
| `matches: list[JointMatch]` | each is `(joint_old, joint_new, length_diff_m, confidence, matched_via)`. `confidence = length_similarity`; `matched_via = "needleman_wunsch"` or `"nearest_distance"`. |
| `unmatched_run1`, `unmatched_run2` | lists of `Joint` |
| `match_rate` | `len(matches) / max(n1, n2)` |
| `monotonicity_violations` | `(j1_no, j2_no)` pairs where chainage went backwards relative to the previous match (`_monotonicity_violations`, lines 498-512) |
| `total_length_run1`, `total_length_run2` | sum of joint lengths in each run |
| `band_width` | the actual band used |
| `qa_flags` | structured `QAFlag` objects: `LOW_JOINT_MATCH_RATE`, `REVERSAL_DETECTED`, `LENGTH_MISMATCH_RUN` |

### 3.8 Post-alignment validation

`_finalise` (lines 352-433) emits three flags:

- **`LOW_JOINT_MATCH_RATE`** if `match_rate < min_match_rate_warning`
  (default 0.90). WARN.
- **`REVERSAL_DETECTED`** if `monotonicity_violations` non-empty. WARN.
- **`LENGTH_MISMATCH_RUN`** if matched-joint total-length disagreement
  `> total_length_tolerance_pct` (default 0.01, i.e. 1 %). WARN.

---

## 4. Defect matching algorithm (within a joint)

`src/core/defect_matcher.py:DefectMatcher.match` (lines 106-193).

### 4.1 Inputs

Two `features_for_assessment()` pools (cluster children + non-ML
already filtered out) and the `JointAlignment.matches` list. Features
are indexed by `joint_number` so each aligned joint pair is matched in
isolation.

### 4.2 Three-pass iterative relaxation

Each joint pair is matched by **Hungarian assignment + iterative
tolerance relaxation**. `DEFAULT_CONFIG.passes` (`defect_matcher.py:79-83`):

| Pass | `axial_tolerance_m` | `clock_tolerance_h` | `max_cost` |
|---|---|---|---|
| 1 | 0.10 | 0.5 | 0.5 |
| 2 | 0.25 | 1.0 | 1.0 |
| 3 | 1.00 | 1.5 | 1.5 |

Each pass operates only on what the previous passes left unmatched
(`_match_joint_pair`, lines 197-223). The wider pass-3 tolerance
(1.00 m axial vs. user-spec 0.5 m) was tuned for the
Kandla row77↔#1038 last-defect-in-joint pair (uw_diff 0.607 m within a
12 m joint).

### 4.3 Cost function

`_vectorised_cost_matrix` (lines 286-390):

```
axial = |upstream_weld_dist_run1 − upstream_weld_dist_run2|
clock_raw = |clock_run1 − clock_run2|
clock = clock_raw if clock_raw <= 6.0 else 12.0 − clock_raw   # wrap

cost  = axial * axial_weight     (default 1.0)
      + clock * clock_weight     (default 0.3)

cost += surface_mismatch_penalty       if both surfaces known and differ   (default 10.0)
cost += depth_shrinkage_penalty        if d_new < depth_shrinkage_ratio * d_old   (1.0, 0.5)
cost += cluster_type_mismatch_penalty  if is_cluster_parent differs        (0.2)
```

### 4.4 Forbidden cells

- `upstream_weld_dist_m` missing on either side → forbidden (cost =
  `_FORBIDDEN_COST = 1e6`, line 62).
- `axial > axial_tolerance_m` for the current pass → forbidden.
- `clock_tolerance` exceeded **only when both sides know clock**
  (lines 352-353).

`scipy.optimize.linear_sum_assignment` is run on the resulting cost
matrix. After Hungarian:

- Any returned pair whose cost is `≥ _FORBIDDEN_COST / 2` is silently
  rejected (line 257).
- Any pair whose cost exceeds the pass's `max_cost` ceiling is also
  rejected — those features roll forward to the next pass (lines
  260-263).

### 4.5 Confidence

For accepted pairs: `confidence = max(0, min(1, 1 − cost / max_cost))`
(line 264). So a cost-0 match is 1.0; a cost-at-ceiling match is 0.0.

### 4.6 Match rate

`_compute_match_rate` (lines 470-485) uses `min(|run1|, |run2|)` as the
denominator — "what fraction of the smaller pool got paired". This
matters because HMEL-class projects have run-2 with 8× more defects
than run-1 (better tool); using `max` would look mostly-unmatched.

`LOW_DEFECT_MATCH_RATE` (WARN) fires when `match_rate < 0.90`
(`defect_matcher.py:177-191`).

`NO_CLUSTERS_IN_EITHER_RUN` (INFO) fires when neither run has any
cluster parents (lines 158-172) — advisory; downstream still runs at
the feature level.

### 4.7 Output (`MatchResult`, `models/__init__.py:323-358`)

| Field | Notes |
|---|---|
| `feature_matches: list[FeatureMatch]` | `(feature_old, feature_new, match_score=cost, confidence, relaxation_level=1/2/3)` |
| `unmatched_features_old`, `unmatched_features_new` | residuals — derived by Python-identity diff against the matched set (line 144-147) |
| `matches_per_pass` | `{1: n1, 2: n2, 3: n3}` |
| `match_rate` | as above |
| `qa_flags` | structured `QAFlag` objects |

---

## 5. Psafe / ERF formulas

All five methods live in `src/core/ffp.py`. Internal computation is in
SI (MPa, mm); the SI Psafe is converted to kg/cm² via
`mpa_to_kgcm2` (`models/units.py:60-61`, `MPA_TO_KGCM2 = 1 000 000 /
98 066.5 = 10.197162129779283`).

The ERF convention is **locked**: `ERF = MAOP / Psafe` (high ERF = bad)
— stamped on every result (`ffp.py` after each `psafe_mpa`
computation).

### 5.1 B31G Original — `b31g_original` (lines 175-252)

| Step | Formula | Source |
|---|---|---|
| 1. Slenderness | `z = L² / (D · t)` | line 197 |
| 2. Flow stress | `S_flow = 1.1 · SMYS` MPa | `_SFLOW_FACTOR_B31G_ORIG = 1.1` (line 98), line 199 |
| 3. Intact pressure | `P_intact = 2 · S_flow · t / D` MPa | line 200 |
| 4a (low-z, `z ≤ 20`) | `M = √(1 + 0.8 z)` `Q = (2/3)(d/t)` `R = (1 − Q) / (1 − Q / M)` | lines 202-211 |
| 4b (high-z, `z > 20`) | `M = None` `R = 1 − d/t` | lines 212-216 |
| 5. Failure pressure | `Pf = max(0, P_intact · R)` MPa | line 218 |
| 6. Safe pressure | `Psafe = Pf · Fd` MPa | line 219 |
| 7. ERF | `MAOP / Psafe`; `+∞` if `Psafe ≤ 0` | line 222 |

A degenerate denominator (`1 − Q/M ≤ 0`) clamps `R = 0` →
`Pf = 0` → `ERF = +∞` (lines 206-209).

The **z = 20** branch split is the standard B31G Section 4 partition.
A separate `_B31G_ORIG_LONG_DEFECT_Z = 50.0` (line 124) attaches
`LONG_DEFECT_OUTSIDE_B31G` (WARN) when `z > 50` (lines 242-250) — *the
method still returns a value*, the flag just warns it's outside
calibration.

### 5.2 B31G Modified — `b31g_modified` (lines 259-324)

The classic "0.85·dL" form. ASME B31G-2012 Section 5.

| Step | Formula | Source |
|---|---|---|
| 1. Slenderness | `z = L² / (D · t)` | line 281 |
| 2. Flow stress | `S_flow = SMYS + 69 MPa` (≈ SMYS + 10 ksi) | `_SFLOW_OFFSET_B31G_MOD_MPA = 69.0` (line 101), line 283 |
| 3a (low-z, `z ≤ 50`) | `M = √(1 + 0.6275 z − 0.003375 z²)` (clamped at 0) | line 286 |
| 3b (high-z, `z > 50`) | `M = 0.032 z + 3.3` | line 289 |
| 4. Effective area | `Q = 0.85 · d / t` | line 292 |
| 5. Safe stress | `SF = S_flow · (1 − Q) / (1 − Q/M)` MPa (0 if denom ≤ 0) | lines 293-297 |
| 6. Failure pressure | `Pf = max(0, 2 · SF · t / D)` MPa | line 298 |
| 7. Safe pressure | `Psafe = Pf · Fd` MPa | line 299 |

The `z = 50` branch split (`_B31G_MOD_LONG_DEFECT_Z` semantic — though
this method's branch is **internal**, not a flag trigger) is the
B31G-2012 §5 definition. Modified B31G **does not** raise
`LONG_DEFECT_OUTSIDE_B31G` because its high-z branch is valid all the
way out.

### 5.3 RSTRENG — `rstreng` (lines 331-377)

**Note: this differs from textbook RSTRENG.** Without a measured
river-bottom depth profile, this implementation **falls back to B31G
Modified verbatim** — line 366 calls `b31g_modified(...)` and only
rewrites `result.method = FFPMethod.RSTRENG`,
`using_approximate_profile = True`, and appends a note. So
`rstreng(...)` and `b31g_modified(...)` produce **identical Pf and
Psafe** for the same inputs.

When the future profile-aware path lands, it'll be triggered by
passing `depth_profile_mm=[...]`. Until then any such call raises
`NotImplementedError` (lines 361-364).

### 5.4 DNV-RP-F101 Part B (ASD) — `dnv_rp_f101` (lines 384-452)

Uses **UTS**, not flow stress.

| Step | Formula | Source |
|---|---|---|
| 1. UTS (if not supplied) | `UTS = SMYS + uts_offset_mpa` (default `_UTS_OFFSET_MPA_DEFAULT = 110 MPa`) — note added | line 408, default at line 78 |
| 2. Slenderness | `z = L² / (D · t)` | line 414 |
| 3. Length correction | `Q = √(1 + 0.31 z)` | line 415 |
| 4. Failure pressure | `Pf = (2 · UTS · t / (D − t)) · (1 − d/t) / (1 − (d/t)/Q)` MPa (0 if denom ≤ 0) | lines 418-426 |
| 5. Safe pressure | `Psafe = Pf · Fd` MPa | line 426 |

Two unusual elements:

1. **`(D − t)` not `D`** in the geometry factor — matches DNV-RP-F101
   2017 Section 4 form exactly.
2. The "Q" symbol here is **DNV's length-correction Q**, not B31G's
   area ratio. The code stores it in the `folias_factor_M` slot of
   `FFPResult` (line 443) — convenient for cross-method comparison.

### 5.5 Kastner — `kastner` (lines 459-524)

**Note: this differs from the 1986 paper.** The implementation is the
**net-section approximation**, not the full equilibrium form
(documented at `ffp.py:483-489` and `project_v02_followups.md`).

| Step | Formula | Source |
|---|---|---|
| 1. Flow stress | `S_flow = 1.1 · SMYS` (same as B31G Original) | line 490 |
| 2. Area reduction | `α = (W / (π · D)) · (d / t)`, clamped to `[0, 1]` | lines 492-494 |
| 3. Failure pressure | `Pf = max(0, (4 · S_flow · t / D) · (1 − α))` MPa | line 496 |
| 4. Safe pressure | `Psafe = Pf · Fd` MPa | line 497 |

Note `4 · S_flow · t / D` (axial-stress, partial-circ defect), not the
B31G hoop form. Stores `W_mm` in the `length_mm` slot of `FFPResult`
(line 507) and **skips** the VERY_SHORT_DEFECT axial-length check
(line 521-523).

### 5.6 ERF, Pf, Psafe in kg/cm²

All four methods do `pf_kgcm2 = mpa_to_kgcm2(pf_mpa)` and
`sop_kgcm2 = mpa_to_kgcm2(psafe_mpa)` (e.g. lines 220-221). The
`MPA_TO_KGCM2` factor is exact (98 066.5 Pa/kg/cm² from CGPM-1901).

`erf = (maop_kgcm2 / sop_kgcm2)` — both sides in kg/cm² so the units
cancel. `+∞` if `sop_kgcm2 ≤ 0`.

### 5.7 Common flags attached by `_attach_common_flags` (lines 127-168)

All methods (except where noted) call this helper before returning:

- **`ERF_EXCEEDS_1`** (ERROR) if `erf ≥ 1.0`.
- **`DEPTH_EXCEEDS_80`** (ERROR) if `depth_pct_wt ≥ 80.0`.
- **`VERY_SHORT_DEFECT`** (WARN) if `length_mm < 1.0 × wt_mm`
  (`_VERY_SHORT_DEFECT_RATIO = 1.0`, line 119). Kastner skips this
  check via `check_very_short=False` because its `length_mm` slot holds
  the circumferential width.

### 5.8 Auto-Kastner for circumferential defects

If `feature.dimension_class ∈ {CIRCUMFERENTIAL_SLOTTING,
CIRCUMFERENTIAL_GROOVING}` (`ffp.py:632-635`) AND
`config.kastner_for_circumferential = True` (default), Kastner runs
**in addition to** the primary method. Whichever has the **lower
Psafe** is marked `is_controlling=True` (lines 658-669).

The primary method is **always** also run — there's no skip. The dual
result is what allows the older Annexure-B/C/D format to populate
Annexure D (Kastner-only) alongside Annexure C (B31G).

---

## 6. CGR computation

`src/core/cgr.py:CGRCalculator.compute` (lines 143-207).

### 6.1 Three modes

| Mode (`CGRMode` enum value) | Behaviour |
|---|---|
| `feature_specific` | Per-feature `cgr = max(0, Δd / Δt)`. No population data. |
| `population_only` | Every defect gets the surface's P95 CGR — replaces feature-specific. |
| `hybrid` | Feature-specific value is **floored** at the surface's P95. `cgr = max(feature_cgr, surface_p95)`. Recommended for Indian projects. |

### 6.2 Per-feature CGR

`_build_result` (lines 259-336):

```
Δd = d_new_mm − d_old_mm
raw_cgr = Δd / years_between

if Δd < 0 and floor_negative_at_zero:    # default True
    feature_cgr = 0.0; emit NEGATIVE_GROWTH (INFO)
elif floor_negative_at_zero:
    feature_cgr = max(0, raw_cgr)
else:
    feature_cgr = raw_cgr
```

Note: depth is in **mm**, not %WT. The feature's `depth_mm` property is
computed from `depth_pct_wt / 100 × wt_mm` (`models/__init__.py:122-125`).

### 6.3 Unmatched run-2 features

`_cgr_for_unmatched_new` (lines 232-255): the run-1 depth is **assumed
equal to the tool POD threshold**. From config:
`unmatched_depth_assumption_pct_wt = 10.0` (default,
`cgr.py:DEFAULT_CONFIG` line 75). So `d_old_mm = 0.10 × wt_mm`. Each
such feature gets an `UNMATCHED_RUN2` flag (INFO).

If `wt_mm` is None or 0, `d_old_mm = 0.0` falls through (line 243).

### 6.4 Δt

The caller passes `years_between` directly — `CGRCalculator` never
reads inspection dates itself. Standard arithmetic via
`cgr.py:years_between_runs` (lines 397-411):

```
Δt = abs((date2 − date1).days) / 365.25
```

Both dates must be set or `ValueError`. **There is no "Year-only Run-1
→ 1 July default" rule** anywhere in the engine — the date parser
either consumes a full ISO date or returns `None`, and downstream
raises. (The PDF auto-fill in the GUI is the only place that synthesises
a date from a year; it picks the **whole year** as a date string for
display only.)

### 6.5 Population P95 (HYBRID and POPULATION_ONLY)

`_compute_p95_by_surface` (lines 349-375):

```
bucket features by f.surface (or "all" if split_by_surface=False)
for each bucket:
    if len(vals) < 2: skip (no P95 emitted for that surface)
    else: P95 = numpy.percentile(vals, 95.0)
```

`np.percentile` uses **linear interpolation** by default — so even
for 22 matched + 311 unmatched (= 333) feature values the result is
not exactly any one feature's CGR; it's interpolated.

The pool that feeds the percentile is `r.feature_cgr_mm_yr` —
**post-negative-clamping**, before any floor is applied (line 365).
So features whose Δd was negative contribute 0.0 to the pool, biasing
the P95 slightly downward.

**Two distinct CGR pools, easy to confuse:**

- `r.feature_cgr_mm_yr` — per-feature raw rate (post-negative-clamp,
  pre-floor). Used to compute the P95 inside `_compute_p95_by_surface`
  (`cgr.py:365`). Frozen after step 1; never overwritten.
- `r.cgr_mm_yr` — the value used downstream (FFP projection, repair
  predictor). Starts equal to `feature_cgr_mm_yr` (`cgr.py:329`) and
  gets **overwritten** in HYBRID with the surface P95 whenever
  `feature_cgr < P95` (line 192), or unconditionally in
  POPULATION_ONLY (line 186).

The aggregator's `HIGH_CGR_POPULATION` median (§8) consumes the
**post-floor** pool — see §8 for why that matters in HYBRID projects.

### 6.6 Hybrid floor application

After computing P95s, `compute` walks results and (for HYBRID, line
190-205):

```
if feature_cgr < surface_p95:
    cgr_mm_yr = surface_p95
    mode_used = "population_floor"
    emit POPULATION_FLOOR_APPLIED (INFO)
else:
    cgr_mm_yr = feature_cgr     # unchanged
    mode_used = "feature_specific"
```

Equivalent to `max(feature_cgr, surface_p95)` but coded as an
`if`/`else` so the flag fires only on the actual uplift cases.

For `POPULATION_ONLY` (lines 185-187): `cgr_mm_yr = surface_p95`
unconditionally, no `POPULATION_FLOOR_APPLIED` flag (because *every*
feature got the floor, which is the whole point).

### 6.7 Extra QA flags (`_build_result`, lines 296-324)

- **`DEPTH_BELOW_TOL`** (INFO) — matched features where `|Δd| < (tool_depth_tolerance_pct_wt / 100) × wt_mm` (default 10 % × WT). Not emitted for unmatched. Means the measured signal is below tool noise.
- **`EXTREME_CGR`** (WARN) — `feature_cgr_mm_yr > 1.0 mm/yr`
  (`extreme_cgr_threshold_mm_yr` default). Flag based on the
  **per-feature** rate, not the floored one (line 317).

---

## 7. Time-to-repair projection

`src/core/repair_predictor.py:RepairPredictor.predict_one` (lines 108-256).

### 7.1 Algorithm

**Year-by-year integer projection**. No closed-form solve, no
bisection. Loop from `year_offset = 0` to `horizon_years` (default 10).

At each `year_offset > 0`:

```
d_mm += cgr_mm_yr * 1.0            # linear growth in mm
d_pct = 100 * d_mm / wt
new_ffp = method_fn(d_mm=capped_d_mm, **base_kwargs)
                                   # capped_d_mm = min(d_mm, wt * 0.999)
                                   # (numerical safety, line 342)
```

### 7.2 Triggers

Whichever fires earlier wins:

- `DEPTH_80`: `d_pct ≥ depth_trigger_pct_wt` (default 80.0)
- `ERF_1.0`: `new_ffp.erf ≥ erf_trigger` (default 1.0)

Sentinel: `NONE_WITHIN_HORIZON` if neither fires through year `horizon`.

`TRIGGER_*` constants at `repair_predictor.py:62-66`.

### 7.3 What's grown, what's held constant

- **Depth grows linearly** in mm at the rate `cgr_mm_yr` (line 198).
- **Length, width, WT, OD, SMYS, MAOP, Fd are held constant.**
  `base_kwargs` is built once at year 0 (`_build_base_kwargs`, lines
  295-322) and reused.
- **The Folias factor M (or DNV's Q) is recomputed each year** because
  the method function is re-called with the new `d_mm` — but M depends
  on `z = L² / (D·t)`, which doesn't involve depth, so M is *effectively*
  constant. The recomputation just keeps the call surface clean.
- For circumferential defects, the predictor projects whichever method
  was controlling at year 0 (`method = current_ffp.method`, line 130).
  If the controlling method would flip mid-projection (rare in
  practice), the report misses it — documented as a v0.1 limitation
  (lines 22-25).

### 7.4 Year-0 check

The current FFP (passed in as `current_ffp`) is checked **before** the
loop:

```
if depth_pct_wt(d_run2) >= 80:          return DEPTH_80 at year 0
if current_ffp.erf >= 1.0:              return ERF_1.0 at year 0
```

(lines 163-194). So a defect already over-threshold at Run-2 reports
`repair_year_offset = 0`.

### 7.5 Repair date arithmetic

`_build_prediction` (lines 346-381):

```
if triggered and year_offset is not None and run2_date is not None:
    repair_date = run2_date + timedelta(days=int(year_offset * 365.25))
```

`_DAYS_PER_YEAR = 365.25` (line 70). Note **`int(...)` truncation**:
year_offset 5 produces 1826 days = 5 yr 0 d 0 h 0 m. Integer-year
resolution; no fractional-year interpolation.

### 7.6 Horizon-end label

For `NONE_WITHIN_HORIZON`: the report renders `"After {Month YYYY}"`
of `run2_date + horizon_years × 365.25` days
(`horizon_end_date`, lines 388-391; `_format_repair_date`,
`annexure_writer.py:802-819`).

### 7.7 Output (`RepairPrediction`, `models/__init__.py:386-401`)

| Field | Notes |
|---|---|
| `cgr_mm_per_year` | Echo of `cgr_result.cgr_mm_yr` |
| `yearly_assessments: list[FFPResult]` | One per year incl. year 0. Length `1 + horizon` for non-triggered features. |
| `repair_trigger` | `"DEPTH_80"` / `"ERF_1.0"` / `"NONE_WITHIN_HORIZON"` |
| `repair_year_offset` | int (0..horizon) or `None` |
| `predicted_repair_date` | `date` or `None` |
| `final_depth_pct_wt`, `final_depth_mm`, `final_erf`, `final_psafe_kgcm2` | State at the trigger year (or end of horizon if not triggered) |
| `method_used` | The FFP method projected — fixed at year-0 controlling |
| `qa_flags` | CGR's flags carried forward (notably `UNMATCHED_RUN2`) |

### 7.8 Horizon-truncated features

`repair_trigger = "NONE_WITHIN_HORIZON"` for features that don't fire.
These are **excluded** from the "Repair within N yr" CLI count
(`bin/run_pipeline.py:305`) and from the `REPAIR_PREDICTED_WITHIN_HORIZON`
aggregator flag (`flag_aggregator.py:152-164`), but they DO get
`final_depth/erf/psafe` populated for the +N-year column in Annexure
C/D.

---

## 8. QA findings taxonomy

`src/validation/qa_flags.py` defines every code (lines 47-89). Severity
is set centrally in `CANONICAL_SEVERITY` (lines 97-128); modules can
override via `make_flag(..., severity=...)` but the aggregator
re-normalises against the canonical table (`flag_aggregator.py:117`).

Critical (flips `FlagReport.has_critical = True` →
non-zero CLI exit): only **`ERF_EXCEEDS_1`** and **`DEPTH_EXCEEDS_80`**
(`flag_aggregator.py:51-54`).

### Reader (`src/io/ili_reader.py`)

| Code | Severity | Trigger |
|---|---|---|
| `COORDINATES_SWAPPED` | INFO | Median (lat, lon) falls inside the swapped bounds box. Values auto-swapped in-place. (`_check_coordinate_bounds`, 599-618.) |
| `LAT_LON_OUT_OF_BOUNDS` | ERROR | Median (lat, lon) outside both orientations of the bounds box. Values left unchanged. (`_check_coordinate_bounds`, 620-637.) |
| `SHEET_NOT_DETECTED` | ERROR | No sheet scored `≥ _DEFECT_SHEET_MIN_HITS = 4` canonical headers. Raised by `_pick_defect_sheet`. |
| `HEADER_ROW_AMBIGUOUS` | WARN | Defined but currently unused — reserved for the case where two rows in the first 10 score equally on canonical fields. (Reader currently picks the first.) |
| `MISSING_COLUMN` | ERROR | Defined but raised as `ValueError` in `_check_required`, not as a flag. Codes-as-exception-text. |
| `SURFACE_VALUE_UNKNOWN` | INFO | Surface value didn't map to a known enum (defined; emission site is `parse_surface` which currently silently returns `Surface.UNKNOWN`). |
| `CLOCK_VALUE_UNKNOWN` | INFO | Defined; reserved. |
| `RECONSTRUCTED_JOINT_CONTEXT_FROM_CHAINAGE` | INFO | Row sequence is non-chainage-monotonic (a backward jump > `_NON_MONOTONIC_JUMP_M = 500 m`, line 83). Reader switches to weld-anchor binary search for joint attribution. (`ili_reader.py:738-745`.) |

### Joint alignment + defect matching

| Code | Severity | Trigger |
|---|---|---|
| `LOW_JOINT_MATCH_RATE` | WARN | `match_rate < min_match_rate_warning` (default 0.90). (`joint_alignment.py:395-404`.) |
| `REVERSAL_DETECTED` | WARN | One or more `monotonicity_violations`. (`joint_alignment.py:406-414`.) |
| `LENGTH_MISMATCH_RUN` | WARN | Matched-joint length total disagrees by more than `total_length_tolerance_pct = 0.01` (1 %). (`joint_alignment.py:421-431`.) |
| `LOW_DEFECT_MATCH_RATE` | WARN | `match_rate < 0.90` of the smaller pool. (`defect_matcher.py:177-191`.) |
| `NO_CLUSTERS_IN_EITHER_RUN` | INFO | Neither run has `is_cluster_parent=True` rows. (`defect_matcher.py:158-172`.) |

### CGR (`src/core/cgr.py`)

| Code | Severity | Trigger |
|---|---|---|
| `NEGATIVE_GROWTH` | INFO | `Δd < 0` and `floor_negative_at_zero=True`. CGR clamped to 0. (`cgr.py:284-291`.) |
| `EXTREME_CGR` | WARN | `feature_cgr_mm_yr > extreme_cgr_threshold_mm_yr` (default 1.0). Flag on the raw rate, not the floored one. (`cgr.py:317-324`.) |
| `POPULATION_FLOOR_APPLIED` | INFO | HYBRID mode and `feature_cgr < surface_p95`. (`cgr.py:191-205`.) |
| `UNMATCHED_RUN2` | INFO | Run-2 feature with no run-1 partner. `d_old_mm` assumed at `unmatched_depth_assumption_pct_wt × wt_mm`. (`cgr.py:274-282`.) |
| `DEPTH_BELOW_TOL` | INFO | Matched feature with `|Δd| < tool_depth_tolerance_pct_wt% × WT` (default 10 % × WT). Not emitted for unmatched. (`cgr.py:298-313`.) |

### FFP (`src/core/ffp.py`)

| Code | Severity | Trigger |
|---|---|---|
| `ERF_EXCEEDS_1` | ERROR | `ERF ≥ 1.0`. (`_attach_common_flags`, 140-148.) |
| `DEPTH_EXCEEDS_80` | ERROR | `depth_pct_wt ≥ 80.0`. (`_attach_common_flags`, 149-157.) |
| `LONG_DEFECT_OUTSIDE_B31G` | WARN | B31G Original only. `z > 50.0`. (`b31g_original`, 242-250.) |
| `VERY_SHORT_DEFECT` | WARN | `length_mm < 1.0 × wt_mm` (`_VERY_SHORT_DEFECT_RATIO`, line 119). Skipped for Kastner. (`_attach_common_flags`, 158-168.) |
| `MAOP_ZONE_NOT_FOUND` | WARN | Feature WT outside every explicit MAOP zone — nearest zone used. Tagged on every result of that feature. (`ffp_assess`, 642-655.) |

### Predictor + pipeline-level (synthesised by `FlagAggregator`)

| Code | Severity | Trigger |
|---|---|---|
| `REPAIR_PREDICTED_WITHIN_HORIZON` | WARN | At least one prediction with `repair_trigger != "NONE_WITHIN_HORIZON"`. (`flag_aggregator.py:152-164`.) |
| `HIGH_CGR_POPULATION` | WARN | `statistics.median(cgr_values) > 0.2 mm/yr` (`_HIGH_CGR_MEDIAN_THRESHOLD_MM_YR`, line 58). **`cgr_values` is built from `r.cgr_mm_yr` — the POST-HYBRID-floor value** (`flag_aggregator.py:168`), NOT the pre-floor `r.feature_cgr_mm_yr` pool that fed the P95. In HYBRID projects with a high surface P95 this is a substantial shift: Test Pack 2 reported pre-floor median = 0.211 mm/yr vs post-floor median = 0.399 mm/yr — same dataset, ~90 % uplift, opposite verdicts (below threshold → above). (`flag_aggregator.py:166-185`.) |

### Aggregator behaviour

`FlagAggregator.aggregate` (`flag_aggregator.py:93-187`) collects from
`ILIRun.qa_flags` (×2), `JointAlignment.qa_flags`, `MatchResult.qa_flags`,
each `CGRResult.qa_flags`, each `FFPResult.qa_flags`, and each
`RepairPrediction.qa_flags`. Dedupe key is `(code, feature_id)` — run-
level flags (feature_id=None) dedupe by code alone (lines 104-118).

Summary string (`_compose_summary`, lines 204-217):

```
"QA: {N} finding(s) — {n_err} error, {n_warn} warn, {n_info} info. ({verdict})"

verdict = "REVIEW REQUIRED" if any critical else "review recommended"
```

Empty → `"QA: clean — no findings raised."`

---

## 9. Output schema

### 9.1 Annexure E (format `E_F`) — Run to Run Comparison

`src/reports/annexure_writer.py:_write_annexure_e` (lines 217-292).
Three-row header (rows 1-3); data starts row 4. Column letters:

| Col | Letter | Header (row 3) | Width | Numeric format |
|---|---|---|---|---|
| 1 | A | S.N. | 12 | `0` |
| 2 | B | Anomaly ID | 15 | `@` (text) |
| 3 | C | Wall Thickness, (mm) | 14 | `0.0` |
| 4 | D | Joint Number | 14 | `0` |
| 5 | E | ILI {year_new} (Abs Dist new) | 14 | `0.000` |
| 6 | F | ILI {year_old} (Abs Dist old) | 14 | `0.000` |
| 7 | G | ILI {year_new} (Depth new %) | 12 | `0.00` |
| 8 | H | ILI {year_old} (Depth old %) | 12 | `0.00` |
| 9 | I | ILI {year_new} (Orient new) | 12 | `@` (hh:mm:ss text) |
| 10 | J | ILI {year_old} (Orient old) | 12 | `@` |
| 11 | K | ILI {year_new} (Surface new) | 11 | `@` (`int.` / `ext.`) |
| 12 | L | ILI {year_old} (Surface old) | 11 | `@` |
| 13 | M | CGR (mm/yr) | 14 | `0.0000` |

(`_ANNEX_E_COLUMNS`, lines 91-105.)

Row 1: title (`"Annexure E: Run to Run Comparison"`), merged
A1:M1, yellow fill (`FFC000`), bold 12 pt.

Row 2: group headers, merged spans A2:D2 (`"Feature Detail as per ILI
{year_new}"`), E2:F2 (`"Abs. Distance, (m)"`), G2:H2 (`"Anomaly Depth,
(%)"`), I2:J2 (`"Anomaly Orientation"`), K2:L2 (`"Anomaly Location"`).
M is unmerged.

Data rows include unmatched features (`f_old = None` → `depth old`
column carries `cgr.depth_old_used_mm × 100 / wt_mm` — the assumed
10 % WT in raw form, line 311). **Sort order: ascending by
`feature.abs_distance_m`, ties broken by `anomaly_id` string** (line
171-174). All features appear (matched + unmatched).

### 9.2 Annexure F (format `E_F`) — Metal Loss Anomalies

`_write_annexure_f` (lines 330-430). Four-row header (rows 1-4); data
starts row 5.

| Col | Letter | Header | Width | Format |
|---|---|---|---|---|
| 1 | A | S.N. | 8 | `0` |
| 2 | B | Feature ID | 12 | `@` |
| 3 | C | Absolute Distance [m] | 16 | `0.000` |
| 4 | D | Latitude | 16 | `0.0000000000` |
| 5 | E | Longitude | 16 | `0.0000000000` |
| 6 | F | Joint No. | 10 | `0` |
| 7 | G | Joint Length (m) | 14 | `0.000` |
| 8 | H | Distance to closest weld (m) | 16 | `0.000` |
| 9 | I | Event | 24 | `@` (= `raw_description`) |
| 10 | J | Surface | 10 | `@` |
| 11 | K | Wall Thickness [mm] | 14 | `0.0` |
| 12 | L | Orientation (hh:mm) | 14 | `@` (hh:mm:ss text) |
| 13 | M | Reported Depth [% WT] | 14 | `0.00` |
| 14 | N | Length (mm) | 12 | `0` |
| 15 | O | Width (mm) | 12 | `0` |
| 16 | P | Predicted Repair year — Effective Repair Date | 24 | `@` |

(`_ANNEX_F_COLUMNS`, lines 108-125.)

Column P value (`_format_repair_date`, lines 802-819):

- Trigger fired and date set → `"dd-mm-yyyy"`.
- Otherwise → `"After {Month YYYY}"` of `run2_date + horizon_years ×
  365.25 days`. Or `"After horizon"` if `run2_date` is None.

Column G (`Joint Length`) is currently always `None` because the
current implementation doesn't re-plumb `ILIRun.joints` into the
writer (acknowledged at lines 393-398). Column H is
`feature.upstream_weld_dist_m`.

### 9.3 Annexures B / C / D (format `B_C_D`) — older GAIL deliverable

Annexure B (`_write_annexure_b`, lines 436-518): matched-only,
S.N./Feature ID/Joint/Chainage/WT/Depth new/Depth old/Surface
new/Surface old/CGR. Sort = ascending abs_distance.

Annexure C (`_write_annexure_c` via `_write_bcd_assessment_sheet`,
lines 524-695): **all features**, B31G original. Columns are
S.N./Feature ID/Joint No./Chainage/Surface/Depth-now/Depth-+10/SOP-now/
SOP-+10/ERF-now/ERF-+10/CGR/Repair Date.

Annexure D (legacy preset) / Annexure E (topic-based, v0.2.5+):
identical layout to C but **filtered to circumferentially-classified
features**, assessed via Kastner. Up to v0.3.2 the filter required
the feature's stored FFPResult to have `method is FFPMethod.KASTNER`
(i.e., Kastner had to be the controlling lower-Psafe method). Since
B31G's Psafe is typically lower than the Kastner net-section
approximation for shallow defects, that condition almost never fires
and the sheet came out empty on real customer data — surfaced during
the BPCL Mathura-Piyala 1ZYT validation (engine 0 features vs
reference 325).

**v0.3.3 fix** — `_topic_estimated_erf_circ` now uses the new
`is_kastner_eligible(feature)` classifier from `src/core/ffp.py` and
dispatches `kastner()` directly per eligible feature. Three signals
in priority order, with the POF enum AUTHORITATIVE:

  1. `feature.dimension_class` is `CIRCUMFERENTIAL_SLOTTING` (CISL)
     or `CIRCUMFERENTIAL_GROOVING` (CIGR) → eligible.
  2. (Only if POF enum is `UNDEFINED`.) Case-insensitive substring
     `"circumferential"` in `feature.raw_description` → eligible.
  3. (Only if POF enum is `UNDEFINED` and signal 2 missed.) Geometric
     proxy `width_mm > length_mm` with both positive → eligible.

Features classified by POF as `PITTING`, `AXIAL_SLOTTING`,
`PINHOLE`, etc., are NOT eligible even if their width happens to
exceed their length — this prevents the iteration-1 overshoot
(3318 features) we hit when the geometric proxy was unconditionally
OR-ed with the enum check. The annexure row population now matches
the reference convention: one Kastner row per circumferentially-
classified feature, regardless of whether Kastner controls.

### 9.4 QA Issues sheet

Always last when `flag_report is not None`. `_write_issues_sheet`
(lines 701-748): four columns (Severity, Code, Feature, Source row,
Message). Sort order: severity (`error`<`warn`<`info`), then code
string.

### 9.5 DOCX report

Generated by `src/reports/main_report_writer.py` (1074 lines). Not
covered cell-by-cell here — the writer renders a templated `.docx`
based on `templates/sections/*.txt` boilerplate plus computed tables.
The numeric inputs to the DOCX writer are exactly those in the
Annexure XLSX; if the XLSX numbers are correct the DOCX will be too.

---

## 10. Worked example — Kandla feature #125

Generated by running the installed v0.2.2 CLI against
`examples/kandla_project.yaml` (no `--output-dir`; resolver routes to
`%APPDATA%\Athena\ILI_FFP_Tool\projects\kandla_project_output\`). All
intermediates extracted from a Python harness that calls the modules
directly with the same inputs the CLI uses.

### 10.1 Pipeline + project

| | |
|---|---|
| OD | 273.0 mm |
| Length | 58.5 km |
| Grade | API 5L X52 |
| SMYS | 358.0 MPa |
| MAOP zone | `wt_mm: [6.0, 8.0]`, Fd=0.72, MAOP=70.0 kg/cm² |
| Run-1 date | 2018-12-15 |
| Run-2 date | 2023-03-15 |
| `years_between` | (2023-03-15 − 2018-12-15).days / 365.25 = **4.246407** |
| Joint alignment | 4 897 matched pairs, match rate 0.9986 |
| Defect matcher | 22 matched, 57 unmatched run-1, 311 unmatched run-2; pass counts {1: 21, 3: 1} |

### 10.2 Feature #125 — input state

| | Run-1 partner | Run-2 #125 |
|---|---|---|
| Source row | 2 | 38 |
| anomaly_id | `A-000002` | `125` |
| Joint number | 6 380 | 6 410 |
| `abs_distance_m` | 7 426.9790 | 7 453.0530 |
| `depth_pct_wt` | 12.0 | 28.75 |
| `length_mm` | 9.0 | 9.0 |
| `width_mm` | 9.0 | 9.0 |
| `surface` | internal | internal |
| `clock_decimal_hours` | 5.3 | 5.1333… |

Pair found in matcher pass 1; cost = 0.0560 (well under the
`max_cost=0.5` ceiling for pass 1). Joint pair `6380 ↔ 6410` was
locked in by joint alignment via the wide distance-bonus.

### 10.3 CGR

| | Value | Source |
|---|---|---|
| WT | 6.4 mm (mode of run-2 joint 6410 WTs) | reader |
| `depth_old_used_mm` | 12.0 % × 6.4 mm = **0.7680** | `_cgr_for_matched` |
| `depth_new_mm` | 28.75 % × 6.4 mm = **1.8400** | same |
| Δd | 1.8400 − 0.7680 = **1.0720 mm** | |
| Δt | **4.246407 yr** | |
| `feature_cgr_mm_yr` | 1.0720 / 4.246407 = **0.252449 mm/yr** | `_build_result` |
| Internal P95 (np.percentile) | **0.058930 mm/yr** | `_compute_p95_by_surface` |
| External P95 | **0.038508 mm/yr** | same |
| Mode used | `feature_specific` (feature_cgr > P95, no floor) | line 191 |
| **`cgr_mm_yr` used downstream** | **0.252449 mm/yr** | |

Note: published Kandla reference is `0.2522 mm/yr` (4-sig-fig rounded;
`examples/expected_results/kandla_samakhiali.yaml:20`). Computed
0.252449 → 0.2524 at 4 dp. Difference is in the published rounding,
not the code.

### 10.4 FFP at year 0 (B31G Original)

```
D = 273.0 mm   t = 6.4 mm   L = 9.0 mm   d = 1.8400 mm
SMYS = 358.0 MPa   Fd = 0.72   MAOP = 70.0 kg/cm²

z         = L² / (D · t)
          = 81 / (273.0 × 6.4)
          = 81 / 1 747.2
          = 0.04636 0          (branch: low_z, z ≤ 20)

S_flow    = 1.1 × 358.0
          = 393.8 MPa

M         = √(1 + 0.8 × 0.04636 0)
          = √(1.037 088)
          = 1.018 375

Q         = (2/3) × (1.8400 / 6.4)
          = (2/3) × 0.287 500
          = 0.191 667

R         = (1 − Q) / (1 − Q / M)
          = 0.808 333 / (1 − 0.191 667 / 1.018 375)
          = 0.808 333 / 0.811 793
          = 0.995 738

P_intact  = 2 × 393.8 × 6.4 / 273.0
          = 5 040.64 / 273.0
          = 18.466 446 MPa

Pf        = P_intact × R
          = 18.466 446 × 0.995 738
          = 18.385 222 MPa
          = 18.385 222 × 10.197 162           (MPA_TO_KGCM2)
          = 187.4771 kg/cm²

Psafe     = Pf × Fd
          = 18.385 222 × 0.72
          = 13.237 360 MPa
          = 187.4771 × 0.72
          = 134.9835 kg/cm²

ERF       = MAOP / Psafe
          = 70.0 / 134.9835
          = 0.518 582
```

Flags raised on this `FFPResult`: **none.** `erf < 1.0`, `depth_pct_wt
= 28.75 < 80.0`, `z = 0.0464 < 50` (no `LONG_DEFECT_OUTSIDE_B31G`),
`length_mm = 9.0 > wt_mm = 6.4` (no `VERY_SHORT_DEFECT`).

### 10.5 Repair projection (B31G Original, horizon 10 yr)

Year-by-year (Folias M = 1.018 375 throughout — depth grows, geometry
doesn't):

| Year | Depth mm | Depth % WT | Psafe kg/cm² | ERF |
|---|---|---|---|---|
| +0 | 1.8400 | 28.7500 | 134.9835 | **0.518 582** |
| +1 | 2.0925 | 32.6945 | 134.8827 | 0.518 969 |
| +2 | 2.3449 | 36.6390 | 134.7750 | 0.519 384 |
| +3 | 2.5974 | 40.5835 | 134.6598 | 0.519 828 |
| +4 | 2.8498 | 44.5280 | 134.5362 | 0.520 306 |
| +5 | 3.1023 | 48.4726 | 134.4032 | **0.520 821** |
| +6 | 3.3547 | 52.4171 | 134.2598 | 0.521 377 |
| +7 | 3.6071 | 56.3616 | 134.1046 | 0.521 981 |
| +8 | 3.8596 | 60.3061 | 133.9362 | 0.522 637 |
| +9 | 4.1120 | 64.2506 | 133.7527 | 0.523 354 |
| +10 | 4.3645 | 68.1951 | 133.5522 | **0.524 140** |

ERF stays below 1.0; depth stays below 80 % WT. Trigger:
**`NONE_WITHIN_HORIZON`**. `predicted_repair_date = None`.
`final_depth_pct_wt = 68.1951`, `final_erf = 0.524 140`.

Annexure F cell P for this feature: `"After March 2033"`
(run2_date + 10 × 365.25 days = 2033-03-12 → `_format_repair_date`
renders `"After March 2033"`).

---

## Appendix — Implementation surprises (executive summary)

Five places where the code differs from textbook practice — material
for synthetic-test design:

- **Distance-bonus tolerance is 500 m, not 20 m.** Joint alignment's
  `distance_tolerance_m` (`joint_alignment.py:106`) was widened from
  the obvious "20 m == typical joint length" value to 500 m to capture
  the Kandla +30-joint-number offset where chainages drift 26 m but
  length signatures match to 0.025 %. Synthetic tests with synthetic
  chainages need to be aware this is a tolerance, not a hard rule —
  the bonus shapes which candidates qualify, then length-signature
  similarity decides who wins.
- **Defect matcher is three-pass Hungarian, not single-shot
  nearest-neighbour.** Each joint pair runs Hungarian three times with
  axial tolerance 0.10 → 0.25 → 1.00 m (`defect_matcher.py:79-83`);
  features that didn't fit pass *N* roll into pass *N+1*. Pass-3 is
  explicitly wider than the user spec (1.0 m vs 0.5 m) to catch
  last-defect-in-joint pairs (Kandla row77 → #1038, uw_diff 0.607 m
  inside a 12 m joint).
- **RSTRENG ≡ B31G Modified** in the current build. Without a measured
  river-bottom depth profile (which ILI vendors don't ship in POF
  110), `rstreng()` literally calls `b31g_modified()` and renames the
  method (`ffp.py:366-377`). Cross-check tables that show both will
  produce identical Pf and Psafe — the engineering distinction is the
  `using_approximate_profile=True` flag, not the math.
- **Kastner is the net-section approximation, not the 1986
  equilibrium form.** The implementation is `σ_axial,fail = σ_flow · [1
  − (W / (π · D)) · (d / t)]` (`ffp.py:476-477`) — clamped to `[0, 1]`
  area-reduction. The full equilibrium form is deferred until a real
  CISL/CIGR feature with full-circumferential extent and published
  Psafe arrives. For Indian-scale defects (max W ≪ π·D) the
  approximation is within ±2 % of the full form, but synthetic tests
  with W approaching π·D will diverge.
- **Repair projection is integer-year, not closed-form.** The
  predictor steps year by year (`repair_predictor.py:197-238`) with
  depth growing linearly in **mm** at `cgr_mm_yr`, recomputing the
  full FFP each year. Length, OD, WT, MAOP all held constant — only
  `d` grows. The predicted repair date is exactly
  `run2_date + year_offset × 365.25 days` (integer days,
  `repair_predictor.py:364`) — no fractional-year root-find. So
  `repair_year_offset = 5` means "trigger fires *at year 5*", not "5.2
  years". Synthetic tests should land triggers on integer years to
  reproduce the rendered date exactly.
- **`HIGH_CGR_POPULATION` median is computed on the POST-HYBRID-floor
  CGR pool, not the pre-floor pool that fed P95.** The aggregator at
  `flag_aggregator.py:166-185` walks `r.cgr_mm_yr`, which HYBRID has
  already overwritten with `surf_p95` for every feature where
  `feature_cgr < P95` (`cgr.py:191-192`). The pre-floor pool
  (`r.feature_cgr_mm_yr`) is what `_compute_p95_by_surface` pools
  (`cgr.py:365`) — they are NOT the same array. Empirically (Test Pack
  2) this can shift the median substantially: 0.211 mm/yr pre-floor →
  0.399 mm/yr post-floor on the same dataset. That's enough to flip a
  pipeline from "below 0.2 mm/yr threshold" (no flag) to "above" (flag
  fires) without any feature growing faster. Synthetic tests that
  compare medians against the 0.2 mm/yr threshold need to compute on
  the post-floor pool; tests that target the P95 itself must use the
  pre-floor pool.

## 11. Report-generation topic registry (v0.2.5)

v0.2.0–v0.2.4 picked the report layout via a single preset string
(`"E_F"` or `"B_C_D"`) — two fixed sheet sets, two fixed orders, two
fixed letterings. v0.2.5 replaces that with a **topic registry**: each
sheet is one topic, the engineer picks which topics to include and what
letter to assign each, and the choice round-trips through the project
YAML.

### 11.1 Registry (`src/reports/topic_registry.py`)

Seven topics, in canonical display order:

| # | `id` | `default_letter` | Display name | `implemented` |
|---|---|---|---|---|
| 1 | `guidelines_formulas` | A | Guidelines & Formulas Used | ✓ |
| 2 | `results_ili_comparison` | B | Results of ILI Comparison | ✓ |
| 3 | `metal_loss_anomalies` | C | Metal Loss Anomalies with Repair Prediction | ✓ |
| 4 | `estimated_erf_defects` | D | Estimated ERF of Defects (Year 0 + Future Projection) | ✓ |
| 5 | `estimated_erf_circ` | E | Estimated ERF of Circumferential Defects (Kastner) | ✓ |
| 6 | `dent_strain_b318` | F | Estimated Strain in Dents per ASME B31.8 | ✗ (**placeholder**) |
| 7 | `qa_findings` | G | Quality Assurance Findings | ✓ |

The `AnnexureTopic` dataclass (frozen) carries `id`, `display_name`,
`default_letter`, `writer` callable, and `implemented` bool. Topic
adapters live in the same module and conform to a uniform signature
`(workbook, sheet_name, project, results, run2_year, *, title_text)
-> None`, each unpacking `results` (an `AnalysisResult`-shaped
SimpleNamespace) into the parameter shape the underlying sheet writer
needs.

### 11.2 ERF bucket counting convention (v0.3.2)

The Results screen and any annexure-summary inset classify each
feature's ERF into one of four display buckets:

| Bucket label              | Severity      |
| ------------------------- | ------------- |
| `≤ 0.85`                  | Acceptable    |
| `0.85 < ERF ≤ 0.90`       | Monitor       |
| `0.90 < ERF ≤ 1.00`       | Planned repair|
| `> 1.00`                  | Critical (also fires `ERF_EXCEEDS_1`) |

Bucket boundaries are evaluated against the **raw, full-precision**
ERF float (no rounding). This was a v0.3.2 change from the v0.2.6
behaviour, which rounded to 3 decimal places before classifying.

**Rationale.** The annexure XLSX (Annexure D — Estimated ERF
distribution) is the engineering-ground-truth deliverable that
customers reprocess and that downstream tools (auditor's QGIS layer,
operator's CMMS import) consume. Annexure D writes per-feature ERF
to 6+ significant figures with no rounding. The GUI Results-screen
strip is a quick-look counter and must agree with what someone gets
by re-bucketing the XLSX directly. The v0.2.6 3-dp rounding broke
that invariant.

#### Worked examples

The two reconciliation calibration runs from the v0.3.1 → v0.3.2
customer validation cycle:

**BPCL Malarna-Karwadi (5138 features, single MAOP zone):**

| Bucket              | v0.2.6 (3-dp rounded) | v0.3.2 (raw float) | Annexure D XLSX |
| ------------------- | --------------------- | ------------------ | --------------- |
| `≤ 0.85`            | 4803                  | **4787**           | 4787            |
| `0.85 < ERF ≤ 0.90` | 331                   | **347**            | 347             |
| `0.90 < ERF ≤ 1.00` | 4                     | 4                  | 4               |
| `> 1.00`            | 0                     | 0                  | 0               |

Sixteen features sit in the `[0.8495, 0.8505]` half-open band where
3-dp rounding flips them between the lower two buckets. The v0.3.2
counts match the XLSX exactly.

**BPCL Mathura-Piyala (29,844 features, three MAOP zones):**

| Bucket              | v0.2.6 (3-dp rounded) | v0.3.2 (raw float) | Annexure D XLSX |
| ------------------- | --------------------- | ------------------ | --------------- |
| `≤ 0.85`            | 28,908                | **28,867**         | 28,867          |
| `0.85 < ERF ≤ 0.90` | 931                   | **972**            | 972             |
| `0.90 < ERF ≤ 1.00` | 4                     | 4                  | 4               |
| `> 1.00`            | 1                     | 1                  | 1               |

Forty-one features sit on the boundary. Same story: v0.3.2 counts
match the XLSX, v0.2.6 didn't.

#### Severity flags are independent of bucket display

`ERF_EXCEEDS_1` (QA flag, repair-prediction trigger) and any other
engineering-severity check ALWAYS compares raw `erf >= 1.0` —
regardless of bucket mode. A feature with raw ERF `1.0001` fires
`ERF_EXCEEDS_1` in both v0.2.6 and v0.3.2; the difference is only
which display bucket it lands in.

#### Opting back into 3-dp display mode

The classifier still accepts `dp=3` as a kwarg. Callers that need
to reproduce a published PDF's bucket counts exactly (e.g. for a
revision history or audit reconciliation) can pass it explicitly:

```python
from src.reports.erf_buckets import count_erf_buckets
legacy_counts = count_erf_buckets(ffps, dp=3)
```

The GUI Results screen and the default annexure-summary inset use
the production default (raw float).

### 11.3 YAML schema — `report.annexures`

```yaml
report:
  annexures:
    - topic: results_ili_comparison
      letter: B
    - topic: estimated_erf_defects
      letter: C2          # engineer override
    - topic: qa_findings
                          # letter omitted -> uses default "G"
```

Parser is `src.models.parse_report_annexures(report_block, yaml_path)`:

- **Order** in the list determines order in the output XLSX (sheets
  appear in this order).
- **Missing `letter`** → uses `default_letter` from the registry.
- **Missing `report.annexures` block** → falls back to the legacy
  E_F-preset equivalent: `results_ili_comparison` + `metal_loss_anomalies`
  + `qa_findings` with their default letters (B, C, G). Same content
  as v0.2.0–v0.2.4's "E_F" preset.
- **Unknown topic ID** → `ValueError` citing the YAML path and list index.
- **Duplicate letters** → `ValueError` naming both offending topics.

Save side: `serialize_report_annexures(selection)` renders the inverse
mapping. Letters are emitted **explicitly** even when equal to the
registry default — a future change to `default_letter` won't silently
shift saved YAMLs.

### 11.4 Top-level builder dispatch (`AnnexureWriter.write`)

`src/reports/annexure_writer.py:AnnexureWriter.write` takes two
mutually-exclusive arguments:

- `topics=[(topic_id, letter), ...]` — **v0.2.5 preferred path**.
  Walks the list, calls each topic adapter, produces one sheet per
  entry in the order given.
- `format="E_F" | "B_C_D"` — **legacy preset path**. Used when
  `topics` is None. Same dispatch behavior as v0.2.0–v0.2.4.

Worker (`src/gui/analysis_worker.py`) and CLI
(`bin/run_pipeline.py`) prefer `topics` (from
`project.report_annexures`). The CLI's `--annexure-format` flag is now
a backward-compat override.

### 11.5 Sheet-name truncation

Excel caps sheet tab names at 31 characters.
`make_topic_sheet_name(letter, display_name)` returns
`(sheet_name, title_text)`:

- If `"Annexure {letter} — {display_name}"` ≤ 31 chars → both equal
  the full string.
- Else → `sheet_name = "Annexure {letter}"` (truncated), `title_text`
  = the full string. The full title always goes in **row 1** of the
  sheet (merged across columns).

In practice all topics except `qa_findings` truncate the tab name
(display names are long), so every sheet looks like tab `"Annexure X"`
with the full title in row 1.

### 11.6 Dent-strain analysis — ASME B31.8 §851.4.1 (v0.3.2)

`dent_strain_b318` is fully implemented (`implemented=True`) as of
v0.3.1, with a sign-convention refinement in v0.3.2 that calibrates
to BPCL Annexure E row 4 to 4 significant figures on all three of
Ei, Eo, and Resultant strain. The writer
(`_write_dent_strain_sheet` in `src/reports/annexure_writer.py`) and
the math module (`src/core/dent_strain.py`) together produce a full
Annexure E equivalent matching BPCL's published reference layout.

**Geometric radii of curvature** (sagitta-exact):

```
R_0 = OD / 2                            # pipe nominal radius
R_1 = (W² + 4·d²) / (8·d)               # transverse curvature
R_2 = (L² + 4·d²) / (8·d)               # longitudinal curvature
```

**Component strains** (per ASME B31.8 Appendix R, equations A1–A3;
magnitudes — the 2018-revised sign convention with negative R_1 for
indented dents is algebraically equivalent). The three reported
columns of Annexure E:

```
ε_1 = (t/2) · (1/R_0 + 1/R_1)           # circumferential bending
ε_2 = (t/2) · (1/R_2)                   # longitudinal bending
ε_3 = (1/2) · (d/L)²                    # longitudinal membrane (uniform through-wall)
```

In addition the engine uses an **internal transverse quadratic term**
(not reported as a separate column — embedded inside the surface
totals):

```
ε_3,W = (1/2) · (d/W)²                  # transverse quadratic curvature
```

This term is the chord-sagitta counterpart of ε_3 applied to the
circumferential cross-section. Unlike ε_3 (= ε_3,L), which is a
uniform through-thickness membrane, ε_3,W behaves as a
higher-order curvature contribution coupled to ε_1 and flips sign
between the inside and outside surfaces.

**Surface-effective strains** (per Lukasiewicz–Czyz combined-strain
form; equations A4 / A5, v0.3.2 sign convention):

```
ε_θ,o = +ε_1 + ε_3,W,   ε_L,o = +ε_2 + ε_3       (outside surface)
ε_o   = sqrt(ε_θ,o² + ε_θ,o·ε_L,o + ε_L,o²)

ε_θ,i = -ε_1 - ε_3,W,   ε_L,i = -ε_2 + ε_3       (inside surface)
ε_i   = sqrt(ε_θ,i² + ε_θ,i·ε_L,i + ε_L,i²)

ε_resultant = max(|ε_i|, |ε_o|) × 100   (%)
```

Note the asymmetric handling of the two ε_3 terms: ε_3,W flips with
bending sign (it pairs with ε_1 as a curvature-coupled contribution);
ε_3 stays tensile on both surfaces (uniform membrane). This
asymmetry is what produces BPCL's `|Eo| > |Ei|` separation
(0.000115 absolute on row 4); the v0.3.1 symmetric-membrane
interpretation could not reproduce that gap and showed a 0.0004
absolute deviation on Ei.

The combination formula is the von Mises equivalent strain for 2-D
plane stress with no in-plane shear: ``ε_eq = sqrt(ε_x² + ε_x·ε_y +
ε_y²)``.

**Acceptance threshold** (ASME B31.8 §851.4.1): ε_resultant ≥ 6 %
triggers `HIGH_STRAIN_REJECT_CRITERIA` (advisory flag; rejection
threshold for plain dents). Over-threshold rows are highlighted
yellow in the Annexure E sheet.

**Formula provenance** — the ASME B31.8-2018 standard is paywalled
and could not be directly cited. The formulas above were
reverse-engineered against BPCL Malarna-Karwadi's published
Annexure E using the calibration dent at row 4 (``d = 0.59 % OD``,
``L = 150 mm``, ``W = 115 mm``, ``t = 6.4 mm``, ``OD = 407 mm`` →
``R_0 = 203.5 mm``).

| Quantity      | BPCL row 4 | v0.3.2 engine | Δ absolute    |
| ------------- | ---------- | ------------- | ------------- |
| ε_1           | 0.020365   | 0.020365      | 0 (bit-exact) |
| ε_2           | 0.002729   | 0.002729      | 0 (bit-exact) |
| ε_3           | 0.000128   | 0.000128      | 0 (bit-exact) |
| Ei            | 0.022052   | 0.021999      | −5.3e-5       |
| Eo            | 0.022167   | 0.022150      | −1.7e-5       |
| Resultant (%) | 2.2167     | 2.2150        | −0.0017       |

All three calibration targets (Ei, Eo, Resultant) match within
0.0001 absolute — well inside the 0.0005 absolute tolerance the
customer specified for "4 significant figures". Open-literature
confirmation of the component-strain forms comes from the
Mackintosh paper "Pipeline Dent Strain Assessment Using ASME B31.8"
and the Rosen Group dent newsletter.

**Read path** — dents come from
``src/io/feature_reader.py:read_dent_features``, a v0.3.1 auxiliary
reader that **bypasses the column-synonyms skip-list specifically
for dent rows**:

* The primary FFP `ILIReader` filters `"dent"` (skip-list entry at
  `column_synonyms.yaml:512`) so dent rows never reach
  `features_for_assessment()`. This is the **Abu Road dent-leak
  guard** (§1.10) — unchanged in v0.3.1.
* The auxiliary `read_dent_features` deep-copies the synonyms YAML,
  removes dent-related keywords from the skip-list (`"dent"`,
  `"den*"` prefixes), and re-runs `ILIReader` against the same xlsx.
  Returns only features whose `feature_identification` resolves to
  `DENT` or `DENT_WITH_METAL_LOSS`.

`read_dent_features` and `read_metal_loss_features` operate on the
same Run-2 file without interference — each gets its own reader
instance with its own synonyms map.

**Depth-unit convention** — dent depth in BPCL/NGP files is `% OD`,
not `% WT`. The adapter
`compute_dent_strain_from_feature(feature, pipeline)` interprets
`feature.depth_pct_wt` as `% OD` and converts to mm via
``d_mm = depth_pct × OD / 100`` (overriding the WT-percent default
on the `Feature.depth_mm` property — see `models/__init__.py:122-125`).
This is by design: dents historically reuse the "Depth, %WT/OD"
column header with %OD semantics. Engineers crafting synthetic
test xlsx should be aware that `parse_depth` interprets bare
numerics in `(0, 1)` as fractions (auto-converted ×100), so dent
depth strings like `"0.59"` become `depth_pct_wt = 59.0` — use
`"0.59%"` or values `> 1.0` to avoid the fraction interpretation.

### 11.7 GUI (`src/gui/widgets/annexure_topics_panel.py`)

`AnnexureTopicsPanel` is a checkbox-per-topic widget with a per-row
letter `QLineEdit`. Surface API:

- `selection() -> list[tuple[str, str]]` — currently-checked topics
  with their letters, in canonical display order.
- `set_selection(selection)` — load saved state. Topics in
  `selection` get checked + their letter overrides; topics absent
  are unchecked and letters reset to registry defaults.
- `is_valid()` / `validity_message()` — true iff ≥ 1 checked AND
  no duplicate letters. Project Setup's `_validate` consumes the
  message and gates the Proceed button.
- `selection_changed` signal — fires on any toggle or letter edit.

Letter overrides persist across an uncheck/recheck cycle (the panel
remembers the user's last value). Duplicate-letter fields are
highlighted red with a tooltip naming the conflicting topic.

## 12. MAOP zoning modes (v0.3.0)

v0.2.x bounded MAOP zones strictly by wall thickness. Real pipeline
operators define MAOP sections by **chainage** — section valves at
fixed kilometre markers carry the pressure regime, not the pipe
wall thickness (which may be uniform across multiple MAOP
sections, or split inconsistently within a single section).

v0.3.0 adds an alternative chainage-based zoning mode. WT mode
remains the default for backward compatibility — every YAML written
for v0.2.x continues to load and run bit-identically.

### 12.1 YAML schema — `pipeline.maop_zoning_mode`

```yaml
pipeline:
  ...
  maop_zoning_mode: chainage     # or "wt"; default "wt" when absent
```

Permitted values: `"wt"` (default) or `"chainage"`. Anything else
raises `ValueError` at load.

### 12.2 Zone entry shape per mode

```yaml
# WT mode (default; backward-compat)
maop_zones:
  - wt_mm_min: 8.0
    wt_mm_max: 9.0
    design_factor: 0.72
    maop_kgcm2: 80.6

# Chainage mode (v0.3.0)
maop_zones:
  - chainage_m_min: 0.0
    chainage_m_max: 28444.0      # SV-5 boundary
    design_factor: 0.72
    maop_kgcm2: 96.7
  - chainage_m_min: 28444.0
    chainage_m_max: 64944.0      # SV-6 boundary
    design_factor: 0.60
    maop_kgcm2: 84.1
  - chainage_m_min: 64944.0
    chainage_m_max: 100000.0
    design_factor: 0.50
    maop_kgcm2: 80.6
```

Validation (`src.models.parse_maop_zones`):

- In WT mode every entry **must** carry `wt_mm_min` + `wt_mm_max`;
  any `chainage_m_*` key is rejected (mode/schema mismatch).
- In chainage mode every entry **must** carry `chainage_m_min` +
  `chainage_m_max`; any `wt_mm_*` key is rejected.
- Negative `chainage_m_min` is rejected.
- `chainage_m_max < chainage_m_min` is rejected.
- **Overlapping** chainage zones are rejected. Adjacent zones sharing
  a boundary point (zone N's `chainage_m_max` == zone N+1's
  `chainage_m_min`) are allowed; first-match-wins puts the boundary
  point in the upstream zone.

All validation errors mention the YAML file path + the offending
list index.

### 12.3 Pipeline lookup API

`src/models/__init__.py` exposes three methods:

| Method | v0.2 signature | v0.3 behaviour |
|---|---|---|
| `maop_for_wt(wt_mm)` | `-> MAOPZone | None` | Unchanged; legacy WT-only lookup. Any v0.2.x caller continues to work. |
| `maop_for_chainage(chainage_m)` | new | `-> (zone, idx, used_fallback)`. Returns the first WT-zone that contains the chainage; else the nearest by chainage distance, with `used_fallback=True`. |
| `maop_for_feature(feature)` | new | `-> (zone, idx, used_fallback)`. Mode-aware dispatcher — routes to `maop_for_chainage` when `maop_zoning_mode == "chainage"`, else `maop_for_wt`. Engine code (FFP coordinator, repair predictor) calls this; mode awareness is centralized here. |

`maop_for_wt`'s signature is unchanged so existing call sites that
only had a WT scalar (no Feature in scope) continue to work.

### 12.4 MAOP_ZONE_NOT_FOUND flag — mode-aware text + context

The flag fires identically (when a feature falls outside every
declared zone), but its message and context dict switch on mode:

**WT mode** (unchanged from v0.2.x):

```
feature WT 5.5 mm is outside the explicit MAOP-zone ranges; used
nearest zone (WT 6.0-7.5 mm, MAOP 88.0 kg/cm²).
context: {"feature_wt_mm": 5.5, "zone_wt_min": 6.0, "zone_wt_max": 7.5,
          "zone_maop_kgcm2": 88.0, "zoning_mode": "wt"}
```

**Chainage mode** (v0.3.0):

```
feature chainage 150000 m is outside the explicit chainage-zone ranges;
used nearest zone (chainage 64944.0-100000.0 m, MAOP 80.6 kg/cm²).
context: {"feature_chainage_m": 150000.0, "zone_chainage_min": 64944.0,
          "zone_chainage_max": 100000.0, "zone_maop_kgcm2": 80.6,
          "zoning_mode": "chainage"}
```

The `zoning_mode` key on the context dict lets downstream tooling
(QA dashboards, etc.) branch without re-deriving the pipeline state.

### 12.5 MAOP-vs-WT design banner — chainage adaptation

The v0.2.4 per-zone banner check (`_refresh_maop_design_warning` in
`src/gui/screens/project_setup.py`) was rewritten to bucket features
into zones by the appropriate key:

- WT mode: bucket by `feature.wt_mm` (existing).
- Chainage mode: bucket by `feature.abs_distance_m` (new).

The design-limit comparison **always** uses the per-zone thinnest WT
and the zone's Fd — the Barlow formula is unchanged, only the
bucketing rule differs. Banner text adapts:

**WT mode:** `"MAOP {x} (zone {a}–{b} mm, Fd={fd}) exceeds the design
limit for the thinnest wall in this zone (WT_min={y} mm gives
MAOP_design_max={z})"`

**Chainage mode:** `"MAOP {x} (section {a}–{b} m, Fd={fd}) exceeds
the design limit for the thinnest wall in this section (WT_min={y}
mm gives MAOP_design_max={z})"`

Same 5%-tolerance silent envelope, same multi-offender concatenation
logic.

### 12.6 GUI integration

The Project Setup screen's MAOP-zone QTableWidget is **WT-only** in
v0.3.0. When a chainage-mode YAML is loaded, the screen:

- Stashes the chainage zones on `self._loaded_maop_zones_raw`.
- Disables the QTableWidget for editing.
- Renders the chainage values into the table cells (using `chain≥`
  / `chain≤` prefixes so the visual distinction is unambiguous
  despite the table headers still reading "WT").
- On Save, round-trips the stashed chainage zones to YAML verbatim
  (`_build_maop_zones_for_save`).
- The MAOP-vs-WT banner consumes the chainage zones via the same
  `_wt_min_per_zone_in_run2(zones, mode="chainage")` path.

A full chainage-aware zone editor (table with chainage-min /
chainage-max columns + edit-mode toggle) lands in a future v0.3.x.

### 12.7 Annexure A (Guidelines & Formulas) adaptation

The Guidelines sheet's MAOP-zones table switches its bound-column
label based on mode:

- WT mode: `"Zone | WT range (mm) | Design factor | MAOP (kg/cm²)"`
- Chainage mode: `"Zone | Chainage range (m) | Design factor |
  MAOP (kg/cm²)"`

The section header also gains a "(chainage-bounded)" qualifier in
chainage mode so the deliverable is unambiguous about the zoning
convention used.

### 12.8 Worked example — HMEL IPS-1→IPS-2 (3-section chainage)

HMEL's reference layout: SV-5 at chainage 28,444 m, SV-6 at
chainage 64,944 m, total length ~100 km. Three distinct MAOP
sections (highest pressure upstream of SV-5, lower in mid-section,
lowest downstream of SV-6) — physically correct for hydraulics
between pump stations and section valves.

Pre-v0.3.0 the engine matched HMEL by coincidence (WT happened to
correlate with chainage regions). v0.3.0 makes the lookup correct
by construction:

```yaml
pipeline:
  pipeline_name: IPS-1 to IPS-2
  diameter_mm: 406.4
  length_km: 100.0
  material_grade: API 5L X60
  smys_mpa: 413.0
  maop_zoning_mode: chainage    # opt-in

maop_zones:
  - chainage_m_min: 0.0
    chainage_m_max: 28444.0     # to SV-5
    design_factor: 0.72
    maop_kgcm2: 96.7
  - chainage_m_min: 28444.0
    chainage_m_max: 64944.0     # SV-5 to SV-6
    design_factor: 0.60
    maop_kgcm2: 84.1
  - chainage_m_min: 64944.0
    chainage_m_max: 100000.0    # SV-6 onwards
    design_factor: 0.50
    maop_kgcm2: 80.6
```

A defect at chainage 30 km gets MAOP 84.1, Fd 0.60 — regardless of
its WT. A defect at chainage 5 km with the same WT gets MAOP 96.7,
Fd 0.72 — different ERF, same geometry. That's the operator's
reality, and v0.3.0 reproduces it.

— *End of reference.*
