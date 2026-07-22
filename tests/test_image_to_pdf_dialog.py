from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtWidgets import QDialogButtonBox

from pdf_workbench.services.image_to_pdf import ImageToPdfService
from pdf_workbench.services.pdf_save_service import TargetSnapshot
from pdf_workbench.ui.widgets.image_to_pdf_dialog import ImageToPdfDialog


def save_image(path: Path, color: str = "red") -> Path:
    image = Image.new("RGB", (20, 10), color)
    try:
        image.save(path)
    finally:
        image.close()
    return path


@pytest.mark.gui
def test_image_to_pdf_dialog_adds_reorders_and_builds_plan(qtbot, tmp_path: Path) -> None:
    first = save_image(tmp_path / "first.png", "red")
    second = save_image(tmp_path / "second.png", "blue")
    service = ImageToPdfService()
    dialog = ImageToPdfDialog(
        input_reader=service.inspect_image_input,
        target_snapshot_reader=TargetSnapshot.capture,
        default_output_directory=tmp_path,
    )
    qtbot.addWidget(dialog)

    dialog.add_inputs((first, second))
    dialog.input_list.setCurrentRow(1)
    dialog.move_selected_input_up()
    dialog.output_path_edit.setText(str(tmp_path / "images.pdf"))
    dialog.accept_with_validation()

    assert dialog.dialog_result is not None
    assert tuple(item.label for item in dialog.dialog_result.plan.inputs) == (
        "second.png",
        "first.png",
    )
    assert dialog.dialog_result.plan.total_page_count == 2


@pytest.mark.gui
def test_image_to_pdf_dialog_rejects_stale_source(qtbot, monkeypatch, tmp_path: Path) -> None:
    image_path = save_image(tmp_path / "source.png", "red")
    service = ImageToPdfService()
    dialog = ImageToPdfDialog(
        input_reader=service.inspect_image_input,
        target_snapshot_reader=TargetSnapshot.capture,
        default_output_directory=tmp_path,
    )
    qtbot.addWidget(dialog)
    dialog.add_inputs((image_path,))
    dialog.output_path_edit.setText(str(tmp_path / "images.pdf"))
    save_image(image_path, "green")
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "pdf_workbench.ui.widgets.image_to_pdf_dialog.QMessageBox.warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )

    dialog.accept_with_validation()

    assert dialog.dialog_result is None
    assert warnings
    assert "変更" in warnings[0][1]


@pytest.mark.gui
def test_image_to_pdf_dialog_ok_disabled_until_input_added(qtbot, tmp_path: Path) -> None:
    service = ImageToPdfService()
    dialog = ImageToPdfDialog(
        input_reader=service.inspect_image_input,
        target_snapshot_reader=TargetSnapshot.capture,
        default_output_directory=tmp_path,
    )
    qtbot.addWidget(dialog)

    ok_button = dialog.button_box.button(QDialogButtonBox.StandardButton.Ok)

    assert not ok_button.isEnabled()
