"""One-time GitHub setup for betomcat (v1) and vezocontrol (v2).

Run by the principal, from the betomcat repo root:

    uv run --with pynacl --with httpx --with cryptography python scripts/setup_github.py

Reads every value from `.env` (GITHUB_ADMIN_TOKEN and the bot credentials) and
never prints a secret. Safe to re-run: each step checks current state first.

Steps:
1. Ensure `STATE_KEY` (Fernet key for the encrypted state snapshots) is in `.env`.
2. Replace the read-only deploy key that lets betomcat's Actions fetch Enhso/iw.
3. Set the Actions secrets on Enhso/betomcat.
4. Create the public repo Enhso/vezocontrol if missing and push ../vezocontrol.
5. Set the Actions secrets on Enhso/vezocontrol.
"""

import base64
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from dotenv import dotenv_values
from nacl import encoding, public

logger = logging.getLogger("setup_github")

API = "https://api.github.com"
OWNER = "Enhso"
ENV_PATH = Path(".env")
DEPLOY_KEY_TITLE = "betomcat-actions (read-only)"
V2_DIR = Path("../vezocontrol")

BETOMCAT_SECRETS = {
    "METACULUS_TOKEN": "METACULUS_TOKEN",
    "OPENROUTER_API_KEY": "OPENROUTER_API_KEY",
    "OPENROUTER_FREE_API_KEY": "OPENROUTER_FREE_API_KEY",
    "ASKNEWS_API_KEY": "ASKNEWS_API_KEY",
    "TYPESAFE_API_KEY": "TYPESAFE_API_KEY",
    "STATE_KEY": "STATE_KEY",
}
V2_SECRETS = {
    "METACULUS_TOKEN": "V2_METACULUS_TOKEN",
    "OPENROUTER_API_KEY": "OPENROUTER_API_KEY",
    "ASKNEWS_API_KEY": "ASKNEWS_API_KEY",
}


def ensure_state_key(env: dict[str, str | None]) -> str:
    """Return STATE_KEY from `.env`, generating and appending one if absent.

    Args:
        env: Parsed `.env` values.

    Returns:
        The Fernet key as a string.
    """
    existing = env.get("STATE_KEY")
    if existing:
        return existing
    key = Fernet.generate_key().decode()
    with ENV_PATH.open("a") as handle:
        handle.write(
            "# Fernet key for the encrypted Actions state snapshots\n"
            f"STATE_KEY={key}\n"
        )
    logger.info("generated STATE_KEY and appended it to .env")
    return key


def set_secrets(client: httpx.Client, repo: str, values: dict[str, str]) -> None:
    """Encrypt and store Actions secrets on a repository.

    Args:
        client: Authenticated GitHub API client.
        repo: `owner/name`.
        values: Secret name to plaintext value.

    Raises:
        httpx.HTTPStatusError: If GitHub rejects a request.
    """
    key = client.get(f"{API}/repos/{repo}/actions/secrets/public-key")
    key.raise_for_status()
    box = public.SealedBox(
        public.PublicKey(key.json()["key"].encode(), encoding.Base64Encoder())
    )
    for name, value in values.items():
        sealed = base64.b64encode(box.encrypt(value.encode())).decode()
        response = client.put(
            f"{API}/repos/{repo}/actions/secrets/{name}",
            json={"encrypted_value": sealed, "key_id": key.json()["key_id"]},
        )
        response.raise_for_status()
        logger.info("%s: secret %s set", repo, name)


def replace_deploy_key(client: httpx.Client) -> str:
    """Create a fresh read-only deploy key on Enhso/iw, removing the old one.

    Args:
        client: Authenticated GitHub API client.

    Returns:
        The private key (OpenSSH format) to store as the IW_DEPLOY_KEY secret.

    Raises:
        httpx.HTTPStatusError: If GitHub rejects a request.
    """
    keys = client.get(f"{API}/repos/{OWNER}/iw/keys")
    keys.raise_for_status()
    for existing in keys.json():
        if existing["title"] == DEPLOY_KEY_TITLE:
            client.delete(
                f"{API}/repos/{OWNER}/iw/keys/{existing['id']}"
            ).raise_for_status()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "key"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", DEPLOY_KEY_TITLE,
             "-f", str(path)],
            check=True,
        )
        created = client.post(
            f"{API}/repos/{OWNER}/iw/keys",
            json={
                "title": DEPLOY_KEY_TITLE,
                "key": path.with_suffix(".pub").read_text().strip(),
                "read_only": True,
            },
        )
        created.raise_for_status()
        logger.info("Enhso/iw: read-only deploy key installed")
        return path.read_text()


def ensure_v2_repo(client: httpx.Client) -> None:
    """Create the public Enhso/vezocontrol repo if missing, then push ../vezocontrol.

    Args:
        client: Authenticated GitHub API client.

    Raises:
        httpx.HTTPStatusError: If GitHub rejects the create request.
        subprocess.CalledProcessError: If the git push fails.
    """
    if client.get(f"{API}/repos/{OWNER}/vezocontrol").status_code == 404:
        client.post(
            f"{API}/user/repos",
            json={
                "name": "vezocontrol",
                "private": False,
                "description": "v2 control bot: the Metaculus template bot on one "
                "free model, the baseline for betomcat v1",
            },
        ).raise_for_status()
        logger.info("created public repo Enhso/vezocontrol")
    remote = f"git@github.com:{OWNER}/vezocontrol.git"
    subprocess.run(["git", "-C", str(V2_DIR), "remote", "set-url", "origin", remote],
                   check=True)
    subprocess.run(["git", "-C", str(V2_DIR), "push", "-q", "-u", "origin", "main"],
                   check=True)
    logger.info("pushed %s to %s", V2_DIR, remote)


def main() -> int:
    """Run every setup step in order.

    Returns:
        Process exit code: 0 on success, 1 if a required value is missing.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    env = dotenv_values(ENV_PATH)
    token = env.get("GITHUB_ADMIN_TOKEN")
    needed = {*BETOMCAT_SECRETS.values(), *V2_SECRETS.values()} - {"STATE_KEY"}
    missing = sorted(name for name in needed if not env.get(name))
    if not token or missing:
        absent = missing or ["GITHUB_ADMIN_TOKEN"]
        logger.error("missing in .env: %s", ", ".join(absent))
        return 1
    state_key = ensure_state_key(env)
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"}
    with httpx.Client(headers=headers, timeout=30.0) as client:
        betomcat = {name: str(env[src]) for name, src in BETOMCAT_SECRETS.items()
                    if src != "STATE_KEY"}
        betomcat["STATE_KEY"] = state_key
        betomcat["IW_DEPLOY_KEY"] = replace_deploy_key(client)
        set_secrets(client, f"{OWNER}/betomcat", betomcat)
        ensure_v2_repo(client)
        set_secrets(client, f"{OWNER}/vezocontrol",
                    {name: str(env[src]) for name, src in V2_SECRETS.items()})
    logger.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
