from __future__ import annotations

from pathlib import Path


def test_packaging_specs_collect_theme_and_icon_resources() -> None:
    packaging_dir = Path(__file__).resolve().parent.parent / "packaging"
    spec_names = (
        "pdf_workbench_macos.spec",
        "pdf_workbench_onedir.spec",
        "pdf_workbench_onefile.spec",
    )

    for spec_name in spec_names:
        spec_text = (packaging_dir / spec_name).read_text(encoding="utf-8")
        assert 'collect_all("pdf_workbench.ui.styles")' in spec_text
        assert 'collect_all("pdf_workbench.ui.icons")' in spec_text
