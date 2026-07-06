# Coordinate Mapping

`pdf_workbench.services.page_coordinates` is the single source of truth for PDF-to-Qt mapping.

## Geometry

- `PageMetadata.geometry` is the only stored page geometry.
- `width_points` and `height_points` are derived from `geometry.visible_box`.
- `PageGeometry.from_pdfium_page()` reads `MediaBox`, `CropBox`, `BBox`, and rotation.
- The visible box comes from `page.get_bbox()`.

## Coordinate systems

- PDF coordinates use a bottom-left origin.
- Qt view and device coordinates use a top-left origin.
- `PagePlaceholder.page_content_rect()` returns the page-local content rectangle.
- `PdfView.page_content_rect()` returns the content rectangle in the scroll-content coordinate system.
- The pixmap is painted directly into the content rectangle, so the painted target and mapped target are the same.
- `paintEvent()` does not apply any extra `KeepAspectRatio` scaling.

## Validation

- Rotation is strict: only `0`, `90`, `180`, and `270` are accepted.
- Zoom and DPR must be finite and positive.
- Invalid rectangle extents are rejected.
- DPR does not change logical view size.

## Oracle coverage

- PDFium `PdfPosConv` is exercised across all 16 intrinsic/additional rotation combinations.
- Representative zoom and DPR cases are also covered.
- Observed oracle error stayed within 1 device pixel on each axis.

## Tests

- Round-trip point, rectangle, and polygon coverage is present.
- Empty and non-convex polygons are covered.
- Lazy-rendering regressions remain covered.
