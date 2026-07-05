---
title: "M1: Centralize PDF-to-view coordinate mapping"
labels: "type:feature,area:core,priority:p0"
---
## Goal

Provide one tested mapping implementation for text selection, annotations, forms, and redaction.

## Tasks

- [ ] PDF point to Qt pixel conversion
- [ ] CropBox and MediaBox handling
- [ ] Page rotation handling
- [ ] Zoom and device-pixel-ratio handling
- [ ] Rectangle and polygon conversion tests

## Acceptance criteria

- Round-trip mapping error is within one display pixel for supported rotations.
