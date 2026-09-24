"""Tests for private-comment and audit-report rendering (contracts.md s E)."""

from __future__ import annotations

from betomcat.comment import (
    COMMENT_MAX_CHARS,
    CommentState,
    ModelForecastInfo,
    format_value,
    render_comment,
    render_report,
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
            ModelForecastInfo(
                "model-a",
                0.62,
                "Anchored on the ECB base rate. " * 2,
                "Reasoning A " * 5,
            ),
            ModelForecastInfo(
                "model-b",
                0.48,
                "Weighed recent inflation prints. " * 2,
                "Reasoning B " * 5,
            ),
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


# -- render_comment (the short, posted comment) ---------------------------


def test_render_comment_header_and_bullets() -> None:
    state = _state()

    text = render_comment(state)

    assert text.startswith("betomcat v1.0.0, final forecast")
    assert "- **model-a** (62.00%): Anchored on the ECB base rate." in text
    assert "- **model-b** (48.00%): Weighed recent inflation prints." in text
    assert len(text) <= COMMENT_MAX_CHARS


def test_render_comment_degraded_flag_in_header() -> None:
    text = render_comment(_state(degraded=True))
    assert text.startswith("betomcat v1.0.0, final forecast, degraded research")


def test_render_comment_not_degraded_omits_flag() -> None:
    text = render_comment(_state(degraded=False))
    assert "degraded" not in text


def test_render_comment_omits_value_for_numeric() -> None:
    state = _state(
        model_forecasts=[
            ModelForecastInfo("model-a", [0.0, 0.5, 1.0], "A numeric summary.", "r"),
        ]
    )

    text = render_comment(state)

    assert "- **model-a**: A numeric summary." in text
    assert "cdf" not in text


def test_render_comment_excludes_report_only_sections() -> None:
    """The short comment carries no family/draw/claims/rationale detail."""
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
            evidence=[],
        )
    ]
    text = render_comment(_state(history=history, claims=claims))

    assert "ECB rate decisions" not in text
    assert "Inflation is falling." not in text
    assert "Will Y happen?" not in text
    assert "Reasoning A" not in text
    assert "## " not in text


def test_render_comment_backstop_cap_holds_for_oversized_input() -> None:
    huge_summary = "x" * 5000
    state = _state(
        model_forecasts=[
            ModelForecastInfo(f"model-{i}", 0.5, huge_summary, "r") for i in range(3)
        ]
    )

    text = render_comment(state)

    assert len(text) == COMMENT_MAX_CHARS
    assert text.endswith(" ...")


# -- render_report (the full audit trail, kept for the ledger only) -------


def test_render_report_includes_all_sections() -> None:
    state = _state()

    text = render_report(state)

    assert "betomcat v1.0.0 -- final forecast" in text
    assert "ECB rate decisions" in text
    assert "fam:ecb-rate-decisions" in text
    assert "model-a" in text and "model-b" in text
    assert "1.1000" in text  # frozen weight
    assert "0.555" in text  # arithmetic
    assert "Second-draw fallback fired: False" in text
    assert "## Rationales" in text
    assert "Reasoning A" in text


def test_render_report_degraded_flag() -> None:
    text = render_report(_state(degraded=True))
    assert "Degraded mode" in text


def test_render_report_referee_section() -> None:
    text = render_report(_state(referee_type="stale_info"))
    assert "## Disagreement" in text
    assert "stale_info" in text


def test_render_report_no_referee_section_when_none() -> None:
    text = render_report(_state(referee_type=None))
    assert "## Disagreement" not in text


def test_render_report_history_and_claims() -> None:
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
    text = render_report(_state(history=history, claims=claims))

    assert "Will Y happen?" in text
    assert "Inflation is falling." in text
    assert "CPI report" in text


def test_render_report_never_truncates_rationale_or_claims() -> None:
    long_rationale = "word " * 5000  # ~25000 chars
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
            ModelForecastInfo("model-a", 0.62, "s", long_rationale),
            ModelForecastInfo("model-b", 0.48, "s", long_rationale),
        ],
        claims=claims,
    )

    text = render_report(state)

    assert "[truncated]" not in text
    assert long_rationale in text
    for i in range(50):
        assert f"Claim number {i} with some supporting detail text." in text
