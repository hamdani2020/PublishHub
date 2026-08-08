"""
In-memory stand-in for the Redis commands the queue client uses, so backend tests
exercise the real client code without a Redis server.

List orientation matches Redis: index 0 is the head (`LPUSH` side) and the last
element is the tail (`RPOPLPUSH` side), which is what makes the queue FIFO.

Mirrors `apps/api/src/queue/testing/fake-redis.ts`, plus the sorted-set commands
the Python-only reaper needs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


class FakeRedis:
    """Implements the `RedisCommands` protocol against dictionaries."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.sorted_sets: dict[str, dict[str, float]] = {}
        #: Every command in order, for asserting sequences such as push-then-remove.
        self.calls: list[tuple[Any, ...]] = []
        self.closed = False

    # --- helpers used by tests -------------------------------------------------

    def contents(self, key: str) -> list[str]:
        """Contents head-first, the way `LRANGE key 0 -1` would report them."""
        return list(self._list(key))

    def scores(self, key: str) -> dict[str, float]:
        return dict(self._sorted_set(key))

    def _list(self, key: str) -> list[str]:
        return self.lists.setdefault(key, [])

    def _sorted_set(self, key: str) -> dict[str, float]:
        return self.sorted_sets.setdefault(key, {})

    # --- list commands ---------------------------------------------------------

    def lpush(self, name: str, *values: str) -> int:
        self.calls.append(("lpush", name, *values))
        target = self._list(name)
        for value in values:
            target.insert(0, value)
        return len(target)

    def rpoplpush(self, src: str, dst: str) -> str | None:
        self.calls.append(("rpoplpush", src, dst))
        source = self._list(src)
        if not source:
            return None
        value = source.pop()
        self._list(dst).insert(0, value)
        return value

    def brpoplpush(self, src: str, dst: str, timeout: int = 0) -> str | None:
        self.calls.append(("brpoplpush", src, dst, timeout))
        source = self._list(src)
        if not source:
            # A real Redis would block for `timeout` seconds and then return
            # None; the fake reports the same outcome immediately.
            return None
        value = source.pop()
        self._list(dst).insert(0, value)
        return value

    def lrem(self, name: str, count: int, value: str) -> int:
        self.calls.append(("lrem", name, count, value))
        target = self._list(name)
        limit = math.inf if count == 0 else abs(count)
        removed = 0
        index = 0
        while index < len(target) and removed < limit:
            if target[index] == value:
                del target[index]
                removed += 1
            else:
                index += 1
        return removed

    def llen(self, name: str) -> int:
        self.calls.append(("llen", name))
        return len(self._list(name))

    def lrange(self, name: str, start: int, end: int) -> list[str]:
        self.calls.append(("lrange", name, start, end))
        target = self._list(name)
        stop = len(target) if end == -1 else end + 1
        return target[start:stop]

    # --- sorted set commands ---------------------------------------------------

    def zadd(self, name: str, mapping: Mapping[str, float]) -> int:
        self.calls.append(("zadd", name, dict(mapping)))
        target = self._sorted_set(name)
        added = sum(1 for member in mapping if member not in target)
        target.update(mapping)
        return added

    def zrem(self, name: str, *values: str) -> int:
        self.calls.append(("zrem", name, *values))
        target = self._sorted_set(name)
        return sum(1 for value in values if target.pop(value, None) is not None)

    def zrangebyscore(self, name: str, min: float | str, max: float | str) -> list[str]:
        self.calls.append(("zrangebyscore", name, min, max))
        low = _bound(min, default=-math.inf)
        high = _bound(max, default=math.inf)
        members = [
            (score, member)
            for member, score in self._sorted_set(name).items()
            if low <= score <= high
        ]
        # Real Redis orders by score, then lexicographically by member.
        members.sort()
        return [member for _, member in members]

    # --- connection ------------------------------------------------------------

    def close(self) -> None:
        self.calls.append(("close",))
        self.closed = True


def _bound(value: float | str, *, default: float) -> float:
    """Accepts the `-inf` / `+inf` strings redis-py passes through verbatim."""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"-inf", "inf", "+inf"}:
            return -math.inf if text == "-inf" else math.inf
        return float(text)
    return float(value) if value is not None else default
