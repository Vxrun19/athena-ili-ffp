"""Matplotlib FigureCanvas wrapper for embedding charts in the Results screen.

We use the same Agg-style ``FigureCanvasQTAgg`` backend that ``main_report_writer``
relies on, so the report charts and the on-screen charts come from the same
plotting code paths (just rendered to different surfaces).
"""
from __future__ import annotations

from typing import Iterable

import matplotlib

# Ensure the QtAgg backend is selected before importing Figure-related modules.
matplotlib.use("QtAgg")

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg  # noqa: E402
from matplotlib.figure import Figure                              # noqa: E402

from .. import theme                                              # noqa: E402


class ChartCanvas(FigureCanvasQTAgg):
    """A self-contained matplotlib canvas for one chart.

    Use :meth:`plot_histogram`, :meth:`plot_scatter`, etc. to populate it,
    then :meth:`refresh` to redraw. The figure background matches the GUI
    card background so the chart looks at home inside a QFrame.
    """

    def __init__(
        self,
        *,
        width: float = 5.0,
        height: float = 3.0,
        dpi: int = 100,
        parent=None,
    ) -> None:
        fig = Figure(figsize=(width, height), dpi=dpi)
        fig.patch.set_facecolor(theme.COLOR_CARD_BG)
        super().__init__(fig)
        self.setParent(parent)
        self._ax = fig.add_subplot(111)
        self._ax.set_facecolor(theme.COLOR_CARD_BG)
        self._style_axes()

    # --------------------------------------------------------------- private
    def _style_axes(self) -> None:
        ax = self._ax
        for spine_name in ("top", "right"):
            ax.spines[spine_name].set_visible(False)
        for spine_name in ("left", "bottom"):
            ax.spines[spine_name].set_color(theme.COLOR_CARD_BORDER)
        ax.tick_params(colors=theme.COLOR_TEXT_MUTED, labelsize=9)
        ax.title.set_color(theme.COLOR_TEXT)
        ax.title.set_fontsize(12)
        ax.xaxis.label.set_color(theme.COLOR_TEXT_MUTED)
        ax.yaxis.label.set_color(theme.COLOR_TEXT_MUTED)
        ax.xaxis.label.set_fontsize(10)
        ax.yaxis.label.set_fontsize(10)
        ax.grid(True, color=theme.COLOR_CARD_BORDER, alpha=0.5, linestyle="--", linewidth=0.6)
        ax.set_axisbelow(True)

    # ---------------------------------------------------------------- public
    def clear(self) -> None:
        self._ax.clear()
        self._style_axes()

    def refresh(self) -> None:
        self.figure.tight_layout()
        self.draw_idle()

    # Plotting helpers -----------------------------------------------------
    def plot_histogram(
        self,
        values: Iterable[float],
        *,
        bins: int = 20,
        title: str = "",
        xlabel: str = "",
        ylabel: str = "Count",
        color: str | None = None,
    ) -> None:
        self.clear()
        vals = [v for v in values if v is not None]
        if vals:
            self._ax.hist(vals, bins=bins, color=color or theme.COLOR_PRIMARY,
                          alpha=0.85, edgecolor="white", linewidth=0.6)
        else:
            self._ax.text(0.5, 0.5, "No data", ha="center", va="center",
                          transform=self._ax.transAxes, color=theme.COLOR_TEXT_MUTED)
        self._ax.set_title(title)
        self._ax.set_xlabel(xlabel)
        self._ax.set_ylabel(ylabel)
        self.refresh()

    def plot_scatter(
        self,
        xs: Iterable[float],
        ys: Iterable[float],
        *,
        title: str = "",
        xlabel: str = "",
        ylabel: str = "",
        color: str | None = None,
        hline: float | None = None,
        hline_label: str = "",
    ) -> None:
        self.clear()
        xs_l, ys_l = list(xs), list(ys)
        if xs_l and ys_l:
            self._ax.scatter(
                xs_l, ys_l,
                s=20, alpha=0.6,
                color=color or theme.COLOR_PRIMARY,
                edgecolor="white", linewidth=0.4,
            )
        else:
            self._ax.text(0.5, 0.5, "No data", ha="center", va="center",
                          transform=self._ax.transAxes, color=theme.COLOR_TEXT_MUTED)
        if hline is not None:
            self._ax.axhline(
                y=hline, color=theme.COLOR_ERROR, linestyle="--", linewidth=1.2,
                label=hline_label or None,
            )
            if hline_label:
                self._ax.legend(frameon=False, fontsize=9)
        self._ax.set_title(title)
        self._ax.set_xlabel(xlabel)
        self._ax.set_ylabel(ylabel)
        self.refresh()

    def plot_bar(
        self,
        labels: Iterable[str],
        values: Iterable[float],
        *,
        title: str = "",
        xlabel: str = "",
        ylabel: str = "",
        color: str | None = None,
    ) -> None:
        self.clear()
        labels_l, values_l = list(labels), list(values)
        if labels_l and values_l:
            self._ax.bar(
                labels_l, values_l,
                color=color or theme.COLOR_PRIMARY,
                edgecolor="white",
                linewidth=0.6,
            )
            for label in self._ax.get_xticklabels():
                label.set_rotation(0)
        else:
            self._ax.text(0.5, 0.5, "No data", ha="center", va="center",
                          transform=self._ax.transAxes, color=theme.COLOR_TEXT_MUTED)
        self._ax.set_title(title)
        self._ax.set_xlabel(xlabel)
        self._ax.set_ylabel(ylabel)
        self.refresh()

    def message(self, text: str) -> None:
        """Render a placeholder message instead of a chart (no data, errors, etc)."""
        self.clear()
        self._ax.text(0.5, 0.5, text, ha="center", va="center",
                      transform=self._ax.transAxes, color=theme.COLOR_TEXT_MUTED)
        self._ax.set_xticks([])
        self._ax.set_yticks([])
        for spine in self._ax.spines.values():
            spine.set_visible(False)
        self.refresh()
