"""Project Setup screen — load / edit / save a project YAML.

The screen is intentionally read-mostly: we surface every field that
matters for an analysis run (project + pipeline + runs + CGR + FFP +
repair horizon), let the user edit any of them, and write the result
back to a YAML file. Heavier edits (MAOP zones table) are exposed as a
small editable QTableWidget.

When the user clicks 'Proceed →', this screen validates the inputs and
emits :pyattr:`ready` carrying an :class:`~src.gui.analysis_worker.AnalysisJob`
that the main window forwards to the Run Analysis screen.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml
from PyQt6.QtCore import QDate, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..analysis_worker import AnalysisJob
from ..widgets.annexure_topics_panel import AnnexureTopicsPanel
from ...io.paths import (
    relativize_if_possible,
    resolve_output_dir,
    resolve_relative_to_yaml,
)
from ...models import parse_report_annexures, serialize_report_annexures


_CGR_MODES = ("hybrid", "feature_specific", "population_only")
_FFP_METHODS = (
    "B31G_Original", "B31G_Modified", "RSTRENG", "DNV_RP_F101", "Kastner",
)
_ANNEX_FORMATS = ("E_F", "B_C_D")


def _build_maop_zones(
    *,
    wall_thicknesses: list[float] | None,
    maop_kgcm2: float | None,
    design_factor: float | None = None,
    maops_per_wt: list[float] | None = None,
) -> list[tuple[float, float, float, float]]:
    """Build MAOP-zone rows from an auto-fill PDF's extracted WTs.

    Returns a list of (wt_min, wt_max, design_factor, maop) tuples
    suitable for the project_setup MAOP-zone table. Zones are
    non-overlapping and gap-free across the WT range.

    The single-MAOP common case (most Athena/NGP pipelines) ends up
    with ONE zone spanning min(WTs) - 0.5 to max(WTs) + 0.5 mm. That
    avoids the v0.2.0 bug where ±0.5 brackets produced overlapping
    zones (e.g. WTs [7.1, 8.7, 9.5] gave 6.6-7.6, 8.2-9.2, 9.0-10.0 —
    the last zone overlapped the second, so the third WT had no
    matching zone).

    When `maops_per_wt` is supplied (one MAOP per sorted WT, e.g. the
    HMEL three-zone case), zone boundaries are placed at midpoints
    between consecutive WTs:
        WTs [8.7, 9.5, 11.1] with MAOPs [80.6, 84.1, 96.7] →
            zone 1: 8.2 - 9.1 mm   MAOP 80.6
            zone 2: 9.1 - 10.3 mm  MAOP 84.1
            zone 3: 10.3 - 11.6 mm MAOP 96.7

    Args:
        wall_thicknesses: list of WT values from the PDF (mm).
        maop_kgcm2: single MAOP applied to ALL WTs. Used when
            ``maops_per_wt`` is None.
        design_factor: design factor for every zone. Defaults to 0.72.
        maops_per_wt: optional per-WT MAOP list (same length as
            ``wall_thicknesses``, paired by sort order). When supplied,
            wins over ``maop_kgcm2`` and triggers midpoint-cut zoning.

    Returns:
        Empty list if `wall_thicknesses` is empty or no usable MAOP
        was supplied.
    """
    if not wall_thicknesses:
        return []
    wts = sorted(float(w) for w in wall_thicknesses)
    if not wts:
        return []
    df = float(design_factor) if design_factor else 0.72

    # ----- Per-WT MAOPs (midpoint-cut bracketing).
    if maops_per_wt and len(maops_per_wt) == len(wts):
        # Re-pair MAOPs with sorted WTs (caller may have supplied them
        # in a different order).
        pairs = sorted(zip(wall_thicknesses, maops_per_wt))
        sorted_wts = [p[0] for p in pairs]
        sorted_maops = [float(p[1]) for p in pairs]

        # Collapse runs of identical MAOPs into a single zone so we
        # don't emit redundant boundaries (HMEL has 3 distinct MAOPs
        # across 6 WTs in the worst case; only 3 zones make sense).
        zones: list[tuple[float, float, float, float]] = []
        zone_start_wt = sorted_wts[0]
        zone_maop = sorted_maops[0]
        for i in range(1, len(sorted_wts)):
            if sorted_maops[i] != zone_maop:
                # Zone boundary sits midway between the last WT of the
                # current zone and the first WT of the next zone.
                cut = (sorted_wts[i - 1] + sorted_wts[i]) / 2.0
                low = (
                    max(0.1, zone_start_wt - 0.5)
                    if not zones
                    else zones[-1][1]
                )
                zones.append((round(low, 3), round(cut, 3), df, zone_maop))
                zone_start_wt = sorted_wts[i]
                zone_maop = sorted_maops[i]
        # Final zone runs to max + 0.5 mm.
        low = (
            max(0.1, zone_start_wt - 0.5)
            if not zones
            else zones[-1][1]
        )
        high = sorted_wts[-1] + 0.5
        zones.append((round(low, 3), round(high, 3), df, zone_maop))
        return zones

    # ----- Single-MAOP case.
    if maop_kgcm2 is None:
        return []
    maop = float(maop_kgcm2)
    # All WTs share one MAOP → one zone spanning min-0.5 to max+0.5.
    # This avoids the overlapping-zones bug that ±0.5 per-WT brackets
    # produced for closely-spaced WTs like [7.1, 8.7, 9.5].
    wt_min = max(0.1, wts[0] - 0.5)
    wt_max = wts[-1] + 0.5
    return [(round(wt_min, 3), round(wt_max, 3), df, maop)]


# Conversion factor: 1 MPa = 10.197 162 13 kg/cm² (exact, from
# 1 kg/cm² = 98 066.5 Pa). The whole tool uses this anchor in
# src/models/units.py; pin to the same constant so MAOP-design
# calculations don't drift from Psafe / ERF math by rounding.
_MPA_PER_KGCM2 = 0.0980665           # 1 kg/cm² in MPa
_KGCM2_PER_MPA = 1.0 / _MPA_PER_KGCM2  # ≈ 10.1972


def _max_design_maop_kgcm2(
    smys_mpa: float, wt_mm: float, od_mm: float, design_factor: float,
) -> float | None:
    """Barlow / ASME B31.4 design pressure for a thin-wall pipe section.

    .. math:: P_{design} = \\frac{2 \\cdot SMYS \\cdot t}{D} \\cdot F_d

    Result is in kg/cm² so it can be compared directly to the user's
    MAOP zones. Returns ``None`` for any non-positive input — the
    caller (Validate-time check) treats that as "can't compute" and
    silently skips the warning rather than firing on a stub form.

    Worked example used by the Validate-time check (X70, 28" OD,
    7.9 mm WT, Fd 0.72):
        P = 2 × 482 × 7.9 / 711 × 0.72  ≈  7.71 MPa
        P = 7.71 × 10.1972             ≈  78.6 kg/cm²
    """
    if smys_mpa <= 0 or wt_mm <= 0 or od_mm <= 0 or design_factor <= 0:
        return None
    p_mpa = (2.0 * smys_mpa * wt_mm / od_mm) * design_factor
    return p_mpa * _KGCM2_PER_MPA


def _min_wt_in_run2_features(run2_path: str | Path) -> float | None:
    """Return the minimum WT (mm) among Run-2 features-for-assessment.

    Pre-v0.2.4 helper, retained for backward compatibility and direct
    use cases that genuinely want the GLOBAL min WT (e.g., diagnostic
    logging). The Validate-time MAOP-vs-WT design check uses the
    per-zone helper :func:`_wt_min_per_zone_in_run2` instead — see the
    v0.2.4 bugfix note in the CHANGELOG.

    The minimum is taken across assessable features (cluster children
    + non-ML fids are already excluded by
    :meth:`ILIRun.features_for_assessment`) because the design-limit
    comparison is what matters for FFP — a weld anomaly with WT=5 mm
    shouldn't trigger the warning if no metal-loss feature sits on the
    same WT.

    Returns ``None`` when:
      * The path doesn't exist (form-incomplete state).
      * The reader raises (file not in NGP format — the user gets the
        Convert-Format banner separately for that).
      * No feature carries a WT value.
    """
    p = Path(run2_path)
    if not p.exists():
        return None
    try:
        # Local import — avoids pulling ili_reader into module load
        # time and lets test code patch the import cleanly.
        from src.io.ili_reader import ILIReader
        run = ILIReader().read(str(p), run_id="run_2_validate_check")
    except Exception:                                            # noqa: BLE001
        return None
    wts = [
        float(f.wt_mm)
        for f in run.features_for_assessment()
        if f.wt_mm is not None and f.wt_mm > 0
    ]
    if not wts:
        return None
    return min(wts)


def _wt_min_per_zone_in_run2(
    run2_path: str | Path,
    maop_zones: list[dict],
    *,
    mode: str = "wt",
) -> list[float | None]:
    """Return per-zone minimum WT (mm) among Run-2 features.

    Per-zone replacement for :func:`_min_wt_in_run2_features` — v0.2.4
    bugfix for the false-positive MAOP-vs-WT warning on multi-zone
    pipelines where higher MAOP zones carried thicker pipe (the
    physically correct setup).

    Returns a list aligned with ``maop_zones`` (same length, same
    order). Each entry is the minimum ``wt_mm`` among Run-2 features
    bucketed into that zone, or ``None`` if no features land in that
    zone (after orphan fallback) — in which case the caller MUST skip
    the design-limit check for that zone (silently).

    Bucketing logic mirrors :meth:`src.models.Pipeline.maop_for_wt`:

      * Feature WT inside ``[zone.wt_mm_min, zone.wt_mm_max]`` →
        assigned to that zone (first matching zone wins on overlap).
      * Feature WT outside ALL zones (orphan) → assigned to the
        NEAREST zone by ``min(|wt − wt_min|, |wt − wt_max|)``. This
        keeps thin-wall orphans contributing to the lowest-WT zone's
        pool rather than disappearing silently. Consistent with the
        ``MAOP_ZONE_NOT_FOUND`` flag the FFP coordinator already emits
        for the same condition.

    Returns ``[None, None, ...]`` (all-None list aligned with zones)
    when:
      * The Run-2 file doesn't exist (stub form state).
      * The reader raises (file not in NGP format — separate banner).
      * No feature carries a WT value.

    Caller treats per-zone ``None`` as "skip this zone's check".
    """
    n_zones = len(maop_zones)
    if n_zones == 0:
        return []
    none_list: list[float | None] = [None] * n_zones

    p = Path(run2_path)
    if not p.exists():
        return none_list
    try:
        from src.io.ili_reader import ILIReader
        run = ILIReader().read(str(p), run_id="run_2_validate_check")
    except Exception:                                            # noqa: BLE001
        return none_list

    # v0.3.0: collect (wt, chainage) pairs for each feature. Both are
    # used: WT for the design-limit check, and one of (wt, chainage)
    # for bucketing depending on `mode`.
    features: list[tuple[float, float]] = []
    for f in run.features_for_assessment():
        if f.wt_mm is None or f.wt_mm <= 0:
            continue
        features.append((float(f.wt_mm), float(f.abs_distance_m or 0.0)))
    if not features:
        return none_list

    # v0.3.0: pick the bucketing key + zone range per mode.
    if mode == "chainage":
        ranges = [
            (i, float(z["chainage_m_min"]), float(z["chainage_m_max"]))
            for i, z in enumerate(maop_zones)
        ]
    else:    # "wt" (default + legacy)
        ranges = [
            (i, float(z["wt_mm_min"]), float(z["wt_mm_max"]))
            for i, z in enumerate(maop_zones)
        ]

    buckets: list[list[float]] = [[] for _ in range(n_zones)]
    for wt, chainage in features:
        key = chainage if mode == "chainage" else wt
        # First-match-wins on overlapping ranges (mirrors
        # Pipeline.maop_for_wt / maop_for_chainage).
        assigned: int | None = None
        for idx, lo, hi in ranges:
            if lo <= key <= hi:
                assigned = idx
                break
        if assigned is None:
            # Orphan — assign to nearest zone by the mode's key.
            assigned = min(
                range(n_zones),
                key=lambda i: min(
                    abs(ranges[i][1] - key), abs(ranges[i][2] - key)
                ),
            )
        # The bucket value is ALWAYS the WT (the design-limit
        # check needs WT regardless of bucketing key).
        buckets[assigned].append(wt)

    return [min(bucket) if bucket else None for bucket in buckets]


class ProjectSetupScreen(QWidget):
    """Top-level project configuration screen."""

    ready = pyqtSignal(object)                       # emits an AnalysisJob
    status_message = pyqtSignal(str)
    convert_run1_requested = pyqtSignal(str)         # emits Run-1 path when
                                                     # the file can't be parsed
                                                     # as NGP and the user
                                                     # clicks "Convert it →"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_config_path: Path | None = None
        # v0.3.0: MAOP zoning mode + cache of the loaded zone dicts.
        # In chainage mode the WT zone table doesn't apply; the raw
        # zones are stashed here for round-trip on save and for the
        # MAOP-vs-WT banner check.
        self._maop_zoning_mode: str = "wt"
        self._loaded_maop_zones_raw: list[dict] = []
        self._build_ui()
        # Wire all editable widgets to clear the validation status the
        # moment the user changes something. Without this, a stale red
        # "Project name is required" lingers under the form even after
        # the user types a name. We attach AFTER _build_ui so every
        # widget exists.
        self._wire_field_edits_clear_status()

    def _wire_field_edits_clear_status(self) -> None:
        """Connect every form field's change signal to :meth:`_on_field_edited`.

        Triggered on:
          * QLineEdit          → textEdited
          * QSpinBox / QDoubleSpinBox → valueChanged
          * QComboBox          → currentTextChanged
          * QDateEdit          → dateChanged
          * QCheckBox          → toggled
          * QTableWidget       → cellChanged
        """
        line_edits = (
            self.ed_project_name, self.ed_pipeline_name, self.ed_client_name,
            self.ed_report_no, self.ed_report_rev, self.ed_prepared_by,
            self.ed_material_grade, self.ed_product,
            self.ed_run1_path, self.ed_run2_path,
            self.ed_run1_vendor, self.ed_run2_vendor,
            self.ed_run1_tool, self.ed_run2_tool,
        )
        for w in line_edits:
            w.textEdited.connect(self._on_field_edited)

        spin_boxes = (
            self.sp_diameter, self.sp_length, self.sp_install_year,
            self.sp_smys, self.sp_years_override, self.sp_horizon,
        )
        for w in spin_boxes:
            w.valueChanged.connect(self._on_field_edited)

        # v0.2.5: cb_annex_format was replaced by panel_annexure_topics;
        # the panel emits its own selection_changed signal which is
        # wired up explicitly in _build_ui.
        combos = (
            self.cb_service_class, self.cb_cgr_mode,
            self.cb_ffp_method,
        )
        for w in combos:
            w.currentTextChanged.connect(self._on_field_edited)

        for w in (self.dt_run1, self.dt_run2):
            w.dateChanged.connect(self._on_field_edited)

        for w in (self.cb_years_override, self.cb_write_docx):
            w.toggled.connect(self._on_field_edited)

        self.tbl_zones.cellChanged.connect(self._on_field_edited)

    def _on_field_edited(self, *_args) -> None:
        """Clear stale status + re-arm Validate when any field changes."""
        # Reset the bottom status — only the Validate button should
        # produce error/success messages.
        if self.lbl_status.text():
            self.lbl_status.setText("")
            self.lbl_status.setStyleSheet(f"color: {theme.COLOR_TEXT_MUTED};")
        # A prior successful validation no longer holds; the user has to
        # re-validate.
        self.btn_proceed.setEnabled(False)
        # Hide the converter-handoff banner once the form starts
        # acquiring real metadata.
        self._maybe_hide_handoff_banner()

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(theme.PAD_XL, theme.PAD_L, theme.PAD_XL, theme.PAD_L)
        root.setSpacing(theme.PAD_M)

        title = QLabel("Project Setup")
        title.setProperty("role", "screenTitle")
        subtitle = QLabel(
            "Load or edit a project YAML, then proceed to run the analysis."
        )
        subtitle.setProperty("role", "screenSubtitle")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        # ----- Top toolbar (auto-fill / load / save / new) ---------------
        toolbar = QHBoxLayout()
        toolbar.setSpacing(theme.PAD_S)

        # The "Auto-fill from Final Report PDF" button is the headline
        # action — most Athena projects ship with a vendor Final Report
        # that already lists OD / length / MAOP / SMYS / Fd / WT etc.
        # Clicking this turns a ~15-field form into a ~1-field form.
        self.btn_autofill = QPushButton("⤵  Auto-fill from Final Report PDF…")
        self.btn_autofill.setProperty("role", "primary")
        self.btn_autofill.setToolTip(
            "Read pipeline metadata (OD, length, MAOP, SMYS, material, "
            "MAOP zones) directly from the vendor's Final Report PDF."
        )
        self.btn_autofill.clicked.connect(self._on_autofill_pdf_clicked)

        self.btn_load = QPushButton("Load YAML…")
        self.btn_save = QPushButton("Save YAML…")
        self.btn_new = QPushButton("New (blank)")
        self.lbl_loaded = QLabel("No project loaded")
        self.lbl_loaded.setStyleSheet(f"color: {theme.COLOR_TEXT_MUTED};")

        self.btn_load.clicked.connect(self._on_load_clicked)
        self.btn_save.clicked.connect(self._on_save_clicked)
        self.btn_new.clicked.connect(self._on_new_clicked)

        toolbar.addWidget(self.btn_autofill)
        toolbar.addSpacing(theme.PAD_L)
        toolbar.addWidget(self.btn_load)
        toolbar.addWidget(self.btn_save)
        toolbar.addWidget(self.btn_new)
        toolbar.addSpacing(theme.PAD_L)
        toolbar.addWidget(self.lbl_loaded, stretch=1)
        root.addLayout(toolbar)

        # ----- Scrollable form ------------------------------------------
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        form_holder = QWidget()
        form_holder.setObjectName("contentRoot")
        scroll.setWidget(form_holder)
        form_layout = QVBoxLayout(form_holder)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(theme.PAD_M)

        # Welcome banner — shown once per user (tracked via QSettings)
        # to introduce the typical workflow. Dismissable via the "Got it"
        # button. Sits above the converter-handoff banner so it
        # appears at the very top of the form on first launch.
        self._welcome_banner = self._build_welcome_banner()
        form_layout.addWidget(self._welcome_banner)

        # Info banner — surfaced by set_run1_file() when the user comes
        # back from the Format Converter without having filled in the
        # rest of the project metadata yet.
        self._converter_handoff_banner = self._build_converter_handoff_banner()
        form_layout.addWidget(self._converter_handoff_banner)

        form_layout.addWidget(self._build_project_card())
        form_layout.addWidget(self._build_pipeline_card())
        form_layout.addWidget(self._build_maop_card())
        form_layout.addWidget(self._build_runs_card())
        form_layout.addWidget(self._build_analysis_card())
        form_layout.addStretch(1)
        root.addWidget(scroll, stretch=1)

        # ----- Footer ----------------------------------------------------
        footer = QHBoxLayout()
        footer.setSpacing(theme.PAD_S)
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet(f"color: {theme.COLOR_TEXT_MUTED};")
        footer.addWidget(self.lbl_status, stretch=1)

        self.btn_validate = QPushButton("Validate")
        self.btn_validate.clicked.connect(self._on_validate_clicked)

        self.btn_proceed = QPushButton("Proceed to Analysis  →")
        self.btn_proceed.setProperty("role", "primary")
        self.btn_proceed.setEnabled(False)
        self.btn_proceed.clicked.connect(self._on_proceed_clicked)

        footer.addWidget(self.btn_validate)
        footer.addWidget(self.btn_proceed)
        root.addLayout(footer)

    # -- Card builders ---------------------------------------------------
    def _card(self, title: str, body: QWidget) -> QFrame:
        card = QFrame()
        card.setProperty("role", "card")
        vbox = QVBoxLayout(card)
        vbox.setContentsMargins(theme.PAD_M, theme.PAD_M, theme.PAD_M, theme.PAD_M)
        vbox.setSpacing(theme.PAD_S)
        header = QLabel(title)
        header.setProperty("role", "sectionHeader")
        vbox.addWidget(header)
        vbox.addWidget(body)
        return card

    def _build_project_card(self) -> QFrame:
        body = QWidget()
        form = QFormLayout(body)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(theme.PAD_M)

        self.ed_project_name = QLineEdit()
        self.ed_project_name.setPlaceholderText(
            "e.g., FFP_Kandla_Samakhiali_10in_LPG"
        )
        self.ed_pipeline_name = QLineEdit()
        self.ed_pipeline_name.setPlaceholderText(
            'e.g., Kandla-Samakhiali 10" LPG'
        )
        self.ed_client_name = QLineEdit()
        self.ed_client_name.setPlaceholderText("e.g., GAIL (India) Limited")
        self.ed_report_no = QLineEdit()
        self.ed_report_no.setPlaceholderText("e.g., ATH-KS-2023-001")
        self.ed_report_rev = QLineEdit()
        self.ed_report_rev.setPlaceholderText("00")
        self.ed_prepared_by = QLineEdit()
        self.ed_prepared_by.setPlaceholderText("e.g., Athena PowerTech LLP")

        # Required-field labels carry an asterisk; the Validate step
        # uses the same set as the truth source.
        form.addRow(self._req_label("Project name"),         self.ed_project_name)
        form.addRow("Pipeline section name:",                self.ed_pipeline_name)
        form.addRow("Client:",                               self.ed_client_name)
        form.addRow("Report number:",                        self.ed_report_no)
        form.addRow("Report revision:",                      self.ed_report_rev)
        form.addRow("Prepared by:",                          self.ed_prepared_by)

        return self._card("Project metadata", body)

    @staticmethod
    def _req_label(text: str) -> QLabel:
        """Build a form label with a red asterisk marking the field as required.

        Belt-and-suspenders: every piece of the rich text gets an
        explicit ``color:`` attribute. Without the wrapping span on the
        label text itself, the surrounding chars could inherit the
        QPalette default (which on some Qt-Fusion + OS combos resolves
        to near-white), leaving only the explicitly-red asterisk
        visible. Hard-coding the dark colour here means the asterisk
        AND the label text are always readable — independent of the
        application stylesheet.
        """
        lbl = QLabel(
            f"<span style='color:#2C3E50'>{text}</span> "
            f"<span style='color:#C0392B'>*</span>"
            f"<span style='color:#2C3E50'>:</span>"
        )
        lbl.setTextFormat(Qt.TextFormat.RichText)
        # Explicit per-widget colour as a third layer (over QSS + palette).
        lbl.setStyleSheet("color: #2C3E50;")
        return lbl

    def _build_pipeline_card(self) -> QFrame:
        body = QWidget()
        form = QFormLayout(body)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(theme.PAD_M)

        # All numerics start at 0 so a blank form doesn't show garbage
        # defaults (the old form showed OD=50, length=0.1, year=2010
        # which all looked like user-entered values).
        self.sp_diameter = QDoubleSpinBox()
        self.sp_diameter.setRange(0.0, 2000.0)
        self.sp_diameter.setDecimals(1)
        self.sp_diameter.setSuffix("  mm")
        self.sp_diameter.setValue(0.0)
        self.sp_diameter.setSpecialValueText("(not set)")    # shown when value == minimum

        self.sp_length = QDoubleSpinBox()
        self.sp_length.setRange(0.0, 5000.0)
        self.sp_length.setDecimals(2)
        self.sp_length.setSuffix("  km")
        self.sp_length.setValue(0.0)
        self.sp_length.setSpecialValueText("(not set)")

        self.sp_install_year = QSpinBox()
        self.sp_install_year.setRange(0, 2100)
        self.sp_install_year.setValue(0)
        self.sp_install_year.setSpecialValueText("(not set)")

        self.ed_material_grade = QLineEdit()
        self.ed_material_grade.setPlaceholderText("e.g., API 5L X52")

        self.sp_smys = QDoubleSpinBox()
        self.sp_smys.setRange(0.0, 1000.0)
        self.sp_smys.setDecimals(1)
        self.sp_smys.setSuffix("  MPa")
        self.sp_smys.setValue(0.0)
        self.sp_smys.setSpecialValueText("(not set)")

        self.ed_product = QLineEdit()
        self.ed_product.setPlaceholderText("e.g., LPG, Crude, Natural Gas")

        self.cb_service_class = QComboBox()
        self.cb_service_class.addItems(["liquid", "gas", "multiphase"])

        form.addRow(self._req_label("Outer diameter (mm)"),     self.sp_diameter)
        form.addRow(self._req_label("Pipeline length (km)"),    self.sp_length)
        form.addRow("Year of construction:",                    self.sp_install_year)
        form.addRow(self._req_label("Material grade"),          self.ed_material_grade)
        form.addRow(
            "Specified Minimum Yield Strength (SMYS):",
            self.sp_smys,
        )
        form.addRow("Product type:",                            self.ed_product)
        form.addRow("Service class:",                           self.cb_service_class)

        return self._card("Pipeline geometry", body)

    def _build_maop_card(self) -> QFrame:
        body = QWidget()
        vbox = QVBoxLayout(body)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(theme.PAD_S)

        self.tbl_zones = QTableWidget(0, 4)
        theme.apply_table_palette(self.tbl_zones)
        _hdr_labels = ("WT min (mm)", "WT max (mm)", "Design factor", "MAOP (kg/cm²)")
        _hdr_tooltips = (
            "Lower bound of wall thickness range for this zone",
            "Upper bound of wall thickness range for this zone",
            "Pipe design factor per ASME B31.4/B31.8 (usually 0.72)",
            "Maximum allowable operating pressure for this zone",
        )
        # Set header items explicitly so the tooltips stick reliably
        # across Qt versions (setHorizontalHeaderLabels auto-creates
        # items but tooltip-setting on those is finicky). theme.themed_item
        # ensures the header text is visible even under Qt-Fusion's
        # QPalette-ignoring renderer.
        for col, (label, tip) in enumerate(zip(_hdr_labels, _hdr_tooltips)):
            item = theme.themed_item(label)
            item.setToolTip(tip)
            self.tbl_zones.setHorizontalHeaderItem(col, item)

        self.tbl_zones.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.tbl_zones.verticalHeader().setVisible(False)
        self.tbl_zones.setMinimumHeight(140)

        btns = QHBoxLayout()
        self.btn_add_zone = QPushButton("+ Add zone")
        self.btn_del_zone = QPushButton("− Remove selected")
        self.btn_add_zone.clicked.connect(lambda: self._append_zone_row())
        self.btn_del_zone.clicked.connect(self._remove_selected_zone_row)
        btns.addWidget(self.btn_add_zone)
        btns.addWidget(self.btn_del_zone)
        btns.addStretch(1)

        vbox.addWidget(self.tbl_zones)
        vbox.addLayout(btns)
        return self._card("MAOP zones (by wall thickness)", body)

    def _build_runs_card(self) -> QFrame:
        body = QWidget()
        vbox = QVBoxLayout(body)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(theme.PAD_S)

        # ---- Run 1 -----------------------------------------------------
        vbox.addWidget(self._run_subheader("Run 1 (older / baseline)"))
        self.ed_run1_path = QLineEdit()
        self.ed_run1_path.setPlaceholderText("Pipe tally .xlsx path")
        self.btn_run1_browse = QPushButton("Browse…")
        self.btn_run1_browse.clicked.connect(
            lambda: self._browse_run_file(self.ed_run1_path)
        )
        # Date defaults to today so the widget shows SOMETHING (Qt
        # requires a valid date), but the date is treated as "unset"
        # downstream until the user picks one explicitly or a YAML load
        # provides it. set_run1_file() preserves the existing value.
        self.dt_run1 = QDateEdit(QDate.currentDate())
        self.dt_run1.setCalendarPopup(True)
        self.dt_run1.setDisplayFormat("yyyy-MM-dd")
        self.ed_run1_vendor = QLineEdit()
        self.ed_run1_vendor.setPlaceholderText("e.g., Athena PowerTech / NGP")
        self.ed_run1_tool = QLineEdit()
        self.ed_run1_tool.setPlaceholderText("e.g., MFL-A or MFL-A + MFL-C")

        vbox.addLayout(
            self._labelled_file_row(
                "ILI file:", self.ed_run1_path, self.btn_run1_browse,
            )
        )

        # NGP-validation banner — hidden by default. Surfaces when the
        # picked Run-1 file can't be read by ILIReader, with a one-click
        # bridge to the Format Converter screen.
        self._run1_banner = self._build_ngp_banner()
        vbox.addWidget(self._run1_banner)

        vbox.addLayout(
            self._two_col_row(
                "Inspection date:", self.dt_run1,
                "Vendor:", self.ed_run1_vendor,
            )
        )
        vbox.addLayout(
            self._single_col_row(
                "Inspection technology:", self.ed_run1_tool,
            )
        )

        # Re-run NGP validation whenever the path field changes (via
        # browse or paste / load YAML).
        self.ed_run1_path.editingFinished.connect(self._validate_run1_as_ngp)

        vbox.addSpacing(theme.PAD_M)

        # ---- Run 2 -----------------------------------------------------
        vbox.addWidget(self._run_subheader("Run 2 (newer / comparison)"))
        self.ed_run2_path = QLineEdit()
        self.ed_run2_path.setPlaceholderText("Pipe tally .xlsx path")
        self.btn_run2_browse = QPushButton("Browse…")
        self.btn_run2_browse.clicked.connect(
            lambda: self._browse_run_file(self.ed_run2_path)
        )
        self.dt_run2 = QDateEdit(QDate.currentDate())
        self.dt_run2.setCalendarPopup(True)
        self.dt_run2.setDisplayFormat("yyyy-MM-dd")
        self.ed_run2_vendor = QLineEdit()
        self.ed_run2_vendor.setPlaceholderText("e.g., GAIL / NGP")
        self.ed_run2_tool = QLineEdit()
        self.ed_run2_tool.setPlaceholderText("e.g., MFL-A + MFL-C")

        vbox.addLayout(
            self._labelled_file_row(
                "ILI file:", self.ed_run2_path, self.btn_run2_browse,
            )
        )
        vbox.addLayout(
            self._two_col_row(
                "Inspection date:", self.dt_run2,
                "Vendor:", self.ed_run2_vendor,
            )
        )
        vbox.addLayout(
            self._single_col_row(
                "Inspection technology:", self.ed_run2_tool,
            )
        )

        vbox.addSpacing(theme.PAD_S)

        # ---- Years between runs ---------------------------------------
        # Two-line block:
        #   "Years between runs (auto-calculated):  5.250 yr"  ← live read-only
        #   "[ ] Override:  [ 5.000  yr ]"                     ← optional
        years_calc_row = QHBoxLayout()
        lbl_calc = QLabel("Years between runs (auto-calculated):")
        lbl_calc.setMinimumWidth(240)
        lbl_calc.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.lbl_years_calc = QLabel("(pick both inspection dates first)")
        self.lbl_years_calc.setStyleSheet(
            f"color: {theme.COLOR_TEXT_MUTED}; font-style: italic;"
        )
        years_calc_row.addWidget(lbl_calc)
        years_calc_row.addWidget(self.lbl_years_calc, stretch=1)
        vbox.addLayout(years_calc_row)

        years_override_row = QHBoxLayout()
        spacer = QLabel("")
        spacer.setMinimumWidth(240)
        self.cb_years_override = QCheckBox(
            "Override the auto-calculated interval with:"
        )
        self.sp_years_override = QDoubleSpinBox()
        self.sp_years_override.setRange(0.0, 50.0)
        self.sp_years_override.setDecimals(3)
        self.sp_years_override.setValue(0.0)
        self.sp_years_override.setSuffix("  yr")
        self.sp_years_override.setSpecialValueText("(not set)")
        self.sp_years_override.setEnabled(False)
        self.cb_years_override.toggled.connect(self.sp_years_override.setEnabled)
        years_override_row.addWidget(spacer)
        years_override_row.addWidget(self.cb_years_override)
        years_override_row.addWidget(self.sp_years_override)
        years_override_row.addStretch(1)
        vbox.addLayout(years_override_row)

        # Keep the auto-calc label in sync when either date changes.
        self.dt_run1.dateChanged.connect(self._refresh_years_calc)
        self.dt_run2.dateChanged.connect(self._refresh_years_calc)

        return self._card("ILI runs", body)

    # Typical reinspection interval range for steel pipelines. Anything
    # below 0.5 yr usually means a typo in one of the dates; anything
    # above 15 yr suggests Run-1 is from a different pipeline OR the
    # year defaulted to today's date by accident. Catch both.
    _YEARS_BETWEEN_MIN = 0.5
    _YEARS_BETWEEN_MAX = 15.0

    def _refresh_years_calc(self) -> None:
        """Recompute the read-only 'years between runs' label.

        Also surfaces an inline warning when the interval is outside
        the typical 0.5-15 yr range. This catches:
          * Run-1 date defaulted to today (negative or near-zero years)
          * Run-1 date left at the QDateEdit default 1900 (huge interval)
          * Typos that flip the date to a wrong century
        """
        try:
            d1 = self.dt_run1.date().toPyDate()
            d2 = self.dt_run2.date().toPyDate()
            yrs = (d2 - d1).days / 365.25
        except Exception:                                        # noqa: BLE001
            self.lbl_years_calc.setText("(unable to calculate)")
            self._set_years_warning(None)
            return

        if yrs <= 0:
            self.lbl_years_calc.setText(
                "(run-2 date must be after run-1 date)"
            )
            self.lbl_years_calc.setStyleSheet(
                f"color: {theme.COLOR_WARNING}; font-style: italic;"
            )
            self._set_years_warning(
                f"Years between runs is {yrs:.1f}. Run-2 must be later "
                "than Run-1 — please verify both inspection dates."
            )
            return

        self.lbl_years_calc.setText(f"{yrs:.3f} yr")
        self.lbl_years_calc.setStyleSheet(
            f"color: {theme.COLOR_TEXT}; font-weight: 500;"
        )

        if yrs < self._YEARS_BETWEEN_MIN or yrs > self._YEARS_BETWEEN_MAX:
            self._set_years_warning(
                f"Years between runs is {yrs:.1f}. Typical pipeline "
                "reinspection intervals are 3–10 years. Verify the "
                "Run-1 date is correct."
            )
        else:
            self._set_years_warning(None)

    def _set_years_warning(self, message: str | None) -> None:
        """Toggle the inline yellow warning banner under the auto-calc.

        Uses the same warm-yellow style as the converter handoff banner
        so the visual vocabulary stays consistent. Created lazily on
        first non-None call so screen construction doesn't allocate
        another widget for the (common) happy path.
        """
        if message is None:
            if hasattr(self, "_years_warning_banner"):
                self._years_warning_banner.setVisible(False)
            return
        if not hasattr(self, "_years_warning_banner"):
            self._years_warning_banner = QLabel(message)
            self._years_warning_banner.setWordWrap(True)
            self._years_warning_banner.setStyleSheet(
                f"background-color: #FFF4E5;"
                f" border: 1px solid {theme.COLOR_WARNING};"
                f" border-left: 4px solid {theme.COLOR_WARNING};"
                f" border-radius: {theme.RADIUS_S}px;"
                f" padding: 6px 10px;"
                f" color: {theme.COLOR_TEXT}; font-size: 12px;"
            )
            # Insert just below the years-between-row in the runs card.
            # The parent layout of lbl_years_calc is the years_calc_row
            # QHBoxLayout; we want to add the banner to the row's
            # parent QVBoxLayout (the runs card body).
            parent_layout = self.lbl_years_calc.parent().layout()
            if parent_layout is not None:
                parent_layout.addWidget(self._years_warning_banner)
        self._years_warning_banner.setText(message)
        self._years_warning_banner.setVisible(True)

    def _build_analysis_card(self) -> QFrame:
        body = QWidget()
        form = QFormLayout(body)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(theme.PAD_M)

        self.cb_cgr_mode = QComboBox()
        self.cb_cgr_mode.addItems(_CGR_MODES)
        self.cb_cgr_mode.setCurrentText("hybrid")
        self.cb_cgr_mode.setToolTip(
            "hybrid: per-feature CGR with population-P95 floor (default).\n"
            "feature_specific: per-feature only, no floor.\n"
            "population_only: every feature gets the population P95."
        )

        self.cb_ffp_method = QComboBox()
        self.cb_ffp_method.addItems(_FFP_METHODS)
        self.cb_ffp_method.setCurrentText("B31G_Original")
        self.cb_ffp_method.setToolTip(
            "Primary FFP method applied to every feature."
        )

        self.sp_horizon = QSpinBox()
        self.sp_horizon.setRange(1, 50)
        self.sp_horizon.setValue(10)
        self.sp_horizon.setSuffix("  yr")
        self.sp_horizon.setToolTip(
            "Number of years to project the corrosion-growth simulation "
            "forward from the run-2 inspection date."
        )

        # v0.2.5: per-topic multi-select replaces the single-string
        # `annexure_format` QComboBox (which only offered "E_F" or
        # "B_C_D" presets). The panel persists into the YAML's
        # `report.annexures` block on Save.
        self.panel_annexure_topics = AnnexureTopicsPanel()
        self.panel_annexure_topics.selection_changed.connect(
            self._on_annexure_selection_changed
        )

        self.cb_write_docx = QCheckBox(
            "Write DOCX main report alongside the Excel annexure"
        )
        self.cb_write_docx.setChecked(True)
        self.cb_write_docx.setToolTip(
            "Generates the multi-page Word report (executive summary, "
            "tables, charts). Unchecking saves ~5 seconds on big runs "
            "but is rarely what you want."
        )

        form.addRow("CGR mode:",                 self.cb_cgr_mode)
        form.addRow("FFP primary method:",       self.cb_ffp_method)
        form.addRow("Repair horizon (years):",   self.sp_horizon)
        form.addRow("Reports:",                  self.cb_write_docx)
        # The annexure-topics panel is wider than a single form row;
        # add it as a full-width row spanning both columns.
        form.addRow(self.panel_annexure_topics)

        return self._card("Analysis options", body)

    # -- Misc layout helpers ---------------------------------------------
    def _run_subheader(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {theme.COLOR_TEXT}; font-size: 12px; font-weight: 600;"
        )
        return lbl

    def _labelled_file_row(
        self, label: str, line_edit: QLineEdit, browse_btn: QPushButton,
    ) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(theme.PAD_S)
        lbl = QLabel(label)
        lbl.setMinimumWidth(180)
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(lbl)
        row.addWidget(line_edit, stretch=1)
        row.addWidget(browse_btn)
        return row

    def _two_col_row(
        self, l1: str, w1: QWidget, l2: str, w2: QWidget,
    ) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(theme.PAD_S)
        lbl1 = QLabel(l1)
        lbl1.setMinimumWidth(180)
        lbl1.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(lbl1)
        row.addWidget(w1, stretch=1)
        lbl2 = QLabel(l2)
        lbl2.setMinimumWidth(120)
        lbl2.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(lbl2)
        row.addWidget(w2, stretch=1)
        return row

    def _single_col_row(self, label: str, widget: QWidget) -> QHBoxLayout:
        """A single labelled row — no trailing column / placeholder filler.

        Used for fields that don't pair naturally with a sibling on the
        same line (the old form put a bare QWidget() filler here, which
        rendered as a default-styled rectangle that users mistook for a
        broken widget).
        """
        row = QHBoxLayout()
        row.setSpacing(theme.PAD_S)
        lbl = QLabel(label)
        lbl.setMinimumWidth(180)
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(lbl)
        row.addWidget(widget, stretch=1)
        return row

    # -- MAOP table helpers ----------------------------------------------
    def _append_zone_row(
        self, *, wt_min: float = 0.0, wt_max: float = 0.0,
        df: float = 0.72, maop: float = 0.0,
    ) -> None:
        r = self.tbl_zones.rowCount()
        self.tbl_zones.insertRow(r)
        # Explicit foreground brush per item — Qt-Fusion ignores
        # QSS color rules on QTableWidget::item AND ignores the table's
        # QPalette ColorRole.Text for cell-text painting. Setting the
        # brush on the item directly always renders correctly.
        fg = QBrush(QColor(theme.COLOR_TEXT))
        for c, v in enumerate((wt_min, wt_max, df, maop)):
            item = QTableWidgetItem(f"{v:g}" if v else "")
            item.setForeground(fg)
            self.tbl_zones.setItem(r, c, item)

    def _remove_selected_zone_row(self) -> None:
        rows = sorted({i.row() for i in self.tbl_zones.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self.tbl_zones.removeRow(r)

    def _read_zones(self) -> list[dict[str, float]]:
        zones: list[dict[str, float]] = []
        for r in range(self.tbl_zones.rowCount()):
            try:
                row = {
                    "wt_mm_min": float(self.tbl_zones.item(r, 0).text()),
                    "wt_mm_max": float(self.tbl_zones.item(r, 1).text()),
                    "design_factor": float(self.tbl_zones.item(r, 2).text()),
                    "maop_kgcm2": float(self.tbl_zones.item(r, 3).text()),
                }
                zones.append(row)
            except (AttributeError, ValueError):
                continue
        return zones

    # -- Browse handlers -------------------------------------------------
    def _browse_run_file(self, target: QLineEdit) -> None:
        start_dir = ""
        if self._current_config_path:
            start_dir = str(self._current_config_path.parent)
        path, _ = QFileDialog.getOpenFileName(
            self, "Select pipe tally", start_dir,
            "Excel files (*.xlsx *.xls *.xlsm);;All files (*.*)",
        )
        if path:
            target.setText(path)
            if target is self.ed_run1_path:
                self._validate_run1_as_ngp()

    # -- Converter-handoff info banner -----------------------------------
    # -- One-time welcome banner -----------------------------------------
    _WELCOME_QSETTING_KEY = "welcome_banner_dismissed"

    def _build_welcome_banner(self) -> QFrame:
        """First-launch help banner explaining the typical workflow.

        Visibility is controlled by a QSettings flag — once the user
        clicks "Got it" the banner is permanently suppressed. Shipping
        upgrades / fresh installs reset that flag implicitly because
        QSettings is per-user.
        """
        banner = QFrame()
        banner.setObjectName("welcomeBanner")
        banner.setStyleSheet(
            f"#welcomeBanner {{ background-color: #F0F7FF;"
            f" border: 1px solid #B6D4FE;"
            f" border-left: 4px solid {theme.COLOR_PRIMARY};"
            f" border-radius: {theme.RADIUS_S}px; }}"
        )
        row = QHBoxLayout(banner)
        row.setContentsMargins(theme.PAD_M, theme.PAD_M, theme.PAD_M, theme.PAD_M)
        row.setSpacing(theme.PAD_M)

        # Welcome copy — terse, action-oriented.
        msg = QLabel(
            "<b>Welcome.</b>  To start an analysis:"
            "<ol style='margin: 6px 0 0 16px; padding: 0;'>"
            "<li>Pick Run-1 and Run-2 ILI files via <b>Browse</b></li>"
            "<li>Fill in pipeline metadata and MAOP zones, or click "
            "<b>Load YAML</b></li>"
            "<li>Click <b>Proceed to Analysis</b></li>"
            "</ol>"
            "<p style='margin: 6px 0 0 0; color: #5A6C7D;'>"
            "If Run-1 is in a non-NGP vendor format, use "
            "<b>Convert Format</b> first.</p>"
        )
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setStyleSheet(f"color: {theme.COLOR_TEXT}; font-size: 12px;")
        msg.setWordWrap(True)
        row.addWidget(msg, stretch=1)

        # Vertical button column so "Got it" sits centred against the
        # multi-line message.
        btn_col = QVBoxLayout()
        btn_col.addStretch(1)
        btn_dismiss = QPushButton("Got it")
        btn_dismiss.setProperty("role", "primary")
        btn_dismiss.clicked.connect(self._dismiss_welcome)
        btn_col.addWidget(btn_dismiss)
        btn_col.addStretch(1)
        row.addLayout(btn_col)

        # Initial visibility — show only if the user hasn't dismissed
        # it before.
        settings = QSettings("Athena PowerTech LLP", "Athena ILI FFP Tool")
        already_dismissed = bool(settings.value(self._WELCOME_QSETTING_KEY, False, type=bool))
        banner.setVisible(not already_dismissed)
        return banner

    def _dismiss_welcome(self) -> None:
        """Hide the welcome banner permanently for this user."""
        self._welcome_banner.setVisible(False)
        settings = QSettings("Athena PowerTech LLP", "Athena ILI FFP Tool")
        settings.setValue(self._WELCOME_QSETTING_KEY, True)
        settings.sync()

    def _build_converter_handoff_banner(self) -> QFrame:
        """Info banner shown when a Run-1 file is loaded via Convert Format.

        The user lands on Project Setup with run_1 path populated but
        everything else blank — without a nudge it's not obvious what
        to do next. The banner explains the two valid paths (Load YAML,
        or fill the form manually) and hides itself once the form has
        meaningful metadata.
        """
        banner = QFrame()
        banner.setObjectName("convHandoffBanner")
        banner.setStyleSheet(
            f"#convHandoffBanner {{ background-color: #E8F4FD;"
            f" border: 1px solid #5DADE2;"
            f" border-left: 4px solid {theme.COLOR_PRIMARY};"
            f" border-radius: {theme.RADIUS_S}px; }}"
        )
        row = QHBoxLayout(banner)
        row.setContentsMargins(theme.PAD_M, theme.PAD_S, theme.PAD_M, theme.PAD_S)
        row.setSpacing(theme.PAD_S)
        self._handoff_label = QLabel(
            "Run-1 file loaded.  Please fill in project metadata, pipeline "
            "geometry, and MAOP zones before running the analysis — or "
            "click Load YAML to apply a saved project configuration."
        )
        self._handoff_label.setStyleSheet(
            f"color: {theme.COLOR_TEXT}; font-size: 12px;"
        )
        self._handoff_label.setWordWrap(True)
        row.addWidget(self._handoff_label, stretch=1)
        self._btn_handoff_load_yaml = QPushButton("Load YAML…")
        self._btn_handoff_load_yaml.clicked.connect(self._on_load_clicked)
        row.addWidget(self._btn_handoff_load_yaml)
        banner.setVisible(False)
        return banner

    def _maybe_hide_handoff_banner(self) -> None:
        """Hide the converter-handoff banner once the form has metadata."""
        if not self._converter_handoff_banner.isVisible():
            return
        if (
            self.ed_project_name.text().strip()
            or self.sp_diameter.value() > 0
            or self.sp_length.value() > 0
            or self.tbl_zones.rowCount() > 0
        ):
            self._converter_handoff_banner.setVisible(False)

    # -- NGP-format validation banner ------------------------------------
    def _build_ngp_banner(self) -> QFrame:
        banner = QFrame()
        banner.setObjectName("ngpBanner")
        banner.setStyleSheet(
            f"#ngpBanner {{ background-color: #FFF4E5;"
            f" border: 1px solid {theme.COLOR_WARNING};"
            f" border-left: 4px solid {theme.COLOR_WARNING};"
            f" border-radius: {theme.RADIUS_S}px; }}"
        )
        row = QHBoxLayout(banner)
        row.setContentsMargins(theme.PAD_M, theme.PAD_S, theme.PAD_M, theme.PAD_S)
        row.setSpacing(theme.PAD_S)
        self._run1_banner_label = QLabel(
            "This file doesn't appear to be in NGP/Athena format."
        )
        self._run1_banner_label.setStyleSheet(
            f"color: {theme.COLOR_TEXT}; font-size: 12px;"
        )
        self._run1_banner_label.setWordWrap(True)
        row.addWidget(self._run1_banner_label, stretch=1)
        self._btn_convert_run1 = QPushButton("Convert it  →")
        self._btn_convert_run1.clicked.connect(self._on_convert_run1_clicked)
        row.addWidget(self._btn_convert_run1)
        banner.setVisible(False)
        return banner

    def _validate_run1_as_ngp(self) -> None:
        """Read Run-1 with ILIReader; on failure, surface the banner."""
        path_str = self.ed_run1_path.text().strip()
        if not path_str:
            self._run1_banner.setVisible(False)
            return
        # v0.2.3: resolve through helper so a relative path (loaded from
        # a portable YAML) lands at the right absolute location.
        path = resolve_relative_to_yaml(self._current_config_path, path_str)
        if path is None or not path.exists():
            self._run1_banner.setVisible(False)
            return
        try:
            # Import locally so this screen still loads on machines where
            # an optional reader dependency is missing.
            from src.io.ili_reader import ILIReader
            ILIReader().read(str(path), run_id="run_1")
        except Exception as e:                                   # noqa: BLE001
            self._run1_banner_label.setText(
                f"This file doesn't appear to be in NGP/Athena format "
                f"({type(e).__name__}: {str(e)[:80]}…)."
            )
            self._run1_banner.setVisible(True)
            return
        self._run1_banner.setVisible(False)

    def _on_convert_run1_clicked(self) -> None:
        path = self.ed_run1_path.text().strip()
        if path:
            self.convert_run1_requested.emit(path)

    # -- Public API used by MainWindow → Format Converter handoff -------
    def set_run1_file(self, path: str) -> None:
        """Programmatically set the Run-1 path (used by the converter screen).

        The rest of the form is NOT mutated — if the user already had a
        YAML loaded, those values stick around. If the form is empty,
        an info banner surfaces to explain the next step.
        """
        self.ed_run1_path.setText(str(path))
        self._validate_run1_as_ngp()

        # Surface the info banner only if the form is essentially blank.
        form_is_blank = not (
            self.ed_project_name.text().strip()
            or self.sp_diameter.value() > 0
            or self.sp_length.value() > 0
            or self.tbl_zones.rowCount() > 0
        )
        if form_is_blank:
            self._converter_handoff_banner.setVisible(True)
            self.lbl_status.setText("")
        else:
            self._converter_handoff_banner.setVisible(False)
            self.lbl_status.setText(
                "Converted file loaded as Run-1. Click Validate when ready."
            )
            self.lbl_status.setStyleSheet(
                f"color: {theme.COLOR_TEXT_MUTED};"
            )

    # -- Load / save / new -----------------------------------------------
    def _on_new_clicked(self) -> None:
        self._populate_from_dict({})
        self._current_config_path = None
        self.lbl_loaded.setText("New project (unsaved)")
        self.lbl_status.setText("Blank project — fill in fields and validate.")
        self.lbl_status.setStyleSheet(f"color: {theme.COLOR_TEXT_MUTED};")
        self.btn_proceed.setEnabled(False)
        self._converter_handoff_banner.setVisible(False)

    def _on_load_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load project YAML", "",
            "YAML files (*.yaml *.yml);;All files (*.*)",
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.critical(self, "Load failed", f"Could not parse YAML:\n{e}")
            return
        self._populate_from_dict(data)
        self._current_config_path = Path(path)
        self.lbl_loaded.setText(f"Loaded: {Path(path).name}")
        self.status_message.emit(f"Loaded {Path(path).name}")
        self.lbl_status.setText("Loaded. Click Validate, then Proceed.")
        self.lbl_status.setStyleSheet(f"color: {theme.COLOR_TEXT_MUTED};")
        self.btn_proceed.setEnabled(False)        # force a re-validate
        # YAML carries everything — hide the converter-handoff hint.
        self._converter_handoff_banner.setVisible(False)
        # Re-compute years-between with the freshly-loaded dates.
        self._refresh_years_calc()

    def _on_save_clicked(self) -> None:
        suggested = (
            str(self._current_config_path)
            if self._current_config_path else "project.yaml"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Save project YAML", suggested,
            "YAML files (*.yaml *.yml);;All files (*.*)",
        )
        if not path:
            return
        # v0.2.3: build the dict AFTER we know the save destination so
        # run-file paths can be expressed relative to it (when criteria
        # met). Saving to a different drive or distant folder keeps the
        # paths absolute.
        save_path = Path(path)
        data = self._build_config_dict(yaml_save_path=save_path)
        try:
            with open(save_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.critical(self, "Save failed", f"Could not write YAML:\n{e}")
            return
        self._current_config_path = save_path
        self.lbl_loaded.setText(f"Saved: {save_path.name}")
        self.status_message.emit(f"Saved {save_path.name}")

    # -- Populate / harvest ---------------------------------------------
    def _populate_from_dict(self, data: dict[str, Any]) -> None:
        proj = (data.get("project") or {})
        pipe = (data.get("pipeline") or {})
        zones = (data.get("maop_zones") or [])
        runs = (data.get("runs") or {})
        cgr = (data.get("cgr") or {})
        ffp = (data.get("ffp") or {})
        rp = (data.get("repair_prediction") or {})

        self.ed_project_name.setText(str(proj.get("project_name", "")))
        self.ed_pipeline_name.setText(str(proj.get("pipeline_name", "")))
        self.ed_client_name.setText(str(proj.get("client_name", "")))
        self.ed_report_no.setText(str(proj.get("report_number", "")))
        self.ed_report_rev.setText(str(proj.get("report_revision", "00")))
        self.ed_prepared_by.setText(str(proj.get("prepared_by", "")))

        self.sp_diameter.setValue(float(pipe.get("diameter_mm") or 0.0))
        self.sp_length.setValue(float(pipe.get("length_km") or 0.0))
        self.sp_install_year.setValue(int(pipe.get("install_year") or 2010))
        self.ed_material_grade.setText(str(pipe.get("material_grade", "")))
        self.sp_smys.setValue(float(pipe.get("smys_mpa") or 0.0))
        self.ed_product.setText(str(pipe.get("product", "")))
        sc = str(pipe.get("service_class", "liquid"))
        if sc in ("liquid", "gas"):
            self.cb_service_class.setCurrentText(sc)

        # v0.3.0: detect MAOP zoning mode and route accordingly.
        mode_raw = pipe.get("maop_zoning_mode") or "wt"
        self._maop_zoning_mode = str(mode_raw).strip().lower() or "wt"
        if self._maop_zoning_mode not in ("wt", "chainage"):
            self._maop_zoning_mode = "wt"
        # In chainage mode, the WT zone table doesn't apply — disable
        # editing and stash the raw zones for round-trip on save. A
        # full chainage-aware editor lands in a later v0.3.x.
        self._loaded_maop_zones_raw = list(zones) if isinstance(zones, list) else []
        self.tbl_zones.setRowCount(0)
        if self._maop_zoning_mode == "chainage":
            # Disable the WT zone table — users edit the YAML directly
            # for now. Banner / lookup still works via the stashed
            # raw zones (see `_refresh_maop_design_warning`).
            self.tbl_zones.setEnabled(False)
            if hasattr(self, "btn_add_zone"):
                self.btn_add_zone.setEnabled(False)
            if hasattr(self, "btn_del_zone"):
                self.btn_del_zone.setEnabled(False)
            # Populate the WT table read-only with a placeholder note
            # row so the user can SEE the chainage zones in the UI.
            from PyQt6.QtWidgets import QTableWidgetItem
            self.tbl_zones.setRowCount(len(self._loaded_maop_zones_raw))
            for ri, z in enumerate(self._loaded_maop_zones_raw):
                if not isinstance(z, dict):
                    continue
                lo = z.get("chainage_m_min")
                hi = z.get("chainage_m_max")
                df = z.get("design_factor")
                mp = z.get("maop_kgcm2")
                # Use the WT columns to display the chainage values
                # (header still says WT — known v0.3.0 limitation).
                cells = [
                    (0, f"chain≥{lo}" if lo is not None else ""),
                    (1, f"chain≤{hi}" if hi is not None else ""),
                    (2, f"{df}" if df is not None else ""),
                    (3, f"{mp}" if mp is not None else ""),
                ]
                for col, txt in cells:
                    item = QTableWidgetItem(str(txt))
                    self.tbl_zones.setItem(ri, col, item)
        else:
            self.tbl_zones.setEnabled(True)
            if hasattr(self, "btn_add_zone"):
                self.btn_add_zone.setEnabled(True)
            if hasattr(self, "btn_del_zone"):
                self.btn_del_zone.setEnabled(True)
            for z in zones:
                if not isinstance(z, dict):
                    continue
                self._append_zone_row(
                    wt_min=float(z.get("wt_mm_min") or 0.0),
                    wt_max=float(z.get("wt_mm_max") or 0.0),
                    df=float(z.get("design_factor") or 0.72),
                    maop=float(z.get("maop_kgcm2") or 0.0),
                )

        r1 = runs.get("run_1") or {}
        r2 = runs.get("run_2") or {}
        self.ed_run1_path.setText(str(r1.get("file_path", "")))
        self.ed_run2_path.setText(str(r2.get("file_path", "")))
        self.ed_run1_vendor.setText(str(r1.get("vendor", "")))
        self.ed_run2_vendor.setText(str(r2.get("vendor", "")))
        self.ed_run1_tool.setText(str(r1.get("tool_type", "")))
        self.ed_run2_tool.setText(str(r2.get("tool_type", "")))
        self._set_date(self.dt_run1, r1.get("inspection_date"))
        self._set_date(self.dt_run2, r2.get("inspection_date"))

        if cgr.get("mode") in _CGR_MODES:
            self.cb_cgr_mode.setCurrentText(cgr["mode"])
        if ffp.get("primary_method") in _FFP_METHODS:
            self.cb_ffp_method.setCurrentText(ffp["primary_method"])
        self.sp_horizon.setValue(int(rp.get("horizon_years") or 10))

        # v0.2.5: load the per-topic annexure selection. Missing
        # block -> legacy 3-topic default (parse_report_annexures
        # handles this). Invalid block raises ValueError at load time
        # so the user sees a clear error instead of a wrong sheet
        # selection silently.
        try:
            selection = parse_report_annexures(
                data.get("report") or {},
                yaml_path=getattr(self, "_current_config_path", None),
            )
        except ValueError as e:
            QMessageBox.warning(
                self, "Invalid report.annexures block", str(e)
            )
            from src.reports.topic_registry import default_annexure_selection
            selection = default_annexure_selection()
        self.panel_annexure_topics.set_selection(selection)

    def _set_date(self, widget: QDateEdit, value: Any) -> None:
        if not value:
            return
        try:
            d = date.fromisoformat(str(value))
            widget.setDate(QDate(d.year, d.month, d.day))
        except ValueError:
            pass

    def _build_config_dict(
        self,
        yaml_save_path: Path | None = None,
    ) -> dict[str, Any]:
        """Harvest the form into a YAML-ready dict.

        v0.2.3: when ``yaml_save_path`` is supplied, run-file paths are
        rendered relative to the save destination via
        ``relativize_if_possible`` so the YAML stays portable. Two
        sub-cases:

          * Form's path text is already RELATIVE (loaded from a
            relative YAML): resolve it against the LOAD location
            (``self._current_config_path``) first, then re-express
            relative to the SAVE location.
          * Form's path text is ABSOLUTE (Browse / typed): just
            relativize.

        With ``yaml_save_path=None`` the raw widget text is written
        verbatim (legacy behaviour).
        """
        run1_text = self.ed_run1_path.text().strip()
        run2_text = self.ed_run2_path.text().strip()
        run1_out = self._path_for_yaml(run1_text, yaml_save_path)
        run2_out = self._path_for_yaml(run2_text, yaml_save_path)
        return {
            "project": {
                "project_name": self.ed_project_name.text().strip(),
                "pipeline_name": self.ed_pipeline_name.text().strip(),
                "client_name": self.ed_client_name.text().strip(),
                "report_number": self.ed_report_no.text().strip(),
                "report_revision": self.ed_report_rev.text().strip() or "00",
                "prepared_by": self.ed_prepared_by.text().strip(),
            },
            "pipeline": self._build_pipeline_dict(),
            "maop_zones": self._build_maop_zones_for_save(),
            "runs": {
                "run_1": {
                    "file_path": run1_out,
                    "inspection_date": self.dt_run1.date().toString("yyyy-MM-dd"),
                    "vendor": self.ed_run1_vendor.text().strip(),
                    "tool_type": self.ed_run1_tool.text().strip(),
                },
                "run_2": {
                    "file_path": run2_out,
                    "inspection_date": self.dt_run2.date().toString("yyyy-MM-dd"),
                    "vendor": self.ed_run2_vendor.text().strip(),
                    "tool_type": self.ed_run2_tool.text().strip(),
                },
            },
            "cgr": {"mode": self.cb_cgr_mode.currentText()},
            "ffp": {"primary_method": self.cb_ffp_method.currentText()},
            "repair_prediction": {
                "horizon_years": int(self.sp_horizon.value()),
            },
            # v0.2.5: persist the per-topic annexure selection so the
            # YAML round-trips between machines (and between v0.2.5+
            # tool versions). serialize_report_annexures emits letters
            # explicitly even when equal to the registry default — see
            # its docstring.
            "report": serialize_report_annexures(
                self.panel_annexure_topics.selection()
            ),
        }

    def _build_pipeline_dict(self) -> dict[str, Any]:
        """Render the pipeline block, including maop_zoning_mode when
        set to anything non-default (v0.3.0).
        """
        out: dict[str, Any] = {
            "diameter_mm": float(self.sp_diameter.value()),
            "length_km": float(self.sp_length.value()),
            "install_year": int(self.sp_install_year.value()),
            "material_grade": self.ed_material_grade.text().strip(),
            "smys_mpa": float(self.sp_smys.value()),
            "product": self.ed_product.text().strip(),
            "service_class": self.cb_service_class.currentText(),
        }
        mode = getattr(self, "_maop_zoning_mode", "wt")
        if mode != "wt":
            out["maop_zoning_mode"] = mode
        return out

    def _build_maop_zones_for_save(self) -> list[dict[str, Any]]:
        """Render the ``maop_zones`` block for save.

        v0.3.0: in chainage mode the WT zone table is disabled in the
        UI; we round-trip the stashed raw chainage zones from load
        instead of trying to re-derive them. WT mode keeps the existing
        behaviour (harvest from the editable table).
        """
        mode = getattr(self, "_maop_zoning_mode", "wt")
        if mode == "chainage":
            stashed = getattr(self, "_loaded_maop_zones_raw", None) or []
            # Round-trip the chainage zones verbatim — preserves the
            # full dict (chainage_m_min/max + design_factor + maop) and
            # any future fields without our explicit involvement.
            return [dict(z) for z in stashed if isinstance(z, dict)]
        return self._read_zones()

    def _path_for_yaml(
        self,
        raw_text: str,
        yaml_save_path: Path | None,
    ) -> str:
        """Convert a line-edit run-file path string to a YAML-ready value.

        Two-step:
          1. Resolve the input through the LOAD context (so a
             previously-relative path lands at the right absolute
             location regardless of CWD).
          2. If a save destination was provided, re-express the
             resolved absolute as relative-to-save when criteria met
             (see ``relativize_if_possible``).

        Empty input -> empty string. No save destination -> raw text
        (legacy behaviour) so non-save callers don't get surprised.
        """
        if not raw_text:
            return ""
        if yaml_save_path is None:
            return raw_text
        resolved = resolve_relative_to_yaml(
            self._current_config_path, raw_text,
        )
        if resolved is None:
            return raw_text
        return relativize_if_possible(yaml_save_path, resolved)

    # -- Validate / proceed ---------------------------------------------
    def _on_validate_clicked(self) -> None:
        ok, message = self._validate()
        if ok:
            self.lbl_status.setText("✓ " + message)
            self.lbl_status.setStyleSheet(f"color: {theme.COLOR_SUCCESS};")
            self.btn_proceed.setEnabled(True)
            self.status_message.emit("Project validated — ready to run.")
        else:
            self.lbl_status.setText("✗ " + message)
            self.lbl_status.setStyleSheet(f"color: {theme.COLOR_ERROR};")
            self.btn_proceed.setEnabled(False)
        # MAOP-vs-WT sanity check: NON-BLOCKING (the banner is purely
        # advisory). Runs after the hard validations regardless of
        # ok/fail — the user often Validates an in-progress form to
        # see what's left, and surfacing the warning then is helpful
        # rather than confusing.
        self._refresh_maop_design_warning()

    def _refresh_maop_design_warning(self) -> None:
        """Compare each MAOP zone to the Barlow design limit for the
        thinnest WT WITHIN THAT ZONE in Run-2. Surface a yellow warning
        banner when any zone exceeds the limit by more than 5%
        (tolerance for vendor sub-nominal WT readings).

        v0.2.4 fix: pre-v0.2.4 the check used the GLOBAL min WT across
        all features for every zone, which fired false-positive
        warnings on multi-zone pipelines where higher MAOPs covered
        thicker pipe (the physically correct setup). The check now
        buckets features by zone (with orphan-fallback to nearest zone,
        mirroring Pipeline.maop_for_wt) and computes the design limit
        from each zone's own WT_min.

        Zones with no Run-2 features after orphan fallback are skipped
        silently — no banner, no crash. This handles over-declared
        YAMLs (zones with no measurements yet) gracefully.

        The 5% tolerance keeps the banner quiet for legitimate
        operating-MAOP-equals-design-MAOP cases where the reader's
        rounding produces a t_min that's nominally a hair below the
        design WT (e.g. 7.91 mm reported on a 7.9 mm spec).
        """
        # v0.3.0: select zones source + mode based on declared zoning.
        mode = getattr(self, "_maop_zoning_mode", "wt")
        if mode == "chainage":
            zones = list(getattr(self, "_loaded_maop_zones_raw", None) or [])
        else:
            zones = self._read_zones()
        if not zones:
            self._set_maop_design_warning(None)
            return
        smys = float(self.sp_smys.value())
        od = float(self.sp_diameter.value())
        if smys <= 0 or od <= 0:
            self._set_maop_design_warning(None)
            return

        run2 = self.ed_run2_path.text().strip()
        if not run2:
            # No Run-2 loaded yet → no t_min to compare against. The
            # check is silent rather than firing on stub form state.
            self._set_maop_design_warning(None)
            return

        # v0.2.4: per-zone WT_min (with orphan fallback to nearest zone).
        # v0.3.0: pass `mode` so bucketing uses the right key.
        per_zone_wt_min = _wt_min_per_zone_in_run2(run2, zones, mode=mode)

        # Collect zones whose MAOP exceeds the design limit. Each zone
        # uses its OWN WT_min (the thinnest pipe in that zone) and its
        # OWN Fd to compute design_max. Zones with no features go
        # through silently.
        offenders: list[dict] = []
        for z, t_min_z in zip(zones, per_zone_wt_min):
            if t_min_z is None or t_min_z <= 0:
                continue
            maop_d = _max_design_maop_kgcm2(
                smys_mpa=smys, wt_mm=t_min_z, od_mm=od,
                design_factor=z["design_factor"],
            )
            if maop_d is None:
                continue
            if z["maop_kgcm2"] > maop_d * 1.05:
                # v0.3.0: capture either WT or chainage bounds for the
                # banner text, depending on mode.
                if mode == "chainage":
                    bound_lo = z.get("chainage_m_min")
                    bound_hi = z.get("chainage_m_max")
                else:
                    bound_lo = z.get("wt_mm_min")
                    bound_hi = z.get("wt_mm_max")
                offenders.append({
                    "bound_lo": bound_lo,
                    "bound_hi": bound_hi,
                    "maop": z["maop_kgcm2"],
                    "fd": z["design_factor"],
                    "design_max": maop_d,
                    "t_min_z": t_min_z,
                })

        if not offenders:
            self._set_maop_design_warning(None)
            return

        bound_unit = "m" if mode == "chainage" else "mm"
        zone_word = "section" if mode == "chainage" else "zone"
        if len(offenders) == 1:
            o = offenders[0]
            msg = (
                f"<b>Warning:</b> MAOP {o['maop']:.1f} kg/cm² "
                f"({zone_word} {o['bound_lo']:.1f}–{o['bound_hi']:.1f} "
                f"{bound_unit}, Fd={o['fd']:.2f}) exceeds the design "
                f"limit for the thinnest wall <i>in this {zone_word}</i> "
                f"(WT_min={o['t_min_z']:.2f} mm gives "
                f"MAOP_design_max={o['design_max']:.1f} kg/cm²). "
                f"Most features in this section will compute ERF ≥ 1.0 "
                f"under this MAOP, which usually indicates the MAOP is "
                f"set for a different section or wrong design factor. "
                f"Recommend verifying the operating MAOP for this "
                f"segment with the operator."
            )
        else:
            lines = "; ".join(
                f"{zone_word} {o['bound_lo']:.1f}–{o['bound_hi']:.1f} "
                f"{bound_unit} at MAOP={o['maop']:.1f} (thinnest in-zone "
                f"WT {o['t_min_z']:.2f} mm, design max {o['design_max']:.1f})"
                for o in offenders
            )
            msg = (
                f"<b>Warning:</b> {len(offenders)} MAOP {zone_word}s exceed "
                f"the design limit for the thinnest wall in their respective "
                f"{zone_word}. Offending {zone_word}s: {lines}. "
                f"Most features in these sections will compute ERF ≥ 1.0 "
                f"— verify the operating MAOPs with the operator."
            )
        self._set_maop_design_warning(msg)

    def _set_maop_design_warning(self, message: str | None) -> None:
        """Toggle the yellow MAOP-vs-WT warning banner under the MAOP
        zones table.

        Same visual vocabulary as the years-between warning banner —
        warm yellow with a Warning-coloured left accent. Lazily
        created on first non-None call so screen construction doesn't
        allocate an extra widget for the common happy path.
        """
        if message is None:
            if hasattr(self, "_maop_warning_banner"):
                self._maop_warning_banner.setVisible(False)
            return
        if not hasattr(self, "_maop_warning_banner"):
            banner = QLabel(message)
            banner.setWordWrap(True)
            banner.setTextFormat(Qt.TextFormat.RichText)
            banner.setStyleSheet(
                f"background-color: #FFF4E5;"
                f" border: 1px solid {theme.COLOR_WARNING};"
                f" border-left: 4px solid {theme.COLOR_WARNING};"
                f" border-radius: {theme.RADIUS_S}px;"
                f" padding: 8px 12px;"
                f" color: {theme.COLOR_TEXT}; font-size: 12px;"
            )
            # Insert the banner directly below the MAOP-zones table.
            # tbl_zones lives inside the maop card's body; we add to
            # that body's layout so it appears right under the table.
            parent_layout = self.tbl_zones.parent().layout()
            if parent_layout is not None:
                parent_layout.addWidget(banner)
            self._maop_warning_banner = banner
        self._maop_warning_banner.setText(message)
        self._maop_warning_banner.setVisible(True)

    def _has_any_maop_zone(self) -> bool:
        """v0.3.0: are there any zones at all, in either mode?"""
        if getattr(self, "_maop_zoning_mode", "wt") == "chainage":
            return bool(getattr(self, "_loaded_maop_zones_raw", None))
        return bool(self._read_zones())

    def _validate(self) -> tuple[bool, str]:
        if not self.ed_project_name.text().strip():
            return False, "Project name is required."
        if self.sp_diameter.value() <= 0:
            return False, "Pipeline diameter must be > 0."
        if self.sp_length.value() <= 0:
            return False, "Pipeline length must be > 0."
        # v0.3.0: chainage-mode YAMLs don't populate the WT zone
        # table (it's disabled), so use the mode-aware helper.
        if not self._has_any_maop_zone():
            return False, "At least one MAOP zone is required."
        if self.sp_smys.value() <= 0:
            return False, "SMYS must be > 0 MPa."
        run1_raw = self.ed_run1_path.text().strip()
        run2_raw = self.ed_run2_path.text().strip()
        # v0.2.3: validate the RESOLVED path (relative-to-YAML if not absolute)
        # rather than the raw widget text. Without this, a portable YAML with
        # "foo.xlsx" would always fail validation because Path("foo.xlsx").exists()
        # checks against CWD.
        run1_resolved = resolve_relative_to_yaml(self._current_config_path, run1_raw)
        run2_resolved = resolve_relative_to_yaml(self._current_config_path, run2_raw)
        if not run1_raw or run1_resolved is None or not run1_resolved.exists():
            return False, (
                f"Run-1 file not found.\n"
                f"  YAML:     {self._current_config_path or '(unsaved)'}\n"
                f"  Resolved: {run1_resolved}\n"
                f"  Raw text: {run1_raw or '(empty)'}"
            )
        if not run2_raw or run2_resolved is None or not run2_resolved.exists():
            return False, (
                f"Run-2 file not found.\n"
                f"  YAML:     {self._current_config_path or '(unsaved)'}\n"
                f"  Resolved: {run2_resolved}\n"
                f"  Raw text: {run2_raw or '(empty)'}"
            )
        # v0.2.5: at least one annexure topic must be selected and
        # letters must be unique.
        annex_msg = self.panel_annexure_topics.validity_message()
        if annex_msg:
            return False, annex_msg
        return True, "Project validated."

    def _on_annexure_selection_changed(self) -> None:
        """v0.2.5: rerun the lightweight `_on_field_edited` clear-status
        hook when the user toggles a topic or edits a letter, so a
        stale red "Select at least one annexure" doesn't linger after
        the user fixes it."""
        self._on_field_edited()

    def _on_proceed_clicked(self) -> None:
        ok, message = self._validate()
        if not ok:
            QMessageBox.warning(self, "Cannot proceed", message)
            return

        config_path = self._current_config_path
        if config_path is None or not config_path.exists():
            # No persisted YAML yet → write a temp YAML next to run-1.
            # v0.2.3: resolve run-1 path through helper before computing
            # the temp dir, so a YAML-relative path doesn't end up
            # writing to CWD.
            run1_resolved = resolve_relative_to_yaml(
                self._current_config_path,
                self.ed_run1_path.text().strip(),
            )
            run1_dir = run1_resolved.parent if run1_resolved else Path.cwd()
            config_path = run1_dir / "_gui_temp_project.yaml"
            try:
                with open(config_path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(
                        self._build_config_dict(yaml_save_path=config_path),
                        f,
                        sort_keys=False, allow_unicode=True,
                    )
            except Exception as e:                               # noqa: BLE001
                QMessageBox.critical(
                    self, "Save failed",
                    f"Could not write temp project YAML:\n{e}",
                )
                return
            self._current_config_path = config_path

        else:
            # Persist current form state back to the file the user loaded
            # so 'proceed' always reflects what's on screen.
            try:
                with open(config_path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(
                        self._build_config_dict(yaml_save_path=config_path),
                        f,
                        sort_keys=False, allow_unicode=True,
                    )
            except Exception as e:                               # noqa: BLE001
                QMessageBox.critical(
                    self, "Save failed",
                    f"Could not update project YAML:\n{e}",
                )
                return

        # v0.2.3: resolve relative run-paths against the YAML's parent
        # before handing them to the worker. The worker also resolves
        # (defense in depth), but we pass the absolute form so the
        # Run Analysis screen displays a meaningful path.
        run1_abs = resolve_relative_to_yaml(
            config_path, self.ed_run1_path.text().strip(),
        )
        run2_abs = resolve_relative_to_yaml(
            config_path, self.ed_run2_path.text().strip(),
        )
        job = AnalysisJob(
            config_path=config_path,
            run1_path=run1_abs or Path(self.ed_run1_path.text()),
            run2_path=run2_abs or Path(self.ed_run2_path.text()),
            years_override=(
                float(self.sp_years_override.value())
                if self.cb_years_override.isChecked() else None
            ),
            # Resolve to a user-writable spot. v0.2.1 used Path("./output")
            # which was relative to CWD; under a Start-Menu launch from
            # %ProgramFiles%, that CWD == install dir == ACL-protected,
            # so out_dir.mkdir() raised PermissionError. The Run Analysis
            # screen still lets the user override via Browse… .
            output_dir=resolve_output_dir(
                config_path,
                self.ed_project_name.text().strip() or "ffp_project",
            ),
            # v0.2.5: pass the per-topic selection. The legacy
            # `annexure_format` field is retained for backward compat
            # but is None when the YAML carried a `report.annexures`
            # block (the worker prefers `report_annexures` when set).
            annexure_format=None,
            report_annexures=self.panel_annexure_topics.selection(),
            write_docx=self.cb_write_docx.isChecked(),
        )
        self.ready.emit(job)

    # =====================================================================
    # Auto-fill from vendor Final Report PDF
    # =====================================================================

    # Form-state introspection used by the auto-fill overwrite guard.
    # If ANY of these widgets carry user-provided values, we treat the
    # form as "dirty" and prompt before overwriting.
    def _form_has_user_data(self) -> bool:
        """True if the form has any data a user would consider their own.

        Used by the PDF auto-fill flow to decide whether to ask before
        overwriting. Empty placeholders / spinbox-at-minimum / blank
        line-edits do NOT count as user data.
        """
        if self.ed_project_name.text().strip():
            return True
        if self.ed_pipeline_name.text().strip():
            return True
        if self.ed_client_name.text().strip():
            return True
        if self.ed_material_grade.text().strip():
            return True
        if self.ed_product.text().strip():
            return True
        if self.sp_diameter.value() > 0:
            return True
        if self.sp_length.value() > 0:
            return True
        if self.sp_smys.value() > 0:
            return True
        if self.sp_install_year.value() > 0:
            return True
        if self.tbl_zones.rowCount() > 0:
            return True
        if self.ed_run1_path.text().strip():
            return True
        if self.ed_run2_path.text().strip():
            return True
        return False

    def _on_autofill_pdf_clicked(self) -> None:
        """Open a PDF picker, parse it, show a preview, apply on confirm.

        If the form already has user-provided data, ask before
        overwriting — without this guard, auto-fill silently overlays
        new metadata on top of old state, which can produce
        self-contradictory project files (e.g. pipeline length from the
        new PDF but section name from the old YAML).
        """
        if self._form_has_user_data():
            reply = QMessageBox.question(
                self,
                "Overwrite existing project data?",
                "This will overwrite the existing project metadata "
                "with values from the PDF.\n\n"
                "Fields that the PDF doesn't carry (e.g. Run-1 file "
                "path, report number, reviewed-by) will be preserved. "
                "But anything the PDF does carry — pipeline name, "
                "OD, length, MAOP zones, dates — will be replaced.\n\n"
                "Continue?",
                QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        start_dir = ""
        if self._current_config_path:
            start_dir = str(self._current_config_path.parent)
        path, _ = QFileDialog.getOpenFileName(
            self, "Select vendor Final Report PDF", start_dir,
            "PDF files (*.pdf);;All files (*.*)",
        )
        if not path:
            return

        # Parser import is lazy so PyQt-only users without pypdf still
        # load the screen.
        try:
            from src.io.vendor_report_parser import VendorReportParser
        except ImportError as e:                                  # noqa: BLE001
            QMessageBox.critical(
                self, "PDF parsing unavailable",
                f"pypdf is not installed:\n{e}\n\n"
                "Install with: pip install pypdf",
            )
            return

        try:
            md = VendorReportParser().parse(path)
        except FileNotFoundError as e:
            QMessageBox.critical(self, "File not found", str(e))
            return
        except ValueError as e:
            QMessageBox.critical(
                self, "Corrupt or unsupported PDF",
                f"Couldn't open this PDF: {e}",
            )
            return
        except Exception as e:                                    # noqa: BLE001
            QMessageBox.critical(
                self, "PDF parsing failed",
                f"{type(e).__name__}: {e}",
            )
            return

        found = md.found_field_count()
        if found == 0:
            QMessageBox.information(
                self, "Nothing extracted",
                "No recognised Athena/NGP-format fields were found in "
                "this PDF.\n\nIf this is a Final Report from a different "
                "vendor (Rosen, Baker Hughes, etc.), fill the form "
                "manually for now.\n\nDetails:\n"
                + "\n".join("• " + n for n in md.extraction_notes[:5]),
            )
            return

        dlg = _PdfPreviewDialog(md, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            # User cancelled — don't touch the form.
            return

        self._apply_extracted_metadata(md)
        applied = md.found_field_count()
        self.status_message.emit(
            f"Pre-filled {applied} field{'s' if applied != 1 else ''} from "
            "the vendor report. Please verify the Run-1 inspection date "
            "before running the analysis."
        )
        # Hide the welcome banner — the user is clearly off the
        # first-launch path now.
        self._welcome_banner.setVisible(False)

    def _apply_extracted_metadata(self, md) -> None:
        """Map :class:`ExtractedMetadata` onto the form's widgets.

        Low-confidence fields (<0.7) get a yellow tint so the user knows
        to double-check them. Multiple wall thicknesses are exploded
        into MAOP-zone rows with a sensible default WT range.
        """
        # ----- Identity
        if md.project_name:
            self.ed_project_name.setText(md.project_name)
        if md.pipeline_name:
            self.ed_pipeline_name.setText(md.pipeline_name)
        if md.client:
            self.ed_client_name.setText(md.client)

        # ----- Geometry
        if md.outer_diameter_mm:
            self.sp_diameter.setValue(float(md.outer_diameter_mm))
        if md.length_km:
            self.sp_length.setValue(float(md.length_km))
        if md.material_grade:
            self.ed_material_grade.setText(md.material_grade)
        if md.smys_mpa:
            self.sp_smys.setValue(float(md.smys_mpa))
        if md.installation_year:
            self.sp_install_year.setValue(int(md.installation_year))

        # ----- Product / service class
        # Parser now pulls these from "Pipeline medium during inspection
        # <X>" lines on page 6, plus a "<X> pipeline" cover-page form.
        # service_class is derived from product via the parser's
        # PRODUCT_CLASS_MAP, so for known products both fields land
        # together; for unknown products only `product` is set and the
        # user picks service_class manually.
        if md.product:
            self.ed_product.setText(md.product)
        if md.service_class and md.service_class in (
            "liquid", "gas", "multiphase"
        ):
            self.cb_service_class.setCurrentText(md.service_class)

        # ----- Run-2 date (best-effort)
        if md.run2_inspection_year:
            self.dt_run2.setDate(QDate(int(md.run2_inspection_year), 1, 1))
            # If the PDF carried a full date string, try to parse it
            # back to a (y, m, d) tuple for better precision.
            if md.run2_inspection_date_str:
                self._try_set_date_from_string(self.dt_run2,
                                               md.run2_inspection_date_str)

        # ----- Run-1 date (best-effort, from "Previous inspections" line)
        # Lets the user skip the only field that previously had to be
        # typed by hand. When the parser only found a year (mid-year
        # default applied), the confidence highlighter on the date
        # widget will tint it yellow so the user verifies.
        if md.run1_inspection_year:
            self.dt_run1.setDate(QDate(int(md.run1_inspection_year), 7, 1))
            if md.run1_inspection_date_str:
                self._try_set_date_from_string(self.dt_run1,
                                               md.run1_inspection_date_str)

        # ----- MAOP zones — see _build_maop_zones() for the algorithm.
        zones = _build_maop_zones(
            wall_thicknesses=md.wall_thicknesses_mm,
            maop_kgcm2=md.maop_kgcm2,
            design_factor=md.design_factor,
        )
        if zones:
            self.tbl_zones.setRowCount(0)
            for wt_min, wt_max, df, maop in zones:
                self._append_zone_row(
                    wt_min=wt_min, wt_max=wt_max, df=df, maop=maop,
                )

        # ----- Vendor / technology → Run-2 metadata (PDF was Run-2's vendor)
        if md.vendor:
            self.ed_run2_vendor.setText(md.vendor)
        if md.inspection_technology:
            self.ed_run2_tool.setText(md.inspection_technology)

        # ----- Apply low-confidence highlights so the user sees what
        #       the parser was unsure about.
        self._apply_confidence_highlights(md.confidence_per_field)

        # Refresh derived state (years-between, export gating, etc.).
        self._refresh_years_calc()
        # Clear status so the user sees a clean form on the next change.
        self._on_field_edited()

    @staticmethod
    def _try_set_date_from_string(widget: QDateEdit, raw: str) -> None:
        """Best-effort date parsing; leave widget alone on failure."""
        import re as _re
        from datetime import datetime as _dt
        # Try a few common formats; fall back to year-only.
        for fmt in (
            "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y",
            "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y",
            "%d-%b-%Y", "%d-%B-%Y",
        ):
            try:
                d = _dt.strptime(raw.strip(), fmt).date()
                widget.setDate(QDate(d.year, d.month, d.day))
                return
            except ValueError:
                continue
        # No format matched — leave whatever year-only set we already did.

    def _apply_confidence_highlights(
        self, confidence_per_field: dict[str, float],
    ) -> None:
        """Tint low-confidence fields yellow so the user inspects them."""
        FIELD_TO_WIDGET = {
            "project_name":           self.ed_project_name,
            "pipeline_name":          self.ed_pipeline_name,
            "client":                 self.ed_client_name,
            "outer_diameter_mm":      self.sp_diameter,
            "length_km":              self.sp_length,
            "material_grade":         self.ed_material_grade,
            "smys_mpa":               self.sp_smys,
        }
        for fname, conf in confidence_per_field.items():
            w = FIELD_TO_WIDGET.get(fname)
            if w is None:
                continue
            if conf < 0.7:
                w.setStyleSheet(
                    "background-color: #FFF9C4; "    # warm yellow
                    f"border: 1px solid {theme.COLOR_WARNING};"
                )
                w.setToolTip(
                    f"Auto-filled from PDF with low confidence "
                    f"({conf:.2f}). Please verify."
                )
            else:
                # Clear any prior tint so a fresh high-confidence value
                # looks normal.
                w.setStyleSheet("")
                w.setToolTip("")


# ---------------------------------------------------------------------------
# PDF-extraction preview dialog
# ---------------------------------------------------------------------------

class _PdfPreviewDialog(QDialog):
    """Modal showing the parser's extracted values + confidence per field.

    The user gets one chance to review before any form field is touched.
    Accepting applies; cancelling leaves the form alone.
    """

    # Human-readable labels for the metadata fields, in display order.
    _FIELD_LABELS: tuple[tuple[str, str], ...] = (
        ("project_name",            "Project name"),
        ("pipeline_name",           "Pipeline section name"),
        ("pipeline_section_code",   "Pipeline section code"),
        ("client",                  "Client"),
        ("vendor",                  "Vendor (Run-2)"),
        ("inspection_technology",   "Inspection technology"),
        ("outer_diameter_mm",       "Outer diameter (mm)"),
        ("length_km",               "Pipeline length (km)"),
        ("wall_thicknesses_mm",     "Wall thicknesses (mm)"),
        ("installation_year",       "Installation year"),
        ("material_grade",          "Material grade"),
        ("smys_mpa",                "SMYS (MPa)"),
        ("maop_kgcm2",              "MAOP (kg/cm²)"),
        ("design_factor",           "Design factor"),
        ("product",                 "Product"),
        ("service_class",           "Service class"),
        ("run1_inspection_date_str", "Run-1 inspection date"),
        ("run1_inspection_year",    "Run-1 inspection year"),
        ("run2_inspection_date_str", "Run-2 inspection date"),
        ("run2_inspection_year",    "Run-2 inspection year"),
    )

    def __init__(self, md, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Auto-fill preview")
        self.resize(720, 540)
        self._build_ui(md)

    def _build_ui(self, md) -> None:
        from .. import theme as _theme

        v = QVBoxLayout(self)
        v.setContentsMargins(theme.PAD_M, theme.PAD_M, theme.PAD_M, theme.PAD_M)
        v.setSpacing(theme.PAD_S)

        found = md.found_field_count()
        title = QLabel(
            f"<b>Found {found} field{'s' if found != 1 else ''} in the vendor "
            "Final Report.</b>"
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(title)

        subtitle = QLabel(
            "Review the values below. Anything marked <span "
            f"style='color:{_theme.COLOR_WARNING}'>low confidence</span> "
            "deserves a second look — that field will be highlighted in "
            "the form after you apply, so you can verify it against the "
            "vendor's actual table."
        )
        subtitle.setTextFormat(Qt.TextFormat.RichText)
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color: {_theme.COLOR_TEXT_MUTED};")
        v.addWidget(subtitle)

        # ----- Extracted-values table
        from PyQt6.QtWidgets import QTableWidget as _QTW
        tbl = _QTW(0, 3)
        _theme.apply_table_palette(tbl)
        tbl.setHorizontalHeaderLabels(["Field", "Value", "Confidence"])
        tbl.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        tbl.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(_QTW.EditTrigger.NoEditTriggers)
        tbl.setSelectionMode(_QTW.SelectionMode.NoSelection)

        for field_name, display_label in self._FIELD_LABELS:
            raw_value = getattr(md, field_name, None)
            if raw_value in (None, [], {}):
                continue
            value_str = self._format_value(raw_value)
            conf = md.confidence_per_field.get(field_name, 0.0)
            r = tbl.rowCount()
            tbl.insertRow(r)
            tbl.setItem(r, 0, _theme.themed_item(display_label))
            tbl.setItem(r, 1, _theme.themed_item(value_str))
            conf_item = _theme.themed_item(f"{conf:.2f}")
            if conf < 0.5:
                conf_item.setForeground(QBrush(QColor(_theme.COLOR_ERROR)))
            elif conf < 0.7:
                conf_item.setForeground(QBrush(QColor(_theme.COLOR_WARNING)))
            tbl.setItem(r, 2, conf_item)
        v.addWidget(tbl, stretch=1)

        # ----- Extraction notes (collapsible-feeling read-only block)
        if md.extraction_notes:
            note_lbl = QLabel(
                "<b>Notes:</b> " + " &nbsp;•&nbsp; ".join(
                    md.extraction_notes[:6]
                )
            )
            note_lbl.setTextFormat(Qt.TextFormat.RichText)
            note_lbl.setStyleSheet(
                f"color: {_theme.COLOR_TEXT_MUTED}; font-size: 11px;"
            )
            note_lbl.setWordWrap(True)
            v.addWidget(note_lbl)

        # ----- Action buttons
        bb = QDialogButtonBox()
        btn_apply = bb.addButton(
            "Apply to form", QDialogButtonBox.ButtonRole.AcceptRole,
        )
        btn_apply.setProperty("role", "primary")
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    @staticmethod
    def _format_value(value) -> str:
        """Render a parser-extracted value as a one-line string."""
        if isinstance(value, (list, tuple)):
            return ", ".join(str(v) for v in value)
        if isinstance(value, float):
            # Avoid printing trailing zeros: 70.0 → "70", 0.72 → "0.72".
            return f"{value:g}"
        return str(value)
