---
title: "M4: Implement secure redaction and sanitization"
labels: "type:feature,area:security,priority:p0"
---
## Goal

Remove sensitive content rather than covering it visually.

## Tasks

- [ ] Rectangle redaction marks
- [ ] Search and regular-expression redaction
- [ ] Remove intersecting text and image content
- [ ] Remove metadata, attachments, comments, hidden layers, and JavaScript
- [ ] Post-save extraction and image checks

## Acceptance criteria

- Redacted content cannot be recovered by text extraction, object inspection, or embedded-image extraction in the supported cases.
