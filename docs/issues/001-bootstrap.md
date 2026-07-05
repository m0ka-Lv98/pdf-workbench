---
title: "M0: Stabilize project bootstrap and macOS/Windows development environment"
labels: "type:chore,area:build,priority:p0"
---
## Goal

Make a fresh clone reproducibly installable, testable, and runnable on macOS and Windows 10/11.

## Tasks

- [ ] Confirm Python 3.12 and 3.13 support
- [ ] Generate and commit `uv.lock`
- [ ] Verify `uv sync --extra dev --frozen`
- [ ] Run Ruff, mypy, and pytest in CI
- [ ] Document macOS and Windows prerequisites
- [ ] Confirm logs and settings are written under the user profile

## Acceptance criteria

- A clean macOS runner and a clean Windows runner start the application from the locked environment.
- CI passes on macOS, Windows, and Ubuntu for Python 3.12 and 3.13.
