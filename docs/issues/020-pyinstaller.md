---
title: "M0: Produce reproducible Windows executables with PyInstaller"
labels: "type:chore,area:build,priority:p0"
---
## Goal

Create a Windows application that runs without a separately installed Python environment.

## Tasks

- [ ] Maintain an `onedir` spec as the supported build
- [ ] Maintain an experimental `onefile` spec
- [ ] Collect PDFium binaries and Qt plugins
- [ ] Store writable files under the user profile
- [ ] Add CLI smoke test and application launch test
- [ ] Upload build artifacts from GitHub Actions
- [ ] Document code-signing and installer options for a later release

## Acceptance criteria

- A clean Windows 11 machine launches the `onedir` build and opens a PDF.
