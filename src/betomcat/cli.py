"""The `betomcat` console script.

Subcommands:
    betomcat daemon                    -- poll every TOURNAMENTS forever.
    betomcat forecast --url URL        -- forecast one question, once.
    betomcat test-run                  -- forecast the bot-testing-area tournament.
    betomcat replay-outbox             -- replay spooled degraded-mode fetches into IW.
    betomcat host                      -- run one GitHub Actions host shift.
    betomcat pull-state                -- download + decrypt the newest state
                                           snapshot for offline review.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import timedelta
from pathlib import Path

import orjson

from betomcat.budget import BudgetGuard
from betomcat.config import Settings, load_settings
from betomcat.daemon import run_daemon
from betomcat.forecast import REPO_ROOT
from betomcat.ledger import Ledger
from betomcat.llm import OpenRouterClient
from betomcat.metaculus import MetaculusWrapper
from betomcat.pipeline import PipelineDeps, run_pipeline
from betomcat.research import IWClient

logger = logging.getLogger(__name__)

TEST_TOURNAMENT_SLUG = "bot-testing-area"
DEFAULT_POOL_PATH = REPO_ROOT / "config" / "models.yaml"
DEFAULT_SHIFT_MINUTES = 330


def build_deps(settings: Settings, dry_run: bool) -> PipelineDeps:
    ledger = Ledger(settings.data_dir / "ledger.sqlite")
    iw = IWClient(settings.iw_url, settings.asknews_api_key, settings.data_dir)
    llm = OpenRouterClient(
        settings.openrouter_api_key or "",
        free_api_key=settings.openrouter_free_api_key,
    )
    metaculus = MetaculusWrapper(settings.metaculus_token, dry_run)
    budget = BudgetGuard(
        funded_api_key=settings.openrouter_api_key,
        free_api_key=settings.openrouter_free_api_key,
        window_end=settings.budget_window_end,
    )
    return PipelineDeps(
        iw=iw,
        llm=llm,
        metaculus=metaculus,
        ledger=ledger,
        pool_path=DEFAULT_POOL_PATH,
        data_dir=settings.data_dir,
        binary_clamp=settings.binary_clamp,
        soft_threshold=timedelta(minutes=settings.soft_threshold_min),
        hard_threshold=timedelta(minutes=settings.hard_threshold_min),
        budget=budget,
    )


async def _teardown(deps: PipelineDeps) -> None:
    await deps.iw.aclose()
    await deps.llm.aclose()
    deps.ledger.close()


async def _cmd_daemon(settings: Settings) -> None:
    deps = build_deps(settings, settings.dry_run)
    try:
        await run_daemon(settings.tournaments, deps, settings.poll_seconds)
    finally:
        await _teardown(deps)


async def _cmd_forecast(settings: Settings, url: str, dry_run: bool) -> None:
    deps = build_deps(settings, dry_run)
    try:
        question = await deps.metaculus.get_question_by_url(url)
        outcome = await run_pipeline(question, deps)
        print(
            f"status={outcome.status} run_id={outcome.run_id} detail={outcome.detail}"
        )
    finally:
        await _teardown(deps)


async def _cmd_test_run(settings: Settings, dry_run: bool) -> None:
    deps = build_deps(settings, dry_run)
    try:
        questions = await deps.metaculus.list_open_questions(TEST_TOURNAMENT_SLUG)
        for question in questions:
            if deps.metaculus.already_forecasted(question):
                continue
            outcome = await run_pipeline(question, deps)
            print(
                f"{question.id_of_question}: status={outcome.status} "
                f"detail={outcome.detail}"
            )
    finally:
        await _teardown(deps)


async def replay_outbox(iw: IWClient, data_dir: Path) -> int:
    """Replay spooled degraded-mode fetches in `data_dir/outbox` into IW.

    Shared by the `replay-outbox` subcommand and `betomcat host`, which
    replays the outbox once at the start of every shift.

    Returns:
        Total number of documents ingested.
    """
    outbox_dir = data_dir / "outbox"
    if not outbox_dir.exists():
        return 0
    replayed_dir = outbox_dir / "replayed"
    files = sorted(p for p in outbox_dir.glob("*.jsonl") if p.is_file())
    total = 0
    for path in files:
        lines = [line for line in path.read_bytes().splitlines() if line.strip()]
        documents = [orjson.loads(line) for line in lines]
        if not documents:
            continue
        ingested = await iw.ingest_documents(documents)
        total += ingested
        replayed_dir.mkdir(parents=True, exist_ok=True)
        path.rename(replayed_dir / path.name)
        logger.info("replayed %s: %d document(s) ingested", path.name, ingested)
    return total


async def _cmd_replay_outbox(settings: Settings) -> None:
    iw = IWClient(settings.iw_url, settings.asknews_api_key, settings.data_dir)
    try:
        if not (settings.data_dir / "outbox").exists():
            print("no outbox directory; nothing to replay")
            return
        total = await replay_outbox(iw, settings.data_dir)
        print(f"total ingested: {total}")
    finally:
        await iw.aclose()


async def _cmd_host(
    settings: Settings, shift_minutes: int, local: bool, max_questions: int | None
) -> None:
    from betomcat.host import run_host

    await run_host(
        settings, shift_minutes=shift_minutes, local=local, max_questions=max_questions
    )


async def _cmd_pull_state(settings: Settings, out_dir: Path) -> None:
    from betomcat import state

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    key = os.environ.get("STATE_KEY", "").encode()
    if not (repo and token and key):
        raise SystemExit(
            "pull-state requires GITHUB_REPOSITORY, GITHUB_TOKEN, and STATE_KEY"
        )
    releases = state.GitHubReleases(repo, token)
    try:
        restored = await state.restore_latest(releases, data_dir=out_dir, key=key)
    finally:
        await releases.aclose()
    if restored:
        print(f"state restored to {out_dir}")
    else:
        print("no state snapshot found")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="betomcat")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("daemon", help="poll every TOURNAMENTS forever")

    forecast_parser = subparsers.add_parser("forecast", help="forecast one question")
    forecast_parser.add_argument("--url", required=True)
    forecast_parser.add_argument("--dry-run", action="store_true")

    test_run_parser = subparsers.add_parser(
        "test-run", help=f"forecast the {TEST_TOURNAMENT_SLUG} tournament"
    )
    test_run_parser.add_argument("--dry-run", action="store_true")

    subparsers.add_parser("replay-outbox", help="replay spooled fetches into IW")

    host_parser = subparsers.add_parser(
        "host", help="run one GitHub Actions host shift"
    )
    host_parser.add_argument("--shift-minutes", type=int, default=DEFAULT_SHIFT_MINUTES)
    host_parser.add_argument("--local", action="store_true")
    host_parser.add_argument("--max-questions", type=int, default=None)

    pull_state_parser = subparsers.add_parser(
        "pull-state", help="download + decrypt the newest state snapshot"
    )
    pull_state_parser.add_argument("--out-dir", required=True)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = _parse_args(argv)
    settings = load_settings()

    if args.command == "daemon":
        asyncio.run(_cmd_daemon(settings))
    elif args.command == "forecast":
        asyncio.run(_cmd_forecast(settings, args.url, args.dry_run or settings.dry_run))
    elif args.command == "test-run":
        asyncio.run(_cmd_test_run(settings, args.dry_run or settings.dry_run))
    elif args.command == "replay-outbox":
        asyncio.run(_cmd_replay_outbox(settings))
    elif args.command == "host":
        asyncio.run(
            _cmd_host(settings, args.shift_minutes, args.local, args.max_questions)
        )
    elif args.command == "pull-state":
        asyncio.run(_cmd_pull_state(settings, Path(args.out_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
