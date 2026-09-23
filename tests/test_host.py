"""Tests for host.py: shift timing, successor dispatch, log redaction."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import orjson
import pytest
from pytest_httpx import HTTPXMock

from betomcat.host import (
    _dispatch_successor,
    _MutableFlag,
    _RedactingFilter,
    _shift_margins,
    _shift_timer,
    configure_host_logging,
)

REPO = "Enhso/betomcat"


class FakeClock:
    """Mirrors test_pipeline.py's FakeClock: `sleep` advances virtual time."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        await asyncio.sleep(0)


def test_shift_margins_use_fixed_values_for_a_production_length_shift() -> None:
    claim_margin, drain_margin = _shift_margins(330)
    assert claim_margin == timedelta(minutes=25)
    assert drain_margin == timedelta(minutes=5)


def test_shift_margins_scale_down_for_a_short_shift() -> None:
    """A 3min shift under the old fixed 25/5min margins would never claim
    anything -- both cutoffs would already be in the past at shift start."""
    claim_margin, drain_margin = _shift_margins(3)
    assert claim_margin == timedelta(minutes=3) * 0.3
    assert drain_margin == timedelta(minutes=3) * 0.1
    assert claim_margin < timedelta(minutes=3)
    assert drain_margin < claim_margin


async def test_shift_timer_flips_claim_then_drain_in_order() -> None:
    clock = FakeClock(datetime(2026, 9, 23, tzinfo=UTC))
    claim_stop_at = clock.now + timedelta(minutes=25)
    drain_stop_at = clock.now + timedelta(minutes=30)
    stop_claiming = _MutableFlag()
    stop_event = asyncio.Event()

    await asyncio.wait_for(
        _shift_timer(
            claim_stop_at=claim_stop_at,
            drain_stop_at=drain_stop_at,
            stop_claiming=stop_claiming,
            stop_event=stop_event,
            clock=lambda: clock.now,
            sleep=clock.sleep,
        ),
        timeout=5,
    )

    assert stop_claiming.value is True
    assert stop_event.is_set()
    assert clock.now >= drain_stop_at


async def test_shift_timer_stops_immediately_if_stop_event_already_set() -> None:
    """E.g. --max-questions already tripped `stop_event` before the timer runs."""
    start = datetime(2026, 9, 23, tzinfo=UTC)
    clock = FakeClock(start)
    stop_claiming = _MutableFlag()
    stop_event = asyncio.Event()
    stop_event.set()

    await asyncio.wait_for(
        _shift_timer(
            claim_stop_at=start + timedelta(minutes=25),
            drain_stop_at=start + timedelta(minutes=30),
            stop_claiming=stop_claiming,
            stop_event=stop_event,
            clock=lambda: clock.now,
            sleep=clock.sleep,
        ),
        timeout=5,
    )

    assert stop_claiming.value is True
    assert clock.now == start  # neither wait loop slept


async def test_dispatch_successor_posts_and_writes_flag(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"https://api.github.com/repos/{REPO}/actions/workflows/host.yml/dispatches",
        status_code=204,
    )
    flag_path = tmp_path / ".betomcat-dispatched"

    async with httpx.AsyncClient() as client:
        await _dispatch_successor(REPO, "gh-token", client, flag_path)

    assert flag_path.exists()
    request = httpx_mock.get_requests()[0]
    assert request.headers["authorization"] == "Bearer gh-token"
    assert orjson.loads(request.content) == {"ref": "main"}


def test_redacting_filter_blanks_sensitive_loggers() -> None:
    filt = _RedactingFilter()
    record = logging.LogRecord(
        name="betomcat.metaculus",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="[dry-run] would post binary %.4f on question %s",
        args=(0.87, 123),
        exc_info=None,
    )

    assert filt.filter(record) is True
    assert "0.87" not in record.getMessage()
    assert record.args == ()


def test_redacting_filter_leaves_other_loggers_alone() -> None:
    filt = _RedactingFilter()
    record = logging.LogRecord(
        name="betomcat.daemon",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="question %s finished: status=%s",
        args=("metaculus:1", "submitted"),
        exc_info=None,
    )

    assert filt.filter(record) is True
    assert record.getMessage() == "question metaculus:1 finished: status=submitted"


def test_configure_host_logging_quiets_chatty_loggers() -> None:
    configure_host_logging()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert logging.getLogger("forecasting_tools").level == logging.WARNING
    assert logging.getLogger("litellm").level == logging.WARNING


def test_configure_host_logging_actually_redacts_through_real_logging_calls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression test: a filter attached to the *root* logger is silently
    a no-op for records from child loggers (they reach root's handlers
    without ever passing through root's own `.filter()`). This drives an
    actual `logger.info(...)` call end to end, the way host mode really
    logs, rather than calling `_RedactingFilter.filter()` directly."""
    configure_host_logging()
    sensitive_logger = logging.getLogger("betomcat.metaculus")
    safe_logger = logging.getLogger("betomcat.daemon")

    with caplog.at_level(logging.INFO):
        sensitive_logger.info(
            "[dry-run] would post binary %.4f on question %s", 0.87, 123
        )
        safe_logger.info("question %s finished: status=%s", "metaculus:1", "submitted")

    messages = [r.getMessage() for r in caplog.records]
    assert not any("0.87" in m for m in messages)
    assert any(m.startswith("[redacted betomcat.metaculus message") for m in messages)
    assert "question metaculus:1 finished: status=submitted" in messages
