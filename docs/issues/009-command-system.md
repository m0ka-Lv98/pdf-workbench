---
title: "M2: Add command-based undo and redo"
labels: "type:feature,area:core,priority:p0"
---
## Goal

Represent all editing operations as commands that can be undone, replayed, and used in batch workflows.

## Tasks

- [ ] Command protocol
- [ ] Undo and redo stacks
- [ ] Compound commands
- [ ] Affected-page invalidation
- [ ] Command descriptions for UI history

## Acceptance criteria

- Page operations can be undone and redone without reopening the source PDF.
