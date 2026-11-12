"""Scheduled check, run as `hermes leadpoet check` by a Hermes no-agent cron job.

Hermes keeps plugin secrets out of cron scripts, so the script only starts this
command and the key arrives through Hermes itself.

Standard output is delivered to the user's chat, and empty output stays silent.
It makes no model calls, so checking every few minutes costs no tokens.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .api import ApiError, LeadpoetApi
from .customers import ADD_KEY, Customers
from .desk import Desk
from .store import Store

FAILURES_BEFORE_ALERT = 3
logger = logging.getLogger(__name__)


def data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir("leadpoet")


def get_key() -> str:
    from agent.secret_scope import get_secret

    return (get_secret("LEADPOET_API_KEY") or "").strip()


def run(desk: Desk) -> str:
    """Return the text to deliver. Raises only for a problem the user must fix."""
    store = desk.store
    try:
        desk.sync()
    except Exception as error:  # A malformed answer counts as a failed check, so it can't stop /customers.
        if isinstance(error, ApiError) and error.status in (401, 403):
            raise
        logger.error("leadpoet reply check failed: %s", type(error).__name__)
        failures = int(store.get("poll_failures", "0") or 0) + 1
        store.put("poll_failures", str(failures))
        if failures == FAILURES_BEFORE_ALERT:
            return "Leadpoet checks are failing, so new replies may be late. I'll keep trying."
        return ""
    store.put("poll_failures", "0")
    return desk.alert_text()


def advance(customers: Customers, store: Store) -> str:
    """A problem here must not lose the reply alerts this same run has already marked as sent."""
    try:
        text = customers.advance()
    except Exception as error:  # Only the type is logged. The job holds contact details.
        logger.error("leadpoet customers check failed: %s", type(error).__name__)
        failures = int(store.get("customers_failures", "0") or 0) + 1
        store.put("customers_failures", str(failures))
        return ("Your /customers job keeps hitting a problem, so it may be stuck. "
                "Nothing extra was sent. /customers check tries again.") if failures == FAILURES_BEFORE_ALERT else ""
    store.put("customers_failures", "0")
    return text


def main() -> int:
    key = get_key()
    if not key:
        print(f"Pings need your Leadpoet API key. Add it where Hermes runs: {ADD_KEY}", file=sys.stderr)
        return 1
    api, store = LeadpoetApi(key), Store(data_dir() / "live.db")
    try:
        texts = [run(Desk(api, store)), advance(Customers(api, store), store)]
    except ApiError as error:
        print(f"Leadpoet refused this key: {error.message}", file=sys.stderr)
        return 1
    text = "\n\n".join(part for part in texts if part)
    if text:
        print(text)
    return 0
