"""Collects errors, warnings and notes produced while validating and converting a file."""
from __future__ import annotations

from dataclasses import dataclass

ERROR = "error"
WARNING = "warning"
INFO = "info"


class ConversionError(Exception):
    """Raised when a chart cannot be converted without losing timing accuracy."""


@dataclass
class Diagnostic:
    level: str
    message: str
    line: int | None
    scope: str
    count: int = 1


class Diagnostics:
    def __init__(self) -> None:
        self.items: list[Diagnostic] = []
        self._index: dict[tuple[str, str, str], Diagnostic] = {}

    def add(self, level: str, message: str, line: int | None = None, scope: str = "") -> None:
        # Identical messages are merged so that e.g. a repeated invalid symbol is reported once.
        key = (level, message, scope)
        existing = self._index.get(key)
        if existing is not None:
            existing.count += 1
            return
        item = Diagnostic(level, message, line, scope)
        self._index[key] = item
        self.items.append(item)

    def error(self, message: str, line: int | None = None, scope: str = "") -> None:
        self.add(ERROR, message, line, scope)

    def warning(self, message: str, line: int | None = None, scope: str = "") -> None:
        self.add(WARNING, message, line, scope)

    def info(self, message: str, line: int | None = None, scope: str = "") -> None:
        self.add(INFO, message, line, scope)

    def scoped(self, scope: str) -> "ScopedDiagnostics":
        return ScopedDiagnostics(self, scope)

    def has_errors(self, scope: str | None = None) -> bool:
        return any(d.level == ERROR and (scope is None or d.scope == scope) for d in self.items)

    def promote_warnings(self) -> None:
        """--strict: treat every warning as an error."""
        for item in self.items:
            if item.level == WARNING:
                item.level = ERROR

    def format(self, verbose: bool) -> list[str]:
        lines = []
        for d in self.items:
            if d.level == INFO and not verbose:
                continue
            where = []
            if d.scope:
                where.append(f"[{d.scope}]")
            if d.line is not None:
                where.append(f"line {d.line}:")
            suffix = f" (x{d.count})" if d.count > 1 else ""
            lines.append(f"  {d.level:<7} {' '.join(where)} {d.message}{suffix}".rstrip())
        return lines


class ScopedDiagnostics:
    """Diagnostics bound to a scope such as a difficulty name."""

    def __init__(self, parent: Diagnostics, scope: str) -> None:
        self.parent = parent
        self.scope = scope

    def error(self, message: str, line: int | None = None) -> None:
        self.parent.error(message, line, self.scope)

    def warning(self, message: str, line: int | None = None) -> None:
        self.parent.warning(message, line, self.scope)

    def info(self, message: str, line: int | None = None) -> None:
        self.parent.info(message, line, self.scope)
