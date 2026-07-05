---
title: "M0: Stabilize project bootstrap and Windows development environment"
labels: "type:chore,area:build,priority:p0"
---
## Goal

Make a fresh clone reproducibly installable, testable, and runnable on Windows 10/11.

## Tasks

- [ ] Confirm Python 3.12 and 3.13 support
- [ ] Generate and commit `uv.lock`
- [ ] Verify `uv sync --extra dev --frozen`
- [ ] Run Ruff, mypy, and pytest in CI
- [ ] Document Windows prerequisites
- [ ] Confirm logs and settings are written under the user profile

## Acceptance criteria

- A clean Windows runner starts the application from the locked environment.
- CI passes on Windows and Ubuntu for Python 3.12 and 3.13.
