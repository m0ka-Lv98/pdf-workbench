# macOS Testing

This project can produce downloadable macOS application artifacts from GitHub Actions.

## What to verify

- The workflow is named `Build macOS application`.
- The matrix includes both `macos-15` and `macos-15-intel`.
- The uploaded artifacts are named:
  - `PDF-Workbench-macOS-arm64.zip`
  - `PDF-Workbench-macOS-x86_64.zip`
- Each archive expands to `PDF Workbench.app`.
- The app bundle contains:
  - `Contents/Info.plist`
  - `Contents/MacOS/PDF Workbench`

## Local notes

This branch does not add a local macOS packaging script. The build is intended to run in GitHub Actions and produce artifacts for download.
