"""Programmatically-drawn QIcon for the Athena ILI FFP Tool.

We don't ship a .ico file in v0.1.0 — the icon is rendered on demand at
several resolutions (16/32/48/256) so it stays crisp on the Windows
taskbar, the title bar, and the alt-tab switcher. The design is a
pipe cross-section with a corrosion wedge: simple, on-brand, and
recognisable at 16x16.
"""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QRadialGradient,
)


_BRAND_BLUE_DARK = QColor("#1F2D3D")
_BRAND_BLUE = QColor("#2C7BE5")
_BRAND_BLUE_LIGHT = QColor("#5DADE2")
_STEEL_OUTER = QColor("#B0BEC5")
_STEEL_INNER = QColor("#37474F")
_DEFECT = QColor("#E74C3C")
_DEFECT_DARK = QColor("#922B21")


def _draw_icon(size: int) -> QPixmap:
    """Render the app icon at the requested pixel size."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)

    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # ---- Background: rounded square in brand gradient
    bg_grad = QLinearGradient(0, 0, size, size)
    bg_grad.setColorAt(0.0, _BRAND_BLUE)
    bg_grad.setColorAt(1.0, _BRAND_BLUE_DARK)
    p.setBrush(QBrush(bg_grad))
    p.setPen(Qt.PenStyle.NoPen)
    radius = max(2, size * 0.18)
    p.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    # ---- Pipe outer ring (steel)
    margin = size * 0.18
    outer_rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    outer_grad = QRadialGradient(
        outer_rect.center(),
        outer_rect.width() * 0.55,
    )
    outer_grad.setColorAt(0.0, _STEEL_OUTER.lighter(120))
    outer_grad.setColorAt(1.0, _STEEL_OUTER.darker(140))
    p.setBrush(QBrush(outer_grad))
    p.setPen(QPen(_BRAND_BLUE_DARK, max(1.0, size * 0.015)))
    p.drawEllipse(outer_rect)

    # ---- Inner bore (dark)
    bore_margin = size * 0.30
    bore_rect = QRectF(
        bore_margin, bore_margin, size - 2 * bore_margin, size - 2 * bore_margin
    )
    bore_grad = QRadialGradient(bore_rect.center(), bore_rect.width() * 0.55)
    bore_grad.setColorAt(0.0, _STEEL_INNER)
    bore_grad.setColorAt(1.0, _STEEL_INNER.darker(180))
    p.setBrush(QBrush(bore_grad))
    p.setPen(QPen(_BRAND_BLUE_DARK, max(1.0, size * 0.012)))
    p.drawEllipse(bore_rect)

    # ---- Corrosion wedge (red) — only drawn at >=24 px (too noisy below)
    if size >= 24:
        wedge_grad = QLinearGradient(
            QPointF(size * 0.55, size * 0.22),
            QPointF(size * 0.55, size * 0.50),
        )
        wedge_grad.setColorAt(0.0, _DEFECT)
        wedge_grad.setColorAt(1.0, _DEFECT_DARK)
        p.setBrush(QBrush(wedge_grad))
        p.setPen(Qt.PenStyle.NoPen)
        # A pie slice centred on the top of the bore, sweeping ~60° clockwise.
        # Qt angles are in 16ths of a degree, measured counter-clockwise from 3-o'clock.
        p.drawPie(outer_rect, 75 * 16, -55 * 16)

    p.end()
    return pm


def build_app_icon() -> QIcon:
    """Return a multi-resolution QIcon for the application."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_draw_icon(size))
    return icon
