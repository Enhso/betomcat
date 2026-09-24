"""Tests for state.py: snapshot round trip, asset rotation, restore."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet
from pytest_httpx import HTTPXMock

from betomcat.state import (
    GitHubReleases,
    build_snapshot_archive,
    decrypt_and_extract,
    encrypt_file,
    restore_latest,
    snapshot_and_upload,
)

REPO = "Enhso/betomcat"
GITHUB_API = "https://api.github.com"
RELEASES_LIST_URL = f"{GITHUB_API}/repos/{REPO}/releases?per_page=100"


def _draft(release_id: int, assets: list[tuple[int, str]]) -> dict[str, object]:
    """A `state` draft as GET /releases lists it (drafts have no git tag yet)."""
    return {
        "id": release_id,
        "tag_name": "state",
        "draft": True,
        "assets": [
            {"id": asset_id, "name": name, "state": "uploaded"}
            for asset_id, name in assets
        ],
    }


def _make_sqlite(path: Path, rows: list[str]) -> sqlite3.Connection:
    """Create `path` with `rows`, returning the still-open connection.

    Simulates a database another process (iw-server, or this process's own
    ledger) holds open while a snapshot is taken.
    """
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE items (value TEXT)")
    conn.executemany("INSERT INTO items VALUES (?)", [(r,) for r in rows])
    conn.commit()
    return conn


async def test_snapshot_round_trip_with_a_live_open_sqlite(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ledger_conn = _make_sqlite(data_dir / "ledger.sqlite", ["a", "b", "c"])
    iw_db = data_dir / "iw.sqlite"
    iw_conn = _make_sqlite(iw_db, ["x", "y"])
    try:
        (data_dir / "weights.json").write_text('{"model-a": 1.0}')
        outbox = data_dir / "outbox"
        outbox.mkdir()
        (outbox / "2026-09-23.jsonl").write_text('{"url": "http://x"}\n')

        archive_path = tmp_path / "snapshot.tar.gz"
        build_snapshot_archive(data_dir, iw_db, archive_path)
        assert archive_path.exists()

        key = Fernet.generate_key()
        encrypted = encrypt_file(archive_path, key)

        restore_dir = tmp_path / "restored"
        decrypt_and_extract(encrypted, key, restore_dir)

        restored_ledger = sqlite3.connect(str(restore_dir / "ledger.sqlite"))
        try:
            rows = [r[0] for r in restored_ledger.execute("SELECT value FROM items")]
        finally:
            restored_ledger.close()
        assert rows == ["a", "b", "c"]

        restored_iw = sqlite3.connect(str(restore_dir / "iw.sqlite"))
        try:
            iw_rows = [r[0] for r in restored_iw.execute("SELECT value FROM items")]
        finally:
            restored_iw.close()
        assert iw_rows == ["x", "y"]

        assert (restore_dir / "weights.json").read_text() == '{"model-a": 1.0}'
        assert (restore_dir / "outbox" / "2026-09-23.jsonl").exists()
    finally:
        ledger_conn.close()
        iw_conn.close()


async def test_snapshot_and_upload_uploads_before_deleting_and_keeps_three(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "weights.json").write_text("{}")

    httpx_mock.add_response(
        method="GET",
        url=RELEASES_LIST_URL,
        json=[
            _draft(
                100,
                [
                    (1, "state-20260101T000000Z.enc"),
                    (2, "state-20260102T000000Z.enc"),
                    (3, "state-20260103T000000Z.enc"),
                ],
            )
        ],
    )
    httpx_mock.add_response(
        method="POST", json={"id": 4, "name": "state-99999999T000000Z.enc"}
    )
    httpx_mock.add_response(method="DELETE", status_code=204)

    releases = GitHubReleases(REPO, "gh-token")
    try:
        uploaded_name = await snapshot_and_upload(
            releases,
            data_dir=data_dir,
            iw_db_path=data_dir / "iw.sqlite",
            key=Fernet.generate_key(),
        )
    finally:
        await releases.aclose()

    assert uploaded_name == "state-99999999T000000Z.enc"

    requests = httpx_mock.get_requests()
    # Upload must happen before delete: never a window with zero snapshots.
    assert [r.method for r in requests] == ["GET", "POST", "DELETE"]
    assert requests[2].url.path.endswith("/releases/assets/1")


async def test_restore_latest_with_no_release_starts_fresh(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    httpx_mock.add_response(method="GET", url=RELEASES_LIST_URL, json=[])
    httpx_mock.add_response(
        method="POST",
        url=f"{GITHUB_API}/repos/{REPO}/releases",
        json={"id": 100},
        status_code=201,
    )

    releases = GitHubReleases(REPO, "gh-token")
    try:
        restored = await restore_latest(
            releases, data_dir=tmp_path / "data", key=Fernet.generate_key()
        )
    finally:
        await releases.aclose()

    assert restored is False
    assert [r.method for r in httpx_mock.get_requests()] == ["GET", "POST"]


async def test_existing_draft_is_found_by_listing_not_recreated(
    httpx_mock: HTTPXMock,
) -> None:
    """Live bug 2026-09-24: GET /releases/tags/state never returns a draft, so
    every call created another draft and restore always started fresh."""
    httpx_mock.add_response(
        method="GET",
        url=RELEASES_LIST_URL,
        json=[
            {"id": 7, "tag_name": "v1.0", "draft": False, "assets": []},
            _draft(300, [(31, "state-20260924T200000Z.enc")]),
            _draft(200, [(21, "state-20260924T193955Z.enc")]),
            _draft(250, []),
        ],
    )

    releases = GitHubReleases(REPO, "gh-token")
    try:
        release_id, assets = await releases.get_or_create_release()
    finally:
        await releases.aclose()

    assert release_id == 200
    assert sorted(a.name for a in assets) == [
        "state-20260924T193955Z.enc",
        "state-20260924T200000Z.enc",
    ]
    assert [r.method for r in httpx_mock.get_requests()] == ["GET"]


async def test_assets_still_uploading_are_ignored(httpx_mock: HTTPXMock) -> None:
    release = _draft(200, [(21, "state-20260924T193955Z.enc")])
    release["assets"].append(  # type: ignore[attr-defined]
        {"id": 22, "name": "state-20260924T200000Z.enc", "state": "starter"}
    )
    httpx_mock.add_response(method="GET", url=RELEASES_LIST_URL, json=[release])

    releases = GitHubReleases(REPO, "gh-token")
    try:
        _release_id, assets = await releases.get_or_create_release()
    finally:
        await releases.aclose()

    assert [a.id for a in assets] == [21]
