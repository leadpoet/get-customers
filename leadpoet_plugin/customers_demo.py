"""Everything /customers demo needs: fictional people, pages and a stand-in model. Nothing can leave."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

SELLER = "https://sunnyday.example"
_PAGES = {
    f"{SELLER}/": "Sunnyday. Route notes for delivery teams. Drivers leave a voice note and Sunnyday turns it "
                  "into a clean handover for the next shift. Teams cut handover time by 40 percent in the first month. Sunnyday works on any phone, and the handover is waiting when the next shift starts. Built for regional delivery and freight companies whose drivers change over every day.",
    "https://pinecrest-freight.example/": "Pinecrest Freight. We now run cold-chain lanes out of Rotterdam six days a week. Our drivers carry fresh produce and pharmaceuticals for growers and clinics across the region, and our dispatch team is on call around the clock for every load.",
    "https://harborlaneroasters.example/": "Harbor Lane Roasters. Our new wholesale program ships to independent cafes across Oregon. We roast small batches every morning, and our own vans bring fresh coffee to each cafe on a weekly route that our drivers know by heart.",
    "https://tallgrassdental.example/": "Tallgrass Dental. Gentle care for the whole family. Our friendly team offers cleanings, fillings, crowns and emergency visits, with early morning appointments for busy parents and a calm, welcoming office for nervous patients.",
}
PEOPLE = [
    {"id": "demo-marcus", "query_id": "demo-find", "name": "Marcus Oyelaran", "role": "VP Operations", "company": "Pinecrest Freight",
     "email": "marcus@pinecrest-freight.example", "website": "https://pinecrest-freight.example/",
     "icp_details": "Posted a night dispatcher role on Oct 28 that asks for clean shift handovers."},
    {"id": "demo-priya", "query_id": "demo-find", "name": "Priya Raman", "role": "Head of Logistics", "company": "Harbor Lane Roasters",
     "email": "priya@harborlaneroasters.example", "website": "https://harborlaneroasters.example/",
     "icp_details": "Launched a wholesale program in October that runs its own van routes to cafes."},
    {"id": "demo-lena", "query_id": "demo-find", "name": "Lena Fischer", "role": "Practice Director", "company": "Tallgrass Dental",
     "email": "lena@tallgrassdental.example", "website": "https://tallgrassdental.example/"},
]
_BRIEF = {
    "product_name": "Sunnyday", "summary": "Sunnyday turns drivers' voice notes into clean shift handovers for delivery teams.",
    "facts": [{"text": "Drivers leave a voice note and Sunnyday turns it into a clean handover.",
               "quote": "Drivers leave a voice note and Sunnyday turns it into a clean handover", "url": f"{SELLER}/"},
              {"text": "Teams cut handover time by 40 percent in the first month.",
               "quote": "Teams cut handover time by 40 percent in the first month", "url": f"{SELLER}/"},
              {"text": "Sunnyday works on any phone, and the handover is waiting when the next shift starts.",
               "quote": "Sunnyday works on any phone, and the handover is waiting when the next shift starts", "url": f"{SELLER}/"}],
    "roles": ["VP Operations", "Head of Logistics"],
    "buyer_description": "Regional delivery and freight companies with shift-based drivers.",
    "company_sizes": ["50–200 employees"], "countries": [], "exclude": ["Solo couriers"],
    "signals": [
        {"source": "job_postings", "description": "Posted a Dispatcher role that names shift handovers. Source: LinkedIn Jobs posting, past 30 days"},
        {"source": "funding_and_company_news", "description": "Opened a new depot that adds a second driver shift. Source: regional business news, past 45 days"},
    ],
    "problem": "Shift handovers lose details and slow the next route.",
    "voice": "Plain, warm, short sentences, never salesy.",
}
_SEQUENCE = {"emails": [
    {"subject": "handover time", "body": "Hi {first_name},\n\n[[PERSONAL]]\n\nIf details get lost when shifts change at {company}, that is rework for the next shift.\n\n"
     "Drivers leave a voice note and Sunnyday turns it into a clean handover [1]. Sunnyday teams cut handover time by 40 percent in the first month [2].\n\nWorth a short call?\n\n{sender}"},
    {"subject": "the next shift", "body": "Hi {first_name},\n\nSunnyday works on any phone, and the handover is waiting when the next shift starts [3].\n\n"
     "Would a short call next week be useful?\n\n{sender}"},
    {"subject": "last note", "body": "Hi {first_name},\n\nSunnyday teams cut handover time by 40 percent in the first month [2].\n\nWho looks after shift handovers at {company}?\n\n{sender}"},
]}
_LINES = {
    "Pinecrest Freight": {"lines": [{"line": "Pinecrest Freight runs cold-chain lanes out of Rotterdam six days a week.",
                                     "quote": "We now run cold-chain lanes out of Rotterdam six days a week", "url": "https://pinecrest-freight.example/"}]},
    "Harbor Lane Roasters": {"lines": [{"line": "Your new wholesale program ships to independent cafes.",
                                        "quote": "Our new wholesale program ships to independent cafes", "url": "https://harborlaneroasters.example/"}]},
    # This one is wrong on purpose: the page never says it, so the check throws it out.
    "Tallgrass Dental": {"lines": [{"line": "Congrats on opening your third clinic in Omaha.",
                                    "quote": "We just opened our third clinic in Omaha", "url": "https://tallgrassdental.example/"}]},
}


def demo_fetch(url: str) -> tuple[str, str]:
    from .website import SiteError

    key = url if url.endswith("/") else url + "/"
    if key not in _PAGES:
        raise SiteError("That page isn't part of the demo.")
    return key, f"<html><head><title>Demo</title></head><body><p>{_PAGES[key]}</p></body></html>"


class DemoLlm:
    """Stand-in model, so the demo needs no key and costs nothing. Its output goes through the same checks."""

    def complete(self, messages: list[dict], **kwargs: Any) -> Any:
        purpose = str(kwargs.get("purpose") or "")
        if purpose.endswith("brief"):
            return SimpleNamespace(text=json.dumps(_BRIEF))
        if purpose.endswith("sequence"):
            return SimpleNamespace(text=json.dumps(_SEQUENCE))
        company = purpose.split(": ", 1)[-1]
        return SimpleNamespace(text=json.dumps(_LINES.get(company, {"lines": []})))


class DemoCustomersApi:
    """Answers like Leadpoet, in memory. There is no way to reach a real inbox from here."""

    demo = True

    def __init__(self) -> None:
        self.approved: list[str] = []
        self._campaigns: dict[str, dict] = {}
        self._drafts: dict[str, dict] = {}
        self._live: set[str] = set()
        self.sender_name = "Sam at Sunnyday"

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
        return [dict(person) for person in PEOPLE]

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
