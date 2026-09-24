"""Encrypted state snapshots, stored as assets on a draft GitHub Release.

There is no always-on host (BUILD_LOG "Answers received 2026-09-22
(evening)"): the daemon runs as ~5.5h GitHub Actions shifts, so the
ledger, the Intelligence Workbench sqlite corpus, `weights.json`, and the
outbox must survive the gap between one shift's runner and the next's.
Each shift snapshots that state into a tar.gz, encrypts it with Fernet,
and uploads it as an asset on a draft release tagged `state` (draft =>
invisible to the public, since the repo itself is public). The newest
asset is restored at the start of every shift; the newest 3 are kept so a
bad snapshot can be rolled back by hand.

`ledger.sqlite` and the IW sqlite database are both live-open by other
processes while a shift runs, so they are copied via sqlite3's online
backup API (`Connection.backup`), which is safe to run against a
database another process holds open. `weights.json` and `outbox/` are
plain files, copied directly.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
UPLOADS_API = "https://uploads.github.com"
RELEASE_TAG = "state"
KEEP_ASSETS = 3


@dataclass(frozen=True)
class ReleaseAsset:
    """One asset attached to the `state` release."""

    id: int
    name: str


class GitHubReleases:
    """Thin async client for the subset of the Releases API state.py needs."""

    def __init__(
        self, repo: str, token: str, client: httpx.AsyncClient | None = None
    ) -> None:
        """Args:
        repo: `"owner/name"`, e.g. `os.environ["GITHUB_REPOSITORY"]`.
        token: A token with `contents: write` on `repo`. Never logged.
        client: Optional pre-built `httpx.AsyncClient` (tests inject one).
            Must follow redirects for `download_asset` to work.
        """
        self._repo = repo
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._client = client or httpx.AsyncClient(follow_redirects=True)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_or_create_release(self) -> tuple[int, list[ReleaseAsset]]:
        """The `state` draft release's id and assets, creating it if absent.

        A draft has no git tag until it is published, so `releases/tags/state`
        never finds one; the release list does (drafts are listed for tokens
        with push access). If several `state` drafts exist, uploads go to the
        oldest and the uploaded assets of all of them are returned, so restore
        and rotation see every snapshot.
        """
        response = await self._client.get(
            f"{GITHUB_API}/repos/{self._repo}/releases",
            headers=self._headers,
            params={"per_page": 100},
            timeout=30,
        )
        response.raise_for_status()
        matches = sorted(
            (r for r in response.json() if r.get("tag_name") == RELEASE_TAG),
            key=lambda r: int(r["id"]),
        )
        if matches:
            assets = [
                ReleaseAsset(id=a["id"], name=a["name"])
                for release in matches
                for a in release.get("assets", [])
                if a.get("state") == "uploaded"
            ]
            return int(matches[0]["id"]), assets

        created = await self._client.post(
            f"{GITHUB_API}/repos/{self._repo}/releases",
            headers=self._headers,
            json={
                "tag_name": RELEASE_TAG,
                "name": RELEASE_TAG,
                "draft": True,
                "prerelease": False,
            },
            timeout=30,
        )
        created.raise_for_status()
        return int(created.json()["id"]), []

    async def upload_asset(
        self, release_id: int, name: str, data: bytes
    ) -> ReleaseAsset:
        response = await self._client.post(
            f"{UPLOADS_API}/repos/{self._repo}/releases/{release_id}/assets",
            headers={**self._headers, "Content-Type": "application/octet-stream"},
            params={"name": name},
            content=data,
            timeout=120,
        )
        response.raise_for_status()
        body = response.json()
        return ReleaseAsset(id=int(body["id"]), name=str(body["name"]))

    async def delete_asset(self, asset_id: int) -> None:
        response = await self._client.delete(
            f"{GITHUB_API}/repos/{self._repo}/releases/assets/{asset_id}",
            headers=self._headers,
            timeout=30,
        )
        if response.status_code not in (204, 404):
            response.raise_for_status()

    async def download_asset(self, asset_id: int) -> bytes:
        response = await self._client.get(
            f"{GITHUB_API}/repos/{self._repo}/releases/assets/{asset_id}",
            headers={**self._headers, "Accept": "application/octet-stream"},
            timeout=120,
        )
        response.raise_for_status()
        return response.content


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _backup_sqlite(source: Path, dest: Path) -> None:
    """Copy `source` to `dest` via the sqlite3 online backup API.

    Safe against a `source` another process (e.g. iw-server) holds open.
    A missing `source` is not an error: nothing has been written yet.
    """
    if not source.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(source))
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def build_snapshot_archive(data_dir: Path, iw_db_path: Path, dest: Path) -> None:
    """Write a consistent tar.gz of `data_dir`'s state to `dest`.

    Args:
        data_dir: betomcat's `DATA_DIR` (holds `ledger.sqlite`,
            `weights.json`, `outbox/`).
        iw_db_path: The Intelligence Workbench's sqlite database file.
        dest: Path to write the tar.gz archive to.
    """
    with tempfile.TemporaryDirectory() as tmp_name:
        staging = Path(tmp_name)
        _backup_sqlite(data_dir / "ledger.sqlite", staging / "ledger.sqlite")
        _backup_sqlite(iw_db_path, staging / "iw.sqlite")

        weights = data_dir / "weights.json"
        if weights.exists():
            shutil.copy2(weights, staging / "weights.json")

        outbox = data_dir / "outbox"
        if outbox.exists():
            shutil.copytree(outbox, staging / "outbox")

        with tarfile.open(dest, "w:gz") as tar:
            tar.add(staging, arcname=".")


def encrypt_file(path: Path, key: bytes) -> bytes:
    """Fernet-encrypt `path`'s bytes."""
    return Fernet(key).encrypt(path.read_bytes())


def decrypt_and_extract(encrypted: bytes, key: bytes, data_dir: Path) -> None:
    """Decrypt a Fernet-encrypted tar.gz and extract it into `data_dir`."""
    archive = Fernet(key).decrypt(encrypted)
    data_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp_name:
        tar_path = Path(tmp_name) / "state.tar.gz"
        tar_path.write_bytes(archive)
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(data_dir, filter="data")


async def snapshot_and_upload(
    releases: GitHubReleases, *, data_dir: Path, iw_db_path: Path, key: bytes
) -> str:
    """Snapshot, encrypt, upload, then rotate down to the newest 3 assets.

    Uploads before deleting anything stale, so there is never a window
    with zero snapshots available to a future restore.

    Returns:
        The uploaded asset's name.
    """
    release_id, existing_assets = await releases.get_or_create_release()
    name = f"state-{_timestamp()}.enc"

    with tempfile.TemporaryDirectory() as tmp_name:
        archive_path = Path(tmp_name) / "state.tar.gz"
        build_snapshot_archive(data_dir, iw_db_path, archive_path)
        encrypted = encrypt_file(archive_path, key)

    uploaded = await releases.upload_asset(release_id, name, encrypted)
    logger.info("state snapshot uploaded: %s", uploaded.name)

    all_assets = sorted(
        [*existing_assets, uploaded], key=lambda a: a.name, reverse=True
    )
    for stale in all_assets[KEEP_ASSETS:]:
        await releases.delete_asset(stale.id)
        logger.info("stale state snapshot deleted: %s", stale.name)

    return uploaded.name


async def restore_latest(
    releases: GitHubReleases, *, data_dir: Path, key: bytes
) -> bool:
    """Restore the newest snapshot into `data_dir`.

    Also serves `betomcat pull-state`: pass the review directory as
    `data_dir` to download and decrypt the newest snapshot there.

    Returns:
        True if a snapshot was found and restored, False if none exists
        yet (a fresh start, not an error).
    """
    _release_id, assets = await releases.get_or_create_release()
    if not assets:
        logger.info("no state snapshot found, starting fresh")
        return False

    latest = max(assets, key=lambda a: a.name)
    encrypted = await releases.download_asset(latest.id)
    decrypt_and_extract(encrypted, key, data_dir)
    logger.info("state snapshot restored: %s", latest.name)
    return True
