from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class CommandChange:
    affected_pages: frozenset[int] | None

    @classmethod
    def from_command(cls, command: DocumentCommand) -> CommandChange:
        return cls(affected_pages=command.affected_pages)


class DocumentCommand(ABC):
    description: str
    affected_pages: frozenset[int] | None

    @abstractmethod
    def execute(self) -> CommandChange | None:
        """Apply this command atomically.

        On success the whole operation must be applied. On failure the document state must
        remain exactly as it was before the call. Partial mutation is not allowed.
        CompoundCommand satisfies this contract by rolling child commands back when needed.
        """

    @abstractmethod
    def undo(self) -> CommandChange | None:
        """Undo this command atomically.

        On success the whole operation must be reverted. On failure the document state must
        remain exactly as it was before the call. Partial mutation is not allowed.
        CompoundCommand satisfies this contract by rolling child commands back when needed.
        """

    def redo(self) -> CommandChange | None:
        """Redo this command atomically.

        On success the whole operation must be re-applied. On failure the document state must
        remain exactly as it was before the call. Partial mutation is not allowed.
        CompoundCommand satisfies this contract by rolling child commands back when needed.
        Commands may override this when re-executing differs from the initial execute step.
        """

        return self.execute()


class CommandHistoryError(Exception):
    """Base class for command-history errors."""


class CommandExecutionError(CommandHistoryError):
    def __init__(self, command: DocumentCommand, cause: Exception) -> None:
        super().__init__(f"Command execution failed: {command.description}")
        self.command = command
        self.cause = cause


class CommandUndoError(CommandHistoryError):
    def __init__(self, command: DocumentCommand, cause: Exception) -> None:
        super().__init__(f"Command undo failed: {command.description}")
        self.command = command
        self.cause = cause


class CommandRedoError(CommandHistoryError):
    def __init__(self, command: DocumentCommand, cause: Exception) -> None:
        super().__init__(f"Command redo failed: {command.description}")
        self.command = command
        self.cause = cause


class CompoundCommandConfigurationError(CommandHistoryError):
    """Raised when a compound command is constructed with invalid children."""


class CompoundCommandRollbackError(CommandHistoryError):
    def __init__(
        self,
        command: CompoundCommand,
        *,
        operation: Literal["execute", "undo", "redo"],
        original_cause: Exception,
        rollback_command: DocumentCommand,
        rollback_cause: Exception,
    ) -> None:
        super().__init__(f"Compound command {operation} rollback failed: {command.description}")
        self.command = command
        self.operation = operation
        self.original_cause = original_cause
        self.rollback_command = rollback_command
        self.rollback_cause = rollback_cause


class CompoundCommand(DocumentCommand):
    def __init__(
        self,
        description: str,
        commands: tuple[DocumentCommand, ...] | list[DocumentCommand],
        *,
        affected_pages: frozenset[int] | None = None,
    ) -> None:
        if not commands:
            raise CompoundCommandConfigurationError("CompoundCommand requires at least one child")
        self.description = description
        self.affected_pages = affected_pages
        self._commands = tuple(commands)

    def execute(self) -> CommandChange:
        executed: list[DocumentCommand] = []
        try:
            for command in self._commands:
                command.execute()
                executed.append(command)
        except Exception as exc:
            for rollback_command in reversed(executed):
                try:
                    rollback_command.undo()
                except Exception as rollback_exc:
                    raise CompoundCommandRollbackError(
                        self,
                        operation="execute",
                        original_cause=exc,
                        rollback_command=rollback_command,
                        rollback_cause=rollback_exc,
                    ) from rollback_exc
            raise exc
        return CommandChange.from_command(self)

    def undo(self) -> CommandChange:
        undone: list[DocumentCommand] = []
        try:
            for command in reversed(self._commands):
                command.undo()
                undone.append(command)
        except Exception as exc:
            for rollback_command in reversed(undone):
                try:
                    rollback_command.redo()
                except Exception as rollback_exc:
                    raise CompoundCommandRollbackError(
                        self,
                        operation="undo",
                        original_cause=exc,
                        rollback_command=rollback_command,
                        rollback_cause=rollback_exc,
                    ) from rollback_exc
            raise exc
        return CommandChange.from_command(self)

    def redo(self) -> CommandChange:
        redone: list[DocumentCommand] = []
        try:
            for command in self._commands:
                command.redo()
                redone.append(command)
        except Exception as exc:
            for rollback_command in reversed(redone):
                try:
                    rollback_command.undo()
                except Exception as rollback_exc:
                    raise CompoundCommandRollbackError(
                        self,
                        operation="redo",
                        original_cause=exc,
                        rollback_command=rollback_command,
                        rollback_cause=rollback_exc,
                    ) from rollback_exc
            raise exc
        return CommandChange.from_command(self)


class CommandHistory:
    def __init__(self, *, initially_dirty: bool = False) -> None:
        self._commands: list[DocumentCommand] = []
        self._cursor = 0
        self._clean_cursor: int | None = None if initially_dirty else 0

    @property
    def can_undo(self) -> bool:
        return self._cursor > 0

    @property
    def can_redo(self) -> bool:
        return self._cursor < len(self._commands)

    @property
    def undo_description(self) -> str | None:
        if not self.can_undo:
            return None
        return self._commands[self._cursor - 1].description

    @property
    def redo_description(self) -> str | None:
        if not self.can_redo:
            return None
        return self._commands[self._cursor].description

    @property
    def is_dirty(self) -> bool:
        return self._clean_cursor is None or self._cursor != self._clean_cursor

    def execute(self, command: DocumentCommand) -> CommandChange:
        try:
            change = command.execute()
        except Exception as exc:
            raise CommandExecutionError(command, exc) from exc

        if self.can_redo:
            if self._clean_cursor is not None and self._clean_cursor > self._cursor:
                self._clean_cursor = None
            del self._commands[self._cursor :]

        self._commands.append(command)
        self._cursor += 1
        return change if change is not None else CommandChange.from_command(command)

    def undo(self) -> CommandChange:
        if not self.can_undo:
            raise IndexError("No command to undo")

        command = self._commands[self._cursor - 1]
        try:
            change = command.undo()
        except Exception as exc:
            raise CommandUndoError(command, exc) from exc

        self._cursor -= 1
        return change if change is not None else CommandChange.from_command(command)

    def redo(self) -> CommandChange:
        if not self.can_redo:
            raise IndexError("No command to redo")

        command = self._commands[self._cursor]
        try:
            change = command.redo()
        except Exception as exc:
            raise CommandRedoError(command, exc) from exc

        self._cursor += 1
        return change if change is not None else CommandChange.from_command(command)

    def mark_clean(self) -> None:
        self._clean_cursor = self._cursor
