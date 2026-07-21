from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFileDialog, QMessageBox
from pytestqt.qtbot import QtBot

from pdf_workbench.domain.document_session import FileFingerprint
from pdf_workbench.domain.pdf_merge import PdfMergeInput
from pdf_workbench.services.pdf_merge import InspectedPdfMergeInput
from pdf_workbench.services.pdf_page_mutation import SourcePdfRevision
from pdf_workbench.services.pdf_save_service import TargetSnapshot
from pdf_workbench.ui.widgets.merge_pdfs_dialog import MergePdfsDialog


def ok_button_enabled(dialog: MergePdfsDialog) -> bool:
    return dialog.button_box.button(QDialogButtonBox.StandardButton.Ok).isEnabled()


def make_reader(tmp_path: Path):
    page_counts = {"a.pdf": 2, "b.pdf": 3, "c.pdf": 1}

    def read_input(path: Path) -> InspectedPdfMergeInput:
        resolved = (tmp_path / path.name).resolve()
        if not resolved.exists():
            resolved.write_bytes(f"%PDF fake {path.name}".encode("ascii"))
        page_count = page_counts[path.name]
        return InspectedPdfMergeInput(
            merge_input=PdfMergeInput(resolved, page_count, path.name),
            source_revision=SourcePdfRevision(
                resolved_path=resolved,
                fingerprint=FileFingerprint.from_path(resolved),
                sha256="a" * 64,
                page_count=page_count,
            ),
        )

    return read_input


def test_merge_dialog_requires_two_inputs(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = MergePdfsDialog(
        input_reader=make_reader(tmp_path),
        default_output_directory=tmp_path,
    )
    qtbot.addWidget(dialog)

    dialog.add_inputs((tmp_path / "a.pdf",))

    assert ok_button_enabled(dialog) is False
    assert "at least two" in dialog.feedback_label.text()


def test_merge_dialog_updates_preview_and_reorders_inputs(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = MergePdfsDialog(
        input_reader=make_reader(tmp_path),
        default_output_directory=tmp_path,
    )
    qtbot.addWidget(dialog)
    dialog.add_inputs((tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "c.pdf"))

    dialog.input_list.setCurrentRow(2)
    dialog.move_selected_input_up()

    assert ok_button_enabled(dialog)
    assert "3個のPDF" in dialog.feedback_label.text()
    assert "1. a.pdf" in dialog.preview_label.text()
    assert "2. c.pdf" in dialog.preview_label.text()
    assert "出力 3-3" in dialog.preview_label.text()


def test_merge_dialog_accepts_metadata_source_and_overwrite(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    dialog = MergePdfsDialog(
        input_reader=make_reader(tmp_path),
        default_output_directory=tmp_path,
    )
    qtbot.addWidget(dialog)
    dialog.add_inputs((tmp_path / "a.pdf", tmp_path / "b.pdf"))
    dialog.metadata_combo.setCurrentIndex(2)
    dialog.overwrite_checkbox.setChecked(True)

    dialog.accept_with_validation()

    assert dialog.result() == int(QDialog.DialogCode.Accepted)
    assert dialog.dialog_result is not None
    assert dialog.dialog_result.overwrite is True
    assert dialog.dialog_result.plan.metadata_source_path == (tmp_path / "b.pdf").resolve()


def test_merge_dialog_file_buttons_add_inputs_and_choose_output(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    dialog = MergePdfsDialog(
        input_reader=make_reader(tmp_path),
        default_output_directory=tmp_path,
    )
    qtbot.addWidget(dialog)
    output_path = tmp_path / "chosen.pdf"
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        lambda *_args: ([str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")], ""),
    )
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args: (str(output_path), ""),
    )

    dialog.choose_inputs()
    dialog.choose_output_path()

    assert dialog.input_list.count() == 2
    assert dialog.output_path_edit.text() == str(output_path.resolve())
    assert ok_button_enabled(dialog)


def test_merge_dialog_reports_rejected_and_duplicate_inputs(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    def reader(path: Path) -> InspectedPdfMergeInput:
        if path.name == "bad.pdf":
            raise ValueError("broken")
        resolved = (tmp_path / path.name).resolve()
        resolved.write_bytes(b"%PDF fake")
        return InspectedPdfMergeInput(
            merge_input=PdfMergeInput(resolved, 1, path.name),
            source_revision=SourcePdfRevision(
                resolved_path=resolved,
                fingerprint=FileFingerprint.from_path(resolved),
                sha256="b" * 64,
                page_count=1,
            ),
        )

    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    dialog = MergePdfsDialog(input_reader=reader, default_output_directory=tmp_path)
    qtbot.addWidget(dialog)

    dialog.add_inputs((tmp_path / "a.pdf", tmp_path / "a.pdf", tmp_path / "bad.pdf"))

    assert dialog.input_list.count() == 1
    assert warnings
    assert "既に追加" in warnings[0][1]
    assert "broken" in warnings[0][1]


def test_merge_dialog_remove_and_boundary_moves_update_preview(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    dialog = MergePdfsDialog(
        input_reader=make_reader(tmp_path),
        default_output_directory=tmp_path,
    )
    qtbot.addWidget(dialog)
    dialog.add_inputs((tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "c.pdf"))

    dialog.input_list.setCurrentRow(0)
    dialog.move_selected_input_up()
    assert dialog.input_list.item(0).text().startswith("a.pdf")

    dialog.move_selected_input_down()
    assert dialog.input_list.item(1).text().startswith("a.pdf")

    dialog.input_list.item(1).setSelected(True)
    dialog.remove_selected_inputs()

    assert dialog.input_list.count() == 2
    assert "a.pdf" not in dialog.preview_label.text()


def test_merge_dialog_rejects_corrupt_internal_item_state(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    dialog = MergePdfsDialog(
        input_reader=make_reader(tmp_path),
        default_output_directory=tmp_path,
    )
    qtbot.addWidget(dialog)
    dialog.add_inputs((tmp_path / "a.pdf", tmp_path / "b.pdf"))
    dialog.input_list.item(0).setData(Qt.ItemDataRole.UserRole, "not merge input")

    dialog.accept_with_validation()

    assert dialog.result() == int(QDialog.DialogCode.Rejected)
    assert warnings
    assert "不正" in warnings[0][1]


def test_merge_dialog_rejects_same_page_count_source_content_drift(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    calls: dict[str, int] = {}

    def reader(path: Path) -> InspectedPdfMergeInput:
        resolved = (tmp_path / path.name).resolve()
        resolved.write_bytes(f"%PDF fake {path.name}".encode("ascii"))
        calls[path.name] = calls.get(path.name, 0) + 1
        sha = "c" * 64
        if path.name == "a.pdf" and calls[path.name] > 1:
            sha = "d" * 64
        return InspectedPdfMergeInput(
            merge_input=PdfMergeInput(resolved, 1, path.name),
            source_revision=SourcePdfRevision(
                resolved_path=resolved,
                fingerprint=FileFingerprint.from_path(resolved),
                sha256=sha,
                page_count=1,
            ),
        )

    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    dialog = MergePdfsDialog(input_reader=reader, default_output_directory=tmp_path)
    qtbot.addWidget(dialog)
    dialog.add_inputs((tmp_path / "a.pdf", tmp_path / "b.pdf"))

    dialog.accept_with_validation()

    assert dialog.result() == int(QDialog.DialogCode.Rejected)
    assert warnings
    assert "変更" in warnings[0]


def test_merge_dialog_rejects_target_created_after_dialog_open(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    dialog = MergePdfsDialog(
        input_reader=make_reader(tmp_path),
        target_snapshot_reader=lambda _path: TargetSnapshot(exists=True, fingerprint=None),
        default_output_directory=tmp_path,
    )
    qtbot.addWidget(dialog)
    dialog.add_inputs((tmp_path / "a.pdf", tmp_path / "b.pdf"))

    dialog.accept_with_validation()

    assert dialog.result() == int(QDialog.DialogCode.Rejected)
    assert warnings
    assert "既に存在" in warnings[0]


def test_merge_dialog_normalizes_missing_pdf_suffix(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = MergePdfsDialog(
        input_reader=make_reader(tmp_path),
        default_output_directory=tmp_path,
    )
    qtbot.addWidget(dialog)
    dialog.add_inputs((tmp_path / "a.pdf", tmp_path / "b.pdf"))
    dialog.output_path_edit.setText(str(tmp_path / "merged"))

    dialog.accept_with_validation()

    assert dialog.dialog_result is not None
    assert dialog.dialog_result.plan.output_path == (tmp_path / "merged.pdf").resolve()


def test_merge_dialog_metadata_source_survives_reorder(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = MergePdfsDialog(input_reader=make_reader(tmp_path), default_output_directory=tmp_path)
    qtbot.addWidget(dialog)
    dialog.add_inputs((tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "c.pdf"))
    b_path = str((tmp_path / "b.pdf").resolve())
    dialog.metadata_combo.setCurrentIndex(dialog.metadata_combo.findData(b_path))

    dialog.input_list.setCurrentRow(1)
    dialog.move_selected_input_down()

    assert dialog.metadata_combo.currentData() == b_path


def test_merge_dialog_metadata_source_removal_resets_policy_with_feedback(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    dialog = MergePdfsDialog(input_reader=make_reader(tmp_path), default_output_directory=tmp_path)
    qtbot.addWidget(dialog)
    dialog.add_inputs((tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "c.pdf"))
    b_path = str((tmp_path / "b.pdf").resolve())
    dialog.metadata_combo.setCurrentIndex(dialog.metadata_combo.findData(b_path))

    dialog.input_list.item(1).setSelected(True)
    dialog.remove_selected_inputs()

    assert dialog.metadata_combo.currentData() == "none"
    assert "Metadata取得元が削除" in dialog.feedback_label.text()
