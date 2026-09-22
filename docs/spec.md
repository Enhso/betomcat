# Tradecraft Spec: FutureEval Bot + Intelligence Workbench Phase 1

**Owner:** Hatim
**Companion to:** `futureeval-bot-brief.md` (Sept 2026)
**Handoff target:** Claude Code
**Status:** Ready to build against. This doc resolves every judgment/tradecraft decision the brief flagged for escalation. Everything not addressed here (schema, stack, library choices, gate sequencing/prioritization beyond what's specified, always-on host provider selection) is Claude Code's call, per the brief's own audit-first framing — do not treat silence on a topic as an oversight.

**Recurring design pattern to preserve throughout:** several decisions below follow the same shape — build the higher-leverage automated/weighted version first, instrument it, keep a simpler mechanical fallback as a real, always-available code path (not bolted on later) in case upkeep outweighs benefit. This applies to: the review-bot automation (Option 1 now, Option 2 possible later), family-merge candidate detection (pre-work now, raw list possible later), and model-pool weighting (degrades to uniform-random before enough resolutions exist). Build all fallback paths as first-class, not contingencies to retrofit.

---

## 1. As-of corpus: scope and reuse

**Ingestion is on-demand only, not standing/background.** No pre-question crawling, no speculative ingestion into anticipated families. "Start ingesting now" (brief §4.1) means: the fetch-timestamp-and-hash discipline applies from the very first MiniBench research call, not that there's a background corpus-builder to bootstrap separately.

**Question families are discovered, never pre-declared.** No fixed taxonomy up front (see §2). Corpus depth grows exactly as fast as questions get researched.

**Both directions of reuse are in scope.** When a question opens in a family with prior history:
- Query the stored corpus for existing claims tagged to that family first.
- Fetch fresh documents to cover the gap since the family's last-seen timestamp (or from scratch if no prior history).
- Both feed the same briefing. This is a genuine accelerant for live research, not solely a backtesting substrate — the read path (query-by-family, get-as-of) must exist as early as the write path, not bolted on later.

---

## 2. Family tagging

**Families are a first-class, explicit object** — not an emergent retrieval pattern. A family is created on first sighting, matched against on every subsequent question, and is what standing dossiers and personal-history retrieval both attach to.

**Classification: growing-but-fixed list, Jev-first.**
- Jev classifies each new question against the existing family list.
- Confidence ≥ 50% → tag with the matched family.
- Confidence < 50% → escalate to a **cheap/free model** (not frontier — see §5 on budget) to mint a new family label. Family creation is infrastructure-adjacent classification, not forecasting judgment, and stays cheap-tier regardless of how architecturally consequential it feels.

**Known risk, accepted with a mitigation:** Jev's documented weaknesses (literal reading, degrades with irrelevant context) bias toward *under*-matching, which could push the family list toward one-per-question fragmentation rather than real recurring clusters. Mitigation:

**Weekly family-set review (Sundays, conditional).**
- Pre-work: a cheap-tier pass specifically surfaces *candidate merge pairs* (families whose member questions look textually/semantically close despite separate tags), ranked for review — not a raw undifferentiated list. (Fallback: revert to raw list if the pre-work doesn't earn its keep — see recurring pattern note above.)
- Human approves/rejects proposed merges.
- **Merges are prospective only.** No retroactive relabeling of existing question-tags or personal-history backfill already done under the absorbed label. A merge only affects routing of *future* matches to either label. Known, accepted limitation: history accumulated under an absorbed label stays siloed unless manually consolidated later.

---

## 3. Personal forecasting history

**Mechanism: retrieval as context, not calibration correction.** Hatim's past forecasts (100+ resolved Metaculus questions) are surfaced to the forecasting models as data points in the briefing when relevant — never used to systematically adjust model output, never a human-in-the-loop intervention on a live question (this is legitimate design-time/static reference material per the brief's own framing).

**Matching reuses the family-tag system** (§2) — no separate similarity logic. This requires a one-time backfill: Hatim's existing question history gets Jev-classified into the family list as it exists/grows.

**Backfill re-check cadence: weekly, conditional.** Runs Sunday, only if at least one new family was minted since the last check (not per-mint). Accepted tradeoff: a family minted mid-week gets no personal-history backfill benefit for the question that triggered its creation — only future questions in that family benefit. State this as intentional in any documentation, not an oversight.

---

## 4. Claims and provenance

**Claim structure: extraction-based with a support layer.**
1. **Extraction** (cheap-tier gate): fetched documents are decomposed into discrete atomic claims, each carrying source + fetch-timestamp provenance. The forecaster reads curated claims, not raw documents.
2. **Scoring** (cheap-tier gate): each claim gets a per-claim support score as a first-class field.

The existing Workbench briefing structure (situation summary, historical context, competing hypotheses, counterfactuals, open questions, source appendix) is unchanged — out of scope here, per the brief's own note.

---

## 5. Model pool and forecast generation

**No persona ensemble.** Prior attempt at ensemble forecasting cost significantly without measurable payoff (persona-based methods are also generally out of favor). v1 uses **two models, randomly drawn from a rotating pool**, ensemble width configurable as a runtime parameter (default 2).

**Pool storage: YAML config file** (not an env var) — hot-editable without redeploy, and a natural place to record cost tier / notes per model for weekly tending.

**Pool composition:** current-generation Anthropic + OpenAI models plus several free models, **as full co-equal peers from day one** — free models are not a budget-safety fallback, they're eligible for every draw at full odds. This is a deliberate acceptance of quality variance per draw, and functions as a live experiment on model performance itself (not just Workbench-vs-baseline), worth noting as a second-order output of the season regardless of whether it was the original goal.

**Weighting: performance-based, degrading gracefully.**
- Selection probability is weighted by each model's resolved-question performance.
- Before enough resolutions exist to weight meaningfully (early season, and for MiniBench's faster cadence this resolves sooner than main-tournament-only data would), weighting degrades to uniform-random.
- **Weights update daily** via a scheduled job (GitHub Actions acceptable here — see §7 on why this differs from the live bot's hosting requirement), fetching newly-resolved questions and recomputing. Retry-on-failure required (see §7).

**Draw mechanism, precisely specified:**
1. First model: unconstrained weighted-random draw from the full pool.
2. Second model: weighted-random draw, but restricted to the subset of remaining models whose weight is high enough that the pair's average weight is ≥ the pool's overall average weight (i.e., subset = models with weight ≥ `2×pool_avg − first_model_weight`).
3. **Degenerate case** (no remaining model satisfies the constraint — happens more as the pool grows and/or weight variance increases, an expected consequence of weighting actually working): fall back to picking the highest-weighted remaining model regardless of whether the constraint is technically met. Log every occurrence of this fallback firing.
4. **This fallback-frequency log is the pool-hygiene signal** — not something to solve per-draw. If a specific model is disproportionately responsible for triggering the fallback (i.e., whenever it's drawn first, no valid second draw exists), that is a pool-hygiene issue.

**Weighted-average reconciliation (mechanical, no override):** final submitted probability = the two models' individual probabilities combined, weighted by each model's *current* pool-weight.

**Weight consistency across a question's lifecycle:** the pool-weight used for the draw (step 2 above) and the weight used for reconciliation are **frozen at draw-time** — both read once, at the moment of the draw, even if a daily weight update lands later in that same question's 1.5–3h window. No mid-question weight drift between draw and reconciliation.

**Disagreement-typing is a side channel only, never feeds back into the number.** A cheap-tier "referee" gate classifies *why* the two models disagreed (stale info, genuine uncertainty, misread resolution criteria, etc.) when they do. This can:
- Trigger follow-up research (re-run extraction/scoring/fetch) if time remains before the soft deadline threshold (§6) — **no special-cased patience**; a disagreement-triggered re-research gets identical treatment to the original research pass, not a shorter budget.
- Log a note for the weekly digest.

It **never** overrides, excludes, or adjusts the weighted-average formula for that question, regardless of what it concludes. This is a deliberate design choice: it means a misfiring referee gate can never corrupt a live submission (consistent with fail-open philosophy), and it keeps the reconciliation math auditable and unconditional.

**Frontier-spend policy, stated precisely:** the two forecast-generating model calls (step where the drawn models actually produce a probability) are the **only** place frontier-tier spend is permitted in the entire pipeline. Family minting, disagreement-typing/refereeing, claim extraction, claim scoring, and every other gate are cheap/free-tier, without exception. Do not relitigate this per-gate during implementation — it is a fixed policy, not a per-decision judgment call.

**MiniBench runs the identical full pipeline** — same frontier-final-forecast rule, same weighting, same everything. It is not a separate/cheaper testing lane; it's the same system against faster-resolving questions. This is deliberate: MiniBench's faster resolution cadence is what solves the model-pool weighting cold-start problem, feeding early performance data before main-tournament questions have had time to resolve. Budget accordingly — **MiniBench question volume consumes the same $100 pool at the same per-question frontier cost as main-tournament questions.** "Warmup" describes the stakes, not the cost.

---

## 6. Deadline-aware fallback ladder

**Two thresholds, not one:**
- **Soft threshold (~30 min before close):** if one or both drawn models haven't responded, trigger a retry for the missing call(s). If the research/briefing step is what's running long (not the forecast call), simply wait for it to finish — no thin-briefing shortcut.
- **Hard threshold (~5 min before close, non-negotiable):** submit whatever exists at that moment — both forecasts, one forecast alone (no weighted-average possible with a single input; submit it as-is), or the retry result. Full stop, no further retries past this point.

**If genuinely nothing exists at the hard cutoff** (both models failed even after retry, and/or research never completed): **the question is missed. No hardcoded default probability, ever** — not 50%, not a community-prediction proxy. This is a deliberate choice: a synthetic fallback number would itself corrupt the resolved-question performance data used for weighting and for the v1-vs-v2 comparison, which matters more than avoiding an honest miss.

**Reframe the P0 goal honestly:** "never miss a question" (brief §6) is not a literal guarantee achievable with this design — it's a reliability-engineering target (minimize misses to cases where the retry ladder genuinely can't produce a number in time, e.g., host downtime, cascading API failures), not a logical invariant. State it this way in any internal documentation so it isn't later treated as violated-on-day-one.

**If manual review later shows step-3/4 diagnosis work (see §8) is eating disproportionate time:** the first lever to pull is tightening the mechanical flagging criteria in review-bot steps 1–2 (narrower "worth investigating" bar), not immediately escalating to full automated diagnosis (Option 2 in §8). Tuning specifics deferred to post-launch iteration.

---

## 7. Hosting

- **Live scheduled bot:** always-on host (e.g., free-tier always-on VM), **not GitHub Actions** — per brief §6, GitHub Actions has known reliability problems this season that directly cause missed questions, which is the single worst outcome this system is designed against. This requires no personal hardware or cost; it's free cloud hosting Claude Code sets up once.
- **Daily weight-update job:** GitHub Actions is acceptable here — the risk profile is different (a late/failed weight update means one extra day of stale weights, not a missed question). **Must include retry-on-failure logic** (a follow-up check that re-triggers the run if the scheduled run didn't fire/complete), since this is the same known GitHub Actions failure mode, just applied to a lower-stakes job.

---

## 8. Weekly review automation

**Review-bot skill (attached separately) is automated for its mechanical stages only — Option 1, not Option 2, for now:**
- **Automated:** `bot-review review` execution, outcome table construction, the four mechanical checks (forecasted-but-unscored, no-trace, truncated-trace, forecaster-count-mismatch), and flagging of worst-scoring questions.
- **Manual, done by Hatim during his own weekly hours:** reading flagged reports, diagnosing *why* questions went wrong, writing `review.md`'s qualitative sections, deciding "well forecast, unlucky outcome" vs. genuine process failure. This is preserved as human judgment deliberately — the skill's own text warns this discrimination is easy to get wrong ("a list of losses looks like a systematic bias whether or not one exists"), and it's exactly the kind of judgment-heavy work the brief reserves for Hatim rather than automating.

**Contingency, not a current decision:** if the manual diagnosis step (steps 3–4) consistently eats more time than the 3–6 hr/week budget tolerates, revisit — first by tightening mechanical flagging (see §6), and only if that's insufficient, consider full end-to-end automation (Option 2: a model performs the diagnosis and writes `review.md` unattended). If Option 2 is adopted later, its frontier-spend treatment (exception to §5's final-forecast-only rule, or accept cheap-tier discrimination risk) is an open question to resolve at that time, not now. Build the Option 1 pipeline so its mechanical stage is cleanly separable — Option 2 should be able to bolt a write-up generator onto it later without rearchitecting.

**Weekly digest: single unified email, start maximal.** Covers everything worth Hatim's attention in one place rather than splitting into separate narrower digests. Deliberately starts broad (add/remove sections by observed usefulness) rather than starting minimal and discovering gaps later. Known contents at spec time:
- Model-pool weights, second-draw fallback frequency (per model), and worst-performer removal suggestions (surfaced for Hatim to act on — never auto-pruned).
- Family-set review: candidate merge pairs (pre-work-ranked) for approval.
- Personal-history backfill re-check results (if it ran that week).
- MiniBench round-over-round performance.
- Review-bot mechanical output: outcome table, the four mechanical-check flags, and the ranked list of worst-scoring questions awaiting Hatim's manual read.

---

## 9. Private comment

**Full mechanical transparency by default.** The comment is a direct rendering of that question's pipeline state — not a separately-authored artifact, so it costs nothing beyond formatting data that already exists. Contents per question:
- Family tag matched (or newly minted).
- The two models drawn and their individual (pre-reconciliation) probabilities.
- The pool-weights used (frozen-at-draw) and the resulting weighted-average arithmetic.
- Disagreement-type classification, if the two forecasts diverged and the referee gate fired.
- Personal-history matches surfaced, if any.
- Claims used, with source and fetch-timestamp provenance.

This serves the prize-eligibility inspection requirement by construction (uniform, auditable format every time) and doubles as the raw material for Hatim's own weekly/manual review.

---

## 10. Explicitly reaffirmed as Claude Code's call (not re-litigated here)

- Exact schema, API contracts, stack/library choices within all constraints above.
- Which of the remaining named Jev gates (resolution-criteria decomposition, research-passage relevance/injection filtering, prediction-market candidate matching, unit/magnitude sanity-checking, rationale-auditing against failure modes) ship for MiniBench v1 vs. defer — audit-first, per the brief's own instruction.
- Specific always-on VM provider selection.
- v2 (template-fork control bot) — zero build effort by design, no secondary/labeled bot planned beyond it.
