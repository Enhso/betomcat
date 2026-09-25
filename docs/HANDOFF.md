# Handoff: state of the deployment and what remains

Rewritten 2026-09-25 (end of session). Read this first, then
`docs/BUILD_LOG.md` (full trace, every decision and why), `docs/contracts.md`
(IW <-> bot interfaces, comment layout), `docs/spec.md` and `docs/brief.md`
(intent). This file lists the state and everything still open, in the order
the next session should take it.

## 1. Where things stand

v1 is deployed: the Actions host has run unattended since 2026-09-24 19:21
UTC, and the BUILD_LOG go-live gate (one dry run and one live run on
bot-testing-area) was met on 2026-09-25. It has not yet forecast a real
tournament question: none has been open since it went live (see s3).

| Repo | Commit | State |
|---|---|---|
| betomcat (v1, public `Enhso/betomcat`) | `a055eff` on `main` | Host shifts chain every ~5.5 h. Today's commits (below) are pushed but only run from the first shift started after ~17:40 UTC 2026-09-25. |
| IW (private `Enhso/iw`) | `1c3fe01` on `main` | Checked out by every shift through the deploy key. Hatim's unstaged `prompt.txt` edit: leave it, never stage it. |
| vezocontrol (v2, public `Enhso/vezocontrol`) | `c045735` | Template bot, the control. Its recent scheduled runs all succeed (checked 2026-09-25). |

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

Check suite (betomcat, 195 tests):
`uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src && uv run pytest -q`
Check suite (IW worker, 179 tests), from `~/projects/iw/python`: the same four
commands.

## 2. Verify first (start of the next session)

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
3. **First real FE Fall question (from Mon 28 Sep).** FE Fall questions are
   long-window; the daemon claims them only within 180 min of their close
   (`LATE_WINDOW_MINUTES`), so heartbeats show them as `deferred` until then.
   For the first one forecast, check through the API as vezo3 (s8): exactly one
   private comment, short form (header plus one `Summary:` bullet per model,
   contracts E), attached to the forecast that stands. Then check its ledger
   rows with `pull-state`: `runs.status`, `submissions` (a `provisional` with
   `comment_posted = 0` then a `final` with `1`, or a lone `provisional` with
   `1`), `model_forecasts` (attempts, errors, `cost_usd`), `draws`.
4. **IW research not degraded on live questions.** In the ledger,
   `runs.degraded` should be 0. If it is 1, the pipeline fell back to direct
   AskNews: find the cause in the shift log (IW 502s are logged by
   `betomcat.research` as redacted; the iw-server stderr is in the job log).
5. **v2's first short comment** (still open from 2026-09-24): the next
   vezocontrol run that forecasts a question posts through `c045735`; check the
   comment is the `# Rationale` form and the `forecast-reports-*` artifact
   holds the full report.

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

The order changed on 2026-09-25: the weight update was first because MiniBench
round 1 was supposed to feed it from 1 Oct, but v1 has no forecasts in round 1,
so there is nothing for it to score before mid-October. The referee and
personal history improve every forecast from now on. **Hatim decides the
order**; this is the lead's proposal with reasons.

### 5.1 Referee gate (spec s5) -- proposed first

- **What:** when the two drawn models disagree past spec s5's bars (binary:
  |p_a - p_b| > 0.15; MC: total variation distance; numeric: medians more
  than 0.25 of the range apart; see `_maybe_referee` in `pipeline.py`), a cheap-tier model classifies *why*
  (stale info, genuine uncertainty, misread resolution criteria, ...). If time
  remains before the soft deadline, re-run research and re-forecast **both**
  models (Hatim approved the extra cost, BUILD_LOG 2026-09-22). The
  disagreement type is logged and goes to the digest.
- **Hard rule (spec s5):** it never overrides, excludes or adjusts the
  weighted-average number. A misfiring referee must never corrupt a
  submission.
- **Current state:** `default_referee` is a no-op stub, and `_maybe_referee`
  runs *after* the final is submitted (inside `_submit`), so today it could
  only label. Re-research needs the call moved to the point where both results
  are in but before the final is posted (the provisional already protects
  against a miss). That is a change to the ladder's shape in
  `_run_model_ladder`: present it to Hatim before building.
- **Cost:** the classification is cheap-tier (spec s5: frontier spend only on
  the two forecast calls; re-forecasting the two drawn models is those same
  calls again). A re-forecast roughly doubles the cost of a disagreeing
  question (~$0.20 -> ~$0.40 average).
- **Open questions for Hatim:** which cheap model classifies (Jev fits the
  "typed gate" role in the brief; gpt-6-luna is the IW default), the category
  list, and whether re-research reuses the same IW query or a targeted one
  built from the disagreement type.
- **Tests:** the fake-clock harness in `tests/test_pipeline.py` already drives
  the ladder; add cases for diverge -> re-forecast before soft, diverge after
  soft -> label only, referee failure -> final unchanged.

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

### 5.3 Daily weight update (spec s5)

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

## 6. Decisions pending with Hatim

1. **Next budget horizon, before 19 Oct.** His rule: MiniBench round 2 first
   (about half the ~$99), the rest to FE Fall until an extension. Measured and
   priced costs: ~$0.20 per question on average (two calls), ~$0.90 worst case
   (Fable + Astra); a 60-question round is ~$12 expected. If the credit is
   renewed, the horizon can follow the renewal period. The constant is
   `DEFAULT_BUDGET_WINDOW_END` in `src/betomcat/config.py` (env
   `BUDGET_WINDOW_END` overrides it; the workflow does not set it).
2. **Priority order of s5** (the lead proposes referee, personal history,
   weights, digest, merges).
3. **Referee design** (s5.1: ladder change, classifier model, categories).
4. **Weight scoring rule** (s5.3).

## 7. Known risks

- **Google via the funded key is fragile.** Every `google/` model goes through a
  BYOK Google AI Studio key: Gemma 429s on a 16k tokens/min free-tier quota
  shared with v2, Gemma `500 INTERNAL` on ~55% of large prompts,
  gemini-3.8-flash `503 high demand` even on tiny prompts (2026-09-24). Gemma
  is disabled in v1 and out of IW's worker chain; the replacement draw covers
  Flash and 3.1 Pro.
- **IW worker timeout.** Each extraction call has a 120 s httpx timeout
  (`iw/python/src/iw_research/llm.py`). One batch hit it on 2026-09-25. Since
  `1c3fe01` that batch is dropped, not the whole job; if many batches time
  out, raise the timeout or lower effort for the worker's model.
- **GitHub drops scheduled runs** (v2's 20-min cron fired 7 times in 25 h).
  v1 self-chains, so the schedule only restarts a broken chain.
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
finishes the work. Hatim has final say on major architectural and tradecraft
decisions and can be persuaded with argued tradeoffs. Every decision goes in
`docs/BUILD_LOG.md`. Replies to Hatim follow the LifeOS format (banner,
closer, at most 15 prose lines unless he asks for depth).
