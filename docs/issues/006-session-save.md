---
title: "M2: Implement DocumentSession, safe save, and recovery"
labels: "type:feature,area:core,priority:p0"
---
## Goal

Prevent source-file corruption and provide a reliable edit lifecycle.

## Tasks

- [ ] Working-copy management
- [ ] Atomic save-as
- [ ] Reopen validation after save
- [ ] Recovery metadata for interrupted sessions
- [ ] File-change detection when another process modifies the source

## Acceptance criteria

- A failed save never replaces the original file.
- Successfully saved output can be reopened and rendered.
