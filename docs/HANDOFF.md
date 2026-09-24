# Handoff: what remains before final deployment

Rewritten 2026-09-24. Read this first, then `docs/BUILD_LOG.md` (full trace and
every decision), `docs/contracts.md` (IW <-> bot interfaces), `docs/spec.md` and
`docs/brief.md` (intent). This file only lists what is still open.

## Where things stand

| Repo | State |
|---|---|
| betomcat (v1, public `Enhso/betomcat`) | pushed and running since 2026-09-24 19:21 UTC; `bf98280` fixes state persistence; shift `36050659995` is the current one |
| IW (private `Enhso/iw`) | `c0a0bda`, pushed; Hatim's `prompt.txt` edit is unstaged, leave it |
| vezocontrol (v2, public `Enhso/vezocontrol`) | live since `dfbc07b`; `c045735` adds short comments |

Setup is done: all Actions secrets exist on both repos and IW has a read-only
deploy key for betomcat (Hatim ran `scripts/setup_github.py` on 2026-09-23).

Check suite (betomcat):
`uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src && uv run pytest -q`.

## Before calling v1 deployed

1. **Watch the shift chain.** Shift `36050659995` (started 19:48 UTC, 330-min
   shifts) should dispatch its successor around 01:20 UTC on 25 Sep. The
   successor's log must show `state snapshot restored` (the first cross-shift
   restore on the fixed code), and snapshots must keep landing in the oldest
   `state` draft with `iw.sqlite` inside (`betomcat pull-state` to check).
   Job logs only appear once a shift ends; mid-shift, read the snapshot.
2. **First real submission.** Open the question on Metaculus as `vezo3`: the
   private comment must be the short form (header plus one `Summary:` bullet per
   model, see contracts E). The full report is in the ledger
   (`submissions.report`); `betomcat pull-state` decrypts the latest snapshot.
3. **Read the ledger after a day of shifts**: per-model failure rates, how often
   the replacement draw fires (`draws` rows beyond the width), real cost per
   question. Disable a pool model that fails most attempts.
4. **v2's first short comment.** The next vezocontrol run with a question
   posts through `c045735`; check the comment is the `# Rationale` form and the
   `forecast-reports-*` artifact holds the full report.
5. **Revoke `GITHUB_ADMIN_TOKEN`** (Hatim; it has delete_repo and admin:org)
   once monitoring through the API is no longer needed.

## Known risks to watch

- **Google via the funded key is fragile.** Every `google/` model goes through a
  BYOK Google AI Studio key. Seen on 2026-09-24: Gemma 429s (16k input
  tokens/min free-tier quota, shared by v2 and anything else on the key),
  Gemma `500 INTERNAL` on ~55% of large prompts, and gemini-3.8-flash
  `503 high demand` even on a 5-token prompt. Gemma is now disabled in v1's pool
  and out of IW's worker chain; the replacement draw covers the other two.
- **GitHub drops scheduled runs.** v2's 20-minute cron fired 7 times in 25 h.
  v1 self-chains its shifts, so it only depends on the schedule to restart a
  broken chain. If v1 shows long gaps between shifts, investigate this first.
- **Model cost depends on effort, not only on price.** GPT-6 Luna is $0.10 in /
  $0.50 out per million tokens (OpenAI, confirmed 2026-09-23), but reasoning
  effort multiplies output tokens (a 15x cost spread between low and max on the
  same task). The budget guard uses measured cost once the ledger has it; until
  then it estimates from `price_in`/`price_out` in `config/models.yaml`.
- **Stopping a shift:** a normal cancel only took effect with force-cancel on
  2026-09-24, and the snapshot on termination failed (token revoked mid-cancel),
  so a cancelled shift can lose up to 15 min of state. Never dispatch a shift by
  hand while another runs: the `guard` job only protects scheduled runs.
- The personal free key allows 50 requests/day; its models drop out when it
  runs low.
- MiniBench openings are irregular (not hourly); a quiet stretch with 0 open
  questions is normal. The round resolves 1-4 Oct and decides the credit renewal.

## Key facts that are easy to get wrong

- **The funded OpenRouter key is BYOK.** `usage.cost` is 0; the real charge is
  `usage.cost_details.upstream_inference_cost`. Day spend is `byok_usage_daily`.
- Budget window ends `2026-10-05T00:00Z` (`BUDGET_WINDOW_END`), about $8/day.
- AskNews: `n_articles` > 10 returns 400; wiki results are under `documents`.
- Jev (TypeSafe) is not on OpenRouter; `TYPESAFE_API_KEY` is in `.env`.
- Public repos = public Actions logs. v1 host logging is redacted per logger;
  never log probabilities, rationales, claims or comment text. v2 is the
  upstream template and logs research and reasoning in the clear (accepted: it
  is the control).
- Comments: Metaculus penalizes long ones (Hatim, 2026-09-24). v1 posts each
  model's own `Summary:` line (<= 60 words); v2 posts a <= 100-word summary.
- [PRACTICE] questions: v1 never forecasts them; v2 does (template behaviour).

## Not built yet (after deployment, in priority order)

1. **Daily weight update** in the host loop (spec s5): per-model scores on
   resolved questions from the ledger, uniform until enough resolutions,
   frozen-at-draw semantics already in place. Matters from 1 Oct.
2. **Referee gate** (spec s5): classify why the two models disagree; before the
   soft threshold, re-research and re-forecast (Hatim approved the extra cost).
   Currently a no-op stub in `pipeline.py`.
3. **Weekly digest** as a GitHub issue on betomcat: weights, draw-fallback and
   replacement frequency, pacing decisions, family merge candidates, MiniBench
   performance.
4. **Personal-history backfill**: Hatim's Metaculus account `Enhso`
   (`METACULUS_PERSONAL_TOKEN`, read-only). Import via `POST /api/history`,
   family-tag with Jev.
5. **Family merge candidates** (spec s2): centroid similarity over the
   feature-hash embeddings, ranked pairs into the digest.

## How this build is run

Hatim is on a Pro plan: usage is the binding constraint. The lead (Opus) plans,
briefs builders (Sonnet, one at a time) with strict file ownership, verifies
every report by re-running the checks and one real run, and commits. Builders
never commit. Every decision goes in `docs/BUILD_LOG.md`. Replies to Hatim
follow the LifeOS format (banner, closer, at most 15 prose lines).
