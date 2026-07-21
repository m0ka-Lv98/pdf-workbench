from __future__ import annotations

from PySide6.QtWidgets import QDialogButtonBox
from pytestqt.qtbot import QtBot

from pdf_workbench.ui.widgets.extract_pages_dialog import ExtractPagesDialog


def ok_button(dialog: ExtractPagesDialog) -> object:
    return dialog.button_box.button(QDialogButtonBox.StandardButton.Ok)


def test_extract_pages_dialog_accepts_selected_pages(qtbot: QtBot) -> None:
    dialog = ExtractPagesDialog(page_count=5, selected_page_indexes=(3, 1))
    qtbot.addWidget(dialog)

    assert dialog.selection_mode_radio.isChecked()
    assert ok_button(dialog).isEnabled()
    assert dialog.feedback_label.text() == "2ページを抽出"

    dialog._accept_with_validation()

    assert dialog.dialog_result is not None
    assert dialog.dialog_result.mode == "selection"
    assert dialog.dialog_result.plan.source_page_indexes == (1, 3)


def test_extract_pages_dialog_validates_range_input(qtbot: QtBot) -> None:
    dialog = ExtractPagesDialog(
        page_count=10,
        selected_page_indexes=(),
        default_mode="range",
    )
    qtbot.addWidget(dialog)

    assert dialog.range_mode_radio.isChecked()
    assert dialog.selection_mode_radio.isEnabled() is False
    assert ok_button(dialog).isEnabled() is False

    dialog.range_edit.setText(" 1 - 3, 3, 8 ")
    assert ok_button(dialog).isEnabled()
    assert dialog.feedback_label.text() == "4ページを抽出"

    dialog._accept_with_validation()

    assert dialog.dialog_result is not None
    assert dialog.dialog_result.mode == "range"
    assert dialog.dialog_result.plan.source_page_indexes == (0, 1, 2, 7)


def test_extract_pages_dialog_rejects_invalid_range(qtbot: QtBot) -> None:
    dialog = ExtractPagesDialog(
        page_count=3,
        selected_page_indexes=(0,),
        default_mode="range",
    )
    qtbot.addWidget(dialog)

    dialog.range_edit.setText("3-1")

    assert ok_button(dialog).isEnabled() is False
    assert "昇順" in dialog.feedback_label.text()
