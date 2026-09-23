# Handoff for the next session lead

Written 2026-09-23 at the end of a usage-limited session. Read this first, then
`docs/BUILD_LOG.md` (full trace and every decision), `docs/contracts.md`
(IW <-> bot interfaces), `docs/spec.md` and `docs/brief.md` (intent).

## Where things stand

v1 bot (`vezo3`) and the Intelligence Workbench are built, tested and committed
locally. **Nothing is live yet.** Two things gate go-live: the push to GitHub and
Hatim's one-time secrets script.

| Repo | Location | State |
|---|---|---|
| betomcat (v1) | `~/projects/betomcat`, public `Enhso/betomcat` | local `main` ahead of GitHub; **not pushed on purpose** (see below) |
| IW | `~/projects/iw`, private `Enhso/iw` | `c0a0bda`, pushed; Hatim's own `prompt.txt` edit is unstaged, leave it |
| vezocontrol (v2) | `~/projects/vezocontrol` | `1b1eda1`, not on GitHub yet (the setup script creates and pushes it) |

Green at last check: bot 163 tests, IW worker 176, IW Rust 104; ruff, mypy,
clippy, fmt clean.

Lessons from this session worth keeping:
- Builder "green" reports were right about tests but twice wrong about behaviour
  (C4a's claim cap and log redaction). Always demand one real run, and read the
  ledger yourself before trusting it.
- Every live check against a real API found something the mocks hid: AskNews's
  `documents` key and 10-article cap, BYOK billing, the Summer tournament pin, the
  22-day practice question. Budget one cheap live probe per external integration.
- The auto-mode classifier blocks secret writes and public-repo creation from
  this session. Package those steps as a script Hatim runs himself.

## Go-live checklist (in order)

1. ~~C3c~~ done and verified, `f73ac98` (BYOK cost + day spend, late-window
   claiming; bot suite 163 tests green). Check suite command:
   `uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src && uv run pytest -q`.
2. **Re-run the dry run yourself** (so far it is only builder-reported): build IW
   (`cd ~/projects/iw && cargo build --release`), then from betomcat
   `set -a; . ./.env; set +a; IW_DIR=~/projects/iw DRY_RUN=1 LATE_WINDOW_MINUTES=100000 uv run betomcat host --local --shift-minutes 20 --max-questions 1`
   with a temp `DATA_DIR`. Check the ledger: run `submitted`, two models `ok`,
   **cost_usd now non-zero** (proves the BYOK fix).
3. **Hatim runs** `! uv run --with pynacl --with cryptography python scripts/setup_github.py`
   (sets betomcat + vezocontrol secrets, IW deploy key, creates/pushes
   vezocontrol). This session's auto-mode classifier refuses secret writes and
   public-repo creation, so do not try to do these yourself.
4. **Push betomcat** `git push origin main`. From then on `host.yml` runs on its
   `*/30` schedule and **forecasts for real** (no DRY_RUN in the workflow).
   Trigger the first shift by hand (Actions -> host -> Run workflow) and watch the
   first run's log: checkout of `Enhso/iw` via deploy key, cargo build, iw-server
   healthy, first poll heartbeat.
5. After the first real submission, open the question on Metaculus as `vezo3` and
   read the private comment (spec s9 layout, contracts E).
6. Check v2's first scheduled runs in `Enhso/vezocontrol` Actions. It runs only
   on `google/gemma-4-31b-it:free`, which failed 3/4 live tries on 2026-09-23
   (Google capacity 503s). If it fails most questions, raise it with Hatim.
7. Ask Hatim to revoke the broad GitHub token in `.env` (`GITHUB_ADMIN_TOKEN`; it
   has delete_repo and admin:org) once setup is done.

## Key facts that are easy to get wrong

- **Funded OpenRouter key is BYOK.** `usage.cost` is 0; the real charge is
  `usage.cost_details.upstream_inference_cost`. Day spend is `byok_usage_daily`.
  `limit_remaining` is already net of BYOK. $98.99 remaining on 2026-09-23.
- Funded key allows only openai/anthropic/google providers (1000 free req/day,
  Google `:free` models included). Personal free key: 50 free req/day, used for
  the non-Google `:free` models.
- Budget window: the two MiniBench weeks, ends `2026-10-05T00:00Z`
  (`BUDGET_WINDOW_END`), about $8/day. MiniBench opens roughly 1 question/hour,
  each open 3 h, resolving 1-4 Oct; that round's performance decides whether
  Metaculus renews the credit.
- AskNews: `n_articles` > 10 returns 400. Wiki results are under `documents`.
- Jev (TypeSafe) is not on OpenRouter; `TYPESAFE_API_KEY` is in `.env`. All gates
  fail open.
- mnestic rejects HNSW on TxTime relations: vectors live in plain
  `claim_vec`/`evidence_vec` relations. Every other IW relation is TxTime, so
  `:as_of` gives a leakage-free historical view.
- The upstream template's pinned forecasting-tools (0.2.92) targets FE **Summer**;
  vezocontrol pins the tournament id to 33121 (FE Fall 2026).
- Public repo = public Actions logs. Host logging is redacted per logger; never
  log probabilities, rationales, claims or comment text.

## Not built yet (chunk C5 and follow-ups)

Priority order after go-live:
1. **Daily weight update** inside the host loop (spec s5): per-model scores on
   resolved questions from the ledger, uniform until enough resolutions,
   frozen-at-draw semantics already in place. MiniBench resolves 1-4 Oct, so this
   matters from then.
2. **Referee gate** (spec s5): classify why the two models disagree; if before the
   soft threshold, re-research and have both models re-forecast (Hatim approved
   the extra cost). Currently a no-op stub in `pipeline.py`. Its numeric trigger
   is an approximation (see C3 notes in BUILD_LOG).
3. **Weekly digest** as a GitHub issue on betomcat (Hatim approved): weights,
   draw-fallback frequency, pacing decisions, family merge candidates, MiniBench
   round performance, review-bot mechanical checks.
4. **Personal-history backfill**: Hatim's Metaculus account `Enhso`, token in
   `.env` as `METACULUS_PERSONAL_TOKEN` (read-only use). Import into IW via
   `POST /api/history`, family-tag with Jev; weekly re-check when families were
   minted (spec s3).
5. Family merge candidates (spec s2): centroid similarity over the existing
   feature-hash embeddings, ranked pairs into the digest.

## Open questions for Hatim

- Review `prompts/*.md` (forecasting tradecraft; he agreed to).
- Free-tier models are set to `reasoning: null`; flip any that support reasoning.
- Whether to forecast [PRACTICE] questions at all (currently yes, near close).

## How this build was run

Hatim is on a Pro plan: usage is the binding constraint. The lead (Opus) plans,
writes precise builder briefs with strict file ownership, verifies every report
by re-running the checks, and commits. Builders (`builder` agent type, Sonnet)
never commit. Record every decision and finding in `docs/BUILD_LOG.md`. Replies
to Hatim follow the LifeOS format (banner, closer, at most 15 prose lines).
