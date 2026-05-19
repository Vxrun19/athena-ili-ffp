"""Per-screen widgets for the FFP Tool GUI."""
from __future__ import annotations

from .format_converter import FormatConverterScreen
from .project_setup import ProjectSetupScreen
from .run_analysis import RunAnalysisScreen
from .results import ResultsScreen
from .output import OutputScreen

__all__ = [
    "FormatConverterScreen",
    "ProjectSetupScreen",
    "RunAnalysisScreen",
    "ResultsScreen",
    "OutputScreen",
]
