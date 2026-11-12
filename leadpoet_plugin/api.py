"""Small client for the public Leadpoet workspace API. Standard library only."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE_URL = "https://leadpoet.com/api/v1"
TIMEOUT_SECONDS = 20


class ApiError(Exception):
    """A failed call. ``ambiguous`` means a write may or may not have happened."""

    def __init__(self, message: str, status: int = 0, ambiguous: bool = False,
                 code: str = "", details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.ambiguous = ambiguous
        self.code = code
        self.details = details or {}


def _server_error(raw: bytes) -> tuple[str, str, dict]:
    """Return the message, the code and the details of an error response."""
    try:
        payload = json.loads(raw.decode("utf-8"))
        error = payload.get("error")
    except (ValueError, AttributeError):
        return "", "", {}
    if isinstance(error, dict):
        message, code, details = error.get("message"), error.get("code"), error.get("details")
    else:
        message, code, details = error, payload.get("code"), payload
    details = details if isinstance(details, dict) else {}
    if code == "semantic_error" and isinstance(details.get("reason"), str):
        code = details["reason"]  # The public API wraps the specific reason in this general code.
    return message[:300] if isinstance(message, str) else "", code if isinstance(code, str) else "", details


class LeadpoetApi:
    def __init__(self, api_key: str, base_url: str = BASE_URL, timeout: float = TIMEOUT_SECONDS):
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def _call(self, method: str, path: str, *, query: dict | None = None,
              body: dict | None = None, headers: dict | None = None) -> Any:
        url = self._base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, method=method)
        request.add_header("Authorization", f"Bearer {self._key}")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "leadpoet-hermes/0.2")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, data=data, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            ambiguous = error.code >= 500
            message, code, details = _server_error(error.read())
            raise ApiError(message or f"Leadpoet returned HTTP {error.code}.", status=error.code,
                           ambiguous=ambiguous, code=code, details=details) from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError):  # A write may have landed.
            raise ApiError("Could not reach Leadpoet.", ambiguous=True) from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError("Leadpoet sent a response this plugin can't read.", ambiguous=True) from None
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def identity(self) -> dict:
        return self._call("GET", "/me")

    def list_replies(self) -> dict:
        return self._call("GET", "/outreach/replies")

    def conversation(self, enrollment_id: str) -> dict | None:
        data = self._call("GET", "/outreach/conversations",
                          query={"enrollment_id": enrollment_id, "limit": 1})
        rows = data.get("conversations") if isinstance(data, dict) else None
        return rows[0] if rows else None

    def profile(self) -> dict:
        return self._call("GET", "/outreach/profile")

    def create_empty_profile(self) -> dict:
        """Launching needs a profile row, and a new workspace may not have one. Only called when none exists."""
        return self._call("PUT", "/outreach/profile", body={})

    def send_reply(self, enrollment_id: str, channel: str, body: str, idempotency_key: str) -> dict:
        path = "/outreach/conversations/" + urllib.parse.quote(enrollment_id, safe="") + "/reply"
        return self._call("POST", path, body={"channel": channel, "body": body},
                          headers={"Idempotency-Key": idempotency_key})

    def credits(self) -> dict:
        return self._call("GET", "/credits")

    def contact_settings(self) -> dict:
        return self._call("GET", "/requests/contact-settings")

    def create_request(self, body: dict, idempotency_key: str) -> dict:
        return self._call("POST", "/requests", body=body, headers={"Idempotency-Key": idempotency_key})

    def get_request(self, query_id: str) -> dict:
        return self._call("GET", "/requests/" + urllib.parse.quote(query_id, safe=""))

    def list_requests(self) -> list[dict]:
        data = self._call("GET", "/requests")
        rows = data.get("queries") if isinstance(data, dict) else None
        return [row for row in rows or [] if isinstance(row, dict)]

    def pipeline_contacts(self, icp_id: str | None) -> list[dict]:
        data = self._call("GET", "/pipeline", query={"icp_id": icp_id} if icp_id else None)
        rows = data.get("contacts") if isinstance(data, dict) else None
        return [row for row in rows or [] if isinstance(row, dict)]

    def email_accounts(self) -> list[dict]:
        data = self._call("GET", "/outreach/accounts")
        rows = data.get("accounts") if isinstance(data, dict) else None
        return [row for row in rows or [] if isinstance(row, dict)]

    def create_audience(self, body: dict, idempotency_key: str) -> dict:
        return self._call("POST", "/outreach/audiences", body=body, headers={"Idempotency-Key": idempotency_key})

    def create_campaign(self, body: dict, idempotency_key: str) -> dict:
        return self._call("POST", "/outreach/campaigns", body=body, headers={"Idempotency-Key": idempotency_key})

    def get_campaign(self, campaign_id: str) -> dict:
        return self._call("GET", "/outreach/campaigns/" + urllib.parse.quote(campaign_id, safe=""))

    def _campaign_step(self, campaign_id: str, step: str, body: dict | None = None) -> dict:
        path = "/outreach/campaigns/" + urllib.parse.quote(campaign_id, safe="") + "/" + step
        return self._call("POST", path, body=body if body is not None else {})

    def contract_proposal(self, campaign_id: str) -> dict:
        return self._campaign_step(campaign_id, "contract-proposal")

    def campaign_preview(self, campaign_id: str) -> dict:
        return self._campaign_step(campaign_id, "preview")

    def launch_campaign(self, campaign_id: str, body: dict) -> dict:
        return self._campaign_step(campaign_id, "launch", body)

    def review_drafts(self, max_pages: int = 20) -> list[dict]:
        """Read the queue. Anything past the last page simply stays in Leadpoet Review."""
        drafts: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            query: dict = {"limit": 200}
            if cursor:
                query["cursor"] = cursor
            data = self._call("GET", "/outreach/review", query=query)
            drafts += [row for row in data.get("drafts") or [] if isinstance(row, dict)]
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return drafts

    def queued_messages(self) -> list[dict]:
        data = self._call("GET", "/outreach/queued")
        rows = data.get("messages") if isinstance(data, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def approve_drafts(self, approvals: list[dict]) -> dict:
        data = self._call("POST", "/outreach/review/approve",
                          body={"approvals": approvals, "only_review_ready": True})
        results = data.get("results") if isinstance(data, dict) else None
        return results if isinstance(results, dict) else {}
