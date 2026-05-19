"""Format Converter screen — vendor-file → NGP-format column mapper.

Three-column layout:

    +-------------------+-------------------+-------------------+
    |  SOURCE FILE      |  VALUE NORMS      |  CANONICAL FIELDS |
    |  (left, 40%)      |  (center, 20%)    |  (right, 40%)     |
    |                   |                   |                   |
    |  [Browse...]      |  Activates when   |  REQUIRED         |
    |  Sheet: [▾]       |  a categorical    |  - anomaly_id     |
    |  Header row: [3]  |  drop target is   |  - abs_distance_m |
    |                   |  clicked. Lists   |  - joint_number   |
    |  ┌─────────────┐  |  the distinct     |  - wt_mm          |
    |  │ Column A    │  |  source values    |  - depth_pct_wt   |
    |  │ samples     │  |  and a dropdown   |                   |
    |  ├─────────────┤  |  per value.       |  RECOMMENDED      |
    |  │ Column B    │  |                   |  - length_mm      |
    |  │ samples     │  |  [Apply suggested]|  ...              |
    |  ├─────────────┤  |                   |                   |
    |  │ Column C    │  |                   |  OPTIONAL         |
    |  │ samples     │  |                   |  ...              |
    |  └─────────────┘  |                   |                   |
    +-------------------+-------------------+-------------------+
    |  Profile name:  [_______]  [Load…] [Save]                 |
    |                            [Preview…]  [Export to NGP →]  |
    +-----------------------------------------------------------+

User flow:
  1. Click "Browse…" — pick a vendor file.
  2. Pick sheet + header row (auto-detected via propose_profile()).
  3. Auto-detect proposes a draft profile; high-confidence mappings
     pre-fill the canonical drop targets.
  4. User drags source columns from the left list onto canonical
     fields (right). One source can map to many canonical fields.
  5. Categorical fields (surface, feature_identification,
     dimension_class) reveal the value-norm panel on click — user maps
     distinct source values to canonical codes.
  6. "Preview" runs the converter on a small slice, shows a modal.
  7. "Export to NGP format" runs FormatConverter.convert(), shows a
     success dialog, emits the export path so the rest of the GUI can
     use it as Run-1.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
from PyQt6.QtCore import (
    QByteArray,
    QMimeData,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QDrag,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QFont,
    QMouseEvent,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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
from ..paths import (
    bundled_vendor_profiles_dir,
    ensure_dir,
    user_vendor_profiles_dir,
)
from ...io.format_converter import (
    FormatConverter,
    VendorProfile,
    propose_profile,
)
from ...io.format_converter.auto_detect import (
    _excel_engine_for,
)
from ...io.format_converter.converter import NGP_OUTPUT_COLUMNS

# ---------------------------------------------------------------------------
# Field catalogue per the prompt
# ---------------------------------------------------------------------------

# Field name → display label.
REQUIRED_FIELDS = (
    ("anomaly_id",      "Feature / Anomaly ID"),
    ("abs_distance_m",  "Absolute distance (chainage)"),
    ("joint_number",    "Joint number"),
    ("wt_mm",           "Wall thickness"),
    ("depth_pct_wt",    "Depth"),
)
RECOMMENDED_FIELDS = (
    ("length_mm",              "Length (axial)"),
    ("width_mm",               "Width (circumferential)"),
    ("clock_position",         "Clock / orientation"),
    ("surface",                "Surface (Int/Ext)"),
    ("feature_identification", "Feature identification"),
    ("upstream_weld_dist_m",   "Distance to upstream weld"),
)
OPTIONAL_FIELDS = (
    ("dimension_class",  "Dimension class"),
    ("latitude",         "Latitude"),
    ("longitude",        "Longitude"),
    ("joint_length_m",   "Joint length"),
    ("description",      "Description (free text)"),
)

ALL_FIELDS = REQUIRED_FIELDS + RECOMMENDED_FIELDS + OPTIONAL_FIELDS
REQUIRED_FIELD_NAMES = {f for f, _ in REQUIRED_FIELDS}
RECOMMENDED_FIELD_NAMES = {f for f, _ in RECOMMENDED_FIELDS}

# ---- Pipe-registry sheet fields (the optional secondary sheet) -------------
# Stored in VendorProfile.pipe_column_mappings under the same canonical keys
# as the defect sheet — the "pipe" context comes from the section heading,
# not the field names.
PIPE_REQUIRED_FIELDS = (
    ("joint_number",   "Joint number"),
    ("joint_length_m", "Joint length"),
)
PIPE_OPTIONAL_FIELDS = (
    ("wt_mm",                "Wall thickness"),
    ("abs_distance_m",       "Absolute distance"),
    ("upstream_weld_dist_m", "Distance to upstream weld"),
    ("latitude",             "Latitude"),
    ("longitude",            "Longitude"),
)

# ---- Defect-sheet picker (the main feature list we'll be converting) ------
#
# Athena's real-world workbooks ship 6-10 sheets and only ONE of them is the
# actual defect list. Older v0.2 builds got this wrong on the Kandla file
# (auto-picked "Reference Point Marker list" — a 30-row marker registry —
# instead of the 79-row "Metal Loss List"), because the older `auto_detect.
# py` heuristic let any-sheet-with-≥3-canonical-hits through. These rules
# are deliberately stricter: skip-list runs first, then preferred-keyword
# match, then row-count tie-break, then exact-name override.

_DEFECT_PREFER_KEYWORDS = (
    "metal loss",     # NGP / Athena multi-sheet 2018+
    "anomaly",
    "defect",
    "feature",
    "ml list",        # shorthand seen in some vendor exports
)

_DEFECT_SKIP_KEYWORDS = (
    "reference",      # "Reference Point Marker list" — markers, not defects
    "marker",
    "weld",           # standalone weld sheets — joint geometry, not defects
    "pipe tally",
    "pipe registry",
    "pipeline tally",
    "summary",
    "abbreviation",
    "cover",
    "toc",
    "wall thickness", # WT-zone tables; not per-feature
    "casing",
    "bend",
    "adjacent",       # Adjacent Metal Object — not defects per se
    "installation",
)


def _pick_defect_sheet(all_sheets) -> str | None:
    """Pick the most likely defect/feature sheet from a workbook.

    Selection order (highest-priority first):
      1. Drop sheets whose name contains any :data:`_DEFECT_SKIP_KEYWORDS`
         substring — references, markers, weld-only, pipe-tally, summary,
         etc.
      2. Among the rest, prefer sheets whose name contains any
         :data:`_DEFECT_PREFER_KEYWORDS` substring (earlier in the tuple =
         stronger match).
      3. Tie-break by row count (the actual defect list is invariably
         the largest of the candidates).
      4. Exact-name override: if any candidate equals "metal loss list"
         (case-insensitive), it wins outright.

    Returns the chosen sheet name, or ``None`` if every sheet was skipped.

    Args:
        all_sheets: ``{name: DataFrame}`` for every sheet in the workbook.
    """
    candidates = []
    for name, df in all_sheets.items():
        n = name.strip().lower()
        if any(kw in n for kw in _DEFECT_SKIP_KEYWORDS):
            continue
        kw_score = 0
        for i, kw in enumerate(_DEFECT_PREFER_KEYWORDS):
            if kw in n:
                kw_score = len(_DEFECT_PREFER_KEYWORDS) - i
                break
        exact = 1 if n == "metal loss list" else 0
        row_count = len(df) if df is not None else 0
        candidates.append((exact, kw_score, row_count, name))

    if not candidates:
        return None
    # If any candidate has a kw_score of 0 (no preferred keyword), we
    # still let it through — it's better than picking nothing — but
    # candidates that match a preferred keyword beat those that don't.
    candidates.sort(reverse=True)
    return candidates[0][3]


# Sheet-name keywords that suggest a pipe-tally / joint-registry sheet,
# in priority order (earlier = better match).
_PIPE_SHEET_KEYWORDS = ("tally", "pipe", "joint", "registry", "weld")


def _pick_pipe_sheet(sheet_names, exclude: str = "") -> str | None:
    """Heuristic: pick the most likely pipe-registry sheet by name.

    Args:
        sheet_names: Iterable of sheet names to consider.
        exclude: Sheet already chosen for defects — skip it.

    Returns the best candidate name, or ``None`` if no sheet name
    contains any of the recognised pipe-tally keywords.
    """
    excl = (exclude or "").strip().lower()
    best: tuple[int, str] | None = None
    for name in sheet_names:
        if not name:
            continue
        if name.strip().lower() == excl:
            continue
        lo = name.lower()
        for i, kw in enumerate(_PIPE_SHEET_KEYWORDS):
            if kw in lo:
                if best is None or i < best[0]:
                    best = (i, name)
                break
    return best[1] if best else None

# Canonical fields that have unit-of-measure choices the user can flip.
# Keys are the canonical field; values are (unit_conventions_key, options, default).
UNIT_CHOICES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "abs_distance_m":       ("chainage",            ("m", "km", "ft"), "m"),
    "upstream_weld_dist_m": ("upstream_weld_dist",  ("m", "km", "ft"), "m"),
    "depth_pct_wt":         ("depth",               ("%", "mm", "fraction"), "%"),
    "wt_mm":                ("wall_thickness",      ("mm", "in"), "mm"),
    "length_mm":            ("length",              ("mm", "in", "cm", "m"), "mm"),
    "width_mm":             ("width",               ("mm", "in", "cm", "m"), "mm"),
    "clock_position":       ("clock",               ("hh:mm", "decimal_hr", "degrees", "radians"), "hh:mm"),
    "joint_length_m":       ("joint_length",        ("m", "km", "ft", "mm"), "m"),
}

# Categorical fields that activate the value-norm sub-panel when clicked.
CATEGORICAL_FIELDS = ("surface", "feature_identification", "dimension_class")


# ---------------------------------------------------------------------------
# Source columns list — QListWidget with text/plain MIME drag support
# ---------------------------------------------------------------------------

_MIME_SOURCE_COLUMN = "application/x-athena-source-column"


class _SourceColumnList(QListWidget):
    """A QListWidget whose drags carry the item text as plain text + a
    private MIME type the drop targets accept.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setUniformItemSizes(False)
        self.setStyleSheet(
            f"QListWidget {{ background-color: {theme.COLOR_CARD_BG};"
            f" border: 1px solid {theme.COLOR_CARD_BORDER};"
            f" border-radius: {theme.RADIUS_S}px; }}"
            f"QListWidget::item {{ padding: 6px 8px;"
            f" border-bottom: 1px solid {theme.COLOR_CARD_BORDER}; }}"
            f"QListWidget::item:selected {{ background-color: #E3F2FD;"
            f" color: {theme.COLOR_TEXT}; }}"
        )

    def startDrag(self, supportedActions) -> None:                # noqa: N802
        item = self.currentItem()
        if item is None:
            return
        column_name = item.data(Qt.ItemDataRole.UserRole) or item.text()
        mime = QMimeData()
        mime.setText(str(column_name))
        mime.setData(_MIME_SOURCE_COLUMN, QByteArray(str(column_name).encode()))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


# ---------------------------------------------------------------------------
# Drop target for one canonical field
# ---------------------------------------------------------------------------

class _CanonicalFieldRow(QFrame):
    """One row in the right panel — drop target + unit dropdown + clear button.

    Signals:
        mapping_changed(canonical_field, source_column_or_empty)
        clicked(canonical_field) — fires when the user clicks the row;
            used by the value-norm panel to refresh.
    """

    mapping_changed = pyqtSignal(str, str)
    clicked = pyqtSignal(str)

    def __init__(
        self,
        canonical_field: str,
        label: str,
        importance: str,                # "required" / "recommended" / "optional"
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.canonical_field = canonical_field
        self.importance = importance
        self._mapped_source: str = ""
        self._build_ui(label)
        self.setAcceptDrops(True)

    def _build_ui(self, label: str) -> None:
        self.setObjectName("dropTarget")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(2)

        row1 = QHBoxLayout()
        row1.setSpacing(6)
        self._label = QLabel(label)
        self._label.setStyleSheet(
            f"color: {theme.COLOR_TEXT}; font-weight: 600; font-size: 12px;"
        )
        row1.addWidget(self._label, stretch=1)

        # Unit dropdown — only visible when this field has unit choices.
        self.unit_combo: QComboBox | None = None
        if self.canonical_field in UNIT_CHOICES:
            _key, options, default = UNIT_CHOICES[self.canonical_field]
            self.unit_combo = QComboBox()
            self.unit_combo.addItems(options)
            self.unit_combo.setCurrentText(default)
            self.unit_combo.setFixedWidth(90)
            row1.addWidget(self.unit_combo)

        self.btn_clear = QPushButton("×")
        self.btn_clear.setFixedSize(20, 20)
        self.btn_clear.setToolTip("Clear this mapping")
        self.btn_clear.clicked.connect(self._on_clear)
        self.btn_clear.setVisible(False)
        row1.addWidget(self.btn_clear)
        outer.addLayout(row1)

        self._mapped_label = QLabel("⤵  Drop a source column here")
        self._mapped_label.setStyleSheet(
            f"color: {theme.COLOR_TEXT_MUTED}; font-size: 11px;"
            f" font-style: italic;"
        )
        outer.addWidget(self._mapped_label)

        self._refresh_style()

    # ----------------------------------------------------------- public API
    def set_mapping(self, source_column: str) -> None:
        self._mapped_source = source_column or ""
        if self._mapped_source:
            self._mapped_label.setText(f"✓  {self._mapped_source}")
            self._mapped_label.setStyleSheet(
                f"color: {theme.COLOR_SUCCESS}; font-size: 11px;"
                f" font-weight: 500;"
            )
            self.btn_clear.setVisible(True)
        else:
            self._mapped_label.setText("⤵  Drop a source column here")
            self._mapped_label.setStyleSheet(
                f"color: {theme.COLOR_TEXT_MUTED}; font-size: 11px;"
                f" font-style: italic;"
            )
            self.btn_clear.setVisible(False)
        self._refresh_style()
        self.mapping_changed.emit(self.canonical_field, self._mapped_source)

    def mapped_source(self) -> str:
        return self._mapped_source

    def selected_unit(self) -> str | None:
        if self.unit_combo is None:
            return None
        return self.unit_combo.currentText()

    def set_unit(self, unit: str) -> None:
        if self.unit_combo is not None and unit:
            idx = self.unit_combo.findText(unit)
            if idx >= 0:
                self.unit_combo.setCurrentIndex(idx)

    # ----------------------------------------------------------- internals
    def _refresh_style(self) -> None:
        # Border styling that conveys (importance × mapped-state).
        if self._mapped_source:
            # Filled: subtle solid border in card colour.
            self.setStyleSheet(
                f"#dropTarget {{ background-color: {theme.COLOR_CARD_BG};"
                f" border: 1px solid {theme.COLOR_SUCCESS};"
                f" border-radius: {theme.RADIUS_S}px;"
                f" border-left: 4px solid {theme.COLOR_SUCCESS}; }}"
            )
        else:
            accent = {
                "required":    theme.COLOR_ERROR,
                "recommended": theme.COLOR_WARNING,
                "optional":    theme.COLOR_TEXT_MUTED,
            }[self.importance]
            self.setStyleSheet(
                f"#dropTarget {{ background-color: {theme.COLOR_CARD_BG};"
                f" border: 1px dashed {accent};"
                f" border-radius: {theme.RADIUS_S}px;"
                f" border-left: 4px solid {accent}; }}"
            )

    def _on_clear(self) -> None:
        self.set_mapping("")

    # ----------------------------------------------------------- drag events
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:    # noqa: N802
        if event.mimeData().hasFormat(_MIME_SOURCE_COLUMN):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:    # noqa: N802
        event.accept()

    def dropEvent(self, event: QDropEvent) -> None:              # noqa: N802
        raw = event.mimeData().data(_MIME_SOURCE_COLUMN)
        if not raw:
            event.ignore()
            return
        source = bytes(raw).decode("utf-8", errors="replace")
        self.set_mapping(source)
        event.acceptProposedAction()

    def mousePressEvent(self, event: QMouseEvent) -> None:       # noqa: N802
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.canonical_field)


# ---------------------------------------------------------------------------
# Preview dialog
# ---------------------------------------------------------------------------

class _PreviewDialog(QDialog):
    """Show the first N transformed rows in a table."""

    def __init__(self, df: pd.DataFrame, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preview — transformed output (first rows)")
        self.resize(900, 460)
        v = QVBoxLayout(self)

        info = QLabel(
            f"Showing first {len(df)} rows in the NGP-canonical layout. "
            "If anything looks wrong (mis-mapped column, wrong units, "
            "stale value normalisations), close this dialog and adjust "
            "before exporting."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {theme.COLOR_TEXT_MUTED}; font-size: 11px;")
        v.addWidget(info)

        table = QTableWidget(len(df), len(df.columns))
        theme.apply_table_palette(table)
        table.setHorizontalHeaderLabels([str(c) for c in df.columns])
        for r in range(len(df)):
            for c, col in enumerate(df.columns):
                value = df.iat[r, c]
                if pd.isna(value):
                    text = ""
                elif isinstance(value, float):
                    text = f"{value:.6g}"
                else:
                    text = str(value)
                item = theme.themed_item(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(r, c, item)
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        table.verticalHeader().setVisible(False)
        v.addWidget(table, stretch=1)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        v.addWidget(btns)


# ---------------------------------------------------------------------------
# Value-normalisation panel (centre strip)
# ---------------------------------------------------------------------------

# Suggested canonical values per categorical field — keep in sync with
# config/column_synonyms.yaml's value_normalisations section.
_CANONICAL_VALUES = {
    "surface": ("internal", "external", "midwall", "unknown"),
    "feature_identification": (
        "CORR", "COCL", "MIAN", "MIAC", "DENT", "DEML",
        "CRAC", "GWAN", "SWAN", "LWAN",
    ),
    "dimension_class": (
        "GENE", "PITT", "PINH", "AXGR", "AXSL", "CIGR", "CISL",
    ),
}


class _ValueNormPanel(QWidget):
    """Centre strip: lists distinct source values for a categorical field
    and lets the user pick a canonical value for each.

    Emits :pyattr:`mapping_changed(canonical_field, {source_value: canonical})`.
    """

    mapping_changed = pyqtSignal(str, dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._active_field: str | None = None
        self._mappings: dict[str, dict[str, str]] = {}
        self._source_df: pd.DataFrame | None = None
        self._column_mappings: dict[str, str] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(theme.PAD_M, theme.PAD_M, theme.PAD_M, theme.PAD_M)
        v.setSpacing(theme.PAD_S)

        title = QLabel("Value Normalisation")
        title.setProperty("role", "sectionHeader")
        v.addWidget(title)

        self._subtitle = QLabel(
            "Click a categorical field (surface, feature_identification, "
            "dimension_class) to map its distinct source values to "
            "canonical codes."
        )
        self._subtitle.setStyleSheet(
            f"color: {theme.COLOR_TEXT_MUTED}; font-size: 11px;"
        )
        self._subtitle.setWordWrap(True)
        v.addWidget(self._subtitle)

        self._table = QTableWidget(0, 2)
        theme.apply_table_palette(self._table)
        self._table.setHorizontalHeaderLabels(["Source value", "Canonical"])
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        v.addWidget(self._table, stretch=1)

        btns = QHBoxLayout()
        self._btn_suggest = QPushButton("Apply suggested")
        self._btn_suggest.setToolTip(
            "Try to auto-map using case-insensitive 'starts with' matching "
            "against the canonical values."
        )
        self._btn_suggest.clicked.connect(self._on_apply_suggested)
        self._btn_suggest.setEnabled(False)
        btns.addWidget(self._btn_suggest)
        btns.addStretch(1)
        v.addLayout(btns)

    # ----------------------------------------------------------- public API

    def set_source_data(
        self,
        df: pd.DataFrame | None,
        column_mappings: dict[str, str],
    ) -> None:
        """Hand the panel the loaded source DataFrame + current mappings."""
        self._source_df = df
        self._column_mappings = dict(column_mappings)
        if self._active_field:
            self.show_field(self._active_field)

    def get_mappings(self) -> dict[str, dict[str, str]]:
        """Return ``{canonical_field: {source_value: canonical_value}}``."""
        return {
            f: dict(m) for f, m in self._mappings.items() if m
        }

    def load_mappings(self, mappings: dict[str, dict[str, str]]) -> None:
        self._mappings = {f: dict(m) for f, m in mappings.items()}
        if self._active_field:
            self.show_field(self._active_field)

    def clear(self) -> None:
        self._active_field = None
        self._mappings.clear()
        self._table.setRowCount(0)
        self._btn_suggest.setEnabled(False)
        self._subtitle.setText(
            "Click a categorical field on the right panel to map values."
        )

    def show_field(self, canonical_field: str) -> None:
        """Activate the panel for one categorical field."""
        self._active_field = canonical_field
        if canonical_field not in CATEGORICAL_FIELDS:
            self._subtitle.setText(
                f"'{canonical_field}' is not a categorical field. "
                "Pick surface, feature_identification, or dimension_class."
            )
            self._table.setRowCount(0)
            self._btn_suggest.setEnabled(False)
            return

        source_col = self._column_mappings.get(canonical_field, "")
        if not source_col or self._source_df is None or (
            source_col not in self._source_df.columns
        ):
            self._subtitle.setText(
                f"Map a source column to '{canonical_field}' on the right "
                "first, then come back here."
            )
            self._table.setRowCount(0)
            self._btn_suggest.setEnabled(False)
            return

        # Compute distinct source values (small set — categorical).
        series = self._source_df[source_col].dropna().astype(str)
        distinct = sorted({s.strip() for s in series if s.strip()})

        self._subtitle.setText(
            f"Mapping '{source_col}' → {canonical_field}. "
            f"{len(distinct)} distinct value(s)."
        )
        self._btn_suggest.setEnabled(True)

        canonical_options = ["(pass through)"] + list(
            _CANONICAL_VALUES.get(canonical_field, ())
        )
        existing = self._mappings.setdefault(canonical_field, {})

        self._table.setRowCount(len(distinct))
        for r, value in enumerate(distinct):
            src_item = theme.themed_item(value)
            src_item.setFlags(src_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(r, 0, src_item)
            combo = QComboBox()
            combo.addItems(canonical_options)
            current = existing.get(value, "")
            if current and current in canonical_options:
                combo.setCurrentText(current)
            elif current:
                # External value the user typed — add it on the fly.
                combo.addItem(current)
                combo.setCurrentText(current)
            else:
                combo.setCurrentIndex(0)
            combo.currentTextChanged.connect(
                lambda txt, src=value: self._on_combo_changed(src, txt)
            )
            self._table.setCellWidget(r, 1, combo)

    # ----------------------------------------------------------- handlers
    def _on_combo_changed(self, source_value: str, canonical: str) -> None:
        if self._active_field is None:
            return
        m = self._mappings.setdefault(self._active_field, {})
        if canonical and canonical != "(pass through)":
            m[source_value] = canonical
        else:
            m.pop(source_value, None)
        self.mapping_changed.emit(self._active_field, dict(m))

    def _on_apply_suggested(self) -> None:
        if not self._active_field:
            return
        options = _CANONICAL_VALUES.get(self._active_field, ())
        if not options:
            return
        m = self._mappings.setdefault(self._active_field, {})
        for r in range(self._table.rowCount()):
            src_item = self._table.item(r, 0)
            if src_item is None:
                continue
            src_value = src_item.text().strip()
            if not src_value:
                continue
            guess = _suggest_canonical_value(src_value, options)
            if guess is None:
                continue
            combo = self._table.cellWidget(r, 1)
            if isinstance(combo, QComboBox):
                idx = combo.findText(guess)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            m[src_value] = guess
        self.mapping_changed.emit(self._active_field, dict(m))


def _suggest_canonical_value(src: str, options: tuple[str, ...]) -> str | None:
    """Cheap suggestion: starts-with / contains case-insensitive match."""
    s = src.strip().lower().rstrip(".")
    for o in options:
        if o.lower() == s:
            return o
    for o in options:
        if o.lower().startswith(s) or s.startswith(o.lower()):
            return o
    # Surface shortcuts
    if src.upper() in ("I", "IN", "INT", "INTERIOR"):
        return "internal" if "internal" in options else options[0]
    if src.upper() in ("E", "EX", "EXT", "EXTERIOR"):
        return "external" if "external" in options else options[0]
    return None


# ---------------------------------------------------------------------------
# Pipe-registry section (optional, collapsible)
# ---------------------------------------------------------------------------

class _PipeRegistrySection(QGroupBox):
    """Self-contained "Pipe Registry (Optional)" section.

    Owns its own sheet selector, header-row spinner, source-column list,
    and canonical-field drop targets — all addressing a second sheet in
    the same workbook (typically "Pipeline Tally" or similar).

    Using a checkable QGroupBox is the standard Qt idiom for an
    "enable / disable this whole sub-section" toggle: the title shows a
    checkbox, and Qt disables every child widget when unchecked.

    Signals:
        contents_changed() — fires on any sheet/header/mapping change so
            the parent screen can refresh export-readiness state.
    """

    contents_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        # The title doubles as the checkbox label per the prompt.
        super().__init__(
            "Source file has a separate pipe-tally / joint-list sheet",
            parent,
        )
        self.setCheckable(True)
        self.setChecked(False)
        self.toggled.connect(self._on_toggled)

        # State ----------------------------------------------------------
        self._all_sheets: dict[str, pd.DataFrame] = {}
        self._defect_sheet_name: str = ""
        self._source_df: pd.DataFrame | None = None
        self._field_rows: dict[str, _CanonicalFieldRow] = {}

        self._build_ui()

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(theme.PAD_M, theme.PAD_L, theme.PAD_M, theme.PAD_M)
        v.setSpacing(theme.PAD_S)

        # Banner shown when we auto-pick a pipe sheet on file load.
        self._banner = QLabel("")
        self._banner.setStyleSheet(
            f"background-color: #E3F2FD;"
            f" color: {theme.COLOR_TEXT};"
            f" border: 1px solid #90CAF9;"
            f" border-radius: {theme.RADIUS_S}px;"
            f" padding: 6px 8px; font-size: 11px;"
        )
        self._banner.setWordWrap(True)
        self._banner.setVisible(False)
        v.addWidget(self._banner)

        subtitle = QLabel(
            "Mapping a pipe-registry sheet gives the joint aligner the full "
            "joint list (welds + every joint, with or without defects). "
            "Without it, only joints with ≥1 defect end up in the registry, "
            "and alignment quality drops — affecting CGR by 10%+ on real "
            "pipelines."
        )
        subtitle.setStyleSheet(
            f"color: {theme.COLOR_TEXT_MUTED}; font-size: 11px;"
        )
        subtitle.setWordWrap(True)
        v.addWidget(subtitle)

        # Sheet + header row picker --------------------------------------
        sheet_row = QHBoxLayout()
        sheet_row.addWidget(QLabel("Pipe sheet:"))
        self._cb_sheet = QComboBox()
        self._cb_sheet.currentIndexChanged.connect(self._on_sheet_changed)
        sheet_row.addWidget(self._cb_sheet, stretch=1)
        sheet_row.addSpacing(theme.PAD_M)
        sheet_row.addWidget(QLabel("Header row:"))
        self._sp_header = QSpinBox()
        self._sp_header.setRange(0, 50)
        self._sp_header.setValue(0)
        self._sp_header.valueChanged.connect(self._on_header_changed)
        sheet_row.addWidget(self._sp_header)
        v.addLayout(sheet_row)

        # Source-column preview list -------------------------------------
        v.addWidget(QLabel("Pipe-sheet columns (drag onto fields below):"))
        self._list = _SourceColumnList()
        self._list.setFixedHeight(120)
        v.addWidget(self._list)

        # Required + optional drop targets -------------------------------
        v.addWidget(self._build_group("REQUIRED",
                                      PIPE_REQUIRED_FIELDS, "required"))
        v.addWidget(self._build_group("OPTIONAL",
                                      PIPE_OPTIONAL_FIELDS, "optional"))

        # Disable everything until the box is checked (Qt also does this
        # automatically for a checkable QGroupBox, but explicit is safer
        # if the user disables/re-enables programmatically).
        self._on_toggled(False)

    def _build_group(
        self,
        title: str,
        fields: tuple[tuple[str, str], ...],
        importance: str,
    ) -> QGroupBox:
        gb = QGroupBox(title)
        gb.setStyleSheet(
            f"QGroupBox {{ font-weight: 600; color: {theme.COLOR_TEXT_MUTED};"
            f" border: 1px solid {theme.COLOR_CARD_BORDER};"
            f" border-radius: {theme.RADIUS_S}px;"
            f" margin-top: 8px; font-size: 11px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin;"
            f" left: 10px; padding: 0 4px; }}"
        )
        v = QVBoxLayout(gb)
        v.setContentsMargins(theme.PAD_S, theme.PAD_M, theme.PAD_S, theme.PAD_S)
        v.setSpacing(4)
        for canon, label in fields:
            row = _CanonicalFieldRow(canon, label, importance)
            row.mapping_changed.connect(
                lambda *_args: self.contents_changed.emit()
            )
            self._field_rows[canon] = row
            v.addWidget(row)
        return gb

    # ---------------------------------------------------------- public API
    def set_workbook(
        self,
        all_sheets: dict[str, pd.DataFrame],
        defect_sheet_name: str,
        default_header_row: int | None = None,
    ) -> None:
        """Hand the section the loaded workbook + current defect-sheet pick.

        Re-populates the sheet dropdown, excluding the defect sheet, and
        runs the keyword heuristic. If a candidate is found, the section
        auto-enables itself and shows the banner.

        ``default_header_row`` (if given) is used as the fallback header
        row for the pipe sheet. Workbooks usually keep the same layout
        across sheets, so using the defect sheet's header row is a much
        better default than 0.
        """
        self._all_sheets = dict(all_sheets)
        self._defect_sheet_name = defect_sheet_name

        previous = self._cb_sheet.currentText()
        self._cb_sheet.blockSignals(True)
        self._cb_sheet.clear()
        for name in self._all_sheets:
            if name == defect_sheet_name:
                continue
            self._cb_sheet.addItem(name)
        self._cb_sheet.blockSignals(False)

        # Inherit the defect sheet's header row by default — workbooks
        # typically share a layout across sheets.
        if default_header_row is not None:
            self._sp_header.blockSignals(True)
            self._sp_header.setValue(int(default_header_row))
            self._sp_header.blockSignals(False)

        # Auto-pick: prefer a sheet matching pipe-tally keywords.
        candidate = _pick_pipe_sheet(
            self._all_sheets.keys(), exclude=defect_sheet_name,
        )

        if candidate:
            self._cb_sheet.setCurrentText(candidate)
            # Auto-enable the section so the user sees the suggestion
            # without having to hunt for it.
            self.setChecked(True)
            self._banner.setText(
                f"✓  Detected pipe-tally sheet '{candidate}' — included "
                "by default. Toggle off if not needed."
            )
            self._banner.setVisible(True)
        else:
            # No obvious candidate; pick the previously-chosen sheet if
            # it's still around, else nothing.
            if previous and previous in self._all_sheets and previous != defect_sheet_name:
                self._cb_sheet.setCurrentText(previous)
            self._banner.setVisible(False)

        self._refresh_active_df()
        self._auto_apply_mappings()
        self.contents_changed.emit()

    def is_active(self) -> bool:
        """``True`` if the user has the section toggled on AND a sheet picked."""
        return self.isChecked() and bool(self._cb_sheet.currentText())

    def selected_sheet(self) -> str:
        return self._cb_sheet.currentText()

    def selected_header_row(self) -> int:
        return int(self._sp_header.value())

    def get_mappings(self) -> dict[str, str]:
        """Return ``{canonical_field: source_column}`` for mapped pipe fields."""
        return {
            canon: row.mapped_source()
            for canon, row in self._field_rows.items()
            if row.mapped_source()
        }

    def load_from_profile(self, profile) -> None:
        """Populate the section from a previously-saved VendorProfile."""
        if not profile.pipe_sheet_name:
            self.setChecked(False)
            return
        self.setChecked(True)
        if profile.pipe_sheet_name in self._all_sheets:
            self._cb_sheet.setCurrentText(profile.pipe_sheet_name)
        self._sp_header.setValue(int(profile.pipe_header_row))
        try:
            mappings = profile.normalised_pipe_mappings()
        except ValueError:
            mappings = {}
        for canon, row in self._field_rows.items():
            row.set_mapping(mappings.get(canon, ""))
        self.contents_changed.emit()

    def required_unmapped(self) -> list[str]:
        """Return canonical names of REQUIRED pipe fields that aren't mapped."""
        return [
            canon for canon, _ in PIPE_REQUIRED_FIELDS
            if not self._field_rows[canon].mapped_source()
        ]

    # ------------------------------------------------------------- handlers
    def _on_toggled(self, checked: bool) -> None:
        # Qt's checkable QGroupBox disables children automatically, but
        # we also need to clear / refresh the source-column list so
        # toggling on after a sheet change picks up new headers.
        if checked:
            self._refresh_active_df()
            self._auto_apply_mappings()
        self.contents_changed.emit()

    def _on_sheet_changed(self, _idx: int) -> None:
        self._refresh_active_df()
        self._auto_apply_mappings()
        self.contents_changed.emit()

    def _on_header_changed(self, _value: int) -> None:
        self._refresh_active_df()
        self._auto_apply_mappings()
        self.contents_changed.emit()

    # --------------------------------------------------------- DF + mapping
    def _refresh_active_df(self) -> None:
        sheet = self._cb_sheet.currentText()
        header_row = self._sp_header.value()
        if not sheet or sheet not in self._all_sheets:
            self._source_df = None
            self._list.clear()
            return
        raw = self._all_sheets[sheet]
        if header_row >= len(raw):
            self._source_df = None
            self._list.clear()
            return
        # Build unique column names: prefer the actual header text, but
        # fall back to "col_{i}" for NaN / empty cells. Without this, two
        # NaN headers collide as duplicates and pandas turns body[col]
        # into a DataFrame, which then breaks the loop below in a Qt-
        # silencing way.
        headers = _unique_column_names(raw.iloc[header_row].tolist())
        body = raw.iloc[header_row + 1:].copy()
        body.columns = headers
        body = body.dropna(axis=1, how="all").dropna(axis=0, how="all")
        body.reset_index(drop=True, inplace=True)
        self._source_df = body

        self._list.clear()
        for col in body.columns:
            samples = body[col].dropna().astype(str).head(3).tolist()
            unit_hint = _detect_unit_hint(str(col), body[col])
            sample_txt = ", ".join(s[:25] for s in samples) if samples else "(empty)"
            label = (
                f"{col}\n   samples: {sample_txt}"
                + (f"   •   detected: {unit_hint}" if unit_hint else "")
            )
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, str(col))
            self._list.addItem(item)

    def _auto_apply_mappings(self) -> None:
        """Run the synonym engine on the pipe sheet's headers."""
        if self._source_df is None:
            return
        # Lazy import to keep startup cheap.
        from ...io.format_converter.auto_detect import (
            propose_mappings_for_dataframe,
        )
        try:
            mappings = propose_mappings_for_dataframe(self._source_df)
        except Exception:                                        # noqa: BLE001
            mappings = {}
        # Only set rows that exist in this section (joint_number,
        # joint_length_m, wt_mm, abs_distance_m, upstream_weld_dist_m,
        # latitude, longitude). Skip canonical fields auto-detect spots
        # but that aren't part of our pipe-mapping vocabulary.
        for canon, row in self._field_rows.items():
            if canon in mappings:
                row.set_mapping(mappings[canon])


# ---------------------------------------------------------------------------
# Main screen
# ---------------------------------------------------------------------------

class FormatConverterScreen(QWidget):
    """The full converter screen.

    Signals:
        export_complete(output_path: str) — fires after a successful
            FormatConverter.convert(). MainWindow saves the path and
            shows the "Use as Run-1" action.
        use_as_run1(output_path: str) — fires when the user clicks
            "Use as Run-1 in next project". MainWindow routes the path
            to ProjectSetup.
        status_message(text: str) — for the status bar.
    """

    export_complete = pyqtSignal(str)
    use_as_run1 = pyqtSignal(str)
    status_message = pyqtSignal(str)
    go_to_project_setup_requested = pyqtSignal()  # user clicked "Go to Project Setup"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source_path: Path | None = None
        self._source_df: pd.DataFrame | None = None
        self._all_sheets: dict[str, pd.DataFrame] = {}
        self._field_rows: dict[str, _CanonicalFieldRow] = {}
        self._last_export_path: Path | None = None
        self._build_ui()

    # ============================================================ build UI

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(theme.PAD_XL, theme.PAD_L, theme.PAD_XL, theme.PAD_L)
        root.setSpacing(theme.PAD_M)

        title = QLabel("Convert Vendor File to NGP Format")
        title.setProperty("role", "screenTitle")
        subtitle = QLabel(
            "Map your vendor's columns to the NGP/Athena format, then save "
            "the mapping as a profile for future use."
        )
        subtitle.setProperty("role", "screenSubtitle")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        # ----- Scope banner -----
        # Most Athena projects don't need this screen — Run-2 is always
        # NGP (Athena-internal), and Run-1 usually is too. Make that
        # explicit so users coming here by accident know to bail back
        # to Project Setup.
        root.addWidget(self._build_scope_banner())

        # ----- 3-column body
        body = QHBoxLayout()
        body.setSpacing(theme.PAD_M)
        body.addWidget(self._build_left_panel(), stretch=4)
        body.addWidget(self._build_centre_panel(), stretch=2)
        body.addWidget(self._build_right_panel(), stretch=4)
        root.addLayout(body, stretch=1)

        # ----- Footer
        root.addWidget(self._build_footer())

    def _build_scope_banner(self) -> QFrame:
        """Top-of-screen banner clarifying when to use Convert Format."""
        banner = QFrame()
        banner.setObjectName("convertScopeBanner")
        banner.setStyleSheet(
            f"#convertScopeBanner {{ background-color: #E8F4FD;"
            f" border: 1px solid #5DADE2;"
            f" border-left: 4px solid {theme.COLOR_PRIMARY};"
            f" border-radius: {theme.RADIUS_S}px; }}"
        )
        row = QHBoxLayout(banner)
        row.setContentsMargins(theme.PAD_M, theme.PAD_S, theme.PAD_M, theme.PAD_S)
        row.setSpacing(theme.PAD_S)
        lbl = QLabel(
            "<b>Use this only if your Run-1 file is in a non-NGP format</b> "
            "(Rosen, Baker Hughes, NDT Global, Onstream, etc.). "
            "If your file already uses NGP/Athena column conventions, go "
            "straight to Project Setup and click Browse — the reader will "
            "accept it directly."
        )
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setStyleSheet(f"color: {theme.COLOR_TEXT}; font-size: 12px;")
        lbl.setWordWrap(True)
        row.addWidget(lbl, stretch=1)
        btn = QPushButton("Go to Project Setup")
        btn.clicked.connect(self.go_to_project_setup_requested.emit)
        row.addWidget(btn)
        return banner

    # ----- Left panel (source file + columns) -------------------------------
    def _build_left_panel(self) -> QWidget:
        card = QFrame()
        card.setProperty("role", "card")
        v = QVBoxLayout(card)
        v.setContentsMargins(theme.PAD_M, theme.PAD_M, theme.PAD_M, theme.PAD_M)
        v.setSpacing(theme.PAD_S)

        hdr = QLabel("Source file")
        hdr.setProperty("role", "sectionHeader")
        v.addWidget(hdr)

        # File picker row
        file_row = QHBoxLayout()
        self.ed_source_path = QLineEdit()
        self.ed_source_path.setReadOnly(True)
        self.ed_source_path.setPlaceholderText("Select vendor file…")
        file_row.addWidget(self.ed_source_path, stretch=1)
        self.btn_browse = QPushButton("Browse…")
        self.btn_browse.clicked.connect(self._on_browse_source)
        file_row.addWidget(self.btn_browse)
        v.addLayout(file_row)

        # Sheet + header-row row
        meta_row = QHBoxLayout()
        meta_row.addWidget(QLabel("Sheet:"))
        self.cb_sheet = QComboBox()
        self.cb_sheet.currentIndexChanged.connect(self._on_sheet_changed)
        meta_row.addWidget(self.cb_sheet, stretch=1)
        meta_row.addSpacing(theme.PAD_M)
        meta_row.addWidget(QLabel("Header row:"))
        self.sp_header = QSpinBox()
        self.sp_header.setRange(0, 50)
        self.sp_header.setValue(0)
        self.sp_header.valueChanged.connect(self._on_header_changed)
        meta_row.addWidget(self.sp_header)
        v.addLayout(meta_row)

        # Source columns list
        v.addWidget(QLabel("Source columns (drag onto canonical fields →):"))
        self.list_source = _SourceColumnList()
        self.list_source.itemClicked.connect(self._on_source_clicked)
        v.addWidget(self.list_source, stretch=1)

        return card

    # ----- Centre panel (value-norm) ----------------------------------------
    def _build_centre_panel(self) -> QWidget:
        card = QFrame()
        card.setProperty("role", "card")
        outer = QVBoxLayout(card)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.value_norm_panel = _ValueNormPanel()
        self.value_norm_panel.mapping_changed.connect(
            self._on_value_norm_changed
        )
        outer.addWidget(self.value_norm_panel)
        return card

    # ----- Right panel (canonical fields) -----------------------------------
    def _build_right_panel(self) -> QWidget:
        card = QFrame()
        card.setProperty("role", "card")
        v = QVBoxLayout(card)
        v.setContentsMargins(theme.PAD_M, theme.PAD_M, theme.PAD_M, theme.PAD_M)
        v.setSpacing(theme.PAD_S)

        hdr = QLabel("Canonical fields (NGP schema)")
        hdr.setProperty("role", "sectionHeader")
        v.addWidget(hdr)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        scroll.setWidget(inner)
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(0, 0, 0, 0)
        iv.setSpacing(theme.PAD_S)

        iv.addWidget(self._build_group("REQUIRED", REQUIRED_FIELDS, "required"))
        iv.addWidget(self._build_group("RECOMMENDED", RECOMMENDED_FIELDS, "recommended"))
        iv.addWidget(self._build_group("OPTIONAL", OPTIONAL_FIELDS, "optional"))

        # Pipe-registry sub-section. Sits at the bottom of the right
        # panel's scroll area, collapsed by default; auto-enabled on
        # file load when the workbook contains a plausible pipe sheet.
        self.pipe_section = _PipeRegistrySection()
        self.pipe_section.contents_changed.connect(self._on_pipe_changed)
        iv.addWidget(self.pipe_section)

        iv.addStretch(1)

        v.addWidget(scroll, stretch=1)
        return card

    def _build_group(
        self,
        title: str,
        fields: tuple[tuple[str, str], ...],
        importance: str,
    ) -> QGroupBox:
        gb = QGroupBox(title)
        gb.setCheckable(False)
        gb.setStyleSheet(
            f"QGroupBox {{ font-weight: 600; color: {theme.COLOR_TEXT_MUTED};"
            f" border: 1px solid {theme.COLOR_CARD_BORDER};"
            f" border-radius: {theme.RADIUS_S}px; margin-top: 8px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin;"
            f" left: 10px; padding: 0 4px; }}"
        )
        v = QVBoxLayout(gb)
        v.setContentsMargins(theme.PAD_S, theme.PAD_M, theme.PAD_S, theme.PAD_S)
        v.setSpacing(4)
        for canon, label in fields:
            row = _CanonicalFieldRow(canon, label, importance)
            row.mapping_changed.connect(self._on_field_mapping_changed)
            row.clicked.connect(self._on_field_clicked)
            self._field_rows[canon] = row
            v.addWidget(row)
        return gb

    # ----- Footer -----------------------------------------------------------
    def _build_footer(self) -> QWidget:
        wrap = QFrame()
        wrap.setProperty("role", "card")
        h = QHBoxLayout(wrap)
        h.setContentsMargins(theme.PAD_M, theme.PAD_S, theme.PAD_M, theme.PAD_S)
        h.setSpacing(theme.PAD_S)

        h.addWidget(QLabel("Profile name:"))
        self.ed_profile_name = QLineEdit()
        self.ed_profile_name.setPlaceholderText("e.g. rosen_2022")
        self.ed_profile_name.setMaximumWidth(220)
        h.addWidget(self.ed_profile_name)

        self.btn_load_profile = QPushButton("Load existing profile…")
        self.btn_load_profile.clicked.connect(self._on_load_profile)
        h.addWidget(self.btn_load_profile)

        self.btn_save_profile = QPushButton("Save profile")
        self.btn_save_profile.clicked.connect(self._on_save_profile)
        h.addWidget(self.btn_save_profile)

        h.addStretch(1)

        self.btn_preview = QPushButton("Preview…")
        self.btn_preview.clicked.connect(self._on_preview)
        h.addWidget(self.btn_preview)

        self.btn_export = QPushButton("Export to NGP format  →")
        self.btn_export.setProperty("role", "primary")
        self.btn_export.clicked.connect(self._on_export)
        self.btn_export.setEnabled(False)
        h.addWidget(self.btn_export)

        # "Use as Run-1" — shows up after a successful export.
        self.btn_use_run1 = QPushButton("Use as Run-1 in next project  →")
        self.btn_use_run1.setVisible(False)
        self.btn_use_run1.clicked.connect(self._on_use_as_run1)
        h.addWidget(self.btn_use_run1)

        return wrap

    # ============================================================ handlers

    def _on_browse_source(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select vendor file", "",
            "Excel / CSV (*.xlsx *.xls *.xlsm *.csv);;All files (*.*)",
        )
        if not path:
            return
        self._load_source(Path(path))

    def _load_source(self, path: Path) -> None:
        self._source_path = path
        self.ed_source_path.setText(str(path))
        # Read all sheets up-front so the sheet dropdown is instant.
        try:
            engine = _excel_engine_for(path)
            if path.suffix.lower() == ".csv":
                # Athena 2018 CSV exports use latin-1 (the ° in
                # "Latitude [°]" headers is 0xb0). The cascade helper
                # tries utf-8 / utf-8-sig / latin-1 / cp1252 / utf-16
                # in order so users don't have to manually convert.
                from ...io.format_converter.csv_input import (
                    read_csv_with_encoding_fallback,
                )
                self._all_sheets = {
                    "Sheet1": read_csv_with_encoding_fallback(path, header=None),
                }
            else:
                self._all_sheets = pd.read_excel(
                    path, sheet_name=None, header=None, engine=engine,
                )
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.critical(
                self, "Read failed", f"Couldn't read {path.name}:\n{e}",
            )
            return

        # Auto-detect (best sheet + header + mappings). We run
        # propose_profile() to inherit its value_normalizations + unit
        # defaults, but then OVERRIDE its sheet pick with the GUI-side
        # _pick_defect_sheet() heuristic — the latter has a stricter
        # skip list and was added specifically because propose_profile
        # picked "Reference Point Marker list" on Kandla workbooks.
        proposal = None
        try:
            proposal = propose_profile(path)
        except Exception as e:                                   # noqa: BLE001
            self.status_message.emit(
                f"Auto-detect failed: {type(e).__name__}: {e}"
            )

        chosen_sheet = _pick_defect_sheet(self._all_sheets)

        # Populate sheet dropdown — block signals while we fill.
        self.cb_sheet.blockSignals(True)
        self.cb_sheet.clear()
        self.cb_sheet.addItems(list(self._all_sheets.keys()))
        if chosen_sheet and chosen_sheet in self._all_sheets:
            self.cb_sheet.setCurrentText(chosen_sheet)
        elif proposal and proposal.profile.sheet_name in self._all_sheets:
            # Fallback 1: propose_profile's pick (it has its own
            # skip-list / synonym-hits scoring).
            self.cb_sheet.setCurrentText(proposal.profile.sheet_name)
        else:
            # Fallback 2: largest sheet by cell count. Last resort —
            # usually wrong for FFP workbooks but better than nothing.
            best = max(
                self._all_sheets.items(),
                key=lambda kv: kv[1].shape[0] * kv[1].shape[1],
                default=("", None),
            )
            if best[0]:
                self.cb_sheet.setCurrentText(best[0])
        self.cb_sheet.blockSignals(False)

        # Detect the header row for the chosen sheet. If our override
        # matches propose_profile's pick, we can reuse its header_row.
        # Otherwise scan the chosen sheet's first ~12 rows for the row
        # with the most canonical-field synonym hits.
        if (
            proposal
            and proposal.profile.sheet_name == self.cb_sheet.currentText()
        ):
            self.sp_header.setValue(int(proposal.profile.header_row))
        else:
            self.sp_header.setValue(
                _detect_header_row(self._all_sheets, self.cb_sheet.currentText())
            )

        # Pull the active dataframe with the chosen header row + apply
        # mappings derived from THAT sheet (not propose_profile's, which
        # might be from a different sheet).
        self._refresh_active_df()
        self._apply_auto_detected_mappings()

        # Carry over the units / value-normalisation defaults that
        # propose_profile assembled — they're sheet-independent.
        if proposal:
            self.ed_profile_name.setText(
                proposal.profile.vendor_name or path.stem
            )
            self._apply_units(proposal.profile.unit_conventions)
            self.value_norm_panel.load_mappings(
                proposal.profile.value_normalizations or {}
            )
        else:
            self.ed_profile_name.setText(path.stem)

        self._refresh_value_norm_data()

        # Hand the workbook to the pipe-registry section. It will
        # auto-pick a candidate sheet (if any) and auto-enable itself.
        self.pipe_section.set_workbook(
            self._all_sheets,
            self.cb_sheet.currentText(),
            default_header_row=int(self.sp_header.value()),
        )

        self._refresh_export_state()
        n_match = sum(1 for r in self._field_rows.values() if r.mapped_source())
        n_pipe = sum(1 for r in self.pipe_section._field_rows.values() if r.mapped_source())
        pipe_blurb = (
            f"; pipe sheet '{self.pipe_section.selected_sheet()}' "
            f"({n_pipe} field{'s' if n_pipe != 1 else ''} mapped)"
            if self.pipe_section.is_active() else ""
        )
        self.status_message.emit(
            f"Loaded {path.name}. Auto-detected {n_match} canonical field"
            f"{'s' if n_match != 1 else ''}{pipe_blurb}."
        )

    def _on_sheet_changed(self, _idx: int) -> None:
        # Switching sheets invalidates every defect-side mapping —
        # column names on the new sheet are very unlikely to match the
        # old sheet's. Without clearing, exports later fail with
        # "Profile references source columns that aren't in the file"
        # because the mappings still point at the previous sheet's
        # headers (this is exactly the bug that prompted Prompt 22).
        # Re-detect the header row + re-run auto-detect against the new
        # sheet's columns so the user gets fresh, valid suggestions.
        new_sheet = self.cb_sheet.currentText()
        if new_sheet and self._all_sheets:
            self.sp_header.blockSignals(True)
            self.sp_header.setValue(
                _detect_header_row(self._all_sheets, new_sheet)
            )
            self.sp_header.blockSignals(False)

        self._refresh_active_df()
        self._apply_auto_detected_mappings()
        self._refresh_value_norm_data()

        # Pipe-registry candidate list also needs to re-filter so the
        # defect sheet isn't offered as a pipe-tally candidate.
        if self._all_sheets:
            self.pipe_section.set_workbook(
                self._all_sheets,
                new_sheet,
                default_header_row=int(self.sp_header.value()),
            )
        self._refresh_export_state()
        n_match = sum(1 for r in self._field_rows.values() if r.mapped_source())
        self.status_message.emit(
            f"Sheet: {new_sheet} — re-detected {n_match} field"
            f"{'s' if n_match != 1 else ''}."
        )

    def _on_header_changed(self, _value: int) -> None:
        self._refresh_active_df()
        self._refresh_value_norm_data()

    def _on_source_clicked(self, _item: QListWidgetItem) -> None:
        # Future: could show full-column distribution. For now no-op.
        pass

    def _on_field_mapping_changed(self, canonical: str, source: str) -> None:
        # If the user just mapped a categorical field, refresh the
        # value-norm panel so the distinct-values list updates.
        self._refresh_value_norm_data()
        self._refresh_export_state()

    def _on_field_clicked(self, canonical: str) -> None:
        # Activate the value-norm panel when a categorical field is clicked.
        if canonical in CATEGORICAL_FIELDS:
            self.value_norm_panel.show_field(canonical)

    def _on_value_norm_changed(self, canonical: str, mapping: dict) -> None:
        # Mostly for status — the panel keeps its own state.
        if mapping:
            self.status_message.emit(
                f"{canonical}: {len(mapping)} value mapping(s) set."
            )

    def _on_pipe_changed(self) -> None:
        """Pipe-registry section toggled or its mappings edited."""
        self._refresh_export_state()

    # ============================================================ data refresh

    def _refresh_active_df(self) -> None:
        sheet = self.cb_sheet.currentText()
        header_row = self.sp_header.value()
        if not sheet or sheet not in self._all_sheets:
            self._source_df = None
            self.list_source.clear()
            return

        raw = self._all_sheets[sheet]
        if header_row >= len(raw):
            self._source_df = None
            self.list_source.clear()
            return

        # Promote the header row, generating unique placeholders for any
        # NaN / empty cells so we don't end up with duplicate column
        # names (which silently break the loop below).
        headers = _unique_column_names(raw.iloc[header_row].tolist())
        body = raw.iloc[header_row + 1:].copy()
        body.columns = headers
        body = body.dropna(axis=1, how="all").dropna(axis=0, how="all")
        body.reset_index(drop=True, inplace=True)
        self._source_df = body

        # Populate the source-column list with sample values.
        self.list_source.clear()
        for col in body.columns:
            sample_values = (
                body[col].dropna().astype(str).head(3).tolist()
            )
            label = self._format_source_item(col, sample_values, body[col])
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, str(col))
            self.list_source.addItem(item)

    def _format_source_item(
        self, col: str, samples: list[str], series: pd.Series,
    ) -> str:
        unit_hint = _detect_unit_hint(str(col), series)
        sample_txt = ", ".join(s[:25] for s in samples) if samples else "(empty)"
        if unit_hint:
            return f"{col}\n   samples: {sample_txt}   •   detected: {unit_hint}"
        return f"{col}\n   samples: {sample_txt}"

    def _refresh_value_norm_data(self) -> None:
        column_mappings = {
            canon: row.mapped_source()
            for canon, row in self._field_rows.items()
            if row.mapped_source()
        }
        self.value_norm_panel.set_source_data(self._source_df, column_mappings)

    def _refresh_export_state(self) -> None:
        # All REQUIRED except anomaly_id must be mapped. anomaly_id is
        # treated as "soft-required" — the converter auto-generates
        # sequential IDs (A-000001 …) when the source has no ID column,
        # which is the case for most older NGP/Athena files (Kandla
        # included). We still warn the user at export time.
        n_required_unmapped = sum(
            1 for canon, _ in REQUIRED_FIELDS
            if canon != "anomaly_id" and not self._field_rows[canon].mapped_source()
        )
        self.btn_export.setEnabled(
            n_required_unmapped == 0 and self._source_df is not None
        )

    # ============================================================ mappings

    def _apply_mappings(self, mappings: dict[str, str]) -> None:
        for canon, row in self._field_rows.items():
            row.set_mapping(mappings.get(canon, ""))

    def _stale_mappings(self) -> list[tuple[str, str, str]]:
        """Return any (canonical, source_column, where) tuples whose
        source column isn't in the corresponding sheet.

        ``where`` is "defect sheet" or "pipe sheet" so the error dialog
        can tell the user which side to fix.
        """
        stale: list[tuple[str, str, str]] = []

        # Defect-sheet mappings vs the current self._source_df
        if self._source_df is not None:
            defect_cols = set(self._source_df.columns)
            for canon, src in self._current_mappings().items():
                if src and src not in defect_cols:
                    stale.append((canon, src, "defect sheet"))

        # Pipe-sheet mappings vs the pipe section's own _source_df
        if self.pipe_section.is_active():
            pipe_df = self.pipe_section._source_df              # noqa: SLF001
            if pipe_df is not None:
                pipe_cols = set(pipe_df.columns)
                for canon, src in self.pipe_section.get_mappings().items():
                    if src and src not in pipe_cols:
                        stale.append((canon, src, "pipe sheet"))

        return stale

    def _apply_auto_detected_mappings(self) -> None:
        """Re-run the synonym engine against the current ``self._source_df``.

        Used on file load (after the defect-sheet picker has chosen a
        sheet) and on sheet-dropdown changes (after the user picks a
        different sheet). The previous mappings are cleared first so a
        stale mapping from another sheet can't leak through.
        """
        # Clear every canonical drop target.
        for row in self._field_rows.values():
            row.set_mapping("")
        if self._source_df is None or self._source_df.empty:
            return
        from ...io.format_converter.auto_detect import (
            propose_mappings_for_dataframe,
        )
        try:
            mappings = propose_mappings_for_dataframe(self._source_df)
        except Exception:                                        # noqa: BLE001
            return
        self._apply_mappings(mappings)

    def _apply_units(self, units: dict[str, str]) -> None:
        for canon, (unit_key, _options, default) in UNIT_CHOICES.items():
            row = self._field_rows.get(canon)
            if row is None or row.unit_combo is None:
                continue
            row.set_unit(units.get(unit_key, default))

    def _current_mappings(self) -> dict[str, str]:
        return {
            canon: row.mapped_source()
            for canon, row in self._field_rows.items()
            if row.mapped_source()
        }

    def _current_units(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for canon, (unit_key, _options, default) in UNIT_CHOICES.items():
            row = self._field_rows.get(canon)
            if row is None or row.unit_combo is None:
                continue
            value = row.selected_unit() or default
            out[unit_key] = value
        return out

    def _build_profile(self) -> VendorProfile:
        profile = VendorProfile(
            vendor_name=(
                self.ed_profile_name.text().strip()
                or "(unnamed profile)"
            ),
            sheet_name=self.cb_sheet.currentText() or None,
            header_row=int(self.sp_header.value()),
            column_mappings=self._current_mappings(),
            unit_conventions=self._current_units(),
            value_normalizations=self.value_norm_panel.get_mappings(),
            notes="Saved from the FFP Tool GUI.",
        )
        # Carry the pipe-registry sub-section through. Only populated
        # when the user has the section toggled on AND the REQUIRED
        # pipe fields (joint_number, joint_length_m) are mapped —
        # otherwise a partial pipe sheet would just confuse the reader.
        if (
            self.pipe_section.is_active()
            and not self.pipe_section.required_unmapped()
        ):
            pipe_maps = self.pipe_section.get_mappings()
            if pipe_maps:
                profile.pipe_sheet_name = self.pipe_section.selected_sheet()
                profile.pipe_header_row = self.pipe_section.selected_header_row()
                profile.pipe_column_mappings = pipe_maps
        return profile

    # ============================================================ profile I/O

    def _on_save_profile(self) -> None:
        profile = self._build_profile()
        problems = profile.validate()
        if problems:
            reply = QMessageBox.question(
                self, "Profile has problems",
                "The profile has these issues — save anyway?\n\n• "
                + "\n• ".join(problems),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        name = self.ed_profile_name.text().strip() or profile.vendor_name
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name).strip("_")
        if not safe:
            safe = "vendor_profile"
        out_dir = ensure_dir(user_vendor_profiles_dir())
        target = out_dir / f"{safe}.json"

        if target.exists():
            reply = QMessageBox.question(
                self, "Overwrite?",
                f"{target.name} already exists. Overwrite?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        try:
            profile.save_to_json(target)
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.critical(self, "Save failed", str(e))
            return
        self.status_message.emit(f"Saved profile → {target}")
        QMessageBox.information(
            self, "Profile saved",
            f"Saved to:\n{target}",
        )

    def _on_load_profile(self) -> None:
        # Build a name → path map: bundled + user.
        catalog: dict[str, Path] = {}
        for p in sorted(bundled_vendor_profiles_dir().glob("*.json")):
            catalog[f"(bundled) {p.stem}"] = p
        user_dir = user_vendor_profiles_dir()
        if user_dir.exists():
            for p in sorted(user_dir.glob("*.json")):
                catalog[p.stem] = p

        if not catalog:
            QMessageBox.information(
                self, "No profiles", "No vendor profiles found yet.",
            )
            return

        choice, ok = QInputDialog.getItem(
            self, "Load vendor profile",
            "Pick a profile:",
            list(catalog.keys()),
            0, False,
        )
        if not ok or not choice:
            return
        try:
            profile = VendorProfile.load_from_json(catalog[choice])
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.critical(self, "Load failed", str(e))
            return

        self.ed_profile_name.setText(profile.vendor_name or choice)
        if profile.sheet_name and profile.sheet_name in self._all_sheets:
            self.cb_sheet.setCurrentText(profile.sheet_name)
        self.sp_header.setValue(int(profile.header_row))
        self._refresh_active_df()
        self._apply_mappings(profile.normalised_mappings())
        self._apply_units(profile.unit_conventions)
        self.value_norm_panel.load_mappings(profile.value_normalizations or {})
        self._refresh_value_norm_data()
        # Repopulate the pipe-registry section *after* the defect sheet
        # is picked, so the candidate list excludes the right name.
        if self._all_sheets:
            self.pipe_section.set_workbook(
                self._all_sheets,
                self.cb_sheet.currentText(),
                default_header_row=int(self.sp_header.value()),
            )
        self.pipe_section.load_from_profile(profile)
        self._refresh_export_state()
        self.status_message.emit(f"Loaded profile: {choice}")

    # ============================================================ preview

    def _on_preview(self) -> None:
        if not self._source_path:
            QMessageBox.warning(self, "No file", "Load a vendor file first.")
            return
        profile = self._build_profile()
        problems = profile.validate()
        # Preview is allowed even with required-field gaps so the user
        # can iterate; but warn so they know what they're looking at.
        if problems:
            self.status_message.emit(
                "Preview: profile has unresolved problems, results partial."
            )
        try:
            conv = FormatConverter(profile)
            df = conv.transform(self._source_df.head(10).copy())
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.critical(
                self, "Preview failed",
                f"{type(e).__name__}: {e}",
            )
            return
        dlg = _PreviewDialog(df, parent=self)
        dlg.exec()

    # ============================================================ export

    def _on_export(self) -> None:
        if not self._source_path:
            QMessageBox.warning(self, "No file", "Load a vendor file first.")
            return

        # Stale-mapping check: before we run the converter, prove every
        # mapped source column actually exists on its target sheet.
        # Mappings can go stale when the user switches the sheet
        # dropdown after auto-detect populated mappings from another
        # sheet — without this guard the user sees a cryptic KeyError
        # from the converter ("Profile references source columns that
        # aren't in the file…"). Surface a clear, actionable message
        # instead.
        stale = self._stale_mappings()
        if stale:
            lines = [
                f"  • {canon} → '{src}'  ({where})"
                for canon, src, where in stale
            ]
            QMessageBox.critical(
                self, "Stale mappings",
                "These mapped columns are no longer in the current sheet:\n\n"
                + "\n".join(lines)
                + "\n\nDid you change the sheet selection? Please re-map "
                "or switch back to the original sheet."
            )
            return

        profile = self._build_profile()
        problems = profile.validate()
        critical = [p for p in problems if "Missing required" in p]
        if critical:
            QMessageBox.warning(self, "Cannot export", "\n".join(critical))
            return

        # Pipe-registry sanity check: if the section is enabled but
        # joint_number / joint_length_m aren't mapped, the pipe sheet
        # won't be written — warn the user up front so they don't get
        # mystified by sparse joint alignment in the analysis run.
        if (
            self.pipe_section.is_active()
            and self.pipe_section.required_unmapped()
        ):
            missing = ", ".join(self.pipe_section.required_unmapped())
            reply = QMessageBox.question(
                self, "Pipe registry incomplete",
                f"The pipe-registry section is enabled but these required "
                f"pipe fields aren't mapped: {missing}.\n\n"
                "The converter will skip writing the pipe sheet — "
                "joint alignment in the downstream FFP analysis will be "
                "based on joints with defects only, which usually drifts "
                "CGR by 10%+ on real pipelines.\n\n"
                "Export defects only?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # If anomaly_id wasn't mapped, the converter will auto-generate
        # IDs. Tell the user up front so they're not surprised.
        if not self._field_rows["anomaly_id"].mapped_source():
            reply = QMessageBox.question(
                self, "No Anomaly ID column",
                "Your file doesn't have an Anomaly/Feature ID column. "
                "The converter will auto-generate sequential IDs "
                "(A-000001, A-000002, …) in the output. "
                "These match nothing across runs, so cross-run defect "
                "matching will rely on geometry alone.\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # Warn on recommended-field gaps.
        recommended_missing = [
            label for canon, label in RECOMMENDED_FIELDS
            if not self._field_rows[canon].mapped_source()
        ]
        if recommended_missing:
            reply = QMessageBox.question(
                self, "Some recommended fields missing",
                "These recommended fields aren't mapped:\n\n• "
                + "\n• ".join(recommended_missing)
                + "\n\nSome FFP-analysis features may be limited. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        out_path = self._source_path.with_name(
            f"{self._source_path.stem}_NGP.xlsx"
        )
        # Pass the GUI's already-read DataFrames into convert() so the
        # converter doesn't re-read the source file. Two benefits:
        #   1. Avoids the Prompt 34 bug where convert() called
        #      pd.read_excel() on a .csv path, raising "Excel file
        #      format cannot be determined".
        #   2. ~halves the wall-clock for large files.
        # The pipe section caches its own DataFrame; we hand that
        # through too when the section is active.
        pipe_df_cached = (
            self.pipe_section._source_df                          # noqa: SLF001
            if self.pipe_section.is_active() else None
        )
        try:
            FormatConverter(profile).convert(
                self._source_path, out_path,
                source_df=self._source_df,
                pipe_df=pipe_df_cached,
            )
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.critical(
                self, "Export failed",
                f"{type(e).__name__}: {e}",
            )
            return

        self._last_export_path = out_path
        self.btn_use_run1.setVisible(True)
        self.export_complete.emit(str(out_path))
        self.status_message.emit(f"Exported → {out_path}")

        # Success dialog with "Open file" action.
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setWindowTitle("Export complete")
        msg.setText(
            f"Wrote NGP-format file:\n{out_path}\n\n"
            "You can now use this as Run-1 in the project setup screen, "
            "or open the file to inspect it."
        )
        btn_open = msg.addButton("Open file", QMessageBox.ButtonRole.ActionRole)
        btn_use = msg.addButton("Use as Run-1", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton(QMessageBox.StandardButton.Close)
        msg.exec()
        if msg.clickedButton() == btn_open:
            _open_with_default(out_path)
        elif msg.clickedButton() == btn_use:
            self._on_use_as_run1()

    def _on_use_as_run1(self) -> None:
        if self._last_export_path is None:
            return
        self.use_as_run1.emit(str(self._last_export_path))


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

_UNIT_HINT_PATTERNS = (
    (r"\bkm\b",                                 "chainage in km"),
    (r"\b\[?m\]?\b(?!m)",                       "metres"),
    (r"\bft\b|feet",                            "feet"),
    (r"\bmm\b",                                 "mm"),
    (r"\bcm\b",                                 "cm"),
    (r"%\s*w?t",                                "% WT"),
    (r"\bdeg(?:rees)?\b|°",                     "degrees"),
    (r"\brad(?:ians)?\b",                       "radians"),
    (r"\bhh:?mm\b|h:min|o'?clock",              "hh:mm"),
)


def _detect_header_row(
    all_sheets: dict, sheet_name: str, max_scan: int = 12,
) -> int:
    """Scan the first ``max_scan`` rows of a sheet for the header row.

    The header row is the one whose cells normalise into the most
    column_synonyms.yaml synonym entries — same heuristic auto_detect.py
    uses, but applied to one specific sheet rather than picking sheet
    AND row at once. Falls back to ``0`` if no row scores meaningfully.
    """
    if sheet_name not in all_sheets:
        return 0
    raw = all_sheets[sheet_name]
    if raw is None or len(raw) == 0:
        return 0

    # Import lazily to avoid loading yaml at GUI startup time.
    from ...io.format_converter.auto_detect import (
        _load_synonyms, _score_header_row,
    )
    synonyms = _load_synonyms()

    best_hits = -1
    best_row = 0
    scan = min(max_scan, len(raw))
    for r in range(scan):
        row_vals = raw.iloc[r].tolist()
        hits, _mapping = _score_header_row(list(row_vals), synonyms)
        if hits > best_hits:
            best_hits = hits
            best_row = r
    return best_row


def _unique_column_names(raw_values: list) -> list[str]:
    """Build a list of unique column names from a raw header row.

    Replaces NaN / blank cells with ``col_{i}`` placeholders, and
    de-duplicates anything else by appending ``__2``, ``__3``, … so
    downstream pandas operations don't accidentally turn ``df[name]``
    into a multi-column DataFrame.
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for i, v in enumerate(raw_values):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            name = f"col_{i}"
        else:
            name = str(v).strip() or f"col_{i}"
        if name in seen:
            seen[name] += 1
            name = f"{name}__{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


def _detect_unit_hint(header: str, series: pd.Series) -> str:
    """Sniff unit info from the header text (and a peek at the data)."""
    import re
    h = header.lower()
    for pattern, label in _UNIT_HINT_PATTERNS:
        if re.search(pattern, h):
            return label
    # Numeric-range heuristic for chainage: values up to tens of km in
    # metres typically run into the thousands.
    try:
        sample = pd.to_numeric(series.dropna().head(20), errors="coerce")
        sample = sample.dropna()
        if len(sample) >= 3:
            mx = float(sample.max())
            if "distance" in h or "chainage" in h:
                if mx < 200:
                    return "likely km"
                if mx > 500:
                    return "likely m"
    except Exception:                                            # noqa: BLE001
        pass
    return ""


def _open_with_default(path: Path) -> None:
    """Open file or folder with the OS-default application."""
    import os
    import platform
    import subprocess

    try:
        if platform.system() == "Windows":
            os.startfile(str(path))                              # noqa: S606
        elif platform.system() == "Darwin":
            subprocess.run(["open", str(path)], check=False)     # noqa: S603, S607
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:                                            # noqa: BLE001
        pass
