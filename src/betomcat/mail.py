"""Mail over Resend's HTTP API (`POST https://api.resend.com/emails`).

The send-only key and the recipient come from the environment. Neither the
recipient, the subject nor the body is ever logged: the repo and its Actions
logs are public, and the mail is the only place details go.
"""

from __future__ import annotations

import httpx

from betomcat.config import Settings

RESEND_URL = "https://api.resend.com/emails"
FROM_ADDRESS = "betomcat <onboarding@resend.dev>"
SEND_TIMEOUT = 30.0


def mail_credentials(settings: Settings) -> tuple[str, str]:
    """The Resend key and the recipient, or exit if either is unset.

    Raises:
        SystemExit: If `RESEND_API_KEY` or `SUMMARY_TO` is missing.
    """
    if not (settings.resend_api_key and settings.summary_to):
        raise SystemExit("sending mail requires RESEND_API_KEY and SUMMARY_TO")
    return settings.resend_api_key, settings.summary_to


async def send_email(
    client: httpx.AsyncClient,
    settings: Settings,
    subject: str,
    html: str,
    text: str,
) -> None:
    """Send one mail from `betomcat <onboarding@resend.dev>` to `SUMMARY_TO`.

    Args:
        client: The HTTP client to send with.
        settings: Supplies `resend_api_key` and `summary_to`.
        subject: Subject line.
        html: HTML body.
        text: Plain-text body.

    Raises:
        SystemExit: If the key or the recipient is unset.
        httpx.HTTPStatusError: If Resend answers with a non-2xx status.
    """
    api_key, recipient = mail_credentials(settings)
    response = await client.post(
        RESEND_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "from": FROM_ADDRESS,
            "to": recipient,
            "subject": subject,
            "html": html,
            "text": text,
        },
        timeout=SEND_TIMEOUT,
    )
    response.raise_for_status()
