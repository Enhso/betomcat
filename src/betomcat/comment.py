"""Private-comment and audit-report rendering (contracts.md s E).

All three renderers are pure functions over pipeline state -- nothing here
calls out to anything, the LLM included. The posted comment takes one of two
forms, both under the same hard character cap: `render_synthesized_comment`
(the header plus one rationale paragraph, written elsewhere by
`rationale.synthesize_rationale` and passed in) and `render_comment`, the
fallback when no rationale is available (the header plus one short summary
line per model, so a comment is never missing). `render_report` renders the
full audit trail (every rationale, every claim, no truncation) for the ledger
only -- it is never posted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from betomcat.pool import DrawResult
from betomcat.research import ClaimView, HistoryItem

COMMENT_MAX_CHARS = 2600


def format_value(value: float | dict[str, float] | list[float]) -> str:
    """Render a per-model forecast value for the comment's Forecasts section."""
    if isinstance(value, dict):
        return ", ".join(f"{option}: {p * 100:.1f}%" for option, p in value.items())
    if isinstance(value, list):
        return f"cdf [{len(value)} pts]"
    return f"{value * 100:.2f}%"


@dataclass(frozen=True)
class ModelForecastInfo:
    model_id: str
    value: float | dict[str, float] | list[float]
    summary: str
    rationale: str


@dataclass(frozen=True)
class CommentState:
    """Everything `render_comment` needs, gathered from one pipeline run."""

    bot_version: str
    kind: str  # "provisional" | "final"
    timestamp: str
    degraded: bool
    family_id: str | None
    family_label: str | None
    family_decision: str
    family_probability: float | None
    draw: DrawResult
    model_forecasts: list[ModelForecastInfo]
    arithmetic: str
    pacing_note: str | None = None
    referee_type: str | None = None
    history: list[HistoryItem] = field(default_factory=list)
    claims: list[ClaimView] = field(default_factory=list)


def render_report(state: CommentState) -> str:
    """Render the full audit report: every rationale, every claim, no cap.

    Never posted to Metaculus -- stored via `Ledger.record_submission`'s
    `report` column, inside the encrypted state snapshot, for later review.
    """
    lines: list[str] = []
    lines.append(f"# betomcat v{state.bot_version} -- {state.kind} forecast")
    lines.append(f"Submitted: {state.timestamp} UTC")
    if state.degraded:
        lines.append(
            "**Degraded mode: Intelligence Workbench was unavailable; research "
            "fell back to direct AskNews (or proceeded question-only).**"
        )

    lines.append("\n## Family")
    if state.family_id:
        prob = (
            f", p={state.family_probability:.2f}"
            if state.family_probability is not None
            else ""
        )
        lines.append(
            f"- {state.family_label} (`{state.family_id}`), "
            f"decision={state.family_decision}{prob}"
        )
    else:
        lines.append(f"- none (decision={state.family_decision})")

    lines.append("\n## Draw")
    lines.append(f"- Models: {', '.join(state.draw.models)}")
    weights_str = ", ".join(f"{m}={w:.4f}" for m, w in state.draw.weights.items())
    lines.append(f"- Frozen weights: {weights_str}")
    lines.append(f"- Pool average: {state.draw.pool_avg:.4f}")
    lines.append(f"- Second-draw fallback fired: {state.draw.fallback_fired}")
    if state.pacing_note:
        lines.append(f"- {state.pacing_note}")

    lines.append("\n## Forecasts")
    for mf in state.model_forecasts:
        lines.append(f"- **{mf.model_id}**: {format_value(mf.value)}")
    lines.append(f"- Weighted-average arithmetic: {state.arithmetic}")

    if state.referee_type:
        lines.append("\n## Disagreement")
        lines.append(f"- {state.referee_type}")

    lines.append("\n## Personal-history matches")
    if state.history:
        for h in state.history:
            lines.append(
                f"- {h.title}: forecast={h.forecast!r}, "
                f"resolution={h.resolution or 'unresolved'} ({h.url})"
            )
    else:
        lines.append("- none")

    lines.append("\n## Claims used")
    if state.claims:
        for c in state.claims:
            support = "n/a" if c.support is None else f"{c.support:.2f}"
            lines.append(f"- [support={support}] {c.text}")
            for ev in c.evidence:
                lines.append(
                    f"    - {ev.title} ({ev.provider}, published "
                    f"{ev.published or 'unknown'}, fetched {ev.fetched_at}) "
                    f"{ev.url} [hash {ev.content_hash[:12]}]"
                )
    else:
        lines.append("- none")

    lines.append("\n## Rationales")
    for mf in state.model_forecasts:
        lines.append(f"### {mf.model_id}")
        lines.append(mf.rationale)

    return "\n".join(lines)


def _comment_bullet(mf: ModelForecastInfo) -> str:
    value_part = "" if isinstance(mf.value, list) else f" ({format_value(mf.value)})"
    return f"- **{mf.model_id}**{value_part}: {mf.summary}"


def _comment_header(state: CommentState) -> str:
    header = f"betomcat v{state.bot_version}, {state.kind} forecast"
    if state.degraded:
        header += ", degraded research"
    return header


def _cap_comment(text: str) -> str:
    if len(text) > COMMENT_MAX_CHARS:
        return text[: COMMENT_MAX_CHARS - 4] + " ..."
    return text


def render_comment(state: CommentState) -> str:
    """Render the fallback comment: header + one summary bullet per model.

    Posted only when no synthesized rationale is available. Metaculus
    penalizes long comments (Hatim, 2026-09-24), so this carries only each
    model's self-written summary line (already capped at 60 words by
    `forecast.extract_summary`). `COMMENT_MAX_CHARS` is a backstop only.
    """
    lines = [_comment_header(state)] + [
        _comment_bullet(mf) for mf in state.model_forecasts
    ]
    return _cap_comment("\n".join(lines))


def render_synthesized_comment(state: CommentState, rationale: str) -> str:
    """Render the comment as the header, a blank line, and one rationale paragraph.

    Args:
        state: The pipeline state the header is drawn from.
        rationale: The synthesized rationale (at most 300 words). Its length is
            not checked here; `COMMENT_MAX_CHARS` is a backstop only.
    """
    return _cap_comment(f"{_comment_header(state)}\n\n{rationale}")
