from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFileDialog, QMessageBox
from pytestqt.qtbot import QtBot

from pdf_workbench.ui.widgets.split_pdf_dialog import SplitPdfDialog


def ok_button_enabled(dialog: SplitPdfDialog) -> bool:
    return dialog.button_box.button(QDialogButtonBox.StandardButton.Ok).isEnabled()


def test_split_pdf_dialog_shows_default_range_preview(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.pdf"
    dialog = SplitPdfDialog(
        source_path=source,
        page_count=6,
        default_output_directory=tmp_path,
    )
    qtbot.addWidget(dialog)

    assert dialog.range_mode_radio.isChecked()
    assert dialog.max_pages_spin.isEnabled() is False
    assert dialog.range_edit.isEnabled() is True
    assert ok_button_enabled(dialog)
    assert "report.pdf (6ページ)" in dialog.summary_label.text()
    assert "2個のPDF" in dialog.feedback_label.text()
    assert "1-3: report_pages_0001-0003.pdf" in dialog.preview_label.text()
    assert "4-6: report_pages_0004-0006.pdf" in dialog.preview_label.text()


def test_split_pdf_dialog_validates_manual_range_input(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    dialog = SplitPdfDialog(
        source_path=tmp_path / "source.pdf",
        page_count=4,
        default_output_directory=tmp_path,
    )
    qtbot.addWidget(dialog)

    dialog.range_edit.setPlainText("1-2\n2-4")

    assert ok_button_enabled(dialog) is False
    assert "重複" in dialog.feedback_label.text() or "抜け" in dialog.feedback_label.text()
    assert dialog.preview_label.text() == ""


def test_split_pdf_dialog_max_pages_mode_updates_preview(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    dialog = SplitPdfDialog(
        source_path=tmp_path / "source.pdf",
        page_count=5,
        default_output_directory=tmp_path,
    )
    qtbot.addWidget(dialog)

    dialog.max_pages_mode_radio.setChecked(True)
    dialog.max_pages_spin.setValue(2)

    assert dialog.range_edit.isEnabled() is False
    assert dialog.max_pages_spin.isEnabled() is True
    assert ok_button_enabled(dialog)
    assert "3個のPDF" in dialog.feedback_label.text()
    assert "1-2: source_pages_0001-0002.pdf" in dialog.preview_label.text()
    assert "5-5: source_pages_0005-0005.pdf" in dialog.preview_label.text()


def test_split_pdf_dialog_accepts_valid_options(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    dialog = SplitPdfDialog(
        source_path=tmp_path / "source.pdf",
        page_count=4,
        default_output_directory=tmp_path,
    )
    qtbot.addWidget(dialog)
    dialog.range_edit.setPlainText("1\n2-4")
    dialog.overwrite_checkbox.setChecked(True)

    dialog._accept_with_validation()

    assert dialog.result() == int(QDialog.DialogCode.Accepted)
    assert dialog.dialog_result is not None
    assert dialog.dialog_result.mode == "range"
    assert dialog.dialog_result.overwrite is True
    assert dialog.dialog_result.output_directory == tmp_path.resolve()
    assert [chunk.display_range for chunk in dialog.dialog_result.plan.chunks] == [
        "1-1",
        "2-4",
    ]


def test_split_pdf_dialog_warns_without_accepting_invalid_options(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    dialog = SplitPdfDialog(
        source_path=tmp_path / "source.pdf",
        page_count=4,
        default_output_directory=tmp_path,
    )
    qtbot.addWidget(dialog)
    dialog.range_edit.setPlainText("1-2")
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )

    dialog._accept_with_validation()

    assert dialog.result() == int(QDialog.DialogCode.Rejected)
    assert dialog.dialog_result is None
    assert warnings


def test_split_pdf_dialog_browse_updates_output_directory(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    dialog = SplitPdfDialog(
        source_path=tmp_path / "source.pdf",
        page_count=4,
        default_output_directory=tmp_path,
    )
    qtbot.addWidget(dialog)
    monkeypatch.setattr(
        QFileDialog,
        "getExistingDirectory",
        lambda *_args: str(chosen),
    )

    dialog._choose_output_directory()

    assert dialog.output_directory_edit.text() == str(chosen)
    assert f"{chosen}" not in dialog.preview_label.text()
    assert "source_pages_0001-0002.pdf" in dialog.preview_label.text()
