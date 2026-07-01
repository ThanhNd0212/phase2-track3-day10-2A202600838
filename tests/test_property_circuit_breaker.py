"""Property-based tests for CircuitBreaker using hypothesis.

Fuzz state transitions to verify invariants hold for arbitrary inputs.
"""
from __future__ import annotations

import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


@given(
    failures=st.integers(min_value=0, max_value=20),
    threshold=st.integers(min_value=1, max_value=10),
)
@settings(max_examples=200)
def test_opens_exactly_at_threshold(failures: int, threshold: int) -> None:
    """Circuit must be OPEN iff failures >= threshold, CLOSED otherwise."""
    cb = CircuitBreaker("prop", failure_threshold=threshold, reset_timeout_seconds=60)
    for _ in range(failures):
        if cb.state == CircuitState.CLOSED:
            cb.record_failure()
    if failures >= threshold:
        assert cb.state == CircuitState.OPEN
    else:
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == failures


@given(threshold=st.integers(min_value=1, max_value=5))
@settings(max_examples=100)
def test_success_resets_failure_count_invariant(threshold: int) -> None:
    """After any success in CLOSED state, failure_count must be 0."""
    cb = CircuitBreaker("prop", failure_threshold=threshold + 1, reset_timeout_seconds=60)
    for _ in range(threshold):
        cb.record_failure()
    cb.record_success()
    assert cb.failure_count == 0
    assert cb.state == CircuitState.CLOSED


@given(
    successes=st.integers(min_value=1, max_value=10),
    success_threshold=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=100)
def test_half_open_closes_after_success_threshold(successes: int, success_threshold: int) -> None:
    """HALF_OPEN must transition to CLOSED only when success_count >= success_threshold."""
    cb = CircuitBreaker(
        "prop",
        failure_threshold=1,
        reset_timeout_seconds=0.01,
        success_threshold=success_threshold,
    )
    cb.record_failure()
    time.sleep(0.02)
    cb.allow_request()  # triggers HALF_OPEN transition
    assert cb.state == CircuitState.HALF_OPEN

    for _ in range(successes):
        if cb.state == CircuitState.HALF_OPEN:
            cb.record_success()

    if successes >= success_threshold:
        assert cb.state == CircuitState.CLOSED
    else:
        assert cb.state == CircuitState.HALF_OPEN


@given(st.integers(min_value=1, max_value=10))
@settings(max_examples=50)
def test_open_denies_all_requests_before_timeout(failure_threshold: int) -> None:
    """Every allow_request() call on a freshly-opened circuit must return False."""
    cb = CircuitBreaker("prop", failure_threshold=failure_threshold, reset_timeout_seconds=60)
    for _ in range(failure_threshold):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN
    for _ in range(10):
        assert cb.allow_request() is False


@given(st.integers(min_value=1, max_value=5))
@settings(max_examples=50)
def test_half_open_failure_always_reopens(failure_threshold: int) -> None:
    """A failure in HALF_OPEN must always transition back to OPEN with reason probe_failure."""
    cb = CircuitBreaker("prop", failure_threshold=failure_threshold, reset_timeout_seconds=0.01)
    for _ in range(failure_threshold):  # đủ lần để mở circuit
        cb.record_failure()
    assert cb.state == CircuitState.OPEN
    time.sleep(0.02)
    cb.allow_request()  # → HALF_OPEN
    assert cb.state == CircuitState.HALF_OPEN
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.transition_log[-1]["reason"] == "probe_failure"
