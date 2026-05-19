"""
Data models for ILI FFP/CGR analysis.

All quantities use SI units internally:
  - distance: metres
  - wall thickness, length, width: millimetres
  - depth: stored as % WT (0-100) AND mm (computed from WT)
  - clock position: decimal hours, range [0.0, 12.0)
  - pressure: kg/cm² (industry standard in Indian pipeline reports)
  - SMYS: MPa
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from enum import Enum
from typing import Any


# ============================================================================
# ENUMS (aligned with POF 110 Appendix 2)
# ============================================================================

class Surface(str, Enum):
    INTERNAL = "internal"
    EXTERNAL = "external"
    MIDWALL = "midwall"
    UNKNOWN = "unknown"


class DimensionClass(str, Enum):
    """POF 110 Feature class enumeration."""
    GENERAL = "GENE"
    PITTING = "PITT"
    PINHOLE = "PINH"
    AXIAL_GROOVING = "AXGR"
    AXIAL_SLOTTING = "AXSL"
    CIRCUMFERENTIAL_GROOVING = "CIGR"
    CIRCUMFERENTIAL_SLOTTING = "CISL"
    UNDEFINED = "UNDEFINED"

    @property
    def is_circumferential(self) -> bool:
        """Circumferential defects use Kastner method instead of B31G."""
        return self in (
            DimensionClass.CIRCUMFERENTIAL_GROOVING,
            DimensionClass.CIRCUMFERENTIAL_SLOTTING,
        )


class FeatureIdentification(str, Enum):
    """POF 110 Feature identification enumeration (subset relevant to ML analysis)."""
    CORROSION = "CORR"
    CORROSION_CLUSTER = "COCL"
    METAL_LOSS = "ML"
    MANUFACTURING = "MIAN"
    MANUFACTURING_CLUSTER = "MIAC"
    DENT = "DENT"
    DENT_WITH_METAL_LOSS = "DEML"
    CRACK = "CRAC"
    GIRTH_WELD_ANOMALY = "GWAN"
    SPIRAL_WELD_ANOMALY = "SWAN"
    LONG_WELD_ANOMALY = "LWAN"
    UNDEFINED = "UNDEFINED"


class FFPMethod(str, Enum):
    B31G_ORIGINAL = "B31G_Original"
    B31G_MODIFIED = "B31G_Modified"
    RSTRENG = "RSTRENG"
    DNV_RP_F101 = "DNV_RP_F101"
    KASTNER = "Kastner"


class CGRMode(str, Enum):
    """Corrosion growth rate computation mode (project-selectable)."""
    FEATURE_SPECIFIC = "feature_specific"
    HYBRID = "hybrid"
    POPULATION_ONLY = "population_only"


# ============================================================================
# CORE DATA MODELS
# ============================================================================

@dataclass
class Feature:
    """A single ILI-reported feature, aligned with POF 110 Appendix 2."""
    anomaly_id: str
    source_run: str
    source_row: int = -1

    abs_distance_m: float = 0.0
    joint_number: int | None = None
    upstream_weld_dist_m: float | None = None
    clock_decimal_hours: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude_m: float | None = None

    wt_mm: float | None = None

    depth_pct_wt: float | None = None
    length_mm: float | None = None
    width_mm: float | None = None

    surface: Surface = Surface.UNKNOWN
    feature_identification: FeatureIdentification = FeatureIdentification.UNDEFINED
    dimension_class: DimensionClass = DimensionClass.UNDEFINED
    raw_description: str = ""

    vendor_erf: float | None = None
    vendor_psafe_kgcm2: float | None = None

    # Cluster awareness (set by the reader / clustering engine).
    is_cluster_parent: bool = False
    cluster_parent_id: str | None = None

    comments: str = ""

    @property
    def depth_mm(self) -> float | None:
        if self.depth_pct_wt is None or self.wt_mm is None:
            return None
        return self.depth_pct_wt / 100.0 * self.wt_mm

    def __post_init__(self) -> None:
        if self.depth_pct_wt is not None and not (0.0 <= self.depth_pct_wt <= 100.0):
            raise ValueError(
                f"depth_pct_wt must be in [0, 100]; got {self.depth_pct_wt!r}"
            )
        if self.clock_decimal_hours is not None and not (
            0.0 <= self.clock_decimal_hours < 12.0
        ):
            raise ValueError(
                f"clock_decimal_hours must be in [0, 12); got {self.clock_decimal_hours!r}"
            )
        if self.latitude is not None and not (-90.0 <= self.latitude <= 90.0):
            raise ValueError(
                f"latitude must be in [-90, 90]; got {self.latitude!r}"
            )
        if self.longitude is not None and not (-180.0 <= self.longitude <= 180.0):
            raise ValueError(
                f"longitude must be in [-180, 180]; got {self.longitude!r}"
            )
        if self.length_mm is not None and self.length_mm < 0:
            raise ValueError(f"length_mm must be >= 0; got {self.length_mm!r}")
        if self.width_mm is not None and self.width_mm < 0:
            raise ValueError(f"width_mm must be >= 0; got {self.width_mm!r}")
        if self.wt_mm is not None and self.wt_mm <= 0:
            raise ValueError(f"wt_mm must be > 0; got {self.wt_mm!r}")


@dataclass
class Joint:
    joint_number: int
    abs_distance_start_m: float
    length_m: float
    wt_mm: float | None = None
    pipe_type: str = ""
    weld_orientation: str = ""

    @property
    def abs_distance_end_m(self) -> float:
        return self.abs_distance_start_m + self.length_m


@dataclass
class MAOPZone:
    """One MAOP-bounded section of the pipeline.

    Two zoning modes (v0.3.0):

      * **WT mode** (legacy) — zone bounded by a wall-thickness range.
        ``wt_mm_min`` / ``wt_mm_max`` are set; ``chainage_m_*`` are None.

      * **Chainage mode** (new) — zone bounded by a chainage range, the
        operator-facing convention (section valves at fixed chainages
        delimit pressure regimes). ``chainage_m_min`` /
        ``chainage_m_max`` are set; ``wt_mm_*`` are None.

    A given zone is bounded by exactly one of the two pairs. The
    project YAML's ``pipeline.maop_zoning_mode`` field
    (``"wt"`` default, ``"chainage"`` opt-in) declares which bound
    type the entire ``maop_zones`` list uses. The
    :class:`Pipeline` dispatcher routes lookups based on that mode.
    """
    # WT-bounded fields (None when zone is chainage-bounded).
    wt_mm_min: float | None = None
    wt_mm_max: float | None = None
    design_factor: float = 0.72
    maop_kgcm2: float = 0.0
    # Chainage-bounded fields (None when zone is WT-bounded; v0.3.0).
    chainage_m_min: float | None = None
    chainage_m_max: float | None = None

    @property
    def is_chainage_bounded(self) -> bool:
        """True if this zone is chainage-bounded (v0.3.0)."""
        return self.chainage_m_min is not None

    def contains(self, wt_mm: float) -> bool:
        """WT-mode containment check. Returns False on a chainage-bounded
        zone — callers use :meth:`contains_chainage` for those.
        """
        if self.wt_mm_min is None or self.wt_mm_max is None:
            return False
        return self.wt_mm_min <= wt_mm <= self.wt_mm_max

    def contains_chainage(self, chainage_m: float) -> bool:
        """Chainage-mode containment check (v0.3.0)."""
        if self.chainage_m_min is None or self.chainage_m_max is None:
            return False
        return self.chainage_m_min <= chainage_m <= self.chainage_m_max

    @property
    def safety_factor(self) -> float:
        return 1.0 / self.design_factor


@dataclass
class Pipeline:
    pipeline_name: str = ""
    client_name: str = ""
    diameter_mm: float = 0.0
    length_km: float = 0.0
    install_year: int = 0
    material_grade: str = ""
    smys_mpa: float = 0.0
    product: str = ""
    service_class: str = "liquid"
    maop_zones: list[MAOPZone] = field(default_factory=list)
    # v0.3.0: declares whether `maop_zones` are bounded by WT (legacy
    # default) or chainage. Engine-side lookups dispatch based on this
    # value via :meth:`maop_for_feature`. Permitted values: "wt", "chainage".
    maop_zoning_mode: str = "wt"

    def maop_for_wt(self, wt_mm: float) -> MAOPZone | None:
        """WT-mode lookup. Walks WT-bounded zones, returns first
        containing, falls back to nearest by WT distance.

        Unchanged from v0.2.x — any caller with only a scalar WT
        continues to work. New v0.3.0 callers with a full feature
        should prefer :meth:`maop_for_feature`, which is mode-aware.
        """
        for zone in self.maop_zones:
            if zone.contains(wt_mm):
                return zone
        if not self.maop_zones:
            return None
        # WT-distance fallback. Skip any chainage-bounded zones (they
        # have None for wt bounds) — shouldn't happen in a well-formed
        # WT-mode pipeline.
        wt_zones = [z for z in self.maop_zones
                    if z.wt_mm_min is not None and z.wt_mm_max is not None]
        if not wt_zones:
            return self.maop_zones[0]
        return min(
            wt_zones,
            key=lambda z: min(abs(z.wt_mm_min - wt_mm), abs(z.wt_mm_max - wt_mm))
        )

    def maop_for_chainage(
        self, chainage_m: float
    ) -> tuple["MAOPZone | None", int | None, bool]:
        """v0.3.0 chainage-mode lookup.

        Returns ``(zone, zone_index, used_fallback)``:

          * ``zone`` — the matching :class:`MAOPZone`, or None when
            there are no zones at all.
          * ``zone_index`` — index into ``self.maop_zones`` of the
            chosen zone; None if no zones.
          * ``used_fallback`` — True when `chainage_m` fell outside
            every zone's range and a nearest-by-chainage fallback was
            applied (mirrors WT mode's
            :class:`QAFlagCode.MAOP_ZONE_NOT_FOUND` behaviour).
        """
        if not self.maop_zones:
            return None, None, False
        for i, zone in enumerate(self.maop_zones):
            if zone.contains_chainage(chainage_m):
                return zone, i, False
        # Nearest-by-chainage fallback. Skip WT-bounded zones (None bounds)
        # to mirror maop_for_wt's defensive behaviour.
        chainage_zones = [
            (i, z) for i, z in enumerate(self.maop_zones)
            if z.chainage_m_min is not None and z.chainage_m_max is not None
        ]
        if not chainage_zones:
            return self.maop_zones[0], 0, False
        best_i, best_zone = min(
            chainage_zones,
            key=lambda iz: min(
                abs(iz[1].chainage_m_min - chainage_m),
                abs(iz[1].chainage_m_max - chainage_m),
            ),
        )
        return best_zone, best_i, True

    def maop_for_feature(
        self, feature: "Feature"
    ) -> tuple["MAOPZone | None", int | None, bool]:
        """v0.3.0 mode-aware dispatcher.

        Routes to :meth:`maop_for_chainage` or :meth:`maop_for_wt`
        based on ``self.maop_zoning_mode``. Returned tuple shape
        matches :meth:`maop_for_chainage` so callers don't need to
        branch.

        For WT mode, the index is computed by linear scan; the
        ``used_fallback`` bool is True iff the returned zone does
        NOT contain the feature's WT (consistent with
        :class:`QAFlagCode.MAOP_ZONE_NOT_FOUND`'s pre-v0.3.0
        semantics).
        """
        if self.maop_zoning_mode == "chainage":
            return self.maop_for_chainage(feature.abs_distance_m)
        # WT mode (default).
        zone = self.maop_for_wt(feature.wt_mm or 0.0)
        if zone is None:
            return None, None, False
        try:
            idx = self.maop_zones.index(zone)
        except ValueError:
            idx = None
        fallback = (
            feature.wt_mm is None
            or not zone.contains(feature.wt_mm)
        )
        return zone, idx, fallback

    def feature_in_zone(self, zone: "MAOPZone", feature: "Feature") -> bool:
        """Whether `feature` is naturally inside `zone` (not a
        nearest-zone fallback). Mode-aware.
        """
        if self.maop_zoning_mode == "chainage":
            return zone.contains_chainage(feature.abs_distance_m)
        return zone.contains(feature.wt_mm or 0.0)


@dataclass
class ILIRun:
    run_id: str
    file_path: str = ""
    inspection_date: date | None = None
    vendor: str = ""
    tool_type: str = ""
    tool_serial: str = ""

    features: list[Feature] = field(default_factory=list)
    joints: list[Joint] = field(default_factory=list)
    skipped_count: int = 0
    parse_warnings: list[str] = field(default_factory=list)

    # Reader audit trail — populated by src/io/ili_reader.py.
    sheet_name: str = ""
    header_row_idx: int = -1
    column_map: dict[str, int] = field(default_factory=dict)
    rows_read: int = 0
    rows_filtered: dict[str, int] = field(default_factory=dict)

    # QA findings raised during parsing. Full QA pipeline arrives in Prompt 8;
    # for now this is the live channel for reader-emitted flags
    # (COORDINATES_SWAPPED, LAT_LON_OUT_OF_BOUNDS, …). Typed as Any to avoid
    # an import cycle with src.validation; the runtime type is QAFlag.
    qa_flags: list[Any] = field(default_factory=list)

    @property
    def feature_count(self) -> int:
        return len(self.features)

    @property
    def joint_count(self) -> int:
        return len(self.joints)

    def features_in_joint(self, joint_number: int) -> list[Feature]:
        return [f for f in self.features if f.joint_number == joint_number]

    def features_for_assessment(self) -> list[Feature]:
        """Features that should drive FFP assessment.

        Two filters live here:

          1. **Cluster children.** Vendor pipe tallies enumerate cluster
             parent rows AND their child rows individually. For B31G /
             RSTRENG / DNV the parent carries the bounding-box
             dimensions used for assessment; the children are
             redundant (already subsumed by the parent).

          2. **Non-metal-loss features.** Dents / welds / cracks share
             the "Anomaly" feature-type label but the FFP methods this
             tool implements aren't valid for them. The reader keeps
             them in `self.features` (so totals match the vendor's
             "Total rows" figure) and the filter happens here at
             consumption time. This is the dent-leak guard from the
             Abu Road 1ZYC bug — feature 1637 ("Dent complex") would
             otherwise have its 0.9 %OD depth treated as 90 %WT and
             produce a wildly wrong ERF.

        Use `.features` directly for raw-row counts (e.g. matching
        against a vendor's "Total rows on Defects sheet" figure).
        """
        # Keep the non-ML list in sync with src/io/ili_reader.py's
        # `_NON_METAL_LOSS_FIDS` and src/core/ffp.py's
        # `_NON_ASSESSABLE_FIDS`. The triple-redundancy is on purpose
        # — defense-in-depth against future vendor variants slipping
        # past column_synonyms.yaml.
        non_ml = {
            FeatureIdentification.DENT,
            FeatureIdentification.DENT_WITH_METAL_LOSS,
            FeatureIdentification.CRACK,
            FeatureIdentification.GIRTH_WELD_ANOMALY,
            FeatureIdentification.SPIRAL_WELD_ANOMALY,
            FeatureIdentification.LONG_WELD_ANOMALY,
        }
        return [
            f for f in self.features
            if f.cluster_parent_id is None
            and f.feature_identification not in non_ml
        ]


@dataclass
class FeatureMatch:
    feature_old: Feature
    feature_new: Feature
    match_score: float = 0.0                  # cost; lower is better
    confidence: float = 0.0                   # 1 - cost/max_cost, clamped to [0, 1]
    relaxation_level: int = 0                 # which DefectMatcher pass found it (1, 2, 3)
    cgr_mm_per_year: float = 0.0
    cgr_method: str = "feature_specific"
    qa_flags: set[str] = field(default_factory=set)

    @property
    def depth_delta_pct_wt(self) -> float | None:
        if self.feature_old.depth_pct_wt is None or self.feature_new.depth_pct_wt is None:
            return None
        return self.feature_new.depth_pct_wt - self.feature_old.depth_pct_wt

    @property
    def depth_delta_mm(self) -> float | None:
        if self.feature_old.depth_mm is None or self.feature_new.depth_mm is None:
            return None
        return self.feature_new.depth_mm - self.feature_old.depth_mm


@dataclass
class JointMatch:
    joint_old: Joint
    joint_new: Joint
    length_diff_m: float
    confidence: float
    matched_via: str = "length_alignment"


@dataclass
class MatchResult:
    feature_matches: list[FeatureMatch] = field(default_factory=list)
    joint_matches: list[JointMatch] = field(default_factory=list)
    unmatched_features_old: list[Feature] = field(default_factory=list)
    unmatched_features_new: list[Feature] = field(default_factory=list)
    unmatched_joints_old: list[Joint] = field(default_factory=list)
    unmatched_joints_new: list[Joint] = field(default_factory=list)
    match_rate: float = 0.0
    final_tolerances: dict[str, float] = field(default_factory=dict)
    cgr_p95_internal_mm_per_year: float | None = None
    cgr_p95_external_mm_per_year: float | None = None
    cgr_summary_stats: dict[str, float] = field(default_factory=dict)

    # Pair-scoped warnings raised by the matcher (e.g., "no clusters in either
    # run" + cluster-aware mode recommendation). Run-scoped warnings still go
    # on ILIRun.parse_warnings; these are specific to the run1/run2 pairing.
    warnings: list[str] = field(default_factory=list)

    # Structured QAFlag objects emitted by the matcher (LOW_DEFECT_MATCH_RATE,
    # NO_CLUSTERS_IN_EITHER_RUN, …). Aggregator pulls from here.
    qa_flags: list[Any] = field(default_factory=list)

    # Diagnostic: counts of matches accepted in each relaxation pass.
    matches_per_pass: dict[int, int] = field(default_factory=dict)

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "n_matched": len(self.feature_matches),
            "n_unmatched_old": len(self.unmatched_features_old),
            "n_unmatched_new": len(self.unmatched_features_new),
            "n_joints_matched": len(self.joint_matches),
            "match_rate": self.match_rate,
            "cgr_p95_int_mm_yr": self.cgr_p95_internal_mm_per_year,
            "cgr_p95_ext_mm_yr": self.cgr_p95_external_mm_per_year,
        }


@dataclass
class FFPResult:
    feature_id: str
    method: FFPMethod
    depth_pct_wt: float
    depth_mm: float
    length_mm: float
    wt_mm: float
    pf_kgcm2: float                                 # failure pressure (no design factor)
    sop_kgcm2: float                                # safe operating pressure = Pf × Fd
    maop_kgcm2: float                               # MAOP used in ERF
    erf: float                                      # MAOP / Psafe

    folias_factor_M: float | None = None
    z_value: float | None = None
    flow_stress_mpa: float | None = None
    area_metal_loss_ratio: float | None = None      # A/A0 used in this method
    branch_used: str = ""                           # "low_z" / "high_z" / "" (n/a)
    using_approximate_profile: bool = False         # True for RSTRENG without river-bottom profile
    is_controlling: bool = True                     # for circ defects, False if Kastner controls
    notes: list[str] = field(default_factory=list)  # human-readable annotations
    qa_flags: list[Any] = field(default_factory=list)  # structured QAFlag objects
    assessment_date: date = field(default_factory=date.today)


@dataclass
class RepairPrediction:
    feature_id: str
    feature: Feature
    cgr_mm_per_year: float
    yearly_assessments: list[FFPResult] = field(default_factory=list)
    predicted_repair_date: date | None = None
    repair_trigger: str = ""                       # "DEPTH_80" / "ERF_1.0" / "NONE_WITHIN_HORIZON"
    repair_year_offset: int | None = None          # years from run-2 inspection date
    final_depth_pct_wt: float = 0.0
    final_depth_mm: float = 0.0
    final_erf: float = 0.0
    final_psafe_kgcm2: float = 0.0
    method_used: FFPMethod | None = None
    horizon_years: int = 0
    qa_flags: list[Any] = field(default_factory=list)


def _default_report_annexures() -> list[tuple[str, str]]:
    """Lazy default for ``Project.report_annexures``.

    Imports inside the function body to avoid a circular import — the
    topic registry pulls in ``src.reports.annexure_writer`` which itself
    imports from ``src.models``.
    """
    from src.reports.topic_registry import default_annexure_selection
    return default_annexure_selection()


@dataclass
class Project:
    config_path: str = ""
    project_name: str = ""
    report_number: str = ""
    report_revision: str = "00"
    prepared_by: str = ""
    reviewed_by: str = ""
    approved_by: str = ""
    project_date: date = field(default_factory=date.today)

    pipeline: Pipeline = field(default_factory=Pipeline)
    run_1: ILIRun = field(default_factory=lambda: ILIRun(run_id="run_1"))
    run_2: ILIRun = field(default_factory=lambda: ILIRun(run_id="run_2"))

    match_result: MatchResult | None = None
    repair_predictions: list[RepairPrediction] = field(default_factory=list)

    config: dict[str, Any] = field(default_factory=dict)

    # v0.2.5: per-topic annexure selection. List of (topic_id, letter)
    # tuples in display order. Populated by `from_yaml`'s
    # ``report.annexures`` parser; defaults to the v0.2.0–v0.2.4 "E_F
    # preset" equivalent (results_ili_comparison + metal_loss_anomalies
    # + qa_findings) when the YAML has no ``report`` block.
    report_annexures: list[tuple[str, str]] = field(
        default_factory=_default_report_annexures,
    )

    tool_version: str = ""
    git_commit: str = ""
    run_timestamp: datetime = field(default_factory=datetime.now)
    input_file_hashes: dict[str, str] = field(default_factory=dict)

    @property
    def years_between_runs(self) -> float:
        if self.run_1.inspection_date is None or self.run_2.inspection_date is None:
            return 0.0
        delta = self.run_2.inspection_date - self.run_1.inspection_date
        return delta.days / 365.25

    @classmethod
    def from_yaml(cls, yaml_path: str) -> "Project":
        """Load a Project from a YAML config (e.g. config/default_project.yaml).

        Populates project metadata, pipeline physical properties, MAOP zones,
        and stows the raw mapping in `config` for downstream engines.
        """
        import yaml
        from pathlib import Path

        path = Path(yaml_path)
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        proj_meta = data.get("project", {}) or {}
        pipe_meta = data.get("pipeline", {}) or {}
        zones_raw = data.get("maop_zones", []) or []
        runs_meta = data.get("runs", {}) or {}

        smys = float(pipe_meta.get("smys_mpa") or 0.0)
        grade = str(pipe_meta.get("material_grade", ""))
        if smys == 0.0 and grade:
            lookup = pipe_meta.get("smys_lookup", {}) or {}
            smys = float(lookup.get(grade, 0.0))

        # v0.3.0: parse and validate the MAOP zoning mode + zones.
        maop_zoning_mode, maop_zones_list = parse_maop_zones(
            pipe_meta.get("maop_zoning_mode"),
            zones_raw,
            yaml_path=path,
        )

        pipeline = Pipeline(
            pipeline_name=str(proj_meta.get("pipeline_name", "")),
            client_name=str(proj_meta.get("client_name", "")),
            diameter_mm=float(pipe_meta.get("diameter_mm") or 0.0),
            length_km=float(pipe_meta.get("length_km") or 0.0),
            install_year=int(pipe_meta.get("install_year") or 0),
            material_grade=grade,
            smys_mpa=smys,
            product=str(pipe_meta.get("product", "")),
            service_class=str(pipe_meta.get("service_class", "liquid")),
            maop_zones=maop_zones_list,
            maop_zoning_mode=maop_zoning_mode,
        )

        def _parse_date(s: Any) -> date | None:
            if not s:
                return None
            try:
                return date.fromisoformat(str(s))
            except ValueError:
                return None

        r1 = runs_meta.get("run_1", {}) or {}
        r2 = runs_meta.get("run_2", {}) or {}
        run_1 = ILIRun(
            run_id="run_1",
            file_path=str(r1.get("file_path", "")),
            inspection_date=_parse_date(r1.get("inspection_date")),
            vendor=str(r1.get("vendor", "")),
            tool_type=str(r1.get("tool_type", "")),
            tool_serial=str(r1.get("tool_serial", "")),
        )
        run_2 = ILIRun(
            run_id="run_2",
            file_path=str(r2.get("file_path", "")),
            inspection_date=_parse_date(r2.get("inspection_date")),
            vendor=str(r2.get("vendor", "")),
            tool_type=str(r2.get("tool_type", "")),
            tool_serial=str(r2.get("tool_serial", "")),
        )

        # v0.2.5: parse the `report.annexures` block (or fall back to
        # the legacy default selection). Pass the YAML path through so
        # validation errors can point to the offending file.
        report_annexures = parse_report_annexures(
            data.get("report") or {},
            yaml_path=path,
        )

        return cls(
            config_path=str(path),
            project_name=str(proj_meta.get("project_name", "")),
            report_number=str(proj_meta.get("report_number", "")),
            report_revision=str(proj_meta.get("report_revision", "00")),
            prepared_by=str(proj_meta.get("prepared_by", "")),
            reviewed_by=str(proj_meta.get("reviewed_by", "")),
            approved_by=str(proj_meta.get("approved_by", "")),
            pipeline=pipeline,
            run_1=run_1,
            run_2=run_2,
            config=data,
            report_annexures=report_annexures,
        )


def parse_maop_zones(
    mode_raw: Any,
    zones_raw: Any,
    *,
    yaml_path: Any = None,
) -> tuple[str, list[MAOPZone]]:
    """Parse + validate the ``pipeline.maop_zoning_mode`` + ``maop_zones`` block.

    v0.3.0. Returns ``(mode, [MAOPZone, ...])``:

      * ``mode`` is ``"wt"`` (default when ``mode_raw`` is None/empty)
        or ``"chainage"``. Other values raise.
      * Each :class:`MAOPZone` has the bound-pair appropriate to the
        mode populated; the other pair stays None.

    Validation rules:

      * In WT mode every entry must carry ``wt_mm_min`` + ``wt_mm_max``.
        Any ``chainage_m_*`` key in any entry is rejected.
      * In chainage mode every entry must carry ``chainage_m_min`` +
        ``chainage_m_max``. Any ``wt_mm_*`` key in any entry is rejected.
      * Chainage zones with negative ``chainage_m_min`` are rejected.
      * Overlapping chainage zones are rejected (after sorting by
        ``chainage_m_min``, each zone's min must be ≥ previous zone's
        max).
      * ``mode_raw`` values other than ``"wt"`` / ``"chainage"`` are
        rejected.

    Errors mention the YAML path (when supplied) so the user can
    locate the offending source line.
    """
    loc = f" in {yaml_path}" if yaml_path is not None else ""

    # ---- 1. Resolve mode ------------------------------------------------
    if mode_raw is None or (isinstance(mode_raw, str) and not mode_raw.strip()):
        mode = "wt"
    elif isinstance(mode_raw, str):
        m = mode_raw.strip().lower()
        if m not in ("wt", "chainage"):
            raise ValueError(
                f"pipeline.maop_zoning_mode must be \"wt\" or \"chainage\"{loc}; "
                f"got {mode_raw!r}"
            )
        mode = m
    else:
        raise ValueError(
            f"pipeline.maop_zoning_mode must be a string{loc}; "
            f"got {type(mode_raw).__name__}"
        )

    if not isinstance(zones_raw, list):
        return mode, []
    zones_raw = [z for z in zones_raw if isinstance(z, dict)]

    # ---- 2. Validate schema match per mode ------------------------------
    zones: list[MAOPZone] = []
    for i, z in enumerate(zones_raw):
        entry_loc = f" (maop_zones[{i}]{', ' + loc.strip() if loc else ''})"
        has_wt = "wt_mm_min" in z or "wt_mm_max" in z
        has_ch = "chainage_m_min" in z or "chainage_m_max" in z
        if mode == "wt":
            if has_ch:
                raise ValueError(
                    f"maop_zoning_mode=\"wt\" but zone entry has "
                    f"chainage_m_* keys{entry_loc}. Either declare "
                    f"`pipeline.maop_zoning_mode: chainage` or remove "
                    f"the chainage bounds."
                )
            if "wt_mm_min" not in z or "wt_mm_max" not in z:
                raise ValueError(
                    f"WT-mode zone entry missing wt_mm_min / wt_mm_max"
                    f"{entry_loc}"
                )
            zones.append(MAOPZone(
                wt_mm_min=float(z["wt_mm_min"]),
                wt_mm_max=float(z["wt_mm_max"]),
                design_factor=float(z.get("design_factor", 0.72)),
                maop_kgcm2=float(z.get("maop_kgcm2", 0.0)),
            ))
        else:    # chainage
            if has_wt:
                raise ValueError(
                    f"maop_zoning_mode=\"chainage\" but zone entry has "
                    f"wt_mm_* keys{entry_loc}. Either declare "
                    f"`pipeline.maop_zoning_mode: wt` or remove "
                    f"the WT bounds."
                )
            if "chainage_m_min" not in z or "chainage_m_max" not in z:
                raise ValueError(
                    f"Chainage-mode zone entry missing chainage_m_min / "
                    f"chainage_m_max{entry_loc}"
                )
            lo = float(z["chainage_m_min"])
            hi = float(z["chainage_m_max"])
            if lo < 0:
                raise ValueError(
                    f"Chainage-mode zone has negative chainage_m_min="
                    f"{lo}{entry_loc}"
                )
            if hi < lo:
                raise ValueError(
                    f"Chainage-mode zone has chainage_m_max ({hi}) < "
                    f"chainage_m_min ({lo}){entry_loc}"
                )
            zones.append(MAOPZone(
                chainage_m_min=lo,
                chainage_m_max=hi,
                design_factor=float(z.get("design_factor", 0.72)),
                maop_kgcm2=float(z.get("maop_kgcm2", 0.0)),
            ))

    # ---- 3. Reject overlapping chainage zones ---------------------------
    if mode == "chainage" and len(zones) > 1:
        sorted_zones = sorted(zones, key=lambda z: z.chainage_m_min)
        for i in range(1, len(sorted_zones)):
            prev = sorted_zones[i - 1]
            cur = sorted_zones[i]
            if cur.chainage_m_min < prev.chainage_m_max:
                raise ValueError(
                    f"Overlapping chainage zones{loc}: "
                    f"[{prev.chainage_m_min}, {prev.chainage_m_max}] and "
                    f"[{cur.chainage_m_min}, {cur.chainage_m_max}] "
                    f"overlap at chainage {cur.chainage_m_min}. "
                    f"Zones must be non-overlapping."
                )

    return mode, zones


def parse_report_annexures(
    report_block: dict | None,
    yaml_path: Any = None,
) -> list[tuple[str, str]]:
    """Parse the ``report.annexures`` YAML block.

    v0.2.5. Returns a list of ``(topic_id, letter)`` tuples in the
    order they appeared in the YAML. Missing / empty / non-dict input
    falls back to :func:`default_annexure_selection` (legacy E_F
    equivalent).

    Validation rules:

      * Each entry must be a dict with a non-empty ``topic`` string.
      * ``topic`` must be a registered ID — otherwise raise
        :class:`ValueError` naming the unknown ID and the YAML path.
      * ``letter`` is optional; if omitted, the topic's
        ``default_letter`` is used.
      * Letters must be unique across the selection — duplicate
        letters raise :class:`ValueError` naming both offending
        topics.

    Error messages name the YAML file path (when supplied) so the
    user can fix the source.
    """
    from src.reports.topic_registry import (
        TOPIC_REGISTRY,
        default_annexure_selection,
    )

    if not isinstance(report_block, dict):
        return default_annexure_selection()
    raw = report_block.get("annexures")
    if raw is None:
        return default_annexure_selection()
    if not isinstance(raw, list):
        loc = f" in {yaml_path}" if yaml_path is not None else ""
        raise ValueError(
            f"report.annexures must be a list{loc}; got "
            f"{type(raw).__name__}"
        )
    if not raw:
        # Explicit empty list — distinguished from "missing block" and
        # treated as "no annexures, no sheets", but downstream may
        # disallow this. Return the legacy default to keep the
        # contract "at least the legacy three sheets" — the GUI also
        # enforces "≥ 1 selected".
        return default_annexure_selection()

    out: list[tuple[str, str]] = []
    seen_letters: dict[str, str] = {}   # letter -> topic_id
    for i, entry in enumerate(raw):
        loc = f" (report.annexures[{i}]" + (
            f" in {yaml_path})" if yaml_path is not None else ")"
        )
        if not isinstance(entry, dict):
            raise ValueError(
                f"report.annexures entry must be a mapping{loc}; got "
                f"{type(entry).__name__}"
            )
        tid = entry.get("topic")
        if not tid or not isinstance(tid, str):
            raise ValueError(
                f"report.annexures entry missing required `topic` key{loc}"
            )
        if tid not in TOPIC_REGISTRY:
            valid = ", ".join(sorted(TOPIC_REGISTRY))
            raise ValueError(
                f"Unknown report.annexures topic {tid!r}{loc}. "
                f"Valid topic IDs: {valid}"
            )
        letter_raw = entry.get("letter")
        if letter_raw is None or (
            isinstance(letter_raw, str) and not letter_raw.strip()
        ):
            letter = TOPIC_REGISTRY[tid].default_letter
        else:
            letter = str(letter_raw).strip()
        if letter in seen_letters:
            prior = seen_letters[letter]
            raise ValueError(
                f"Duplicate annexure letter {letter!r}{loc}: used by "
                f"both {prior!r} and {tid!r}. Each topic needs a "
                f"unique letter."
            )
        seen_letters[letter] = tid
        out.append((tid, letter))
    return out


def serialize_report_annexures(
    selection: list[tuple[str, str]],
) -> dict[str, list[dict[str, str]]]:
    """Render a topic selection as a YAML-ready dict.

    Inverse of :func:`parse_report_annexures` for round-trip. Output
    shape::

        {"annexures": [
            {"topic": "results_ili_comparison", "letter": "B"},
            ...
        ]}

    Letters are always emitted explicitly (even when equal to the
    topic's default) so a future change to the registry's defaults
    doesn't silently shift saved YAMLs.
    """
    return {
        "annexures": [
            {"topic": tid, "letter": letter}
            for tid, letter in selection
        ],
    }

    def to_manifest(self) -> dict[str, Any]:
        return {
            "project_name": self.project_name,
            "report_number": self.report_number,
            "report_revision": self.report_revision,
            "project_date": self.project_date.isoformat(),
            "tool_version": self.tool_version,
            "git_commit": self.git_commit,
            "run_timestamp": self.run_timestamp.isoformat(),
            "input_file_hashes": self.input_file_hashes,
            "pipeline": {
                "name": self.pipeline.pipeline_name,
                "client": self.pipeline.client_name,
                "diameter_mm": self.pipeline.diameter_mm,
                "length_km": self.pipeline.length_km,
                "material_grade": self.pipeline.material_grade,
                "smys_mpa": self.pipeline.smys_mpa,
                "product": self.pipeline.product,
                "maop_zones": [asdict(z) for z in self.pipeline.maop_zones],
            },
            "run_1": {
                "vendor": self.run_1.vendor,
                "inspection_date": self.run_1.inspection_date.isoformat() if self.run_1.inspection_date else None,
                "tool_type": self.run_1.tool_type,
                "feature_count": self.run_1.feature_count,
            },
            "run_2": {
                "vendor": self.run_2.vendor,
                "inspection_date": self.run_2.inspection_date.isoformat() if self.run_2.inspection_date else None,
                "tool_type": self.run_2.tool_type,
                "feature_count": self.run_2.feature_count,
            },
            "years_between_runs": self.years_between_runs,
            "match_summary": self.match_result.to_summary_dict() if self.match_result else None,
            "config": self.config,
        }
