# Handoff: state of the deployment and what remains

Updated 2026-09-30 (end of session). **Next session, in order:** s2 items
a-e (verify today's pushes live), then build s5.3 (weights, fully decided),
then s5.1 stage 2 (referee, fully decided), then ask Hatim the open
decisions of s5.7 before building it. Read this first, then
`docs/BUILD_LOG.md` (full trace, every decision and why), `docs/contracts.md`
(IW <-> bot interfaces, comment layout), `docs/spec.md` and `docs/brief.md`
(intent). This file lists the state and everything still open, in the order
the next session should take it.

## 1. Where things stand

v1 is deployed: the Actions host has run unattended since 2026-09-24 19:21
UTC. It forecast all three FE Fall questions opened on 28-29 Sep (3-hour
windows, BUILD_LOG 2026-09-30). On 2026-09-30 it gained the synthesized
comment, the MiniBench round allowance, a 4-question concurrency cap,
`max_tokens` 8000 and a direct Google route for Flash; all of it runs from
the first shift started after ~10:30 UTC 2026-09-30.

| Repo | Commit | State |
|---|---|---|
| betomcat (v1, public `Enhso/betomcat`) | `f96a291` on `main` | Host shifts chain every ~5.5 h. |
| IW (private `Enhso/iw`) | `adf3f72` on `main` | Checked out by every shift through the deploy key. `adf3f72` (2026-09-30) adds optional `extra_queries` to `POST /api/research` (referee stage 1; unused until 5.1 stage 2). Hatim's unstaged `prompt.txt` edit: leave it, never stage it. |
| vezocontrol (v2, public `Enhso/vezocontrol`) | `28bdfac` | Template bot, the control. Since 2026-09-30 it runs as self-chaining ~5 h shifts polling `main.py` every 5 min (first shift `36693458833`, dispatched by hand); forecasting code unchanged. |

Committed 2026-09-30 (details in BUILD_LOG "2026-09-30"):

- `289234e` `z-ai/glm-5.2:free` disabled (retired from OpenRouter).
- `f0b0368` MiniBench round allowance ($50 over `start_date` + 4 days, from
  the funded key's `limit_remaining` at first sighting, `round_starts` table),
  at most 4 questions in flight (`MAX_CONCURRENT_QUESTIONS`, early wake on a
  freed slot), `max_tokens` 8000 for every pool model.
- `ea047e2` `google/gemini-3.8-flash` on Hatim's AI Studio key through a
  direct Google route (`key: google`, `GEMINI_API_KEY`, cost 0, never paced
  out); `gemini-3.1-pro-preview` disabled (free-tier Pro quota is 0).
- `f96a291` one synthesized rationale per comment (s5.0 done).
- vezocontrol `28bdfac` self-chaining shifts.

Committed 2026-09-25 (details in BUILD_LOG "2026-09-25"):

- `1c8323a` questions a shift dropped at its end are retried by the next shift
  (open runs marked `abandoned` at startup; `has_run` ignores them).
- `61231e7` budget pacing horizon moved to 19 Oct (Hatim's decision).
- `6a184e6` `nex-agi/nex-n2.5-pro:free` disabled (0 OpenRouter endpoints).
- `9e0a6c6` one comment per question, posted with the forecast that stands.
- IW `1c3fe01` a failed extraction batch is dropped instead of failing the
  whole research job (it had caused a 502 and degraded research live).

Setup is complete: all Actions secrets exist on both repos, IW has a read-only
deploy key for betomcat.

Check suite (betomcat, 306 tests):
`uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src && uv run pytest -q`
Check suite (IW worker, 187 tests), from `~/projects/iw/python`: the same four
commands. IW Rust (115 tests), from `~/projects/iw`: `cargo fmt --check &&
cargo clippy --all-targets -- -D warnings && cargo test`.

## 2. Verify first (start of the next session)

Added 2026-09-30 (check these first):

a. **v2 on its new shifts.** The 14:00 UTC 30 Sep FE Fall question should
   have a vezocontrol forecast within ~10 min of opening, and the first
   shift (`36693458833`, ends ~14:03) should have dispatched its successor.
b. **First synthesized comment.** For the first question forecast after
   ~10:30 UTC 30 Sep: vezo3's comment is the header plus a paragraph of at
   most 300 words that opens with the submitted forecast; the shift log shows
   `rationale synthesis ok` (or `failed, posting fallback`) for it.
c. **Flash on the direct route.** When Flash is drawn: `model_forecasts` row
   `ok` with `cost_usd` 0; a 429 means the free-tier quota bit.
d. **Round mode on Mon 5 Oct.** Once the `minibench` slug moves, the shift
   log shows `budget: MiniBench round mode, spend $X of $50`, a
   `round_starts` row appears in the ledger, and heartbeats show `(cap 4)`
   with a `waiting for a slot` count during the burst.
e. **Public NZ comment.** Re-check whether Q45844's comment turned public
   after 17:00 UTC 30 Sep (BUILD_LOG 2026-09-30).

These are deployed but not yet observed running. Do them before new work.

1. **Today's commits are running.** The first host run created after ~17:40
   UTC 2026-09-25 must have `head_sha` `a055eff` or later, and its log must show
   `marked N unfinished run(s) from earlier shifts abandoned` right after
   `iw-server healthy`, and `state snapshot restored`. Job logs only exist once
   a shift ends; mid-shift, `betomcat pull-state --out-dir <tmp>` decrypts the
   latest snapshot. How to read runs and logs: s8.
2. **The shift chain is unbroken.** Each `workflow_dispatch` run should start
   within a minute or two of the previous one ending. The `schedule` runs every
   30 min are the backstop and should all end in seconds (guard skips). A gap of
   more than ~10 min between shifts means the self-dispatch failed: read the
   last step of the ended shift.
3. **First real FE Fall question (from Mon 28 Sep).** Correction
   (2026-09-30): FE Fall questions are open for 3 h (seen 14:00-17:00 and
   21:00-00:00 UTC), not long-window, so the daemon claims them as soon as
   they open. vezo3 forecast all three opened on 28-29 Sep (BUILD_LOG
   2026-09-30). For the first one forecast, check through the API as vezo3 (s8): exactly one
   private comment, short form (header plus one `Summary:` bullet per model,
   contracts E), attached to the forecast that stands. Then check its ledger
   rows with `pull-state`: `runs.status`, `submissions` (a `provisional` with
   `comment_posted = 0` then a `final` with `1`, or a lone `provisional` with
   `1`), `model_forecasts` (attempts, errors, `cost_usd`), `draws`.
4. **IW research not degraded on live questions.** In the ledger,
   `runs.degraded` should be 0. If it is 1, the pipeline fell back to direct
   AskNews: find the cause in the shift log (IW 502s are logged by
   `betomcat.research` as redacted; the iw-server stderr is in the job log).
5. **v2's shift chain.** Each shift should start within a minute or two of
   the previous one ending (`workflow_dispatch` runs of
   `run_bot_on_tournament.yaml`); scheduled runs should end in seconds (guard
   skips). Every FE Fall and MiniBench question should get a vezocontrol
   forecast within ~10 min of opening. Its first short comment was posted on
   45848 (2026-09-29); the `# Rationale` form is not yet checked.

## 3. Calendar and deadlines

| Date | Event | What it means for us |
|---|---|---|
| Mon 28 Sep | FE Fall (`fall-futureeval-2026`, id 33121) opens | First real v1 questions. Forecasting ends 6 Jan 2027, closes 5 Mar 2027. |
| 1-4 Oct | MiniBench round 1 (id 33125) resolves | Decides the OpenRouter credit renewal. v1 has **no** forecasts in it: its 60 questions opened 21-23 Sep and all closed before v1 went live. v2 has none either (spot check). Hatim may want to know how Metaculus weighs a round a bot missed. |
| Mon 5 Oct | MiniBench round 2 expected | Questions open over its first ~3 days (previous rounds: 59-60 questions in 3-4 days), each open 3 h. The `minibench` slug moves to the new round on its own. This is where half the credit goes (Hatim). |
| before 19 Oct | Budget horizon ends | Set the next horizon (Hatim decides, s6.1). Past it the guard allows the whole remaining credit in a single day. |
| ~15-19 Oct | Round 2 resolves | First resolved v1 forecasts in quantity: the weight update (s5.2) has data from here. |

## 4. Remaining monitoring and hygiene (small, recurring)

1. **Read the ledger after the first day of real questions**: per-model
   failure rate (`model_forecasts.status`), how often the replacement draw
   fires (`draws` rows beyond the ensemble width per run), real cost per
   question (sum of `cost_usd` per run), time from claim to final. Disable a
   pool model that fails most attempts (set `enabled: false` in
   `config/models.yaml` with a dated `notes` line, as done for Gemma and nex).
   Known flaky from probes: `qwen/qwen3.8-27b:free` and `z-ai/glm-5.2:free`
   (upstream 429s), `nemotron-3-ultra:free` (one timeout and one unparseable
   numeric reply on 2026-09-25).
2. **Personal free key: 50 requests/day.** Its six free models drop out when
   the quota runs low (the guard handles it). On busy MiniBench days expect the
   pool to be mostly funded-key models by the afternoon.
3. **Funded key: 1000 free requests/day** on top of the credit; ~$99 credit
   left on 2026-09-25 (`limit_remaining` on `GET /api/v1/key`).
4. **Revoke `GITHUB_ADMIN_TOKEN`** (Hatim; it has delete_repo and admin:org)
   once monitoring through the API is no longer needed. It is the only way the
   lead reads Actions runs and logs (no `gh` CLI on this machine).
5. **v2 also runs the template's "Forecast on Metaculus Cup" workflow**
   (seen 2026-09-25 04:11). Not checked what it spends or on which key; look
   once, since the funded key's quota is shared.

## 5. Not built yet (in proposed priority order)

**Order (2026-09-30, left to the lead by Hatim, most crux-y first):** 5.0
done; 5.3 weights next (must exist before MiniBench round 2 resolves
~15-19 Oct); 5.1 stage 2 referee; 5.7 model list; then 5.6, 5.2, 5.4, 5.5.
5.3 and 5.1 have every decision made; the others still have open decisions
that go to Hatim before any building (his rule).

### 5.0 A fuller rationale in the posted comment -- DONE 2026-09-30 (`f96a291`)

Built as option (b): `rationale.py`, `openai/gpt-6-luna`, at most 300 words,
opens with the submitted forecast, fallback to the per-model summary lines.
Follow-ups: the prompt lives in `rationale.py`, not `prompts/` (move it if
Hatim wants to review it with the others); the Luna call's cost is not in
the ledger (the budget guard still sees it through the key's usage). The
original brief follows for reference.

- **The ask:** Hatim read vezo3's two comments on Q43332 and found them too
  terse. The comment should expand on the rationale behind the final
  forecast. Take this first: it is small, and it shapes every comment from
  the first real question (FE Fall, Mon 28 Sep).
- **Current state:** the comment is a header plus one `Summary:` line per
  model, written by each forecasting model itself and capped at 60 words
  (`SUMMARY_INSTRUCTION` and `_SUMMARY_WORD_CAP` in `forecast.py`,
  `render_comment` in `comment.py`, `COMMENT_MAX_CHARS` = 1500 as a backstop).
  The live final comment was 82 words for two models. It never states the
  submitted number or how the two forecasts combined into it, and it reads as
  two separate opinions rather than one argument for the forecast. Read the
  two comments (post 43327) through the API, s8; they are deliberately not
  copied into this public repo.
- **Constraints that still hold:** Metaculus penalizes long comments (Hatim,
  2026-09-24) and there is one comment per question (`9e0a6c6`). The target
  is fuller, not full: agree a length with Hatim (the lead would start around
  150-250 words). The comment text must never reach the public Actions logs.
- **What it should carry:** the submitted forecast; the base rate or status
  quo anchored on; the two or three pieces of evidence that moved it, with
  dates; where the two models disagreed, if they did (later fed by the
  referee's disagreement type, s5.1); and what would change the forecast.
- **Decided (Hatim, 2026-09-25): option (b)**, one synthesized rationale.
  Still open: the length (lead's suggestion 150-250 words) and the model
  (lead's proposal: `openai/gpt-6-luna`, the IW default, ~$0.004 a call;
  not Gemma, whose Google quota is shared with v2). Fallback when the
  call fails or returns something unusable: today's per-model summary
  lines, so a comment is never missing.
- **The two options as presented:**
  (a) *No new LLM call.* Ask each model for a longer, structured summary
  (anchor, key evidence, main risk, what would change it), raise the cap, and
  have `render_comment` open with the submitted number and one mechanical
  line on how it was combined (weights). No extra cost, the models' own
  words, but still two voices.
  (b) *One synthesized rationale.* A cheap-tier call (spec s5: no frontier
  spend outside the two forecast calls) writes one paragraph from both
  models' full rationales and the reconciled number, as v2 does with Gemma.
  One voice, coherent with the final number, but it costs a call, must be
  fed only the rationales so it cannot invent claims, and needs a fallback to
  (a) when the call fails so the comment is never missing.
- **Where:** `forecast.py` (instruction and cap), `comment.py`
  (`render_comment`), contracts E (layout), tests in `tests/test_forecast.py`
  and `tests/test_comment.py`. The audit report (`render_report`) is
  unchanged.
- **Check before shipping:** a local dry run (s8) prints the comment; compare
  old and new on the same bot-testing-area question and show Hatim.

### 5.1 Referee gate (spec s5) -- DESIGN DECIDED 2026-09-30, stage 2 next

All design decisions are Hatim's (BUILD_LOG 2026-09-30); nothing is open.
Stage 1 (IW `extra_queries`) was built on 2026-09-30 (s1 table). **Stage 2
is the next build**: brief one builder with this section.

- **Trigger (unchanged):** `_maybe_referee`'s divergence bars in
  `pipeline.py` (binary |p_a - p_b| > 0.15; MC total variation distance;
  numeric medians more than 0.25 of the range apart). Kennedy (Q45844,
  24% vs 4%) would have fired.
- **Ladder change (approved):** today the final posts the moment both models
  are in and the referee stub runs after, inside `_submit`. New shape in
  `_run_model_ladder`: when both are in, diverge, and `now < soft`: post their
  weighted average as a *provisional* (no comment), then classify. If the
  class triggers a re-run: targeted re-research, then re-forecast **both**
  drawn models; the final is the weighted average of the two re-forecasts
  (same frozen weights). If the re-run fails, is not triggered, or soft/hard
  arrives first, the first average stands and its comment is posted as the
  provisional's is today (at hard) -- or post it as the final right away when
  no re-run is triggered. Hard rule (spec s5): the referee never overrides,
  excludes or adjusts the weighted-average arithmetic; it only decides
  whether a second round of forecasts happens. A referee exception must
  never block or corrupt a submission (fail open to today's behaviour).
- **Classifier: Jev (TypeSafe)**, not an LLM. One `choice` question over
  the state {question, resolution criteria, both rationales}: categories
  `stale_or_missing_information`, `misread_resolution_criteria`,
  `different_base_rate`, `genuine_uncertainty`, `other`. Client shape: copy
  `iw/python/src/iw_research/jev.py` (`POST https://api.typesafe.ai/v1/systemone`,
  body `{state, model: "jev-latest", questions: {id: {type: "choice",
  instructions, criteria: {category: description}}}}`, answers under
  `answers`; one retry on 429/5xx). `TYPESAFE_API_KEY` is in `.env`, is an
  Actions secret, and `host.yml` already passes it to the shift.
- **Re-run only for** `stale_or_missing_information` and
  `misread_resolution_criteria` (Hatim). Other classes are labeled only.
- **Targeted research (full build, Hatim):** when a re-run triggers, one
  `openai/gpt-6-luna` call (like `rationale.py`'s `RATIONALE_MODEL`, ~$0.004)
  writes 1-3 search queries aimed at the disagreement plus a one-line
  diagnosis. betomcat's `research.py` sends them as `extra_queries` on
  `POST /api/research` (IW validates: at most 3, non-empty, <= 200 chars).
  IW's family `last_seen` means the second research only pulls news newer
  than the first. If Luna fails: re-research without extra queries and a
  fixed diagnosis sentence per class.
- **Re-forecast prompt:** each model gets the new research and the diagnosis
  line, **never the other model's rationale** (avoids herding).
- **Logging:** store the class, whether a re-run happened, and its outcome
  per run (a small ledger table, `CREATE TABLE IF NOT EXISTS`), for the
  digest (5.4) and review. Public logs: class names and ids only.
- **Cost:** doubles on re-run questions (~$0.53 -> ~$1.06); the MiniBench
  round allowance covers it.
- **Tests:** the fake-clock harness in `tests/test_pipeline.py`: diverge ->
  re-run before soft -> final from re-forecasts; diverge after soft -> label
  only; class not triggering -> final from first average; Jev failure / Luna
  failure / IW failure -> first average stands; exactly one comment still.

### 5.2 Personal-history backfill (spec s3)

- **What:** Hatim's resolved Metaculus forecasts (100+ questions, account
  `Enhso`, `METACULUS_PERSONAL_TOKEN` in `.env`, read-only) become context in
  the briefing when the question's family matches. Retrieval as context only:
  never a calibration correction.
- **How:** fetch his forecasts and resolutions from the Metaculus API,
  family-tag each through IW's classifier (`POST /api/families/classify`, Jev
  first, cheap-tier mint below 50%), then `POST /api/history` with
  `kind: "personal"` items (contracts C4 has the `HistoryItem` shape). The
  bot side is already wired: IW's research response carries `history`,
  `research.py` parses it and `render_history_text` puts it in the prompt.
  Check in IW how the research endpoint fills `history` for the question's
  family before relying on it.
- **Where it runs:** once, locally or as a one-off command, against the live
  IW database. The IW database lives inside the encrypted state snapshot, so
  the import has to run inside a shift or be merged into the snapshot: design
  this carefully (never write the snapshot while a shift runs; the host owns
  state). Simplest safe option: a host start-up step that imports from a
  committed or secret-provided file once, guarded by a ledger flag.
- **Re-check cadence (spec s3):** weekly, only if a new family was minted
  since the last check. Can come later with the digest.
- **Privacy:** the repo and its logs are public; Hatim's forecasts must never
  be logged.

### 5.3 Daily weight update (spec s5) -- DECIDED 2026-09-30, build FIRST next session

**Status:** all decisions made (below); a builder was started on 2026-09-30
but produced no code (it stalled when the session left auto mode) and was
stopped. Nothing of it is in the repo. Needed before MiniBench round 2
resolves (~15-19 Oct). Launch one builder with this brief:

- **Decided (Hatim):** score = the model's own log score minus the community
  prediction's (CP) log score at close, per resolved question (peer-style,
  removes question difficulty); baseline score (vs uniform) when no CP;
  annulled/ambiguous skipped. Neutral (s = 0) below 10 scored questions.
  Mapping: s_i = mean * n_i / (n_i + 10); raw w_i = exp(s_i / T), T = 0.2
  (lead's choice); normalize to mean 1.0 over ENABLED pool models (unscored
  ones get s = 0); floor at 0.5. Constants at module level. Removals only
  suggested (digest), never automatic.
- **Scoring details (lead):** binary ln p(outcome); MC ln p[option]; numeric,
  discrete and date: 201-point CDF -> 200 inner bin masses plus the tails
  `cdf[0]` / `1 - cdf[-1]`, locate the resolution's bin with the question's
  scaling (range, `zero_point` for log scale), ln of that mass; tail
  resolutions use the tail masses. Floor masses at 1e-4, clip each per-question
  relative score to [-5, 5]. Use each model's LAST `ok` forecast per run.
- **Data source:** `GET /api/posts/<post id>/` (post id from `questions.url`):
  resolution, scheduled resolve time, scaling, CP at close (check
  `question.aggregations.recency_weighted` `latest` vs the `history` entry
  covering the close; capture real fixtures from resolved questions of the
  past MiniBench round, tournament 33122, into `tests/fixtures/`). Before
  hand-rolling the parsing, look at how forecasting-tools' review tooling
  reads resolutions and scores (vezocontrol's `bot-review` integration, and
  `.claude/skills/review-bot/SKILL.md` here): the stopped builder was reading
  it as a possible reuse. Metaculus rate-limits bursts: pause >= 1 s.
- **Ledger:** `resolutions` cache (question_id PK, status, resolution,
  cp_json, scaling_json, scheduled_resolve_time, fetched_at): fetch a
  resolved question once; refetch an unresolved one only after its scheduled
  resolve time or 7 days after the last fetch. `model_scores` (run_id,
  model_id, question_id, score, method, scored_at; unique per run+model) for
  audit and the digest. `CREATE TABLE IF NOT EXISTS` (old snapshots must open).
- **Host:** a periodic task beside `_periodic_snapshot`: at shift start and
  hourly, run the update only if `weights.json`'s `updated_at` is not today
  (UTC); snapshot on success; log and retry next tick on failure; never block
  the shift. Log counts only (public logs). CLI: `betomcat update-weights`.
- **Files:** new `src/betomcat/weights.py`, `tests/test_weights.py`,
  `tests/fixtures/`; edits to `ledger.py`, `host.py`, `cli.py` and their tests.

Original notes:

- **What:** each model's selection weight follows its performance on resolved
  questions; uniform until enough resolutions exist. The draw and the
  reconciliation already read `DATA_DIR/weights.json` once at draw time
  (`pool.load_weights`, frozen-at-draw semantics in place); nothing writes it
  yet except tests (`pool.write_weights`).
- **How:** a step in the host loop (BUILD_LOG: daily jobs run inside the shift
  loop, one state owner, retry = next iteration), once per UTC day: fetch
  resolutions for ledger questions that closed, score each model's own
  forecast (`model_forecasts.forecast_json`, per model, not the reconciled
  number), aggregate per model, write `weights.json`, snapshot.
- **Decisions for Hatim (tradecraft, not engineering):** the scoring rule
  (log score or Brier on the model's own forecast, or a peer-style score
  against the community), how numeric and MC questions are scored, the
  minimum number of resolved questions per model before it leaves uniform,
  and how scores map to weights (e.g. softmax with a floor so no model reaches
  0; spec s5 wants removals surfaced to Hatim, never automatic).
- **Data arrives:** FE Fall questions resolve over weeks to months; MiniBench
  round 2 resolves ~15-19 Oct. Build it before then.

### 5.4 Weekly digest (spec s8)

- **What:** one GitHub issue per week on `Enhso/betomcat` (Hatim chose an
  issue over email, BUILD_LOG 2026-09-22). Start maximal, prune later: pool
  weights, second-draw fallback frequency per model (`draw_fallbacks`),
  replacement-draw frequency, pacing decisions (`pacing_decisions`), per-model
  failure rates and cost, referee disagreement types, family merge candidates
  (s5.5), personal-history re-check results, MiniBench round-over-round
  performance, and the review-bot mechanical output (outcome table, the four
  mechanical checks, worst-scoring questions for Hatim's manual read).
- **Public repo caveat:** issues are public. The digest must not contain
  probabilities, rationales or claims for questions that are still open. It
  can for resolved ones; confirm with Hatim.
- **Review-bot:** the skill is at `.claude/skills/review-bot/SKILL.md`; only its
  mechanical stages are automated (spec s8, Option 1).

### 5.5 Family merge candidates (spec s2)

- **What:** a cheap pass ranks pairs of families whose member questions look
  close despite separate tags (centroid similarity over IW's feature-hash
  embeddings); ranked pairs go into the digest; Hatim approves merges;
  `POST /api/families/merge` applies them prospectively only (no relabeling).

### 5.6 Full rationales kept in a dossier (Hatim, 2026-09-30)

- **The ask:** every model's full rationale is stored in a dossier so it can
  be examined later (post-resolution review, why a forecast went wrong).
- **Current state:** the full text is already kept, but only in the ledger:
  `model_forecasts.rationale` (per attempt) and `submissions.report` (the
  whole audit report). Both live inside the encrypted state snapshot and are
  readable only through `pull-state` plus `sqlite3` (s8). The IW dossier
  linked from `runs.dossier_id` holds the research claims, not the forecasts.
- **Open (Hatim): which dossier.** (a) Write the rationales back into the
  question's IW dossier: needs a new IW endpoint and storage, and makes them
  retrievable by family. Feeding past rationales into future prompts would be
  a separate design decision, not part of this item. (b) A readable
  per-question dossier exported from the ledger (e.g. a `betomcat dossier
  <question>` command rendering Markdown): no IW change.
- **Constraint:** the repo and its logs are public, so rationales are never
  committed or logged while a question is open.

### 5.7 Keep the model pool current (Hatim, 2026-09-30)

- **The ask:** add models as they are released and remove them when they are
  retired, possibly through a separate Actions workflow.
- **Current state:** `config/models.yaml` is edited by hand. A retired model
  is only noticed when its calls fail: nex 404s (disabled 2026-09-25), and on
  2026-09-30 `z-ai/glm-5.2:free` was missing from OpenRouter's catalog (only
  the paid `z-ai/glm-5.2` remains) while still enabled, after failing 3/3
  attempts live.
- **Proposal:** a scheduled workflow that reads OpenRouter's public
  `GET /api/v1/models` (no key needed; each entry has `expiration_date`,
  `pricing`, `context_length`, `created`) and diffs it against the pool.
  Pool entries that are missing or have an `expiration_date` are flagged for
  removal. New models from the allowed providers (openai, anthropic and google
  on the funded key; `:free` ids on the free key) are flagged as candidates,
  with prices and context already filled in. It posts a PR editing
  `models.yaml` or an issue, and Hatim decides. Spec s5: removals are
  surfaced, never automatic. It needs no state or secrets beyond
  `GITHUB_TOKEN`, so it can be separate from the host. A merged change takes
  effect at the next shift, which checks out `main` when it starts.
- **Open (Hatim):** PR or issue, cadence (daily or weekly), and what makes a
  new model a candidate (provider allowlist, tier from price).

## 6. Decisions pending with Hatim

1. **Next budget horizon, before 19 Oct.** His rule: MiniBench round 2 first
   (about half the ~$99), the rest to FE Fall until an extension. Measured on
   the first three live questions: ~$0.53 per question (a floor: failed
   attempts carry no cost), so a 60-question round is ~$32; round 2 is paced
   by the $50 round allowance, not the daily rule. If the credit is
   renewed, the horizon can follow the renewal period. The constant is
   `DEFAULT_BUDGET_WINDOW_END` in `src/betomcat/config.py` (env
   `BUDGET_WINDOW_END` overrides it; the workflow does not set it).
2. ~~Comment rationale~~ (s5.0): done, 300 words, Luna.
3. ~~Priority order~~: left to the lead (Hatim, 2026-09-30), most crux-y
   first: 5.3 weights (must exist before round 2 resolves ~15-19 Oct), 5.1
   referee, 5.7 model list, then 5.6, 5.2, 5.4, 5.5. Every open decision
   inside an item still goes to Hatim before building.
4. ~~Referee design~~ (s5.1): decided 2026-09-30, see s5.1.
5. ~~Weight scoring rule~~ (s5.3): decided 2026-09-30 (BUILD_LOG).
6. **Rationale dossier** (s5.6): IW dossier or ledger export.
7. **Model-list workflow** (s5.7): PR or issue, cadence, candidate rule.

## 7. Known risks

- **Google via the funded key is fragile.** Every `google/` model goes through a
  BYOK Google AI Studio key: Gemma 429s on a 16k tokens/min free-tier quota
  shared with v2, Gemma `500 INTERNAL` on ~55% of large prompts,
  gemini-3.8-flash `503 high demand` even on tiny prompts (2026-09-24). Gemma
  is disabled in v1 and out of IW's worker chain. Since 2026-09-30 Flash
  runs on Hatim's own AI Studio key through the direct Google route (also
  free tier: per-minute/day limits unknown, a 429 falls to the replacement
  draw), and 3.1 Pro is disabled (free-tier Pro quota is 0 on any key; it
  needs a billing-enabled Google key).
- **IW worker timeout.** Each extraction call has a 120 s httpx timeout
  (`iw/python/src/iw_research/llm.py`). One batch hit it on 2026-09-25. Since
  `1c3fe01` that batch is dropped, not the whole job; if many batches time
  out, raise the timeout or lower effort for the worker's model.
- **GitHub drops scheduled runs** (v2's 20-min cron fired 7 times in 25 h,
  ~5 a day on 29 Sep). v1 and, since 2026-09-30, v2 self-chain, so the
  schedule only restarts a broken chain, possibly hours late.
- **Cost depends on reasoning effort**, not only on price: output tokens grow
  up to ~15x from low to max effort. The guard uses measured cost per model
  once the ledger has it, priced estimates until then.
- **Stopping a shift:** a normal cancel took effect only with force-cancel
  (2026-09-24) and the snapshot on termination failed, so a cancelled shift
  can lose up to 15 min of state (questions in flight are now retried, s1).
  Never dispatch a shift by hand while another runs: the `guard` job only
  protects scheduled runs.
- **A `failed` run is never retried** (deliberate: no re-spend on a
  deterministic failure every poll). If a real question ends `failed`, read
  why before anything else.
- **Comment post errors:** if `post_comment` raises, the run is recorded
  `failed` even though the forecast was posted (pre-existing behaviour).
- **Credit renewal** depends on MiniBench, and v1 missed round 1 entirely.

## 8. Recipes

- **Actions runs and job logs** (no `gh` here; `GITHUB_ADMIN_TOKEN` from
  `.env`): `GET /repos/Enhso/betomcat/actions/workflows/host.yml/runs`, then
  `GET /repos/Enhso/betomcat/actions/runs/<run>/jobs` and
  `GET /repos/Enhso/betomcat/actions/jobs/<job>/logs` (only after the shift
  ends). Grep for `restored`, `snapshot`, `heartbeat`, `question`, `WARNING`,
  `ERROR`, `dispatched`.
- **Live state mid-shift:** `GITHUB_REPOSITORY=Enhso/betomcat
  GITHUB_TOKEN=$GITHUB_ADMIN_TOKEN uv run betomcat pull-state --out-dir <tmp>`
  (with `.env` loaded for `STATE_KEY`), then `sqlite3 <tmp>/ledger.sqlite`.
- **What vezo3 posted:** the website only shows the logged-in account's
  forecasts and its own private comments, so Hatim cannot see vezo3's from his
  account. Use the API with `METACULUS_TOKEN`: `GET /api/posts/<post>/`
  (`question.my_forecasts.history`) and
  `GET /api/comments/?author=308852&is_private=true&on_post=<post>`. The posts
  *list* endpoint does not fill `my_forecasts`; the detail endpoint does.
  Metaculus rate-limits bursts (~40 quick requests triggered errors).
- **Metaculus from Python:** `urllib` gets a 403 (Cloudflare); use `curl`,
  `httpx` or `requests` with normal headers.
- **Local end-to-end run** (what met the gate): start a local iw-server with
  `IW_DB_ENGINE=sqlite IW_DB_PATH=<tmp>/iw.sqlite IW_BIND=127.0.0.1:8080
  IW_PYTHON_DIR=~/projects/iw/python LLM_API_BASE=https://openrouter.ai/api/v1
  LLM_API_KEY=$OPENROUTER_API_KEY LLM_MODEL=openai/gpt-6-luna
  ~/projects/iw/target/release/iw-server`, then
  `DATA_DIR=<tmp> uv run betomcat forecast --url <question url> [--dry-run]`.
  Use a fresh `DATA_DIR` (a question with a run in the ledger is skipped).
  bot-testing-area questions: post 43327 (binary), 43323 (numeric), 43326
  (MC), 43321 (discrete). Posting live is an external write: the session's
  auto-mode classifier refuses it, so Hatim runs that command himself with `!`.
- **MiniBench and FE Fall schedule:** `GET /api/projects/tournaments/minibench/`
  and `/33121/`; past rounds are unlisted (round of 7 Sep: id 33122).

## 9. Key facts that are easy to get wrong

- **The funded OpenRouter key is BYOK.** `usage.cost` is 0; the real charge is
  `usage.cost_details.upstream_inference_cost`. Day spend is
  `byok_usage_daily`.
- AskNews: `n_articles` > 10 returns 400; wiki results are under `documents`.
- Jev (TypeSafe) is not on OpenRouter; `TYPESAFE_API_KEY` is in `.env`.
- Public repos = public Actions logs. v1 host logging is redacted per logger;
  never log probabilities, rationales, claims or comment text. v2 is the
  upstream template and logs in the clear (accepted: it is the control).
- Comments: Metaculus penalizes long ones (Hatim, 2026-09-24). v1 posts one
  private comment per question, with the forecast that stands: the final, or
  the provisional if no final by the hard deadline (Hatim, 2026-09-25; this
  replaces BUILD_LOG decision 4's "every submission gets its own comment").
  Each model contributes its own `Summary:` line (<= 60 words). The full audit
  report is in `submissions.report`, never posted. v2 posts a <= 100-word
  summary.
- [PRACTICE] questions: v1 never forecasts them; v2 does (template behaviour).
- The `minibench` slug always points at the active round; no config change is
  needed between rounds.
- vezo3 is Metaculus user id 308852, a bot account.

## 10. How this build is run

Hatim is on a Pro plan: usage is the binding constraint. The lead (Opus)
investigates, decides the approach, briefs builders (Sonnet) with strict file
ownership, verifies every report by reading the diff and re-running the
checks, and commits and pushes. Builders never commit. **At most two agents in
parallel** (Hatim, 2026-09-25: one gives no speedup, three risks the usage
limit); work iteratively so a limit never loses logs or state. Sonnet builders
stalled twice on 2026-09-25 (stream watchdog, no progress for 600 s) while
writing tests: keep briefs small, and if a builder stalls twice, the lead
finishes the work. On 2026-09-30 a builder produced nothing after the session
left auto mode (its tool calls presumably waited on approvals nobody saw):
check builders' file mtimes, and stop one that writes nothing for ~15 min.
Builders cannot write repo secrets or post to Metaculus (the classifier
refuses); Hatim does those himself. Hatim has final say on major architectural and tradecraft
decisions and can be persuaded with argued tradeoffs. Every decision goes in
`docs/BUILD_LOG.md`. Replies to Hatim follow the LifeOS format (banner,
closer, at most 15 prose lines unless he asks for depth).
