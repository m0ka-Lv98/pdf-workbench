from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QMessageBox, QWidget
from pytestqt.qtbot import QtBot

from pdf_workbench.domain.page_crop import PageCropMargins
from pdf_workbench.domain.page_insertion import SourcePageSelection
from pdf_workbench.ui.widgets.crop_pages_dialog import CropPagesDialog
from pdf_workbench.ui.widgets.document_toolbar import DocumentToolbar, ToolbarState, button_has_icon
from pdf_workbench.ui.widgets.empty_state import EmptyState
from pdf_workbench.ui.widgets.insert_pages_dialog import InsertPagesDialog
from pdf_workbench.ui.widgets.replace_pages_dialog import ReplacePagesDialog


def test_document_toolbar_updates_state_and_emits_signals(qtbot: QtBot) -> None:
    toolbar = DocumentToolbar()
    qtbot.addWidget(toolbar)

    assert toolbar.zoom_field.currentText() == "100%"
    assert toolbar.page_field.value() == 0
    assert toolbar.page_field.text() == "—"

    toolbar.setState(ToolbarState(True, 2, 8, 1.25))
    assert toolbar.page_field.value() == 3
    assert toolbar.page_field.text() == "3"
    assert toolbar.page_field.maximum() == 8
    assert toolbar._page_label.text() == "/ 8"
    assert toolbar.zoom_field.currentText() == "125%"
    assert toolbar.previous_button.isEnabled()
    assert toolbar.next_button.isEnabled()
    assert toolbar.page_field.minimum() == 1
    assert toolbar.previous_button.text() == ""
    assert toolbar.next_button.text() == ""
    assert toolbar.zoom_out_button.text() == ""
    assert toolbar.zoom_in_button.text() == ""
    assert toolbar.rotate_button.text() == ""
    assert toolbar.delete_button.text() == ""
    assert toolbar.duplicate_button.text() == ""
    assert toolbar.rotate_button.accessibleName() == "Rotate selected pages clockwise"
    assert toolbar.delete_button.accessibleName() == "Delete selected pages"
    assert toolbar.duplicate_button.accessibleName() == "Duplicate selected pages"
    assert toolbar.previous_button.toolTip() == "前のページ"
    assert toolbar.zoom_out_button.toolTip() == "ズームを縮小"
    assert toolbar.zoom_in_button.toolTip() == "ズームを拡大"
    assert toolbar.rotate_button.toolTip() == "選択したページを時計回りに90°回転"
    assert toolbar.delete_button.toolTip() == "選択したページを削除"
    assert toolbar.duplicate_button.toolTip() == "選択したページを複製"
    assert toolbar.height() == 54
    assert button_has_icon(toolbar.open_button)
    assert button_has_icon(toolbar.search_button)
    assert button_has_icon(toolbar.previous_button)
    assert button_has_icon(toolbar.next_button)
    assert button_has_icon(toolbar.rotate_button)
    assert button_has_icon(toolbar.delete_button)
    assert button_has_icon(toolbar.duplicate_button)
    assert 56 <= toolbar.page_field.width() <= 64
    assert 90 <= toolbar.zoom_field.width() <= 104
    separators = toolbar.findChildren(QWidget, "toolbarSeparator")
    assert separators
    assert all(18 <= separator.height() <= 22 for separator in separators)

    page_requests: list[int] = []
    zoom_requests: list[float] = []
    toolbar.page_requested.connect(page_requests.append)
    toolbar.zoom_requested.connect(zoom_requests.append)

    toolbar.page_field.setValue(4)
    toolbar.zoom_field.activated.emit(toolbar.zoom_field.findText("150%"))
    toolbar.zoom_field.lineEdit().setText("150%")
    toolbar.zoom_field.lineEdit().editingFinished.emit()

    assert page_requests == [3]
    assert zoom_requests == [1.5]

    toolbar.zoom_field.lineEdit().setText("150%")
    toolbar.zoom_field.lineEdit().editingFinished.emit()
    assert zoom_requests == [1.5]


def test_document_toolbar_rejects_invalid_zoom_and_keeps_previous_value(
    qtbot: QtBot,
) -> None:
    toolbar = DocumentToolbar()
    qtbot.addWidget(toolbar)

    emitted: list[float] = []
    toolbar.zoom_requested.connect(emitted.append)

    for invalid in ["", "abc", "NaN", "inf", "-infinity", "0%", "24%", "501%"]:
        toolbar.zoom_field.lineEdit().setText(invalid)
        toolbar.zoom_field.lineEdit().editingFinished.emit()
        assert toolbar.zoom_field.currentText() == "100%"

    assert emitted == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("25%", 0.25),
        ("100%", 1.0),
        ("125%", 1.25),
        ("200%", 2.0),
        ("333%", pytest.approx(3.33)),
        ("400%", 4.0),
        ("500%", 5.0),
    ],
)
def test_document_toolbar_parses_full_user_zoom_range(
    qtbot: QtBot,
    text: str,
    expected: float,
) -> None:
    toolbar = DocumentToolbar()
    qtbot.addWidget(toolbar)

    emitted: list[float] = []
    toolbar.zoom_requested.connect(emitted.append)
    toolbar.zoom_field.lineEdit().setText(text)
    toolbar.zoom_field.lineEdit().editingFinished.emit()

    if text == "100%":
        assert emitted == []
    else:
        assert emitted == [expected]


def test_document_toolbar_zoom_buttons_nudge_by_factor(qtbot: QtBot) -> None:
    toolbar = DocumentToolbar()
    qtbot.addWidget(toolbar)
    toolbar.show()
    qtbot.waitExposed(toolbar)

    emitted: list[float] = []
    toolbar.zoom_requested.connect(emitted.append)

    toolbar._nudge_zoom(0.2)
    toolbar._nudge_zoom(-1 / 6)

    assert emitted[0] == 1.2
    assert emitted[1] == pytest.approx(1.0)


def test_document_toolbar_does_not_use_toolbar_group_frames(qtbot: QtBot) -> None:
    toolbar = DocumentToolbar()
    qtbot.addWidget(toolbar)

    assert toolbar.findChild(QWidget, "toolbarGroup") is None
    assert not toolbar.findChildren(type(toolbar), "toolbarGroup")
    separators = toolbar.findChildren(QWidget, "toolbarSeparator")
    assert separators
    assert all(not isinstance(separator, QFrame) for separator in separators)


def test_document_toolbar_responsive_layout_keeps_primary_controls_visible(qtbot: QtBot) -> None:
    toolbar = DocumentToolbar()
    qtbot.addWidget(toolbar)
    toolbar.setState(ToolbarState(True, 1, 12, 1.25))
    toolbar.resize(800, toolbar.height())
    toolbar.show()
    qtbot.waitExposed(toolbar)

    for widget in (
        toolbar.open_button,
        toolbar.search_button,
        toolbar.previous_button,
        toolbar.page_field,
        toolbar.next_button,
        toolbar.zoom_out_button,
        toolbar.zoom_field,
        toolbar.zoom_in_button,
        toolbar.rotate_button,
        toolbar.delete_button,
        toolbar.duplicate_button,
    ):
        assert widget.isVisible()
        assert widget.geometry().width() > 0
        assert widget.geometry().height() > 0
        assert widget.geometry().right() <= toolbar.rect().right()
    assert toolbar.minimumSizeHint().width() <= 800


def test_document_toolbar_page_state_switches_minimum(qtbot: QtBot) -> None:
    toolbar = DocumentToolbar()
    qtbot.addWidget(toolbar)

    toolbar.setState(ToolbarState(False, 0, 0, 1.0))
    assert toolbar.page_field.minimum() == 0
    assert toolbar.page_field.maximum() == 0
    assert toolbar.page_field.value() == 0
    assert toolbar.page_field.specialValueText() == "—"

    emitted: list[int] = []
    toolbar.page_requested.connect(emitted.append)
    toolbar.setState(ToolbarState(True, 0, 12, 1.0))
    assert toolbar.page_field.minimum() == 1
    assert toolbar.page_field.maximum() == 12
    assert toolbar.page_field.value() == 1
    assert toolbar._page_label.text() == "/ 12"
    assert emitted == []

    toolbar.page_field.setValue(0)
    assert toolbar.page_field.value() == 1
    assert emitted == []


def test_empty_state_shows_recent_files_and_emits_selection(qtbot: QtBot, tmp_path: Path) -> None:
    empty_state = EmptyState()
    qtbot.addWidget(empty_state)

    files = [tmp_path / f"{index}.pdf" for index in range(6)]
    empty_state.set_recent_files(files)

    buttons = empty_state.findChildren(type(empty_state.open_button))
    file_names = {path.name for path in files}
    recent_buttons = [button for button in buttons if button.text() in file_names]

    assert len(recent_buttons) == 5
    assert empty_state._recent_message.isVisible() is False
    assert recent_buttons[0].toolTip() == str(files[0])
    assert recent_buttons[0].focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert not empty_state._icon_label.pixmap().isNull()

    requested: list[Path] = []
    empty_state.recent_file_requested.connect(requested.append)
    qtbot.mouseClick(recent_buttons[0], Qt.MouseButton.LeftButton)

    assert requested == [files[0]]
    assert (
        empty_state.findChild(type(empty_state._recent_message), "emptyStateRecentMessage")
        is not None
    )
    assert empty_state.findChild(type(empty_state.open_button), "openPdfButton") is not None


def test_crop_pages_dialog_exposes_fields_and_reset_mode(qtbot: QtBot) -> None:
    dialog = CropPagesDialog(selected_page_count=3)
    qtbot.addWidget(dialog)

    assert dialog.left_margin_spin.decimals() == 2
    assert dialog.top_margin_spin.minimum() == 0.0
    assert dialog.right_margin_spin.suffix() == " pt"
    assert dialog.explanation_label.text().startswith("トリミングは表示範囲だけを変更します")
    assert dialog.reset_checkbox.isChecked() is False
    assert dialog.left_margin_spin.isEnabled() is True

    dialog.reset_checkbox.setChecked(True)
    assert dialog.left_margin_spin.isEnabled() is False
    assert dialog.top_margin_spin.isEnabled() is False
    assert dialog.right_margin_spin.isEnabled() is False
    assert dialog.bottom_margin_spin.isEnabled() is False


def test_crop_pages_dialog_returns_current_margins(qtbot: QtBot) -> None:
    dialog = CropPagesDialog(selected_page_count=1)
    qtbot.addWidget(dialog)
    dialog.left_margin_spin.setValue(12.5)
    dialog.top_margin_spin.setValue(7.25)
    dialog.right_margin_spin.setValue(9.0)
    dialog.bottom_margin_spin.setValue(3.5)

    dialog._accept_with_result()

    assert dialog.dialog_result is not None
    assert dialog.dialog_result.margins == PageCropMargins(12.5, 7.25, 9.0, 3.5)
    assert dialog.dialog_result.reset_to_media_box is False


def test_insert_pages_dialog_accepts_valid_selection(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = InsertPagesDialog(
        tmp_path / "source.pdf",
        5,
        (("先頭", 0), ("末尾", 3)),
        default_index=1,
    )
    qtbot.addWidget(dialog)

    assert dialog.page_range_edit.objectName() == "insertSourcePageRangeEdit"
    assert dialog.insertion_combo.objectName() == "insertPositionCombo"
    assert dialog.page_range_edit.placeholderText() == "all または 1,3-5"
    assert dialog.page_range_edit.text() == "all"
    assert dialog.insertion_combo.currentIndex() == 1

    dialog.page_range_edit.setText("1,3-4")
    dialog.insertion_combo.setCurrentIndex(0)
    dialog._accept_with_validation()

    assert dialog.result() == dialog.DialogCode.Accepted
    assert dialog.dialog_result is not None
    assert dialog.dialog_result.page_selection == SourcePageSelection(5, (0, 2, 3))
    assert dialog.dialog_result.insertion_slot == 0


def test_insert_pages_dialog_returns_structured_result(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = InsertPagesDialog(
        tmp_path / "source.pdf",
        4,
        (("先頭", 0), ("選択後", 2), ("末尾", 4)),
        default_index=5,
    )
    qtbot.addWidget(dialog)

    dialog.page_range_edit.setText("2-3")
    dialog.insertion_combo.setCurrentIndex(1)
    dialog._accept_with_validation()

    assert dialog.dialog_result is not None
    assert dialog.dialog_result.page_selection == SourcePageSelection(4, (1, 2))
    assert dialog.dialog_result.insertion_slot == 2


def test_insert_pages_dialog_shows_warning_for_invalid_page_range(
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = InsertPagesDialog(
        tmp_path / "source.pdf",
        3,
        (("先頭", 0), ("末尾", 3)),
    )
    qtbot.addWidget(dialog)
    warnings: list[tuple[str, str]] = []

    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message, *args, **kwargs: (
            warnings.append((title, message)) or QMessageBox.StandardButton.Ok
        ),
    )

    dialog.page_range_edit.setText("4")
    dialog._accept_with_validation()

    assert dialog.result() == dialog.DialogCode.Rejected
    assert dialog.dialog_result is None
    assert warnings == [("入力エラー", "page number is out of range")]


def test_insert_pages_dialog_shows_warning_for_invalid_insertion_index(
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = InsertPagesDialog(
        tmp_path / "source.pdf",
        2,
        (("先頭", 0),),
    )
    qtbot.addWidget(dialog)
    warnings: list[str] = []

    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message, *args, **kwargs: (
            warnings.append(message) or QMessageBox.StandardButton.Ok
        ),
    )

    dialog.insertion_combo.setCurrentIndex(-1)
    dialog._accept_with_validation()

    assert dialog.dialog_result is None
    assert warnings == ["挿入位置が不正です"]


def test_insert_pages_dialog_summary_is_selectable(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = InsertPagesDialog(
        tmp_path / "named-source.pdf",
        7,
        (("先頭", 0), ("末尾", 5)),
    )
    qtbot.addWidget(dialog)

    summary = next(
        label for label in dialog.findChildren(QLabel) if label.text().startswith("挿入元PDF:")
    )
    assert "named-source.pdf" in summary.text()
    assert "全ページ数: 7" in summary.text()
    assert (
        summary.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    ) == Qt.TextInteractionFlag.TextSelectableByMouse


def test_replace_pages_dialog_accepts_matching_selection(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = ReplacePagesDialog(
        tmp_path / "source.pdf",
        5,
        (2, 5),
    )
    qtbot.addWidget(dialog)

    assert dialog.page_range_edit.objectName() == "replaceSourcePageRangeEdit"
    dialog.page_range_edit.setText("1,3")
    dialog._accept_with_validation()

    assert dialog.result() == dialog.DialogCode.Accepted
    assert dialog.dialog_result is not None
    assert dialog.dialog_result.page_selection == SourcePageSelection(5, (0, 2))


def test_replace_pages_dialog_rejects_mismatched_page_counts(
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = ReplacePagesDialog(
        tmp_path / "source.pdf",
        5,
        (1, 2, 3),
    )
    qtbot.addWidget(dialog)
    warnings: list[str] = []

    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message, *args, **kwargs: (
            warnings.append(message) or QMessageBox.StandardButton.Ok
        ),
    )

    dialog.page_range_edit.setText("1-2")
    dialog._accept_with_validation()

    assert dialog.dialog_result is None
    assert warnings == ["置換元ページ数は置換対象ページ数と一致する必要があります"]


def test_empty_state_shows_muted_message_for_no_recent_files(qtbot: QtBot) -> None:
    empty_state = EmptyState()
    qtbot.addWidget(empty_state)
    empty_state.show()
    qtbot.waitExposed(empty_state)

    empty_state.set_recent_files([])

    assert empty_state._recent_message.isVisible() is True


def test_empty_state_recent_files_are_limited_to_five(qtbot: QtBot, tmp_path: Path) -> None:
    empty_state = EmptyState()
    qtbot.addWidget(empty_state)

    empty_state.show()
    qtbot.waitExposed(empty_state)

    paths = [tmp_path / f"{index}.pdf" for index in range(7)]
    empty_state.set_recent_files(paths)
    buttons = [
        button
        for button in empty_state.findChildren(type(empty_state.open_button))
        if button.text().endswith(".pdf")
    ]

    assert len(buttons) == 5
