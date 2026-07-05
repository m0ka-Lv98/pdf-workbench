---
title: "M5: Implement local batch-processing pipelines"
labels: "type:feature,area:automation,priority:p1"
---
## Goal

Use Python's automation advantage for repetitive local PDF work.

## Tasks

- [ ] Batch input selection
- [ ] OCR, optimize, watermark, page number, and encryption actions
- [ ] Reusable JSON pipeline definitions
- [ ] Per-file logs and failure continuation
- [ ] CLI entry point

## Acceptance criteria

- One failed PDF does not stop the remaining batch and produces an actionable report.
