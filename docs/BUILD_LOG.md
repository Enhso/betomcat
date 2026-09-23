# betomcat build log

Running trace of the v1 bot + Intelligence Workbench (IW) Phase-1 build.
Newest entries at the bottom of each section. Source of intent: `docs/brief.md`,
`docs/spec.md`. Cross-repo contracts: `docs/contracts.md`.

---

## 0. Audit (2026-09-22)

### betomcat (this repo)

- A plain clone of `Metaculus/metac-bot-template` (last upstream commit `6dab04c`),
  remote `git@github.com:Enhso/betomcat.git`. Nothing custom yet.
- `main.py` is the *Summer* 2026 template; `forecasting-tools` 0.3.1 already ships a
  Fall 2026 template and points `CURRENT_AI_COMPETITION_ID` at FE Fall 2026 (33121)
  and `CURRENT_MINIBENCH_ID` at `minibench`.
- **Hazard:** `.github/workflows/run_bot_on_tournament.yaml` runs the *template*
  every 20 min. If a `METACULUS_TOKEN` secret were ever added to this repo, the
  template would forecast under v1's account and pollute the v1-vs-v2 comparison.
  Removed in chunk C0.

### Intelligence Workbench (`../iw`)

What runs (verified 2026-09-22: `cargo test` 7/7, `uv run pytest` 90/90):

- Rust `iw-server` (Axum 0.8, mnestic 0.18 embedded graph/vector DB) owns the DB.
  Endpoints: `/health`, `POST /api/ingest`, `POST /api/questions`,
  `GET /api/dossiers/{id}/briefing`.
- Python `iw-research` stateless worker: Wikipedia + arXiv fetch, polars
  normalisation, one-LLM extraction into entities/events/claims/evidence/causal
  links, forecasting-language filter. Spawned by Rust as a subprocess.
- Deterministic 11-section briefing renderer (PRD s10), no LLM prose, cannot emit
  probabilities by construction. Epistemic firewall already enforced.

What does not exist (gaps against brief s4 / spec s1-4):

| Need | State |
|---|---|
| As-of corpus: fetch time + content hash, versioned, never overwritten | Missing. `source` has `retrieved_at` only; every relation is a keyed `:put`, so re-ingest overwrites. No content stored (only excerpts). |
| "Evidence as of T" query | Missing. |
| Families as first-class objects | Missing. |
| Claim support score | Partial: evidence has `stance` + `quality`; no per-claim support. |
| Personal / bot forecast history | Missing. |
| AskNews news + wiki providers | Missing (Wikipedia + arXiv only). |
| Jev gates | Missing. |
| Persistence in practice | `sqlite` engine exists; default is in-memory. |

`docs/todo.md` in IW describes an older GitOps design and is stale; `plan-phase1.md`
is what was actually built.

### External services (verified 2026-09-22)

- OpenRouter key: **$99.33 remaining**, 1000 free-model requests/day.
  Account's allowed-providers list is `openai, anthropic, google-ai-studio`, so
  most `:free` models are refused (only `google/gemma-4-31b-it:free` works).
  `thinkingmachines/inkling:free` is agentic-harness-only: unusable.
- AskNews: new-style `ank_` key works as `Authorization: Bearer`; `/v1/news/search`
  and `/v1/wiki/search` both return 200.
- Jev (TypeSafe): **not on OpenRouter**. Needs its own key from
  `console.typesafe.ai` (waitlist removed 2026-09-20). `POST
  https://api.typesafe.ai/v1/systemone`, model `jev-latest`/`jev-1.13`.
- Metaculus: no bot token yet.

---

## 1. Architecture decisions (Claude Code's call per brief s8 / spec s10)

1. **The Workbench is IW, extended in place**, not a bot-side corpus. Rust keeps
   sole DB ownership; the Python worker stays stateless.
2. **As-of corpus = mnestic bitemporality.** Every IW relation gets a trailing
   `tt: TxTime` key column. Transaction time is stamped by the engine at commit
   and cannot be supplied by callers, so `:as_of "T"` reproduces the corpus
   (and the briefing) exactly as it stood at T, with no leakage by construction.
   `:put` on a TxTime relation appends a version; nothing is overwritten.
   Document bodies are content-addressed (`blob {content_hash => content}`).
   `fetched_at` is also kept as a plain value (worker wall clock).
3. **Bot talks to IW over localhost HTTP.** If IW is *down* (connection refused /
   5xx), the bot falls back to a first-class direct-AskNews research path and
   spools the fetched documents (with fetch time + sha256) to an outbox that is
   replayed into IW later, so the as-of discipline holds even in degraded mode.
   If IW is merely *slow*, the bot waits (spec s6: no thin-briefing shortcut).
4. **Provisional submission.** The bot submits as soon as it has any valid
   forecast and resubmits when it has a better one (e.g. the second model
   arrives). Spot scoring counts only the last pre-close forecast, so the final
   number is unchanged from spec s5/s6; this only removes crash-between-steps
   misses. Every submission gets its own private comment.
5. **Weight job runs on the VM (systemd timer, `Persistent=true`, retry on
   failure), not GitHub Actions.** Spec s7 allows Actions; it does not require
   it, and the per-model ledger lives on the VM anyway.
6. **Hosting: Oracle Cloud Always Free (Ampere A1 ARM, up to 4 OCPU / 24 GB).**
   Enough RAM to build Rust + run mnestic + Python on the box. Fallback: GCP
   e2-micro with a prebuilt binary.
7. **Package management: uv** (converted from Poetry). `forecasting-tools` is used
   for its Metaculus client, question models and numeric-distribution helpers,
   not for its `ForecastBot` orchestration (our draw / reconcile / deadline logic
   does not fit its median-of-N shape).
8. **Forecasting prompts live in `prompts/*.md`**, editable without code changes.
   They are tradecraft: flagged for Hatim's review.

---

## 2. Chunk plan

| # | Chunk | Repo | Status |
|---|---|---|---|
| C0 | Repo prep: uv, `.env`, remove template workflows, this log, contracts | betomcat | done |
| C1 | IW Rust: TxTime schema, blobs, families, claim support, history, new endpoints | iw | pending |
| C2 | IW worker: AskNews news+wiki, content hashing, Jev client + gates, family classify/mint, stdin/stdout subcommands | iw/python | pending |
| C3 | Bot core: pool/draw/reconcile, forecasters + prompts, deadline ladder, comment, ledger, IW client + fallback, daemon + CLI | betomcat | pending |
| C4 | Deploy: VM bootstrap, systemd units (iw-server, daemon, timers), deploy script | both | blocked on VM |
| C5 | Weekly/daily jobs: weights, referee gate, digest, review-bot mechanics, merge candidates, personal-history backfill | both | pending |
| C6 | v2 control bot: fork upstream template, secrets, Actions | GitHub | Hatim |

Go-live gate for MiniBench: C1 + C2 + C3 green, C4 deployed, Metaculus token set,
one successful dry run and one live run on the bot-testing-area tournament.

---

## 3. Open items for Hatim

Tracked in the chat hand-off; resolved answers get recorded here.

Accounts / access (blocking go-live marked *):

- [ ] * Metaculus v1 bot account + token (primary, prize-eligible).
- [ ] * Oracle Cloud Always Free account + Ampere A1 VM (Ubuntu 24.04, 2 OCPU /
      12 GB is plenty), with `~/.ssh/id_ed25519.pub` from the dev machine installed;
      send the public IP.
- [ ] OpenRouter `settings/privacy`: widen allowed providers so free models work
      (currently only openai/anthropic/google-ai-studio). Budget-relevant.
- [ ] TypeSafe key from `console.typesafe.ai` (Jev gates fail open without it).
- [ ] v2 control: second Metaculus bot account with version suffix; import
      `Metaculus/metac-bot-template` as a new repo; secrets `METACULUS_TOKEN` (v2's)
      and `ASKNEWS_API_KEY` only. **Never give v2 the OpenRouter key**: the
      unmodified template would then run `openrouter/openai/gpt-4o` x5 per question
      on the shared $100. With only a Metaculus token it uses the free
      `metaculus/gpt-4o` proxy.
- [ ] Personal history: Metaculus username + either a data export or a personal
      token (read-only use) for the one-time backfill.

Decisions (defaults in force until answered):

- [ ] Model pool: Sonnet 5, Opus 5.5, GPT-6 Sol (paid) + Gemma 4 31B (free) +
      three free models waiting on the provider setting.
- [ ] Budget pacing guard: auto-disable the priciest paid model when projected
      spend exceeds remaining credit (default) vs. alert only.
- [ ] Referee-triggered re-research (spec s5): does it also re-run both forecasts
      on the refreshed briefing (costs 2 more frontier calls on disagreement
      questions)? Default: yes, only if before the soft threshold.
- [ ] Binary clamp [0.01, 0.99] on each model's probability (default) vs.
      Metaculus's [0.001, 0.999].
- [ ] Weekly digest delivery: GitHub issue on this repo (default) vs. SMTP.
- [ ] Review `prompts/*.md` once C3 lands (tradecraft).
- Interpretation recorded: spec s2 "confidence >= 50%" is implemented as the
  selected family's Jev probability >= 0.5 (Jev's separate `confidence` field is
  logged alongside).

### Answers received 2026-09-22 (evening)

- v1 bot `vezo3`, v2 control `vezocontrol`, tokens in `.env` (gitignored).
  Principal's Metaculus account `Enhso`; personal token in `.env` for the
  read-only history backfill.
- **No Oracle (no credit card). Host = GitHub Actions, hardened** (deviation from
  spec s7, approved by Hatim as the fallback). This laptop is not always-on
  (uptime < 1 h at check), so it is not a host. The repo is public, so Actions
  minutes are unlimited. Design (chunk C4a):
  - one long-running job per ~5.5 h "shift" that polls every few minutes and
    runs the full pipeline plus iw-server inside the job; at shift end it stops
    claiming, drains, snapshots state, and dispatches its successor via
    `workflow_dispatch` (GITHUB_TOKEN may trigger `workflow_dispatch`). No
    dependence on cron timing.
  - a `*/30` cron backstop that starts a shift only if none is running/queued.
  - state (IW sqlite corpus, ledger, weights, outbox) persisted as an
    **encrypted** snapshot (Fernet, `STATE_KEY` secret) in a GitHub Release asset,
    after every completed question and every 15 min.
  - daily weights + weekly digest run inside the shift loop (one state owner,
    retry = next loop iteration), not as separate workflows.
  - public repo means public logs: the host runs with redacted logging (question
    ids and statuses only; never probabilities, rationales or claims before close).
  - MiniBench windows are 3 h, so a start-up gap of minutes costs nothing.
- **Two OpenRouter keys.** `OPENROUTER_API_KEY` (Metaculus-funded, allowed
  providers openai/anthropic/google, 1000 free req/day) and
  `OPENROUTER_FREE_API_KEY` (personal, free tier, **50 free req/day**). Routing:
  paid models and Google `:free` models -> funded key; all other `:free` -> free key.
  Free key verified 2026-09-22: nemotron-3-ultra, nemotron-3-super, nex-n2.5-pro,
  dots-3-note OK; qwen3.8-27b and glm-5.2 "Provider returned error".
- **Pool: as large as possible, reasoning effort high.** Paid: claude-fable-5.1,
  gpt-6-astra, claude-opus-5.5, claude-sonnet-5, gpt-6-sol, gemini-3.1-pro-preview,
  gemini-3.8-flash, gpt-6-luna. Free: gemma-4-31b-it (funded key) + the six free-key
  models. The ledger's reliability data decides removals later.
- **Budget pacing (replaces the earlier default).** Window = the two MiniBench
  weeks, ending 2026-10-05T00:00Z (configurable). Daily budget = remaining credit
  / days left in window, recomputed each UTC day. Spend today = funded key's
  `usage_daily`. When spend runs ahead of the pro-rated daily budget, drop the
  most expensive models first (by measured cost per call, priced estimate until
  measured), progressively. Free-key models drop when its daily quota runs low.
- Estimate at 2026-09-22: MiniBench opened 44 questions on day 1 of the round,
  then ~1/hour (all open 3 h, resolve 1-4 Oct); main tournament 2 questions so
  far. Assuming ~12k input + ~10k output tokens per forecast call at high effort,
  a uniform draw from the full pool averages ~$0.13/call, ~$0.26/question; at
  20-30 questions/day that is $5-8/day against a budget of ~$8/day. The guard
  will bite on burst days (Fable and Astra, ~$0.60/call, drop first).
- Referee re-research re-runs both forecasts: yes. Clamp [0.01, 0.99]: yes.
  Digest as GitHub issue: yes. Prompt review: yes, after C3.
- **v2 blocker found:** the Metaculus LLM proxy refuses `vezocontrol` ("You don't
  have an allowance for model gpt-4o"). The unmodified template with the free key
  would call `openrouter/openai/gpt-4o` (paid) and fail every question; with the
  funded key it would spend ~$0.10/question from v1's budget. Decision pending.
- IW has no GitHub remote yet; an Actions host must be able to fetch it.

---

## 4. Chunk log

- **C0** (2026-09-22) done: uv, `.env`, template workflows removed. `42a49a6`.
- **C2** (2026-09-22) done by builder; live check found the AskNews wiki payload
  is under `documents` (not `results`) and news text is `full_text`/`summary`;
  fixed. Live run on a MiniBench question: pipeline works (58 s), but the
  relevance gate dropped 17/20 docs incl. the tournament's own wiki page and
  extraction gave 1 claim / 0 evidence -> chunk C2b.
- **C1** (2026-09-23) builder ran out of session mid-clippy; finished here (one
  `len_zero` lint). Findings: HNSW is rejected on TxTime relations, so vectors
  sit in plain `claim_vec`/`evidence_vec` side relations; sqlite persistence and
  version retention are tested. 104 Rust tests green. IW committed `8242992` and
  pushed to private `Enhso/iw`.
- **C3** (2026-09-23) verified and committed `e888097` (77 tests).
- **v2 / vezocontrol** prepared at `../vezocontrol` (`1b1eda1`). Findings: the
  template's pinned forecasting-tools 0.2.92 still targets FE *Summer* 2026, so
  as-is v2 would only forecast MiniBench; bumping to 0.3.1 conflicts with the
  optional review plugin, so the tournament id is pinned to 33121 instead. All
  LLM purposes pinned to `openrouter/google/gemma-4-31b-it:free` on the funded
  key (free, 1000 req/day; the personal free key's 50/day cannot carry the
  template's ~11 calls per question).
- **Permission boundary:** the auto-mode classifier blocks this session from
  writing repo secrets and from creating public repos. Both live in
  `scripts/setup_github.py`, which Hatim runs once himself.
- **C2b** (2026-09-23) `c0a0bda` (IW, pushed). Root cause of thin research:
  AskNews rejects `n_articles` > 10 with a 400, so every live news fetch was
  empty. Plus: relevance gate re-worded (background counts), cut 0.15, top-5
  floor; batched extraction (4 docs/call). Live: Gulf Cup 1 -> 28 claims, ECB 56,
  all with evidence + support. Gemma free failed 3/4 live tries (Google capacity
  503s, truncated JSON on large prompts), so the worker chain falls through to
  gpt-6-luna; v2 (Gemma-only) may fail questions on busy days.
- **C3b** (2026-09-23) `cafde2d`: 15-model pool at effort high, two-key routing,
  context-fit filter, pacing guard (live: daily budget $8.28, 15/15 eligible).
- **C4a** (2026-09-23) `9d873dc`: Actions host, encrypted draft-release state,
  self-chaining shifts, redacted logs. Builder found two real bugs (claim cap
  consumed by the per-poll gate => nothing ever claimed; root-logger redaction
  filter never saw propagated records => probabilities in logs). Live dry run:
  one real question, sonnet-5 + gpt-6-astra, provisional then final, 84 s.
- **Finding: the funded key is BYOK.** Responses report `cost: 0` with the real
  charge in `usage.cost_details.upstream_inference_cost`; the key's day spend is
  in `byok_usage_daily`, not `usage_daily`. Unfixed, the pacing guard would see
  $0 spend and treat Astra/Fable as free. Fixed in C3c.
- **Finding: long-window questions.** The only open FE Fall question is a
  22-day [PRACTICE] one; forecasting at open would be weeks stale at close under
  spot scoring. C3c adds late-window claiming (claim only within 180 min of
  close; MiniBench's 3 h windows are claimed at once).
- Real spend so far (setup + tests + dry run): $0.35.
- Push to GitHub is held until C3c lands: once `host.yml` is on `main` and the
  secrets exist, the schedule starts live forecasting.
