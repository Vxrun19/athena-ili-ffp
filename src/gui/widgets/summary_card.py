"""KPI / summary card widget used on the Results screen.

Each card renders a title, a big value, an optional sub-label, and an
optional accent colour stripe along the left edge. They're stacked in a
grid layout to communicate the highlights of an analysis at a glance.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .. import theme


class SummaryCard(QFrame):
    """A simple KPI tile: title at top, big value, optional subtitle."""

    def __init__(
        self,
        title: str,
        value: str = "—",
        subtitle: str = "",
        accent: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._accent = accent or theme.COLOR_PRIMARY
        self._apply_accent_style()

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Left accent stripe — drawn as a thin sibling widget so the card
        # itself can keep a normal border-radius.
        self._stripe = QWidget()
        self._stripe.setFixedWidth(4)
        self._set_stripe_colour(self._accent)
        outer.addWidget(self._stripe)

        body = QVBoxLayout()
        body.setContentsMargins(theme.PAD_M, theme.PAD_M, theme.PAD_M, theme.PAD_M)
        body.setSpacing(4)

        self._title = QLabel(title)
        self._title.setStyleSheet(
            f"color: {theme.COLOR_TEXT_MUTED}; font-size: 11px;"
            f" font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;"
        )

        self._value = QLabel(value)
        self._value.setStyleSheet(
            f"color: {theme.COLOR_TEXT}; font-size: 28px; font-weight: 700;"
        )

        self._subtitle = QLabel(subtitle)
        self._subtitle.setStyleSheet(
            f"color: {theme.COLOR_TEXT_MUTED}; font-size: 11px;"
        )
        self._subtitle.setVisible(bool(subtitle))
        self._subtitle.setWordWrap(True)

        body.addWidget(self._title)
        body.addWidget(self._value)
        body.addWidget(self._subtitle)
        body.addStretch(1)

        body_holder = QWidget()
        body_holder.setLayout(body)
        outer.addWidget(body_holder, stretch=1)

        self.setMinimumHeight(110)

    # ------------------------------------------------------------------ helpers
    def _apply_accent_style(self) -> None:
        # The card itself keeps the global card style; only the stripe colour
        # changes per-instance, so we don't need to override stylesheet here.
        pass

    def _set_stripe_colour(self, colour: str) -> None:
        self._stripe.setStyleSheet(
            f"background-color: {colour};"
            f" border-top-left-radius: {theme.RADIUS_M}px;"
            f" border-bottom-left-radius: {theme.RADIUS_M}px;"
        )

    # ------------------------------------------------------------------ public
    def set_value(self, value: str) -> None:
        self._value.setText(value)

    def set_subtitle(self, text: str) -> None:
        self._subtitle.setText(text)
        self._subtitle.setVisible(bool(text))

    def set_accent(self, colour: str) -> None:
        self._accent = colour
        self._set_stripe_colour(colour)

    def set_accent_by_severity(self, *, critical: int, errors: int, warnings: int) -> None:
        """Pick a stripe colour from QA severity counts."""
        if critical or errors:
            self.set_accent(theme.COLOR_ERROR)
        elif warnings:
            self.set_accent(theme.COLOR_WARNING)
        else:
            self.set_accent(theme.COLOR_SUCCESS)

    # Mirror the colour helper as plain functions for callers that don't have
    # a SummaryCard instance handy.
    @staticmethod
    def severity_colour(*, critical: int, errors: int, warnings: int) -> str:
        if critical or errors:
            return theme.COLOR_ERROR
        if warnings:
            return theme.COLOR_WARNING
        return theme.COLOR_SUCCESS

    @staticmethod
    def threshold_colour(count: int) -> str:
        """Red when any features cross a hard ERF/depth threshold, else green."""
        return theme.COLOR_ERROR if count > 0 else theme.COLOR_SUCCESS

    @staticmethod
    def darken(hex_color: str, factor: float = 0.85) -> str:
        c = QColor(hex_color)
        c = c.darker(int(100 / factor))
        return c.name()
