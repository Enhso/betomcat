"""Private-comment rendering (contracts.md s E, spec s9).

A pure function over pipeline state -- no LLM authorship, nothing here calls
out to anything. Kept under Metaculus's comment length limit by truncating
rationales first, then the claims list, in that order.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from betomcat.pool import DrawResult
from betomcat.research import ClaimView, HistoryItem

MAX_COMMENT_CHARS = 9500
_RATIONALE_CAPS: list[int | None] = [None, 2000, 1000, 500, 200]


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


def _build(
    state: CommentState, rationale_cap: int | None, claims: list[ClaimView]
) -> str:
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
    if claims:
        for c in claims:
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
        rationale = mf.rationale
        if rationale_cap is not None and len(rationale) > rationale_cap:
            rationale = rationale[:rationale_cap] + "... [truncated]"
        lines.append(f"### {mf.model_id}")
        lines.append(rationale)

    return "\n".join(lines)


def render_comment(state: CommentState) -> str:
    """Render the private comment, truncating to fit `MAX_COMMENT_CHARS`.

    Truncation order: rationales are shortened first (progressively, via
    `_RATIONALE_CAPS`); if still over budget, claims are dropped from the
    tail of the list (already support-sorted, so weakest first).
    """
    for cap in _RATIONALE_CAPS:
        text = _build(state, cap, state.claims)
        if len(text) <= MAX_COMMENT_CHARS:
            return text

    min_cap = _RATIONALE_CAPS[-1]
    claims = list(state.claims)
    while claims:
        claims = claims[:-1]
        text = _build(state, min_cap, claims)
        if len(text) <= MAX_COMMENT_CHARS:
            return text

    text = _build(state, min_cap, [])
    if len(text) > MAX_COMMENT_CHARS:
        text = text[: MAX_COMMENT_CHARS - 20] + "\n\n[truncated]"
    return text
