"""Tests for the Project Setup auto-fill overwrite-confirm dialog.

When the user clicks "Auto-fill from Final Report PDF…" while the form
already has data loaded, the GUI must:

  * Detect that the form has user data (any populated metadata field
    or any MAOP zone row).
  * Show a confirmation dialog before clobbering anything.
  * Cancel → leave the form alone (NOT open the file picker, NOT
    parse anything, NOT touch any widget).
  * Yes → proceed with the file picker / parse / apply flow.

These tests drive the form state directly and patch the QFileDialog +
QMessageBox calls to avoid blocking on user input.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qt_app():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def screen(qt_app):
    """Fresh Project Setup screen per test."""
    from src.gui.screens.project_setup import ProjectSetupScreen
    s = ProjectSetupScreen()
    s.show()
    qt_app.processEvents()
    yield s
    s.close()


class TestFormHasUserData:
    """Sanity-check the dirty-state detector — every form field that
    can carry user data must mark the form dirty when filled, and an
    empty form must report clean."""

    def test_empty_form_reports_clean(self, screen):
        assert not screen._form_has_user_data()

    @pytest.mark.parametrize("setter", [
        lambda s: s.ed_project_name.setText("My Project"),
        lambda s: s.ed_pipeline_name.setText("Test Pipeline"),
        lambda s: s.ed_client_name.setText("ACME"),
        lambda s: s.ed_material_grade.setText("API 5L X52"),
        lambda s: s.ed_product.setText("LPG"),
        lambda s: s.sp_diameter.setValue(273.0),
        lambda s: s.sp_length.setValue(58.5),
        lambda s: s.sp_smys.setValue(358.0),
        lambda s: s.sp_install_year.setValue(2011),
        lambda s: s.ed_run1_path.setText("/tmp/run1.xlsx"),
        lambda s: s.ed_run2_path.setText("/tmp/run2.xlsx"),
    ])
    def test_each_field_marks_form_dirty(self, screen, setter):
        setter(screen)
        assert screen._form_has_user_data(), (
            "form should report dirty after a single field is filled"
        )

    def test_maop_zone_row_marks_form_dirty(self, screen):
        screen._append_zone_row(wt_min=6.0, wt_max=8.0, df=0.72, maop=70.0)
        assert screen._form_has_user_data()

    def test_whitespace_only_is_not_dirty(self, screen):
        """A field with only whitespace shouldn't count — it's an
        accidental space-bar press, not real user data."""
        screen.ed_project_name.setText("   ")
        assert not screen._form_has_user_data()


class TestAutofillOverwriteGuard:
    """Behavioural tests for the confirmation flow."""

    def test_clean_form_skips_confirmation(self, screen):
        """When the form is empty, no confirm dialog appears.
        The file picker is opened directly."""
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        # The user cancels the file picker — that's how we end the
        # flow without needing a real PDF.
        with patch.object(
            QFileDialog, "getOpenFileName", return_value=("", ""),
        ) as mock_picker, patch.object(
            QMessageBox, "question",
        ) as mock_confirm:
            screen._on_autofill_pdf_clicked()
            mock_picker.assert_called_once()
            mock_confirm.assert_not_called()

    def test_dirty_form_shows_confirmation(self, screen):
        """When the form has data, the confirm dialog appears BEFORE
        the file picker. If the user cancels, the file picker is never
        opened."""
        from PyQt6.QtWidgets import QFileDialog, QMessageBox

        screen.ed_project_name.setText("Existing project")
        screen.sp_diameter.setValue(273.0)
        screen._append_zone_row(wt_min=6.0, wt_max=8.0, df=0.72, maop=70.0)

        # User chooses Cancel on the confirm dialog.
        with patch.object(
            QMessageBox, "question",
            return_value=QMessageBox.StandardButton.Cancel,
        ) as mock_confirm, patch.object(
            QFileDialog, "getOpenFileName",
        ) as mock_picker:
            screen._on_autofill_pdf_clicked()
            mock_confirm.assert_called_once()
            mock_picker.assert_not_called()       # cancelled before picker

        # Form state must be untouched.
        assert screen.ed_project_name.text() == "Existing project"
        assert screen.sp_diameter.value() == 273.0
        assert screen.tbl_zones.rowCount() == 1

    def test_dirty_form_confirm_yes_proceeds_to_picker(self, screen):
        """Yes → file picker is opened. (User then cancels the picker
        to end the flow without needing a real PDF on disk.)"""
        from PyQt6.QtWidgets import QFileDialog, QMessageBox

        screen.ed_project_name.setText("Existing project")
        with patch.object(
            QMessageBox, "question",
            return_value=QMessageBox.StandardButton.Yes,
        ) as mock_confirm, patch.object(
            QFileDialog, "getOpenFileName", return_value=("", ""),
        ) as mock_picker:
            screen._on_autofill_pdf_clicked()
            mock_confirm.assert_called_once()
            mock_picker.assert_called_once()

        # Form state still untouched — file picker returned empty
        # (user cancelled), so no parse happened.
        assert screen.ed_project_name.text() == "Existing project"

    def test_confirm_dialog_uses_cancel_as_default_button(self, screen):
        """Default-Cancel is conservative: a stray Enter key on the
        confirm dialog must NOT clobber the user's data."""
        from PyQt6.QtWidgets import QMessageBox

        screen.ed_project_name.setText("Existing project")
        with patch.object(QMessageBox, "question") as mock_q:
            mock_q.return_value = QMessageBox.StandardButton.Cancel
            screen._on_autofill_pdf_clicked()

        # Inspect the call to QMessageBox.question — the 5th positional
        # arg is the default button.
        args = mock_q.call_args
        # `defaultButton` is the last positional arg in the call.
        default_btn = args.args[-1]
        assert default_btn == QMessageBox.StandardButton.Cancel, (
            f"default button is {default_btn}, expected Cancel"
        )
