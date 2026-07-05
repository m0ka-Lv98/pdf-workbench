# Coordinate Mapping

`pdf_workbench.services.page_coordinates` provides the single source of truth for mapping
between PDF coordinates and Qt coordinates.

## Geometry source

- `PageGeometry.from_pdfium_page()` reads `MediaBox`, `CropBox`, and `BBox`.
- The visible page area comes from `page.get_bbox()`.
- `PageMetadata.geometry` carries the geometry into the UI layer.

## Mapping rules

- PDF points use bottom-left origin.
- Qt view and device coordinates use top-left origin.
- Rotation is limited to `0`, `90`, `180`, and `270` degrees.
- Logical zoom and device pixel ratio are both applied to view/device sizes.
- Invalid zoom, DPR, or rotation values raise `ValueError`.

## Tests

- Round-trip tests cover points, rectangles, and polygons.
- A PDFium `PdfPosConv` oracle test verifies the device mapping.
- Rotation, zoom, and device-pixel-ratio edge cases are covered explicitly.
