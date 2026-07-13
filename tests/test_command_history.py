from __future__ import annotations

import pytest

from pdf_workbench.domain.command_history import (
    CommandChange,
    CommandExecutionError,
    CommandHistory,
    CommandRedoError,
    CommandUndoError,
    CompoundCommand,
    CompoundCommandConfigurationError,
    CompoundCommandRollbackError,
    DocumentCommand,
)


class RecordingCommand(DocumentCommand):
    def __init__(
        self,
        description: str,
        *,
        affected_pages: frozenset[int] | None = None,
        fail_execute: bool = False,
        fail_undo: bool = False,
        fail_redo: bool = False,
        events: list[str] | None = None,
    ) -> None:
        self.description = description
        self.affected_pages = affected_pages
        self._fail_execute = fail_execute
        self._fail_undo = fail_undo
        self._fail_redo = fail_redo
        self._events = events if events is not None else []

    def execute(self) -> CommandChange:
        self._events.append(f"execute:{self.description}")
        if self._fail_execute:
            raise RuntimeError(f"execute failed: {self.description}")
        return CommandChange.from_command(self)

    def undo(self) -> CommandChange:
        self._events.append(f"undo:{self.description}")
        if self._fail_undo:
            raise RuntimeError(f"undo failed: {self.description}")
        return CommandChange.from_command(self)

    def redo(self) -> CommandChange:
        self._events.append(f"redo:{self.description}")
        if self._fail_redo:
            raise RuntimeError(f"redo failed: {self.description}")
        return CommandChange.from_command(self)


def test_command_history_starts_clean() -> None:
    history = CommandHistory()

    assert history.is_dirty is False
    assert history.can_undo is False
    assert history.can_redo is False
    assert history.undo_description is None
    assert history.redo_description is None


def test_command_history_execute_undo_and_redo_track_dirty_and_descriptions() -> None:
    history = CommandHistory()
    command = RecordingCommand("Rotate page 1", affected_pages=frozenset({0}))

    change = history.execute(command)

    assert change.affected_pages == frozenset({0})
    assert history.is_dirty is True
    assert history.can_undo is True
    assert history.undo_description == "Rotate page 1"

    history.undo()

    assert history.is_dirty is False
    assert history.can_redo is True
    assert history.redo_description == "Rotate page 1"

    history.redo()

    assert history.is_dirty is True
    assert history.can_undo is True
    assert history.can_redo is False


def test_command_history_tracks_clean_marker_across_save_and_undo_redo() -> None:
    history = CommandHistory()
    first = RecordingCommand("Rotate", affected_pages=frozenset({0}))
    second = RecordingCommand("Delete", affected_pages=None)
    history.execute(first)
    history.execute(second)

    history.mark_clean()
    assert history.is_dirty is False

    history.undo()
    assert history.is_dirty is True
    assert history.redo_description == "Delete"

    history.redo()
    assert history.is_dirty is False


def test_command_history_discards_redo_branch_and_unreachable_clean_marker_stays_dirty() -> None:
    history = CommandHistory()
    history.execute(RecordingCommand("First"))
    history.execute(RecordingCommand("Second"))
    history.mark_clean()

    history.undo()
    history.undo()
    assert history.is_dirty is True
    assert history.can_redo is True

    history.execute(RecordingCommand("Third"))

    assert history.can_redo is False
    assert history.is_dirty is True


def test_command_history_supports_initially_dirty_recovery_state() -> None:
    history = CommandHistory(initially_dirty=True)

    assert history.is_dirty is True
    assert history.can_undo is False
    assert history.can_redo is False


def test_command_history_execute_failure_preserves_invariants() -> None:
    history = CommandHistory()
    failing = RecordingCommand("Fail", fail_execute=True)

    with pytest.raises(CommandExecutionError):
        history.execute(failing)

    assert history.is_dirty is False
    assert history.can_undo is False
    assert history.can_redo is False


def test_command_history_undo_failure_preserves_cursor() -> None:
    history = CommandHistory()
    command = RecordingCommand("Fail undo", fail_undo=True)
    history.execute(command)

    with pytest.raises(CommandUndoError):
        history.undo()

    assert history.can_undo is True
    assert history.can_redo is False
    assert history.undo_description == "Fail undo"


def test_command_history_redo_failure_preserves_cursor() -> None:
    history = CommandHistory()
    command = RecordingCommand("Fail redo", fail_redo=True)
    history.execute(command)
    history.undo()

    with pytest.raises(CommandRedoError):
        history.redo()

    assert history.can_undo is False
    assert history.can_redo is True
    assert history.redo_description == "Fail redo"


def test_compound_command_rejects_empty_children() -> None:
    with pytest.raises(CompoundCommandConfigurationError):
        CompoundCommand("Empty", [])


def test_compound_command_execute_undo_and_redo_order() -> None:
    events: list[str] = []
    command = CompoundCommand(
        "Compound",
        [
            RecordingCommand("First", events=events),
            RecordingCommand("Second", events=events),
        ],
        affected_pages=frozenset(),
    )

    change = command.execute()
    command.undo()
    command.redo()

    assert change.affected_pages == frozenset()
    assert events == [
        "execute:First",
        "execute:Second",
        "undo:Second",
        "undo:First",
        "redo:First",
        "redo:Second",
    ]


def test_compound_command_rolls_back_partial_execute_failure() -> None:
    events: list[str] = []
    command = CompoundCommand(
        "Compound",
        [
            RecordingCommand("First", events=events),
            RecordingCommand("Second", fail_execute=True, events=events),
        ],
    )

    with pytest.raises(RuntimeError, match="execute failed: Second"):
        command.execute()

    assert events == [
        "execute:First",
        "execute:Second",
        "undo:First",
    ]


def test_compound_command_raises_dedicated_error_when_rollback_fails() -> None:
    first = RecordingCommand("First", fail_undo=True)
    second = RecordingCommand("Second", fail_execute=True)
    command = CompoundCommand("Compound", [first, second])

    with pytest.raises(CompoundCommandRollbackError) as exc_info:
        command.execute()

    assert exc_info.value.command is command
    assert isinstance(exc_info.value.original_cause, RuntimeError)
    assert exc_info.value.rollback_command is first
    assert isinstance(exc_info.value.rollback_cause, RuntimeError)
