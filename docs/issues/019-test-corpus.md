---
title: "M7: Build PDF compatibility and visual regression corpus"
labels: "type:test,area:quality,priority:p1"
---
## Goal

Detect corruption, rendering regressions, and compatibility failures before release.

## Tasks

- [ ] Digital, scanned, OCR, Japanese, vertical-text, form, annotation, encrypted, and damaged fixtures
- [ ] MediaBox/CropBox and rotated-page fixtures
- [ ] Structural validation
- [ ] Rendered-image comparison with tolerances
- [ ] Memory and large-document tests
- [ ] Redaction security tests

## Acceptance criteria

- All PDF-writing features have round-trip and cross-viewer validation coverage.
