# Product Brief: FutureEval Bot + Intelligence Workbench Phase 1

**Owner:** Hatim
**Handoff target:** Claude Code (repo-level build agent)
**Status:** Ready for spec. This brief fixes intent and constraints; Claude Code owns schema, stack details, and library choices within them.

---

## 1. What this actually is

Two things being built together on purpose, not two projects sharing a repo by accident:

1. **Intelligence Workbench, Phase 1 — for real.** Not a demo, not a bot-specific side corpus. This is the living-dossier system from the PRD/Spec/Plan (Notion, July 2026), and this tournament is its forcing function. If the tournament ended tomorrow, the Workbench should still be worth having built.
2. **A FutureEval bot entered in the Fall 2026 tournament to win.** Leaderboard rank and spot peer score at season end are the metric that matters — not calibration in the abstract, not a research writeup as the primary output. The $50k pool and rank are real goals, not framing.

The two are linked by design, not by convenience: the bot's v1 forecaster consumes the Workbench as its evidence layer. This also makes the tournament a live, adversarial test of whether the Workbench's structured-evidence approach actually beats a vibes-based baseline — which is a real question Hatim wants answered, not a story to tell after the fact regardless of outcome. Both goals are held equally; neither is allowed to quietly become secondary during the build.

**Epistemic firewall (non-negotiable, inherited from the PRD):** the Workbench does not forecast. It has no auto-forecasting and no default probabilities — this is an explicit non-goal in the original spec and it stays a non-goal here. The Workbench's job ends at producing a structured, sourced, as-of-a-timestamp briefing. The bot's forecasting layer is a separate consumer that turns that briefing into a probability. Do not blur this boundary for convenience even under tournament time pressure.

---

## 2. Non-negotiable constraints

**Tournament rules (external, fixed):**
- Questions open ~1.5 hours (temporarily up to 3h while GitHub Actions reliability is degraded tournament-wide) and close on a fixed schedule. Only the last forecast before close counts (spot peer scoring).
- No human in the loop on live submissions. No previewing and tuning, no manual resubmission, no copy-paste. API-only, fully autonomous.
- A private comment is mandatory on every forecast. Treat it as a real analytic product, not a formality.
- One prize-eligible bot per person. A secondary bot is allowed and must be labeled as such (version suffix in username/email).
- Prize eligibility requires being willing to share code or an architecture description and accept a Metaculus inspection.
- Publicly available forecasts from other platforms or Metaculus itself are legal inputs. Creating a question elsewhere to feed the bot is not.

**Timing (fixed, act on immediately):**
- MiniBench warmup is open now (today). The bot must go live on MiniBench questions as soon as possible — do not wait for the Workbench to mature.
- Main Fall tournament questions begin opening shortly after, ramping slowly at first.

**Budget (fixed):**
- $100 of OpenRouter credit from Metaculus, renewable based on MiniBench performance. This is the entire budget — no personal money will be spent.
- Design the model mix as paid-and-free, not paid-only: burn the $100 deliberately on the calls that need frontier reasoning (final ensemble forecasts, judgment-heavy steps), and default to free-tier or cheap models everywhere else (routing, extraction, formatting, typed gates). Running out of credit before season end is a failure mode to design against explicitly.

**Time budget (fixed):**
- 3–6 hours/week of Hatim's own attention once the system is running. This is a real constraint on how much needs to be autonomous vs. how much can require weekly hands-on iteration. It is not near-zero (some ongoing tending is expected and wanted) and not a full-time project.
- Given that budget, and given Hatim's stated preference under time pressure: when a tradeoff must be made between spending his own hours on forecasting judgment / tradecraft design versus infrastructure engineering, judgment and tradecraft win. Claude Code should default toward getting infrastructure decisions "good enough and correct" autonomously, and reserve escalation-to-Hatim for decisions that shape forecasting judgment, tradecraft design, or the Workbench's evidentiary structure — not for routine engineering choices (library X vs Y, code organization, deployment mechanics).

---

## 3. Architecture: the A/B test

- **v1 — the real entry.** Full tradecraft + Workbench-fed pipeline, as detailed in Section 4. This is the bot competing to win and the vehicle for testing whether structured epistemics beats baseline LLM forecasting.
- **v2 — true control, zero build cost.** The unmodified Metaculus template bot (`metac-bot-template`), forked and run as-is with its own labeled account (version suffix per tournament rules). Do not customize it. Its entire purpose is being an honest, cost-free baseline to diff v1 against on the same questions. If v2 needs engineering effort beyond "fork, add secrets, enable Actions," that effort is being misspent.

This framing resolves the "win vs. test the hypothesis" tension directly: v1 winning **is** the hypothesis being confirmed; v1 losing to v2 **is** a real, informative result. Nothing about the brief should be structured to make the hypothesis unfalsifiable.

---

## 4. The Workbench's role in the bot (v1 only)

This section names the priorities; Claude Code owns the concrete schema and implementation.

1. **As-of / point-in-time corpus is the top priority.** Every fetched document stored with fetch timestamp and content hash; changes versioned, never overwritten. This must support a query like "what did the evidence look like as of timestamp T" — this is what makes future backtesting against resolved questions honest (no leakage from post-resolution web content), and it's the single piece of infrastructure that compounds in value the longer it runs. Start ingesting now even before the rest of the pipeline is live; every week of delay is corpus that can't be backfilled.
2. **Standing dossiers for recurring question families** (the tournament re-asks about the same topics — markets, macro indicators, named ongoing situations, etc.). Where a family can be identified, a standing dossier with a data connector, a coded statistical baseline, and a resolution-source scraper should exist before a live question in that family ever opens, so the 1.5-hour window is spent on synthesis, not cold research.
3. **Claims with provenance**, not raw article dumps, as what the forecasting layer actually reads. Every claim traces to a source and a fetch time.
4. **Hatim's own forecasting history as a first-class input.** His existing Metaculus track record (100+ resolved questions, top 2%) and this season's own bot forecasts/rationales/resolutions belong in the graph as a comparison class the pipeline can retrieve against — this is legitimate design-time input, not the human-in-the-loop the rules forbid, since it's static reference material rather than live intervention on a specific open question.
5. **The briefing structure stays what the PRD already specifies** (situation summary, historical context, competing hypotheses, counterfactuals, open questions, source appendix, etc.) — this section is a reminder that it applies here, not a request to redesign it.

**First engineering task, before any of the above is extended:** audit the actual current repo state. The July Phase-1 prompts assumed a Rust/Axum + Python + Neo4j + Weaviate stack and produced some vertical-slice work, but its current runnable state is unverified. Do not assume ingestion→graph code exists and works. Produce an honest status report (what runs, what doesn't, what's stubbed) before writing the Section 4 build plan in detail.

---

## 5. Jev / TypeSafe integration

Use Jev (TypeSafe's System One model, `jev-1.13`) as typed decision gates inside the pipeline — routing, extraction, filtering, auditing — not as a forecaster and not as an ensemble member for the primary probability. Its documented weaknesses (poor at arithmetic, counting, date comparison; literal reading; accuracy degrades with irrelevant context; not hardened against adversarial input; 32k token state+question limit; rate limits shifting without notice) make it unsuited to owning judgment calls, but a strong fit for cheap, parallel, typed checks between pipeline stages. Fits the budget: input is $0.042/MTok, output is free.

Concrete candidate gates (Claude Code to finalize which ship v1 for MiniBench vs. defer): question-family routing, resolution-criteria decomposition into atomic checks (already-occurred, status-quo direction, pre-window disqualification traps), research-passage relevance/injection filtering, per-claim support scoring, prediction-market candidate matching, ensemble-disagreement typing (routing disagreements to the right kind of follow-up research), unit/magnitude sanity-checking against the family's data connector, and rationale auditing against known agentic-forecaster failure modes (treating rhetoric as commitment, missing an obvious catalyst, over-extrapolating a disrupted base rate).

All gates fail open: if a gate errors or times out, the pipeline proceeds without it rather than blocking a submission. Missing a question is a worse outcome than a slightly weaker gate result.

---

## 6. Reliability

Do not run the live scheduled bot on GitHub Actions past initial testing — known unreliability this season is already costing entrants scored questions. Move to an always-on host (e.g., a free-tier always-on VM) before relying on it for real tournament questions. Every pipeline stage needs a deadline-aware fallback: at some threshold before question close, submit the best available forecast rather than risk missing the window entirely. Missing a question is one of the largest avoidable point losses available in this competition; treat it as a P0 failure mode throughout, not an edge case.

---

## 7. Success criteria

- **Primary:** v1's spot peer score / leaderboard rank at Fall 2026 season end, and the v1-vs-v2 delta as the read on whether the tradecraft/Workbench approach genuinely helps.
- **Secondary, ongoing:** MiniBench round-over-round performance as the fast feedback loop guiding weekly iteration.
- **Structural, non-negotiable regardless of tournament outcome:** the Workbench's as-of corpus and schema exist, run, and are genuinely reusable as Phase 1 of the broader Workbench product — this must be true even in a world where the bot places poorly.

---

## 8. Explicitly out of scope for this brief

- Exact graph schema, API contracts, library choices, hosting provider selection — Claude Code's call, made against the priorities above.
- The full Workbench product roadmap beyond what v1 forecasting needs this season.
- Any spend beyond the $100 OpenRouter allocation.
- Any human review of individual live forecasts before submission (forbidden by tournament rules regardless of preference).
