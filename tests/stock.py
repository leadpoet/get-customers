"""The tests' made-up world: Sunnyday, a delivery-software seller, three freight prospects and a stand-in model.

The plugin ships only neutral made-up prospects for /customers demo. Loading this module puts Sunnyday's page and
its freight prospects in their place, so every test runs the whole job with no network and no model.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from leadpoet_plugin import customers_demo
from leadpoet_plugin.customers_demo import DemoCustomersApi as _Shipped, demo_fetch  # noqa: F401

SHIPPED_PEOPLE = [dict(person) for person in customers_demo.PEOPLE]
SHIPPED_PAGES = dict(customers_demo._PAGES)

SELLER = "https://sunnyday.example"
_PAGES = {
    f"{SELLER}/": "Sunnyday. Route notes for delivery teams. Drivers leave a voice note and Sunnyday turns it "
                  "into a clean handover for the next shift. Teams cut handover time by 40 percent in the first month. Sunnyday works on any phone, and the handover is waiting when the next shift starts. Built for regional delivery and freight companies whose drivers change over every day.",
    "https://pinecrest-freight.example/": "Pinecrest Freight. We now run cold-chain lanes out of Rotterdam six days a week. Our drivers carry fresh produce and pharmaceuticals for growers and clinics across the region, and our dispatch team is on call around the clock for every load.",
    "https://harborlaneroasters.example/": "Harbor Lane Roasters. Our new wholesale program ships to independent cafes across Oregon. We roast small batches every morning, and our own vans bring fresh coffee to each cafe on a weekly route that our drivers know by heart.",
    "https://tallgrasscouriers.example/": "Tallgrass Couriers. Same-day courier routes across Omaha and Lincoln. Our drivers cover medical, legal and retail deliveries, and every route is planned the night before by our dispatch desk. Two shifts run each weekday, and a driver picks up within the hour.",
}
PEOPLE = [
    {"id": "demo-marcus", "query_id": "demo-find", "name": "Marcus Oyelaran", "role": "VP Operations", "company": "Pinecrest Freight",
     "email": "marcus@pinecrest-freight.example", "website": "https://pinecrest-freight.example/",
     "icp_details": "Posted a night dispatcher role on Oct 28 that asks for clean shift handovers."},
    {"id": "demo-priya", "query_id": "demo-find", "name": "Priya Raman", "role": "Head of Logistics", "company": "Harbor Lane Roasters",
     "email": "priya@harborlaneroasters.example", "website": "https://harborlaneroasters.example/",
     "icp_details": "Launched a wholesale program in October that runs its own van routes to cafes."},
    {"id": "demo-lena", "query_id": "demo-find", "name": "Lena Fischer", "role": "Operations Director", "company": "Tallgrass Couriers",
     "email": "lena@tallgrasscouriers.example", "website": "https://tallgrasscouriers.example/"},
]
_BRIEF = {
    "product_name": "Sunnyday", "summary": "Sunnyday turns drivers' voice notes into clean shift handovers for delivery teams.",
    "facts": [{"text": "Drivers leave a voice note and Sunnyday turns it into a clean handover.",
               "said_by": "seller", "quote": "Drivers leave a voice note and Sunnyday turns it into a clean handover", "url": f"{SELLER}/"},
              {"text": "Teams cut handover time by 40 percent in the first month.",
               "said_by": "seller", "quote": "Teams cut handover time by 40 percent in the first month", "url": f"{SELLER}/"},
              {"text": "Sunnyday works on any phone, and the handover is waiting when the next shift starts.",
               "said_by": "seller", "quote": "Sunnyday works on any phone, and the handover is waiting when the next shift starts", "url": f"{SELLER}/"}],
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
    "Tallgrass Couriers": {"lines": [{"line": "Congrats on opening your third depot in Omaha.",
                                    "quote": "We just opened our third depot in Omaha", "url": "https://tallgrasscouriers.example/"}]},
}


_SIGNAL_LINES = {
    "Pinecrest Freight": "Pinecrest Freight posted a night dispatcher role on Oct 28 that asks for clean shift handovers.",
    "Harbor Lane Roasters": "Harbor Lane Roasters launched a wholesale program in October that runs its own van routes to cafes.",
}


class DemoLlm:
    """Stand-in model, so the demo needs no key and costs nothing. Its output goes through the same checks."""

    def complete(self, messages: list[dict], **kwargs: Any) -> Any:
        purpose = str(kwargs.get("purpose") or "")
        if purpose.endswith("brief"):
            return SimpleNamespace(text=json.dumps(_BRIEF))
        if purpose.endswith("sequence"):
            return SimpleNamespace(text=json.dumps(_SEQUENCE))
        company = purpose.split(": ", 1)[-1]
        if purpose.startswith("leadpoet signal line"):
            return SimpleNamespace(text=json.dumps({"line": _SIGNAL_LINES.get(company, "")}))
        return SimpleNamespace(text=json.dumps(_LINES.get(company, {"lines": []})))


customers_demo._PAGES.update(_PAGES)
customers_demo.PEOPLE[:] = PEOPLE


class DemoCustomersApi(_Shipped):
    """Leadpoet's stand-in, with Sunnyday's sender."""

    def __init__(self) -> None:
        super().__init__()
        self.sender_name = "Sam at Sunnyday"
