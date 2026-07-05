---
title: "M1: Add lazy PDFium page rendering and render cache"
labels: "type:feature,area:viewer,priority:p0"
---
## Goal

Render only visible and adjacent pages instead of rasterizing the entire document.

## Tasks

- [ ] Continuous-page canvas
- [ ] Visible-page detection
- [ ] Background render queue
- [ ] LRU cache keyed by file revision, page, scale, and rotation
- [ ] Cancellation of stale render jobs
- [ ] High-DPI support

## Acceptance criteria

- Opening a 1000-page PDF does not render all pages eagerly.
- Scrolling remains responsive while pages are rendered in the background.
