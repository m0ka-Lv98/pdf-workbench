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


class StateCommand(DocumentCommand):
    def __init__(
        self,
        description: str,
        state: list[str],
        *,
        token: str,
        fail_execute: bool = False,
        fail_undo: bool = False,
        fail_redo: bool = False,
        fail_rollback_redo: bool = False,
        fail_rollback_undo: bool = False,
    ) -> None:
        self.description = description
        self.affected_pages = frozenset({0})
        self._state = state
        self._token = token
        self._fail_execute = fail_execute
        self._fail_undo = fail_undo
        self._fail_redo = fail_redo
        self._fail_rollback_redo = fail_rollback_redo
        self._fail_rollback_undo = fail_rollback_undo

    def execute(self) -> CommandChange:
        if self._fail_execute:
            raise RuntimeError(f"execute failed: {self.description}")
        self._state.append(self._token)
        return CommandChange.from_command(self)

    def undo(self) -> CommandChange:
        if self._fail_undo:
            raise RuntimeError(f"undo failed: {self.description}")
        if self._fail_rollback_undo:
            self._fail_rollback_undo = False
            raise RuntimeError(f"rollback undo failed: {self.description}")
        removed = self._state.pop()
        assert removed == self._token
        return CommandChange.from_command(self)

    def redo(self) -> CommandChange:
        if self._fail_redo:
            raise RuntimeError(f"redo failed: {self.description}")
        if self._fail_rollback_redo:
            self._fail_rollback_redo = False
            raise RuntimeError(f"rollback redo failed: {self.description}")
        self._state.append(self._token)
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
    assert exc_info.value.operation == "execute"
    assert isinstance(exc_info.value.original_cause, RuntimeError)
    assert exc_info.value.rollback_command is first
    assert isinstance(exc_info.value.rollback_cause, RuntimeError)


def test_compound_command_undo_rolls_back_partially_undone_state() -> None:
    state: list[str] = []
    first = StateCommand("A", state, token="A", fail_undo=True)
    second = StateCommand("B", state, token="B")
    third = StateCommand("C", state, token="C")
    command = CompoundCommand("Compound", [first, second, third])
    command.execute()

    with pytest.raises(RuntimeError, match="undo failed: A"):
        command.undo()

    assert state == ["A", "B", "C"]


def test_compound_command_undo_rollback_failure_raises_dedicated_error() -> None:
    state: list[str] = []
    first = StateCommand("A", state, token="A", fail_undo=True)
    second = StateCommand("B", state, token="B", fail_rollback_redo=True)
    third = StateCommand("C", state, token="C")
    command = CompoundCommand("Compound", [first, second, third])
    command.execute()

    with pytest.raises(CompoundCommandRollbackError) as exc_info:
        command.undo()

    assert exc_info.value.operation == "undo"
    assert isinstance(exc_info.value.original_cause, RuntimeError)
    assert isinstance(exc_info.value.rollback_cause, RuntimeError)
    assert exc_info.value.rollback_command is second


def test_compound_command_redo_rolls_back_partially_redone_state() -> None:
    state: list[str] = []
    first = StateCommand("A", state, token="A")
    second = StateCommand("B", state, token="B")
    third = StateCommand("C", state, token="C", fail_redo=True)
    command = CompoundCommand("Compound", [first, second, third])
    command.execute()
    command.undo()
    assert state == []

    with pytest.raises(RuntimeError, match="redo failed: C"):
        command.redo()

    assert state == []


def test_compound_command_redo_rollback_failure_raises_dedicated_error() -> None:
    state: list[str] = []
    first = StateCommand("A", state, token="A")
    second = StateCommand("B", state, token="B")
    third = StateCommand("C", state, token="C", fail_redo=True)
    command = CompoundCommand("Compound", [first, second, third])
    command.execute()
    command.undo()
    second._fail_rollback_undo = True

    with pytest.raises(CompoundCommandRollbackError) as exc_info:
        command.redo()

    assert exc_info.value.operation == "redo"
    assert isinstance(exc_info.value.original_cause, RuntimeError)
    assert isinstance(exc_info.value.rollback_cause, RuntimeError)
    assert exc_info.value.rollback_command is second


def test_command_history_undo_failure_leaves_dirty_state_and_descriptions_unchanged() -> None:
    history = CommandHistory()
    history.execute(RecordingCommand("Works"))
    history.mark_clean()
    history.execute(RecordingCommand("Fail undo", fail_undo=True))

    assert history.is_dirty is True
    assert history.undo_description == "Fail undo"
    assert history.redo_description is None

    with pytest.raises(CommandUndoError):
        history.undo()

    assert history.is_dirty is True
    assert history.can_undo is True
    assert history.can_redo is False
    assert history.undo_description == "Fail undo"
    assert history.redo_description is None


def test_command_history_redo_failure_leaves_dirty_state_and_descriptions_unchanged() -> None:
    history = CommandHistory()
    history.execute(RecordingCommand("Fail redo", fail_redo=True))
    history.mark_clean()
    history.undo()

    assert history.is_dirty is True
    assert history.redo_description == "Fail redo"
    assert history.undo_description is None

    with pytest.raises(CommandRedoError):
        history.redo()

    assert history.is_dirty is True
    assert history.can_undo is False
    assert history.can_redo is True
    assert history.undo_description is None
    assert history.redo_description == "Fail redo"
