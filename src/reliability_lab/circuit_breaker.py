from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit is open and calls should fail fast."""


@dataclass(slots=True)
class CircuitBreaker:
    """Circuit breaker skeleton.

    TODO(student): Implement a production-safe state machine:
    - CLOSED: calls pass through; count failures.
    - OPEN: fail fast until reset timeout elapses.
    - HALF_OPEN: allow a probe; close on success or re-open on failure.
    """

    name: str
    failure_threshold: int
    reset_timeout_seconds: float
    success_threshold: int = 1
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    opened_at: float | None = None
    transition_log: list[dict[str, str | float]] = field(default_factory=list)

    def allow_request(self) -> bool:
        """Return whether a request should be attempted."""
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.HALF_OPEN:
            return True
        # OPEN: kiểm tra xem timeout đã hết chưa
        if self.opened_at is not None:
            elapsed = time.monotonic() - self.opened_at
            if elapsed >= self.reset_timeout_seconds:
                self._transition(CircuitState.HALF_OPEN, "timeout_elapsed")
                return True
        return False

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        """Call a function through the circuit breaker."""
        if not self.allow_request():
            raise CircuitOpenError(f"Circuit '{self.name}' is OPEN")
        try:
            result = fn(*args, **kwargs)
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise

    def record_success(self) -> None:
        """Record a successful call."""
        self.failure_count = 0
        self.success_count += 1
        if self.state == CircuitState.HALF_OPEN and self.success_count >= self.success_threshold:
            self._transition(CircuitState.CLOSED, "probe_success")
            self.success_count = 0

    def record_failure(self) -> None:
        """Record a failed call."""
        self.failure_count += 1
        self.success_count = 0
        if self.state == CircuitState.HALF_OPEN:
            self._transition(CircuitState.OPEN, "probe_failure")
            self.opened_at = time.monotonic()
        elif self.failure_count >= self.failure_threshold:
            self._transition(CircuitState.OPEN, "failure_threshold_reached")
            self.opened_at = time.monotonic()

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        if self.state == new_state:
            return
        self.transition_log.append(
            {"from": self.state.value, "to": new_state.value, "reason": reason, "ts": time.time()}
        )
        self.state = new_state


class RedisCircuitBreaker:
    """Circuit breaker backed by Redis for multi-instance state sharing.

    Multiple gateway instances share the same circuit state — one instance's
    failures are visible to all instances immediately via Redis.

    Data model:
        Key  = "cb:{name}"  (Redis Hash)
        Fields: state, failure_count, success_count, opened_at
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int,
        reset_timeout_seconds: float,
        success_threshold: int = 1,
        redis_url: str = "redis://localhost:6379/0",
    ) -> None:
        import redis as redis_lib

        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout_seconds = reset_timeout_seconds
        self.success_threshold = success_threshold
        self._redis = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        self._key = f"cb:{name}"
        self.transition_log: list[dict[str, str | float]] = []
        # Initialize fields only if key doesn't exist
        self._redis.hsetnx(self._key, "state", CircuitState.CLOSED.value)
        self._redis.hsetnx(self._key, "failure_count", "0")
        self._redis.hsetnx(self._key, "success_count", "0")

    @property
    def state(self) -> CircuitState:
        return CircuitState(self._redis.hget(self._key, "state") or "closed")

    def allow_request(self) -> bool:
        s = self.state
        if s == CircuitState.CLOSED:
            return True
        if s == CircuitState.HALF_OPEN:
            return True
        opened_at_str: str | None = self._redis.hget(self._key, "opened_at") or None  # type: ignore[assignment]
        if opened_at_str and time.monotonic() - float(opened_at_str) >= self.reset_timeout_seconds:
            self._transition(CircuitState.HALF_OPEN, "timeout_elapsed")
            return True
        return False

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        if not self.allow_request():
            raise CircuitOpenError(f"Circuit '{self.name}' is OPEN")
        try:
            result = fn(*args, **kwargs)
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise

    def record_success(self) -> None:
        self._redis.hset(self._key, "failure_count", "0")
        success_count = int(self._redis.hincrby(self._key, "success_count", 1))
        if self.state == CircuitState.HALF_OPEN and success_count >= self.success_threshold:
            self._transition(CircuitState.CLOSED, "probe_success")
            self._redis.hset(self._key, "success_count", "0")

    def record_failure(self) -> None:
        failure_count = int(self._redis.hincrby(self._key, "failure_count", 1))
        self._redis.hset(self._key, "success_count", "0")
        if self.state == CircuitState.HALF_OPEN:
            self._transition(CircuitState.OPEN, "probe_failure")
            self._redis.hset(self._key, "opened_at", str(time.monotonic()))
        elif failure_count >= self.failure_threshold:
            self._transition(CircuitState.OPEN, "failure_threshold_reached")
            self._redis.hset(self._key, "opened_at", str(time.monotonic()))

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        current = self.state
        if current == new_state:
            return
        self.transition_log.append(
            {"from": current.value, "to": new_state.value, "reason": reason, "ts": time.time()}
        )
        self._redis.hset(self._key, "state", new_state.value)

    def flush(self) -> None:
        """Reset circuit state in Redis (for testing)."""
        self._redis.delete(self._key)
