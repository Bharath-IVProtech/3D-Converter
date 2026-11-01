"""Synchronous, typed publish/subscribe."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class ElementsChanged:
    added: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    storeys: bool = False  # levels were added, removed, renamed or moved
    systems: bool = False  # MEP systems / circuits changed
    issues: bool = False  # coordination issues changed

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.removed or self.storeys or self.systems or self.issues)


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable[[Any], None]]] = defaultdict(list)

    def subscribe(self, event_type: type[T], handler: Callable[[T], None]) -> Callable[[], None]:
        """Register a handler; returns a function that unsubscribes it."""
        self._handlers[event_type].append(handler)
        return lambda: self._handlers[event_type].remove(handler)

    def publish(self, event: object) -> None:
        for handler in list(self._handlers[type(event)]):
            handler(event)
