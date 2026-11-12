"""Everything /customers demo needs: made-up companies and people, and a Leadpoet that answers from memory. Nothing can leave."""

from __future__ import annotations

import re
import urllib.parse

# Three made-up companies that could buy almost anything, so the preview reads right whatever the site sells.
_PAGES = {
    "https://pinecrestgroup.example/": "Pinecrest Group opened a second office in Rotterdam this spring. Pinecrest Group now serves clients six days a week. Every client works with one named contact from the first call onward, and the partners review each account at the end of every month.",
    "https://harborlanepartners.example/": "Harbor Lane Partners has grown from twelve people to forty in two years. Harbor Lane Partners now serves clients across Oregon. Every enquiry is answered within one working day, and a partner takes the first call with every new client.",
    "https://tallgrassassociates.example/": "Tallgrass Associates serves clients across Omaha and Lincoln from two offices. Most of the staff have worked together for more than a decade, and every new client meets the whole team during their first week.",
}
PEOPLE = [
    {"id": "demo-marcus", "query_id": "demo-find", "name": "Marcus Oyelaran", "role": "Managing Director", "company": "Pinecrest Group",
     "email": "marcus@pinecrestgroup.example", "website": "https://pinecrestgroup.example/"},
    {"id": "demo-priya", "query_id": "demo-find", "name": "Priya Raman", "role": "Head of Operations", "company": "Harbor Lane Partners",
     "email": "priya@harborlanepartners.example", "website": "https://harborlanepartners.example/"},
    {"id": "demo-lena", "query_id": "demo-find", "name": "Lena Fischer", "role": "Operations Director", "company": "Tallgrass Associates",
     "email": "lena@tallgrassassociates.example", "website": "https://tallgrassassociates.example/"},
]


def demo_fetch(url: str) -> tuple[str, str]:
    """The made-up companies' pages come from here. Any other address is the user's own site, read for real."""
    from .website import SiteError, fetch

    key = url if url.endswith("/") else url + "/"
    if key in _PAGES:
        return key, f"<html><head><title>Demo</title></head><body><p>{_PAGES[key]}</p></body></html>"
    if (urllib.parse.urlsplit(url).hostname or "").endswith(".example"):
        raise SiteError("That page isn't part of the demo.")
    return fetch(url)


_DATES = ("Oct 28", "Oct 21")
_WITHIN = re.compile(r"\s*\(within the [^)]*\)")


def fitted(brief: dict) -> list[dict]:
    """The made-up people, shaped like the user's own search: its roles, and its buying signals as the reason.

    A reason about someone else's business reads wrong above an email about theirs.
    """
    from .brief import signal_line

    roles = [role for role in brief.get("buyers") or [] if isinstance(role, str) and role]
    events = [_WITHIN.sub("", signal_line(signal)).rstrip(".") for signal in brief.get("signals") or []
              if isinstance(signal, dict) and "source" in signal and signal.get("description")]
    people = []
    for index, stock in enumerate(PEOPLE):
        person = {key: value for key, value in stock.items() if key != "icp_details"}
        if roles:
            person["role"] = roles[index % len(roles)]
        if index < min(len(events), len(_DATES)):
            person["icp_details"] = f"{events[index]}, on {_DATES[index]}."
        people.append(person)
    return people


class DemoCustomersApi:
    """Answers like Leadpoet, in memory. There is no way to reach a real inbox from here."""

    demo = True

    def __init__(self) -> None:
        self.approved: list[str] = []
        self._campaigns: dict[str, dict] = {}
        self._drafts: dict[str, dict] = {}
        self._live: set[str] = set()
        self.sender_name = ""  # The emails are signed with the name of the site being previewed.
        self.fit: dict | None = None  # The brief of the search being previewed.

    def identity(self) -> dict:
        from .customers import SCOPES

        return {"scopes": list(SCOPES)}

    def profile(self) -> dict:
        return {"profile": {"voice": {"sender_name": self.sender_name}}}

    def credits(self) -> dict:
        return {"credits_remaining": 100, "plan": "free"}

    def contact_settings(self) -> dict:
        return {"included_contacts_per_company": 1, "pricing": {"credits_per_company": 10, "credits_per_extra_contact": 5}}

    def create_request(self, body: dict, idempotency_key: str) -> dict:
        return {"query_id": "demo-find", "icp_id": "demo-icp", "credits_remaining": 0}

    def get_request(self, query_id: str) -> dict:
        return {"id": query_id, "status": "completed", "companies_delivered": len(PEOPLE), "progress": {"state": "ready"}}

    def list_requests(self) -> list[dict]:
        return []

    def pipeline_contacts(self, icp_id: str | None) -> list[dict]:
        return fitted(self.fit) if self.fit else [dict(person) for person in PEOPLE]

    def email_accounts(self) -> list[dict]:
        return [{"id": "demo-sender", "channel": "email", "status": "active", "paused": False}]

    def create_audience(self, body: dict, idempotency_key: str) -> dict:
        return {"snapshot_id": f"snapshot-{body['lead_ids'][0]}", "admitted": 1, "excluded_count": 0}

    def create_campaign(self, body: dict, idempotency_key: str) -> dict:
        strategy_id = f"campaign-{len(self._campaigns) + 1}"
        self._campaigns[strategy_id] = body
        return {"strategy_id": strategy_id}

    def contract_proposal(self, campaign_id: str) -> dict:
        return {"ready": True, "contract": {"id": campaign_id}, "contract_sha256": "demo"}

    def campaign_preview(self, campaign_id: str) -> dict:
        return {"preview_version": "demo", "fit_confirmation": {"ok": True}, "complianceComplete": True, "missingSenderDetails": []}

    def get_campaign(self, campaign_id: str) -> dict:
        return {"strategy": {"id": campaign_id, "status": "active" if campaign_id in self._live else "draft"}}

    def launch_campaign(self, campaign_id: str, body: dict) -> dict:
        self._live.add(campaign_id)
        body_of_campaign = self._campaigns[campaign_id]
        sequence = body_of_campaign["advanced_settings"]["controlled_campaign"]["sequence"]
        lead_id = body_of_campaign["audience_snapshot_id"].removeprefix("snapshot-")
        first = sequence["touches"][0]
        self._drafts[f"draft-{campaign_id}"] = {
            "id": f"draft-{campaign_id}", "lead_id": lead_id, "lead": {"id": lead_id}, "can_approve": True,
            "rendered_subject": first["subject"], "rendered_body": first["body"] + '\n--\nDon\'t want these? Reply "unsubscribe".',
            "quality_attestation_id": "demo", "review_revision": 1,
            "intended_send_start": "2026-11-11T14:00:00Z", "intended_send_end": "2026-11-11T18:00:00Z",
        }
        return {"status": "active"}

    def review_drafts(self, max_pages: int = 5) -> list[dict]:
        return list(self._drafts.values())

    def approve_drafts(self, approvals: list[dict]) -> dict:
        results = {}
        for approval in approvals:
            self.approved.append(approval["message_id"])
            self._drafts.pop(approval["message_id"], None)
            results[approval["message_id"]] = {"status": "authorized"}
        return results
