"""Multi-select annexure-topics panel (v0.2.5).

Replaces the v0.2.0–v0.2.4 "annexure format" QComboBox (E_F / B_C_D)
with a checkbox-per-topic widget plus a per-topic letter override.
Selection round-trips through the project YAML's ``report.annexures``
block, parsed and validated by :func:`src.models.parse_report_annexures`.

Surface API:

    panel.selection() -> list[tuple[str, str]]
        Currently-checked topics, in their canonical display order,
        each with the user's (possibly overridden) letter. Returns []
        if nothing is checked — caller enforces "≥ 1 selected" as a
        UX constraint.

    panel.set_selection(selection)
        Load a saved selection. Topics not in `selection` are
        unchecked. Topics IN `selection` get their checkbox set and
        the provided letter wins over the registry default.

    panel.selection_changed (signal)
        Emitted on any change (check, uncheck, letter edit).

    panel.is_valid() -> bool
        True iff at least one topic is checked AND no duplicate
        letters across checked topics.

    panel.validity_message() -> str
        Empty string when valid; else a human-readable diagnostic
        for the Save/Proceed buttons to surface.
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QIntValidator
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from src.reports.topic_registry import (
    AnnexureTopic,
    all_topics_in_order,
)


_MAX_LETTER_LEN = 3            # spec: "1-3 chars"
_LETTER_FIELD_PX = 60          # narrow input next to each topic
_PLACEHOLDER_BADGE = " ⓘ"      # tail badge on unimplemented topics
_LETTER_OK_STYLE = ""          # default Qt — no inline style
_LETTER_BAD_STYLE = (
    "background-color: #FFE0E0; border: 1px solid #C00000;"
)


class _TopicRow:
    """Internal record for one checkbox+label+letter triple."""

    def __init__(self, topic: AnnexureTopic):
        self.topic = topic
        self.checkbox = QCheckBox(
            topic.display_name + (_PLACEHOLDER_BADGE if not topic.implemented else "")
        )
        if not topic.implemented:
            self.checkbox.setToolTip(
                "Placeholder in v0.2.5. Selecting this produces a sheet "
                "listing dent features identified in Run-2 with a note "
                "that full strain computation per ASME B31.8 is a future "
                "addition."
            )
        self.letter = QLineEdit(topic.default_letter)
        self.letter.setMaxLength(_MAX_LETTER_LEN)
        self.letter.setFixedWidth(_LETTER_FIELD_PX)
        self.letter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # User-overridden value (sticks across check/uncheck). None
        # means "no override; use registry default on (re-)check".
        self._user_letter: str | None = None


class AnnexureTopicsPanel(QFrame):
    """Multi-select annexure-topic panel."""

    selection_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        self._rows: list[_TopicRow] = []
        self._build()
        # Default: legacy 3-topic preset checked. The screen calls
        # set_selection() with the real value when a YAML loads.
        from src.reports.topic_registry import default_annexure_selection
        self.set_selection(default_annexure_selection())

    # ----------------------------------------------------------------
    # UI build
    # ----------------------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        header = QLabel("Annexures to include in the report")
        header.setProperty("role", "sectionHeader")
        outer.addWidget(header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        outer.addLayout(grid)

        for i, topic in enumerate(all_topics_in_order()):
            row = _TopicRow(topic)
            grid.addWidget(row.checkbox, i, 0)
            grid.addWidget(row.letter, i, 1)
            # Stretch column 0 so the letter field sits flush right.
            row.checkbox.stateChanged.connect(self._on_checkbox_toggled)
            row.letter.textEdited.connect(self._on_letter_edited)
            self._rows.append(row)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 0)

        # Footer note about the placeholder topic. Always visible —
        # users selecting it should know what they're getting.
        note = QLabel(
            "ⓘ Dent strain analysis is a placeholder in v0.2.5. "
            "Selecting it produces a sheet listing dent features "
            "identified in Run-2 with a note that full strain "
            "computation is a future addition."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #555; font-size: 11px; padding-top: 6px;")
        outer.addWidget(note)

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    def selection(self) -> list[tuple[str, str]]:
        """Return [(topic_id, letter), ...] for currently-checked topics."""
        out: list[tuple[str, str]] = []
        for row in self._rows:
            if row.checkbox.isChecked():
                out.append((row.topic.id, row.letter.text().strip()
                            or row.topic.default_letter))
        return out

    def set_selection(self, selection: list[tuple[str, str]]) -> None:
        """Load a saved selection, applying letter overrides as given.

        Topics absent from `selection` are unchecked and their letter
        field reset to the registry default. Topics in `selection`
        retain their saved letter even if it differs from the
        registry's default_letter.

        Order in `selection` does NOT reorder rows (rows are fixed in
        registry order). The persisted YAML order is preserved by
        :meth:`selection`, which walks rows in display order and only
        emits checked ones — combined with the YAML save logic in
        project_setup.py, that's enough to round-trip.

        Note: the v0.2.5 spec keeps row order = registry order. If
        future versions want user-reorderable rows, this widget will
        need a drag handle and a separate `display_order` list.
        """
        by_id = {tid: letter for tid, letter in selection}
        # Block signal emission during bulk update.
        self.blockSignals(True)
        for row in self._rows:
            if row.topic.id in by_id:
                row.checkbox.setChecked(True)
                row.letter.setText(by_id[row.topic.id])
                row._user_letter = by_id[row.topic.id]
                row.letter.setEnabled(True)
            else:
                row.checkbox.setChecked(False)
                row.letter.setText(row.topic.default_letter)
                row._user_letter = None
                row.letter.setEnabled(False)
        self.blockSignals(False)
        self._validate_letters()
        self.selection_changed.emit()

    def is_valid(self) -> bool:
        """True iff ≥ 1 checked AND no duplicate letters across checked."""
        return not self.validity_message()

    def validity_message(self) -> str:
        """Empty when valid; else a single human-readable diagnostic."""
        sel = self.selection()
        if not sel:
            return "Select at least one annexure to include in the report."
        seen: dict[str, str] = {}
        for tid, letter in sel:
            if letter in seen:
                return (
                    f"Letter {letter!r} is used by both "
                    f"{seen[letter]!r} and {tid!r}. Each topic needs "
                    f"a unique letter."
                )
            seen[letter] = tid
        return ""

    # ----------------------------------------------------------------
    # Handlers
    # ----------------------------------------------------------------

    def _on_checkbox_toggled(self) -> None:
        for row in self._rows:
            row.letter.setEnabled(row.checkbox.isChecked())
            # When re-checking after an uncheck, restore the user's
            # last override (if any) — otherwise reset to default.
            if row.checkbox.isChecked() and not row.letter.text().strip():
                row.letter.setText(
                    row._user_letter or row.topic.default_letter
                )
        self._validate_letters()
        self.selection_changed.emit()

    def _on_letter_edited(self, _text: str) -> None:
        # Capture the user's override so a future uncheck+recheck
        # cycle restores it (the spec says "if user changes a letter,
        # that override persists").
        for row in self._rows:
            if row.checkbox.isChecked():
                row._user_letter = row.letter.text().strip() or None
        self._validate_letters()
        self.selection_changed.emit()

    def _validate_letters(self) -> None:
        """Highlight duplicate-letter fields red; clear otherwise."""
        sel = self.selection()
        # Tally letters across checked rows.
        counts: dict[str, int] = {}
        for _tid, letter in sel:
            counts[letter] = counts.get(letter, 0) + 1
        for row in self._rows:
            if not row.checkbox.isChecked():
                row.letter.setStyleSheet(_LETTER_OK_STYLE)
                row.letter.setToolTip("")
                continue
            letter = row.letter.text().strip() or row.topic.default_letter
            if counts.get(letter, 0) > 1:
                row.letter.setStyleSheet(_LETTER_BAD_STYLE)
                others = [
                    other_t for other_t, other_l in sel
                    if other_l == letter and other_t != row.topic.id
                ]
                row.letter.setToolTip(
                    f"Letter {letter!r} is already used by " +
                    ", ".join(others)
                )
            else:
                row.letter.setStyleSheet(_LETTER_OK_STYLE)
                row.letter.setToolTip("")


__all__ = ["AnnexureTopicsPanel"]
