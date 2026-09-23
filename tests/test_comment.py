"""Tests for private-comment rendering, including truncation (contracts.md s E)."""

from __future__ import annotations

from betomcat.comment import (
    MAX_COMMENT_CHARS,
    CommentState,
    ModelForecastInfo,
    format_value,
    render_comment,
)
from betomcat.pool import DrawResult
from betomcat.research import ClaimView, Evidence, HistoryItem


def _draw() -> DrawResult:
    return DrawResult(
        models=["model-a", "model-b"],
        weights={"model-a": 1.10, "model-b": 0.95},
        pool_avg=1.0,
        fallback_fired=False,
    )


def _state(**overrides: object) -> CommentState:
    base: dict[str, object] = dict(
        bot_version="1.0.0",
        kind="final",
        timestamp="2026-09-22T19:30:00Z",
        degraded=False,
        family_id="fam:ecb-rate-decisions",
        family_label="ECB rate decisions",
        family_decision="matched",
        family_probability=0.83,
        draw=_draw(),
        model_forecasts=[
            ModelForecastInfo("model-a", 0.62, "Reasoning A " * 5),
            ModelForecastInfo("model-b", 0.48, "Reasoning B " * 5),
        ],
        arithmetic="(0.62*1.10 + 0.48*0.95) / (1.10+0.95) = 0.555",
        referee_type=None,
        history=[],
        claims=[],
    )
    base.update(overrides)
    return CommentState(**base)  # type: ignore[arg-type]


def test_format_value_binary() -> None:
    assert format_value(0.6234) == "62.34%"


def test_format_value_multiple_choice() -> None:
    text = format_value({"Yes": 0.7, "No": 0.3})
    assert "Yes: 70.0%" in text
    assert "No: 30.0%" in text


def test_format_value_numeric() -> None:
    assert format_value([0.0, 0.5, 1.0]) == "cdf [3 pts]"


def test_render_comment_includes_all_sections() -> None:
    state = _state()

    text = render_comment(state)

    assert "betomcat v1.0.0 -- final forecast" in text
    assert "ECB rate decisions" in text
    assert "fam:ecb-rate-decisions" in text
    assert "model-a" in text and "model-b" in text
    assert "1.1000" in text  # frozen weight
    assert "0.555" in text  # arithmetic
    assert "Second-draw fallback fired: False" in text
    assert "## Rationales" in text
    assert len(text) <= MAX_COMMENT_CHARS


def test_render_comment_degraded_flag() -> None:
    text = render_comment(_state(degraded=True))
    assert "Degraded mode" in text


def test_render_comment_referee_section() -> None:
    text = render_comment(_state(referee_type="stale_info"))
    assert "## Disagreement" in text
    assert "stale_info" in text


def test_render_comment_no_referee_section_when_none() -> None:
    text = render_comment(_state(referee_type=None))
    assert "## Disagreement" not in text


def test_render_comment_history_and_claims() -> None:
    history = [
        HistoryItem(
            id="metaculus:100",
            kind="personal",
            title="Will Y happen?",
            url="https://metaculus.com/questions/100",
            question_type="binary",
            forecast=0.7,
            resolution="yes",
        )
    ]
    claims = [
        ClaimView(
            claim_id="c1",
            text="Inflation is falling.",
            kind="fact",
            support=0.9,
            support_method="jev",
            dossier_id="dos:1",
            evidence=[
                Evidence(
                    source_id="src:1",
                    title="CPI report",
                    url="https://example.com/cpi",
                    provider="asknews_news",
                    published="2026-09-01T00:00:00Z",
                    fetched_at="2026-09-20T00:00:00Z",
                    content_hash="abc123def456",
                )
            ],
        )
    ]
    text = render_comment(_state(history=history, claims=claims))

    assert "Will Y happen?" in text
    assert "Inflation is falling." in text
    assert "CPI report" in text


def test_render_comment_truncates_rationales_before_claims() -> None:
    long_rationale = "word " * 5000  # ~25000 chars, forces rationale truncation
    claims = [
        ClaimView(
            claim_id=f"c{i}",
            text=f"Claim number {i} with some supporting detail text.",
            kind="fact",
            support=1.0 - i * 0.01,
            support_method="jev",
            dossier_id="dos:1",
            evidence=[],
        )
        for i in range(50)
    ]
    state = _state(
        model_forecasts=[
            ModelForecastInfo("model-a", 0.62, long_rationale),
            ModelForecastInfo("model-b", 0.48, long_rationale),
        ],
        claims=claims,
    )

    text = render_comment(state)

    assert len(text) <= MAX_COMMENT_CHARS
    # Rationales got truncated (the full 25000-char blob can't survive).
    assert "[truncated]" in text
    # At least some claims should have survived given the truncation order.
    assert "## Claims used" in text


def test_render_comment_extreme_truncation_still_fits() -> None:
    # So many huge claims that even dropping the rationale to its floor and
    # every claim still risks overflow -- render_comment must still return a
    # string within budget (hard-truncating as a last resort).
    huge_claim_text = "x" * 500
    claims = [
        ClaimView(
            claim_id=f"c{i}",
            text=huge_claim_text,
            kind="fact",
            support=0.5,
            support_method="jev",
            dossier_id="dos:1",
            evidence=[],
        )
        for i in range(200)
    ]
    state = _state(
        model_forecasts=[
            ModelForecastInfo("model-a", 0.62, "reason " * 3000),
            ModelForecastInfo("model-b", 0.48, "reason " * 3000),
        ],
        claims=claims,
    )

    text = render_comment(state)

    assert len(text) <= MAX_COMMENT_CHARS
