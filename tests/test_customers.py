"""The /customers job, against fakes shaped like the real Leadpoet API responses."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from leadpoet_plugin.api import ApiError
from leadpoet_plugin.brief import description, product_text, write_brief
from leadpoet_plugin.customers import Customers, affordable_companies
from stock import StockApi, StockLlm, SELLER, stock_fetch
from leadpoet_plugin.store import Store
from leadpoet_plugin.website import Page, Scan
from leadpoet_plugin.brief import validate_brief
from leadpoet_plugin.website import SiteError, _hidden_classes
from leadpoet_plugin.writer import line_is_supported, render, research_person, validate_lines, validate_sequence, write_sequence


class Api(StockApi):
    """The stand-in Leadpoet, with switches for what goes wrong and a record of every write."""

    def __init__(self, credits_left=100):
        super().__init__()
        self.requests, self.keys, self.campaigns = [], [], []
        self.credits_left, self.state, self.senders = credits_left, "pending", ["acct-1"]
        self.admitted, self.proposal, self.sender_missing = 1, {"ready": True, "contract": {"c": 1}}, []
        self.fail_request, self.app_requests = None, []

    def credits(self):
        return {"credits_remaining": self.credits_left}

    def create_request(self, body, key):
        self.keys.append(key)
        if self.fail_request:
            raise self.fail_request
        for field in ("request_description", "product_description"):  # Leadpoet's own limit.
            if len(body[field]) > 500:
                raise ApiError("Lead request fields must be 500 characters or fewer", 400, code="lead_request_field_too_long")
        self.requests.append(body)
        return {"query_id": "find-1", "icp_id": "icp-1", "credits_remaining": 0}

    def get_request(self, query_id):
        return {"status": self.state, "progress": {"state": "ready" if self.state == "completed" else "working"}}

    def list_requests(self):
        return self.app_requests

    def email_accounts(self):
        return [{"id": a, "channel": "email", "status": "active", "paused": False} for a in self.senders]

    def create_audience(self, body, key):
        self.keys.append(key)
        return {"snapshot_id": f"snapshot-{body['lead_ids'][0]}", "admitted": self.admitted}

    def create_campaign(self, body, key):
        self.keys.append(key)
        self.campaigns.append(body)
        return super().create_campaign(body, key)

    def contract_proposal(self, campaign_id):
        return self.proposal

    def campaign_preview(self, campaign_id):
        return {"preview_version": "v1", "fit_confirmation": {"f": 1}, "missingSenderDetails": self.sender_missing}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = Api()
        self.store = Store(Path(self.tmp.name) / "a.db")
        self.customers = Customers(self.api, self.store, StockLlm(), stock_fetch)
        self.start()

    def start(self):
        self.text = self.customers.start(SELLER)

    def code(self, word):
        return self.text.split(f"/customers {word} ")[1].split()[0]

    def to_review(self):
        self.text = self.customers.handle("go", self.code("go"))
        self.api.state = "completed"
        self.customers.advance()
        self.text = self.customers.handle("write", "")

    def all_code(self):
        self.text = self.customers.handle("all", "")
        return self.code("all")

    def people(self):
        return {row["name"].split()[0]: row for row in self.customers.job()["people"]}


class FindCase(Case):
    def test_the_offer_shows_the_exact_cost_and_balance(self):
        self.assertIn("Cost: 100 credits. You have 100.", self.text)
        self.assertIn("To confirm, reply:\n/customers go ", self.text)

    def test_a_price_change_after_the_offer_spends_nothing(self):
        code = self.code("go")
        self.api.credits_left = 1000
        self.api.contact_settings = lambda: {"included_contacts_per_company": 1, "pricing": {"credits_per_company": 12}}
        text = self.customers.handle("go", code)
        self.assertIn("price changed", text)
        self.assertIn("Cost: 120 credits. You have 1,000.", text)
        self.assertEqual(self.api.requests, [])

    def test_a_long_brief_still_fits_leadpoets_limits(self):
        # Shaped like the real leadpoet.com brief that Leadpoet refused on Nov 10.
        job = self.customers.job()
        job["brief"]["summary"] = ("Leadpoet is B2B lead sourcing and outreach software that finds companies showing current "
                                   "buying signals and runs personalized email and LinkedIn outreach for B2B sales teams.")
        job["brief"]["facts"] = [{"text": f"Fact {n}: Leadpoet finds people in the roles you target and confirms they still work there.",
                                  "said_by": "seller", "quote": "q", "url": "https://leadpoet.com/about-us"} for n in range(6)]
        job["brief"]["company_fit"] = "B2B companies that want a short list of accounts with a reason to talk now. " * 6
        self.customers.save(job)
        self.text = self.customers.handle("", "")
        self.customers.handle("go", self.code("go"))
        body = self.api.requests[0]
        self.assertLessEqual(len(body["product_description"]), 500)
        self.assertLessEqual(len(body["request_description"]), 500)
        self.assertIn("Fact 0:", body["product_description"])
        self.assertNotIn("https://", body["product_description"])

    def test_the_request_carries_checkable_buying_signals(self):
        self.customers.handle("go", self.code("go"))
        body = self.api.requests[0]
        self.assertEqual(body["lead_type"], "high_intent")
        self.assertEqual(body["intent_signals"], "Posted a Dispatcher role that names shift handovers (within the past 30 days).\n"
                                                 "Opened a new depot that adds a second driver shift (within the past 45 days).")
        self.assertTrue(body["request_description"].startswith("Regional delivery and freight companies with shift-based drivers.\n"
                                                               "Target roles: VP Operations, Head of Logistics."))
        self.assertIn("for a recent buying signal", self.customers.handle("", ""))

    def test_without_a_signal_it_never_claims_one(self):
        job = self.customers.job()
        job["brief"]["signals"] = []
        self.customers.save(job)
        self.text = self.customers.handle("", "")
        self.customers.handle("go", self.code("go"))
        body = self.api.requests[0]
        self.assertEqual(body["lead_type"], "target_fit")
        self.assertNotIn("intent_signals", body)
        self.assertNotIn("signal", self.customers.handle("", ""))

    def test_a_wrong_code_spends_nothing(self):
        text = self.customers.handle("go", "zzzz")
        self.assertIn("isn't the one for Sunnyday", text)
        self.assertNotIn("Who buys", text)  # The card is already on screen. Only the way to confirm is repeated.
        self.assertIn("/customers go " + self.code("go"), text)
        self.assertEqual(self.api.requests, [])

    def paid(self, balance):
        self.api.credits = lambda: {"credits_remaining": balance[0], "plan": "pro"}
        self.text = self.customers.handle("", "")
        return self.text.split("/customers go ")[1].split()[0]

    def test_a_refusal_with_credits_showing_never_asks_for_a_negative_top_up(self):
        code = self.paid([8116])
        self.api.fail_request = ApiError("Not enough credits", 402, code="insufficient_credits")
        for text in (self.customers.handle("go", code), self.customers.handle("", "")):
            self.assertIn("turned this search down", text)
            self.assertIn("8,116", text)
            self.assertNotIn("Add -", text)
            self.assertIn("/customers go ", text)
        self.api.fail_request = None
        self.customers.handle("go", self.customers.handle("", "").split("/customers go ")[1].split()[0])
        self.assertEqual(self.customers.job()["stage"], "finding")

    def test_a_top_up_shows_at_the_next_look(self):
        balance = [40]
        code = self.paid(balance)
        self.assertIn("balance of 40", self.customers.handle("go", code))
        balance[0] = 500
        text = self.customers.handle("", "")
        self.assertIn("Your credits are in.", text)
        self.assertIn("You have 500", text)
        self.assertNotIn("balance of 40", text)
        self.customers.handle("go", text.split("/customers go ")[1].split()[0])
        self.assertEqual(len(self.api.requests), 1)

    def test_a_rate_limit_at_the_request_gives_the_way_back(self):
        code = self.code("go")
        self.api.fail_request = ApiError("Too many requests", 429)
        text = self.customers.handle("go", code)
        self.assertIn("nothing was spent", text)
        self.assertIn(f"/customers go {code}", text)
        self.assertEqual(self.customers.job()["stage"], "scanned")

    def test_leadpoet_down_while_waiting_says_to_try_again(self):
        self.customers.handle("go", self.code("go"))
        def down(_):
            raise ApiError("Leadpoet returned HTTP 502.", status=502, ambiguous=True)
        self.api.get_request = down
        text = self.customers.handle("", "")
        self.assertIn("Nothing was lost. Try again in a minute.", text)
        self.assertNotIn("502", text)
        self.assertEqual(self.customers.job()["stage"], "finding")

    def test_picking_up_the_request_from_the_app_says_so(self):
        self.api.credits_left = 0
        self.customers.handle("go", self.code("go"))
        self.assertEqual(self.customers.advance(), "")  # Nothing in the app yet, so nothing to say.
        self.api.app_requests = [{"id": "app-1", "icp_id": "i2", "created_at": "2099-01-01T00:00:00Z",
                                  "request_description": description(self.customers.job()["brief"])}]
        ping = self.customers.advance()
        self.assertIn("I see your search in Leadpoet.", ping)
        self.assertIn("1 to 2 days", ping)
        self.assertEqual(self.customers.advance(), "")  # Said once.

    def test_nobody_is_researching_before_the_emails_are_asked_for(self):
        self.customers.handle("go", self.code("go"))
        self.api.state = "completed"
        self.customers.handle("", "")
        listing = self.customers.handle("list", "")
        self.assertIn("Marcus Oyelaran, Pinecrest Freight: email not written yet", listing)
        self.assertNotIn("researching", listing)
        self.assertNotIn("no personal line", listing)

    def test_spend_uses_a_stable_key_so_a_retry_cannot_double_charge(self):
        code = self.code("go")
        self.api.fail_request = ApiError("timeout", ambiguous=True)
        self.assertIn("no double charge", self.customers.handle("go", code))
        self.api.fail_request = None
        self.customers.handle("go", code)
        self.assertEqual(len(set(self.api.keys)), 1)
        self.assertEqual(len(self.api.requests), 1)
        self.assertEqual(self.api.requests[0]["credit_budget"], 100)

    def test_short_credits_hand_off_to_the_free_offer_and_adopt_the_app_request(self):
        self.api.credits_left = 0
        go = self.code("go")
        text = self.customers.handle("go", go)
        brief = self.customers.job()["brief"]
        # Both of the form's required boxes, each with the text that goes in it, so nobody has to write anything.
        ideal, product = text.index('"Describe your ideal customer"'), text.index('"Describe your product or service"')
        self.assertLess(text.index("/dashboard/lead-engine"), ideal)
        self.assertLess(ideal, text.index(description(brief)))
        self.assertLess(text.index(description(brief)), product)
        self.assertLess(product, text.index(product_text(brief)))
        self.assertIn("Submit request", text)
        self.assertIn(f"/customers go {go}", text)  # The way back for someone whose free credits are spent.
        self.assertEqual(self.api.requests, [])
        self.api.app_requests = [{"id": "old", "created_at": "2020-01-01T00:00:00Z"},
                                 {"id": "find-1", "created_at": "2099-01-01T00:00:00Z", "icp_id": "icp-1",
                                  "request_description": description(self.customers.job()["brief"])}]
        self.customers.advance()
        self.assertEqual(self.customers.job()["query_id"], "find-1")

    def test_no_key_shows_the_free_offer(self):
        customers = Customers(None, Store(Path(self.tmp.name) / "b.db"), StockLlm(), stock_fetch)
        text = customers.start(SELLER)
        self.assertIn("It takes a free Leadpoet account.", text)
        code = text.split("/customers go ")[1].split()[0]
        steps = customers.handle("go", code)
        # Where to sign up, where the key is made, the one command that adds it, and the way back.
        for needed in ("https://leadpoet.com/auth", "https://leadpoet.com/dashboard/account?section=integrations", "Create API key",
                       "Select all", "hermes config set LEADPOET_API_KEY <your key>", f"/customers go {code}"):
            self.assertIn(needed, steps)

    def test_a_key_added_after_the_offer_is_checked_before_it_is_welcomed(self):
        customers = Customers(None, Store(Path(self.tmp.name) / "c.db"), StockLlm(), stock_fetch)
        code = customers.start(SELLER).split("/customers go ")[1].split()[0]
        customers.handle("go", code)
        customers.api = self.api
        self.assertIn("Your key is in.", customers.handle("", ""))
        self.api.identity = lambda: (_ for _ in ()).throw(ApiError("Invalid API credential", 401))
        text = customers.handle("", "")
        self.assertNotIn("Your key is in.", text)
        self.assertIn("hermes config set LEADPOET_API_KEY <your key>", text)

    def test_a_free_account_with_no_credits_is_offered_its_free_search_not_a_top_up(self):
        self.api.credits_left = 0
        self.api.credits = lambda: {"credits_remaining": 0, "plan": "free"}
        text = self.customers.handle("", "")
        self.assertIn("Leadpoet takes this one in its app", text)
        self.assertNotIn("doesn't cover", text)
        self.assertIn("Describe your ideal customer", self.customers.handle("go", self.code("go")))

    def test_a_paid_account_with_no_credits_is_told_what_it_is_short(self):
        self.api.credits_left = 0
        self.api.credits = lambda: {"credits_remaining": 0, "plan": "growth"}
        offer = self.customers.handle("", "")
        self.assertIn("doesn't cover", offer)
        self.assertIn("Add credits here", offer)  # Confirming would only repeat the shortfall, so it asks for credits first.
        self.assertNotIn("To confirm", offer)
        text = self.customers.handle("go", self.code("go"))  # A paid plan tops up. It has no free credits to claim.
        self.assertIn("Add credits here", text)
        self.assertNotIn("Free accounts", text)

    def test_waiting_is_silent_until_contacts_land_then_pings_once(self):
        self.customers.handle("go", self.code("go"))
        self.assertEqual(self.customers.advance(), "")
        self.api.state = "completed"
        self.assertIn("3 verified contacts", self.customers.advance())
        self.assertEqual(self.customers.advance(), "")

    def test_the_ping_says_why_each_company_and_never_echoes_the_request(self):
        self.customers.handle("go", self.code("go"))
        body = self.api.requests[0]
        real = self.api.pipeline_contacts
        copies = {"Tallgrass Couriers": body["request_description"].upper() + ".", "Harbor Lane Roasters": " N/A "}
        self.api.pipeline_contacts = lambda icp: [{**row, "icp_details": copies.get(row["company"], row.get("icp_details"))}
                                                  for row in real(icp)]
        self.api.state = "completed"
        text = self.customers.advance()
        self.assertIn("Why each company:\n• Pinecrest Freight: Posted a night dispatcher role", text)
        self.assertIn("• Harbor Lane Roasters: Matches your profile.", text)  # Every company is listed, so the count adds up.
        self.assertIn("• Tallgrass Couriers: Matches your profile.", text)
        self.assertNotIn(body["request_description"].upper(), text)
        self.assertNotIn("N/A", text)
        self.assertEqual([person["why"] != "" for person in self.customers.job()["people"]], [True, False, False])

    def test_each_company_gets_its_best_buyer_and_the_ping_says_where_the_rest_are(self):
        """Leadpoet delivers two contacts a company. Both were paid for, so the extra ones can't vanish without a word."""
        self.customers.handle("go", self.code("go"))
        real = self.api.pipeline_contacts
        long_note = ("Posted a night dispatcher role on Oct 28 that asks for clean shift handovers. The team runs three depots, "
                     "so handovers cross sites every night. The evidence confirms the hiring; it does not show that they are evaluating Sunnyday.")
        self.api.pipeline_contacts = lambda icp: [
            *({**row, "id": row["id"] + "-office", "name": "Sam Office", "role": "Office Manager",
               "email": "sam@" + row["email"].split("@")[1]} for row in real(icp)),  # Arrives first, fits worse.
            *({**row, "icp_details": long_note} if row["company"] == "Pinecrest Freight" else row for row in real(icp))]
        self.api.state = "completed"
        text = self.customers.advance()
        self.assertIn("6 verified contacts at 3 companies are in.", text)
        self.assertIn("the other 3 are in your Leadpoet pipeline", text)
        self.assertIn("https://leadpoet.com/dashboard/pipeline", text)
        # No listed role fits Tallgrass, so the senior title wins: an Operations Director over an Office Manager.
        self.assertEqual([person["name"] for person in self.customers.job()["people"]], ["Marcus Oyelaran", "Priya Raman", "Lena Fischer"])
        self.assertIn("• Pinecrest Freight: Posted a night dispatcher role on Oct 28 that asks for clean shift handovers.\n", text)
        self.assertNotIn("three depots", text)  # The phone shows the event, not the whole note.

    def test_a_listed_hands_on_role_never_beats_a_senior_title(self):
        """Seen live: a card listed "Sales Development Representative" last, which put an SDR ahead of the VP of Sales."""
        self.customers.handle("go", self.code("go"))
        job = self.customers.job()
        job["brief"]["buyers"] = ["Head of Sales", "Sales Development Representative"]
        self.customers.save(job)
        real = self.api.pipeline_contacts
        self.api.pipeline_contacts = lambda icp: [
            *({**row, "id": row["id"] + "-sdr", "name": "Sid Rep", "role": "Sales Development Representative",
               "email": "sid@" + row["email"].split("@")[1]} for row in real(icp)),
            *({**row, "id": row["id"] + "-vp", "name": "Vera Pratt", "role": "VP of Sales",
               "email": "vera@" + row["email"].split("@")[1]} for row in real(icp))]
        self.api.state = "completed"
        self.customers.advance()
        self.assertEqual({person["name"] for person in self.customers.job()["people"]}, {"Vera Pratt"})

    def test_with_no_listed_role_the_senior_title_is_written_to(self):
        """Seen live: "Sales Development Representative" arrived first and was chosen over the Director of Sales."""
        from leadpoet_plugin.customers import _seniority
        ranked = sorted(["Sales Development Representative", "Account Executive", "Director of Sales", "Sales Manager",
                         "Head of Sales", "Vice President of Sales", "Chief Revenue Officer"], key=lambda role: _seniority(role.lower()))
        self.assertEqual(ranked, ["Chief Revenue Officer", "Vice President of Sales", "Head of Sales", "Director of Sales",
                                  "Sales Manager", "Sales Development Representative", "Account Executive"])

    def test_another_workspaces_contacts_are_ignored(self):
        self.customers.handle("go", self.code("go"))
        real = self.api.pipeline_contacts
        self.api.pipeline_contacts = lambda icp: [{**row, "query_id": "someone-else"} for row in real(icp)]
        self.api.state = "completed"
        self.customers.advance()
        self.assertEqual(self.customers.job()["stage"], "finding")
        self.assertIn("can't see a contact", self.customers.handle("check", ""))

    def test_a_balance_is_never_stranded(self):
        self.assertEqual(affordable_companies(100, 10), 10)
        self.assertEqual(affordable_companies(150, 10), 0)
        self.assertEqual(affordable_companies(5, 10), 0)


class WriteCase(Case):
    def test_a_line_the_page_does_not_say_is_thrown_out(self):
        self.to_review()
        self.assertEqual(self.people()["Lena"]["lines"], [])
        self.assertEqual(self.people()["Lena"]["dropped"], 1)
        self.assertNotIn("Omaha", json.dumps(self.people()["Lena"]["touches"]))
        self.assertIn("threw out 1", self.text)

    def test_the_summary_counts_one_person_in_the_singular(self):
        real = self.api.pipeline_contacts
        self.api.pipeline_contacts = lambda icp: [row for row in real(icp) if row["id"] == "lead-marcus"]
        self.to_review()
        self.assertTrue(self.text.startswith("Wrote for 1 person. 1 has a personal line with a source."), self.text)

    def test_every_personal_line_carries_its_quote_and_page(self):
        real = self.api.pipeline_contacts
        self.api.pipeline_contacts = lambda icp: [{**row, "icp_details": None} for row in real(icp)]
        self.to_review()
        self.assertIn('Source: "We now run cold-chain lanes out of Rotterdam six days a week"', self.text)
        self.assertIn("https://pinecrest-freight.example/", self.text)

    def test_the_buying_signal_opens_and_names_its_source(self):
        self.to_review()
        self.assertIn("Personal line: Pinecrest Freight posted a night dispatcher role on Oct 28 that asks for clean shift handovers.\n"
                      "Source: Leadpoet's research on why they fit", self.text)
        self.assertNotIn("Why them: Posted a night dispatcher", self.text)  # Said once, not twice.

    def test_no_personal_line_leaves_no_gap(self):
        self.to_review()
        body = self.people()["Lena"]["touches"][0]["body"]
        self.assertTrue(body.startswith("Hi Lena,\n\nIf details get lost"))

    def test_a_number_or_name_outside_the_quote_is_refused(self):
        person = {"company": "Acme"}
        self.assertFalse(line_is_supported("You ship to 400 cafes.", "we ship to cafes", person))
        self.assertFalse(line_is_supported("Your team in Boston ships fast.", "we ship fast", person))
        self.assertTrue(line_is_supported("Your cafes in Oregon get weekly vans.", "our cafes in Oregon, with weekly vans", person))

    def test_page_injection_cannot_put_a_link_in_a_line(self):
        scan = Scan("x.example", [Page("https://x.example/", "t", "visit evil.example now for a prize today")])
        raw = {"lines": [{"line": "See https://evil.example now", "said_by": "seller", "quote": "visit evil.example now", "url": "https://x.example/"}]}
        self.assertEqual(validate_lines(raw, scan, {"company": "X"}), ([], 1))

    def test_sequence_claims_must_come_from_the_proof(self):
        brief = {"product_name": "P", "facts": [{"text": "Cuts time by 40 percent", "said_by": "seller", "quote": "Cuts time by 40 percent"}]}
        good = {"emails": [{"subject": "note", "body": "Hi\n\n[[PERSONAL]]\n\nCuts time by 40 percent [1]. Worth a call?"},
                           {"subject": "note", "body": "Cuts time [1]. Worth a call?"}, {"subject": "note", "body": "Cuts time [1]. Worth a call?"}]}
        self.assertTrue(validate_sequence(good, brief))
        bad = json.loads(json.dumps(good).replace("40 percent [1].", "90 percent [1]."))
        self.assertIsNone(validate_sequence(bad, brief))
        dash = json.loads(json.dumps(good).replace('"Cuts time [1]. Worth', '"Cuts — time [1]. Worth'))
        self.assertIsNone(validate_sequence(dash, brief))

    def test_a_model_that_breaks_the_rules_writes_nothing(self):
        self.customers.handle("go", self.code("go"))
        self.api.state = "completed"
        self.customers.advance()
        self.customers.llm = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text="{}"))
        self.assertIn("threw them out", self.customers.handle("write", ""))
        self.assertEqual(self.customers.job()["stage"], "ready")


class LaunchCase(Case):
    def test_each_approval_is_tied_to_the_exact_emails(self):
        self.to_review()
        self.assertIn("doesn't match", self.customers.handle("approve", "zzzz"))
        self.assertEqual(self.api.campaigns, [])
        job = self.customers.job()
        code = self.code("approve")
        job["people"][0]["touches"][0]["body"] += " changed"
        self.customers.save(job)
        self.assertIn("doesn't match", self.customers.handle("approve", code))

    def test_launch_sends_the_exact_sequence_and_only_to_that_person(self):
        self.to_review()
        self.customers.handle("all", self.all_code())
        self.assertEqual(len(self.api.campaigns), 3)
        body = self.api.campaigns[0]
        touches = body["advanced_settings"]["controlled_campaign"]["sequence"]["touches"]
        self.assertEqual(touches[0]["body"], self.people()["Marcus"]["touches"][0]["body"])
        self.assertEqual(body["mode"], "copilot")

    def test_retrying_a_launch_reuses_the_same_keys(self):
        self.to_review()
        self.api.proposal = {"ready": False, "reasons": ["profile_not_confirmed"]}
        text = self.customers.handle("all", self.all_code())
        self.assertIn("profile not confirmed", text)
        first = list(self.api.keys)
        self.api.proposal = {"ready": True, "contract": {}}
        self.customers.advance()
        self.assertEqual(len(self.api.campaigns), 3)
        self.assertEqual(len([k for k in self.api.keys if k.endswith("-campaign")]), 3)
        self.assertLessEqual(len(first), len(self.api.keys))

    def test_no_inbox_keeps_approvals_and_says_what_to_do(self):
        self.to_review()
        self.api.senders = []
        text = self.customers.handle("all", self.all_code())
        self.assertIn("Connect an email inbox", text)
        self.api.senders = ["acct-1"]
        self.customers.advance()
        self.assertEqual(self.customers.job()["stage"], "live")

    def test_live_says_when_the_next_email_goes_out(self):
        import time as clock
        from datetime import datetime, timezone
        at = lambda minutes: datetime.fromtimestamp(clock.time() + minutes * 60, timezone.utc).isoformat()
        self.to_review()
        self.customers.handle("all", self.all_code())
        self.customers.advance()
        self.assertEqual(self.customers.job()["stage"], "live")
        self.assertNotIn("next email", self.customers.handle("", ""))
        self.api.queued_messages = lambda: [
            {"lead_id": "someone-else", "display_state": "scheduled", "scheduled_for": at(5)},
            {"lead_id": "lead-marcus", "display_state": "sent", "scheduled_for": at(10)},
            {"lead_id": "lead-priya", "display_state": "scheduled", "scheduled_for": at(181)}]
        self.assertIn("The next email goes out in about 3 hours.", self.customers.handle("", ""))
        self.api.queued_messages = lambda: [{"lead_id": "lead-priya", "display_state": "queued", "scheduled_for": at(-15)}]
        self.assertIn("The next email is due now.", self.customers.handle("", ""))
        def broken():
            raise ApiError("down", 503)
        self.api.queued_messages = broken
        self.assertIn("Live.", self.customers.handle("", ""))

    def test_a_check_that_finishes_the_launch_says_live_once_and_approvals_go_quietly(self):
        """Leadpoet prepares drafts a few at a time. A ping per batch of approvals is noise; Live is the news."""
        from unittest import mock
        self.to_review()
        with mock.patch("leadpoet_plugin.customers.MAX_LAUNCHES_PER_RUN", 1):
            text = self.customers.handle("all", self.all_code())
            self.assertIn("Launching: 1 of 3", text)
            self.assertNotIn("on the way", text)
            self.assertEqual(self.customers.advance(), "")
            self.assertIn("Live. 3 sequences running", self.customers.advance())
            self.assertEqual(self.customers.advance(), "")
        self.assertEqual(len(self.api.approved), 3)

    def test_missing_sender_details_block_the_launch(self):
        self.to_review()
        self.api.sender_missing = ["postal_address"]
        self.assertIn("postal address", self.customers.handle("all", self.all_code()))
        self.assertTrue(all(person["state"] == "approved" for person in self.people().values()))

    def test_an_opted_out_contact_is_held_not_forced(self):
        self.to_review()
        self.api.admitted = 0
        self.assertIn("held back 3", self.customers.handle("all", self.all_code()))
        self.assertEqual(self.api.campaigns, [])

    def test_only_a_draft_matching_the_approved_text_is_approved(self):
        self.to_review()
        self.customers.handle("approve", self.code("approve"))
        self.assertEqual(self.api.approved, ["draft-campaign-1"])
        self.api._drafts["changed"] = {"id": "changed", "lead_id": "lead-marcus", "can_approve": True,
                                       "rendered_subject": "x", "rendered_body": "A different email."}
        self.assertIn("doesn't match", self.customers.advance())
        self.assertNotIn("changed", self.api.approved)

    def test_a_draft_the_server_will_not_approve_is_left_alone(self):
        self.to_review()
        self.customers.handle("approve", self.code("approve"))
        draft = {"id": "d2", "lead_id": "lead-marcus", "can_approve": False,
                 "rendered_subject": "the next shift", "rendered_body": self.people()["Marcus"]["touches"][1]["body"]}
        self.api._drafts["d2"] = draft
        self.customers.advance()
        self.assertNotIn("d2", self.api.approved)

    def test_a_scheduled_check_cannot_overlap_a_command(self):
        token = self.store.try_lock("customers", self.customers.now())
        self.assertTrue(token)
        self.assertEqual(self.customers.advance(), "")
        self.assertIn("still working", self.customers.handle("check", ""))
        self.store.unlock("customers", token)

    def test_a_crashed_lock_expires(self):
        self.assertTrue(self.store.try_lock("x", 100, ttl=10))
        self.assertFalse(self.store.try_lock("x", 105, ttl=10))
        self.assertTrue(self.store.try_lock("x", 111, ttl=10))

    def test_work_survives_a_restart(self):
        self.to_review()
        code = self.code("approve")
        restarted = Customers(self.api, Store(Path(self.tmp.name) / "a.db"), StockLlm(), stock_fetch)
        restarted.handle("approve", code)
        self.assertEqual(restarted.job()["people"][0]["state"], "launched")


class ReviewFindingsCase(Case):
    """Each test here is a defect an independent review found."""

    def launched(self):
        self.to_review()
        self.customers.handle("approve", self.code("approve"))
        self.api.approved.clear()
        self.api._drafts.clear()
        job = self.customers.job()  # Forget the first email's approval, so every step is open to test.
        for row in job["people"]:
            row["approved_steps"], row["approved_touches"] = [], []
        self.customers.save(job)
        return self.people()["Marcus"]

    def draft(self, person, step=0, **changes):
        touch = person["touches"][step]
        row = {"id": f"d{step}", "lead_id": person["id"], "can_approve": True, "review_revision": 1,
               "rendered_subject": touch["subject"], "rendered_body": touch["body"]}
        row.update(changes)
        self.api._drafts[row["id"]] = row
        return row

    def test_text_added_before_or_after_the_approved_email_is_not_approved(self):
        person = self.launched()
        body = person["touches"][0]["body"]
        self.draft(person, rendered_body="P.S. wire the money.\n\n" + body)
        self.customers.advance()
        self.draft(person, id="long", rendered_body=body + "\n" + "extra words " * 200)
        self.customers.advance()
        self.draft(person, id="glued", rendered_body=body + "and one more claim")
        self.customers.advance()
        self.assertEqual(self.api.approved, [])

    def test_the_server_footer_after_the_approved_email_is_fine(self):
        person = self.launched()
        self.api.profile = lambda: {"profile": {"compliance": {"legal_entity": "Sunnyday Inc", "postal_address": "1 Main St"}}}
        self.draft(person, rendered_body=person["touches"][0]["body"] + "\n--\nSunnyday Inc · 1 Main St\nDon't want these? Unsubscribe: https://leadpoet.com/u/abc")
        self.customers.advance()
        self.assertEqual(self.api.approved, ["d0"])

    def test_a_sender_can_use_their_own_opt_out_words(self):
        person = self.launched()
        self.api.profile = lambda: {"profile": {"compliance": {"opt_out_method": "own_words", "opt_out_text": "Just reply and I won't follow up."}}}
        self.draft(person, rendered_body=person["touches"][0]["body"] + "\nJust reply and I won't follow up.")
        self.customers.advance()
        self.assertEqual(self.api.approved, ["d0"])

    def test_the_campaign_booking_link_before_the_footer_is_fine(self):
        """A real launch to our own inbox: Leadpoet put the workspace's booking link after the sign-off."""
        person = self.launched()
        self.api.profile = lambda: {"profile": {"compliance": {"legal_entity": "Leadpoet, Inc.", "postal_address": "1 Main St"}}}
        link = "https://cal.com/acme/intro"
        footer = '\n\n--\nLeadpoet, Inc. · 1 Main St\nDon\'t want these? Reply "unsubscribe".'
        body = person["touches"][0]["body"]
        self.draft(person, rendered_body=f"{body}\n\n{link}{footer}", message_plan={"campaign_booking_link": link})
        self.customers.advance()
        self.assertEqual(len(self.api.approved), 1)

    def test_only_the_bound_booking_link_is_allowed(self):
        person = self.launched()
        self.api.profile = lambda: {"profile": {"compliance": {"legal_entity": "Leadpoet, Inc.", "postal_address": "1 Main St"}}}
        footer = '\n\n--\nLeadpoet, Inc. · 1 Main St\nDon\'t want these? Reply "unsubscribe".'
        body = person["touches"][0]["body"]
        for plan in ({"campaign_booking_link": "https://cal.com/acme/intro"}, {}, None):
            with self.subTest(plan=plan):
                self.api._drafts.clear()
                self.draft(person, rendered_body=f"{body}\n\nhttps://evil.example/x{footer}", message_plan=plan)
                self.customers.advance()
                self.assertEqual(self.api.approved, [])

    def test_text_dressed_as_a_footer_is_not_approved(self):
        person = self.launched()
        body = person["touches"][0]["body"]
        for extra in ("\nP.S. we guarantee 3x results.", "\n--\nP.S. we guarantee 3x results.",
                      "\n--\nDon't want these? Reply \"unsubscribe\". We guarantee 3x."):
            self.draft(person, rendered_body=body + extra)
            self.customers.advance()
        self.assertEqual(self.api.approved, [])

    def test_a_mismatch_warning_is_shown_on_a_manual_check(self):
        person = self.launched()
        self.draft(person, rendered_body="Something else entirely.")
        self.assertIn("doesn't match", self.customers.handle("check", ""))

    def test_a_temporary_refusal_is_tried_again_quietly(self):
        person = self.launched()
        self.draft(person)
        posts = []
        def later(approvals):
            posts.append(approvals)
            return {row["message_id"]: {"status": "needs_review", "code": "CAMPAIGN_NOT_ACTIVE"} for row in approvals}
        self.api.approve_drafts = later
        self.assertEqual(self.customers.advance(), "")
        self.customers.advance()
        self.assertEqual(len(posts), 2)

    def test_a_retry_refused_as_in_progress_keeps_the_guard(self):
        self.api.fail_request = ApiError("lost", ambiguous=True)
        self.customers.handle("go", self.code("go"))
        self.api.fail_request = ApiError("idempotency_in_progress", status=409)
        self.assertIn("no double charge", self.customers.handle("go", self.code("go")))
        self.assertIn("find_body", self.customers.job())
        self.assertIn("may already be spent", self.customers.handle("reset", ""))

    def test_a_first_try_refused_as_in_progress_keeps_the_guard(self):
        self.api.fail_request = ApiError("idempotency_in_progress", status=409)
        self.customers.handle("go", self.code("go"))
        self.assertIn("find_body", self.customers.job())

    def test_after_a_refused_payment_a_top_up_uses_a_new_key(self):
        go = self.code("go")
        self.api.fail_request = ApiError("no credits", status=402)
        self.customers.handle("go", go)
        self.api.fail_request = None
        self.customers.handle("go", go)
        self.assertEqual(len(set(self.api.keys)), 2)
        self.assertEqual(len(self.api.requests), 1)

    def test_a_refused_payment_on_a_paid_plan_shows_the_real_balance(self):
        go = self.code("go")
        self.api.fail_request = ApiError("no credits", status=402)
        self.api.credits = lambda: {"credits_remaining": 60, "plan": "growth"}
        text = self.customers.handle("go", go)
        self.assertIn("Your balance of 60 credits", text)
        self.assertNotIn("New free accounts", text)

    def test_a_key_revoked_mid_search_says_how_to_replace_it(self):
        self.customers.handle("go", self.code("go"))
        def refused(query_id):
            raise ApiError("Invalid API credential", status=401)
        self.api.get_request = refused
        text = self.customers.handle("check", "")
        self.assertIn("didn't accept that API key", text)
        self.assertIn("hermes config set LEADPOET_API_KEY <your key>", text)

    def test_a_repeat_go_does_not_move_the_hand_off_time(self):
        go = self.code("go")
        self.api.credits_left, clock = 0, [1000.0]
        self.customers.now = lambda: clock[0]
        self.customers.handle("go", go)
        first = self.customers.job()["handed_off_at"]
        clock[0] = 2000.0
        self.customers.handle("go", go)
        self.assertEqual(self.customers.job()["handed_off_at"], first)

    def test_an_unrelated_request_is_offered_not_adopted(self):
        self.api.credits_left = 0
        self.customers.handle("go", self.code("go"))
        self.api.app_requests = [{"id": "other", "icp_id": "i2", "created_at": "2099-01-01T00:00:00Z", "request_description": "Find dentists"}]
        text = self.customers.handle("check", "")
        self.assertEqual(self.customers.job()["stage"], "needs_credits")
        code = text.split("/customers use ")[1].split()[0]
        self.customers.handle("use", "0000")
        self.assertEqual(self.customers.job()["stage"], "needs_credits")
        self.customers.handle("use", code)
        self.assertEqual(self.customers.job()["query_id"], "other")

    def test_a_fixable_launch_refusal_keeps_the_approval(self):
        self.to_review()
        def refuse(*_):
            raise ApiError("Add sender details", status=422, code="SENDER_PROFILE_INCOMPLETE")
        self.api.launch_campaign = refuse
        self.customers.handle("approve", self.code("approve"))
        self.assertEqual(self.people()["Marcus"]["state"], "approved")

    def test_no_eligible_contact_is_held(self):
        self.to_review()
        def refuse(*_):
            raise ApiError("No one eligible", status=422, code="NO_ELIGIBLE_MEMBERS")
        self.api.launch_campaign = refuse
        self.customers.handle("approve", self.code("approve"))
        self.assertEqual(self.people()["Marcus"]["state"], "held")

    def test_one_rejected_campaign_does_not_block_the_next_person(self):
        self.to_review()
        real, calls = self.api.create_campaign, []
        def picky(body, key):
            calls.append(key)
            if len(calls) == 1:
                raise ApiError("bad template", status=422)
            return real(body, key)
        self.api.create_campaign = picky
        self.customers.handle("all", self.all_code())
        states = sorted(row["state"] for row in self.customers.job()["people"])
        self.assertEqual(states, ["held", "launched", "launched"])

    def test_odd_characters_in_a_line_do_not_break_writing(self):
        from leadpoet_plugin.writer import render
        emails = [{"subject": "s", "body": "Hi\n[[PERSONAL]]\nBye"}]
        touches = render(emails, {"name": "A B", "company": "C"}, [{"line": "Path \\o and \\1 here"}], "S")
        self.assertIn("Path \\o and \\1 here", touches[0]["body"])

    def test_a_company_dash_is_cleaned_and_leftover_braces_are_held(self):
        from leadpoet_plugin.writer import fits, render
        emails = [{"subject": "{company}", "body": "Hi {company}"}]
        touches = render(emails, {"name": "A", "company": "Smith—Jones"}, [], "S")
        self.assertEqual(touches[0]["subject"], "Smith-Jones")
        self.assertTrue(fits(touches))
        self.assertFalse(fits([{"subject": "s", "body": "Hi {role}"}]))

    def test_an_email_names_the_company_the_way_people_say_it(self):
        from leadpoet_plugin.writer import render
        emails = [{"subject": "Question for {company}", "body": "Hi {first_name},\n\nWho handles outbound at {company}?"}]
        said = {"Spekit, Inc.": "Spekit", "Outreach Corporation": "Outreach", "Bill Hicks & Co., Ltd.": "Bill Hicks & Co.",
                "75way Technologies Pvt. Ltd.": "75way Technologies", "COMPRESSORS UNLIMITED INTERNATIONAL, L.L.C.": "Compressors Unlimited International",
                "ACE OUTDOOR SERVICES LLC": "ACE Outdoor Services", "NASA FEDERAL CREDIT UNION": "NASA Federal Credit Union",
                "Instantly AI": "Instantly AI", "22nd Century Group, Inc.": "22nd Century Group", "IBM": "IBM", "Inc.": "Inc.",
                "Balch & Bingham LLP": "Balch & Bingham", "IMRON Corporation": "IMRON", "IRALOGIX": "IRALOGIX",
                "DYNICS, Inc.": "DYNICS", "Kahuna | Skills & Competency Management SaaS Platform": "Kahuna",
                "AG Adjustments, a Cadex Company": "AG Adjustments", "CSG (Commercial Services Group, Inc.)": "CSG",
                "Blitz: Automated Commission Management": "Blitz", "APS - Adaptive Processing Solutions": "APS",
                "Intermountain Electric, Inc. (IME)": "Intermountain Electric", "Post Guard / Encore Commercial Products": "Post Guard",
                "(TIG) The Innovation Group": "The Innovation Group", "Hewlett-Packard": "Hewlett-Packard",
                "Kraft / Heinz": "Kraft / Heinz", "Sherwin - Williams": "Sherwin - Williams", "Re: Build Manufacturing": "Re: Build Manufacturing",
                "A / B Testing Partners": "A / B Testing Partners", "Smokeball - Legal Software": "Smokeball",
                "ACME WIDGETS | Industrial Supplies": "ACME Widgets", "ASCO - (Asphalt Surfacing Company)": "ASCO"}
        for record, spoken in said.items():
            touch = render(emails, {"name": "Mark Bell", "company": record}, [], "S")[0]
            self.assertEqual(touch["subject"], f"Question for {spoken}", record)
            self.assertTrue(touch["body"].endswith(f"at {spoken}?"), record)

    def test_an_empty_subject_is_not_approved(self):
        person = self.launched()
        self.draft(person, rendered_subject="")
        self.customers.advance()
        self.assertEqual(self.api.approved, [])

    def test_the_first_email_cannot_arrive_as_a_reply_subject(self):
        person = self.launched()
        self.draft(person, rendered_subject="Re: " + person["touches"][0]["subject"])
        self.customers.advance()
        self.assertEqual(self.api.approved, [])

    def test_a_second_draft_of_the_same_email_is_not_approved(self):
        person = self.launched()
        self.draft(person)
        self.draft(person, id="twin")
        self.customers.advance()
        self.assertEqual(len(self.api.approved), 1)

    def test_the_code_for_approving_everyone_appears_only_under_every_email(self):
        self.to_review()
        self.assertNotRegex(self.text, r"/customers all [0-9a-f]{4}")
        everyone = self.customers.handle("all", "")
        job = self.customers.job()
        new = [person for person in job["people"] if person["state"] == "new"]
        shown = everyone.split("\n---\n")[0]  # The shared emails, filled in for each person, must be exactly theirs.
        for person in new:
            filled = shown.replace("[name]", person["name"].split()[0]).replace("[company]", person["company"])
            line = person["lines"][0]["line"] if person["lines"] else ""
            filled = filled.replace("\n\n[their personal line]\n\n", f"\n\n{line}\n\n" if line else "\n\n")
            for touch in person["touches"]:
                self.assertIn(touch["body"], filled)
            self.assertIn(person["email"], everyone)
            for entry in person["lines"]:
                self.assertIn(entry["line"], everyone)
        self.assertLess(len(everyone), sum(len(touch["body"]) for person in new for touch in person["touches"]))
        self.assertRegex(everyone, r"/customers all [0-9a-f]{4}")

    def test_emails_that_differ_from_the_shared_text_are_shown_in_full(self):
        self.to_review()
        job = self.customers.job()
        odd = next(person for person in job["people"] if person["state"] == "new")
        odd["touches"][1]["body"] = odd["touches"][1]["body"] + "\n\nP.S. A different ending."
        self.customers.save(job)
        everyone = self.customers.handle("all", "")
        self.assertIn(odd["touches"][1]["body"], everyone)

    def test_a_refused_draft_is_reported_once_and_not_posted_again(self):
        person = self.launched()
        self.draft(person)
        posts = []
        def refuse(approvals):
            posts.append(approvals)
            return {row["message_id"]: {"status": "needs_review"} for row in approvals}
        self.api.approve_drafts = refuse
        self.assertIn("Review", self.customers.advance())
        self.assertEqual(self.customers.advance(), "")
        self.assertEqual(len(posts), 1)
        self.draft(person, review_revision=2)
        self.customers.advance()
        self.assertEqual(len(posts), 2)

    def test_a_retry_after_a_lost_request_repeats_it_exactly(self):
        self.api.fail_request = ApiError("lost", ambiguous=True)
        self.customers.handle("go", self.code("go"))
        self.api.fail_request, self.api.credits_left = None, 0
        self.customers.handle("go", self.code("go"))
        self.assertEqual(self.customers.job()["stage"], "finding")
        self.assertEqual(self.api.requests[0]["max_leads"], 10)
        self.assertEqual(len(set(self.api.keys)), 1)

    def test_reset_after_a_lost_request_warns_about_the_possible_charge(self):
        self.api.fail_request = ApiError("lost", ambiguous=True)
        self.customers.handle("go", self.code("go"))
        self.assertIn("may already be spent", self.customers.handle("reset", ""))
        self.assertIn("may already be spent", self.customers.handle("https://other.example", ""))
        self.assertIsNotNone(self.customers.job())
        self.customers.handle("reset", "confirm")
        self.assertIsNone(self.customers.job())

    def test_a_refused_request_clears_so_the_next_one_can_differ(self):
        self.api.fail_request = ApiError("no", status=400)
        self.customers.handle("go", self.code("go"))
        self.assertNotIn("find_body", self.customers.job())

    def test_a_launch_whose_answer_was_lost_counts_as_launched(self):
        self.to_review()
        real = self.api.launch_campaign
        def lost(campaign_id, body):
            real(campaign_id, body)
            raise ApiError("lost", ambiguous=True)
        self.api.launch_campaign = lost
        self.customers.handle("approve", self.code("approve"))
        self.assertEqual(self.people()["Marcus"]["state"], "approved")
        self.api.launch_campaign = lambda *_: self.fail("launched twice")
        self.customers.advance()
        self.assertEqual(self.people()["Marcus"]["state"], "launched")

    def test_the_specific_reason_inside_a_semantic_error_is_used(self):
        from leadpoet_plugin.api import _server_error
        raw = json.dumps({"error": {"code": "semantic_error", "message": "m", "details": {"reason": "ALREADY_AUTHORIZED"}}}).encode()
        self.assertEqual(_server_error(raw)[1], "ALREADY_AUTHORIZED")

    def test_only_ten_companies_can_be_bought(self):
        self.assertEqual(affordable_companies(100, 10), 10)
        self.assertEqual(affordable_companies(99, 10), 0)
        self.assertEqual(affordable_companies(250, 10), 10)
        self.assertEqual(affordable_companies(40, 10), 0)

    def test_a_small_balance_gets_an_honest_message_and_spends_nothing(self):
        self.api.credits_left = 40
        text = self.customers.handle("go", self.code("go"))
        self.assertIn("40 credits", text)
        self.assertEqual(self.api.requests, [])

    def test_included_contacts_are_requested(self):
        self.api.contact_settings = lambda: {"included_contacts_per_company": 3, "pricing": {"credits_per_company": 10}}
        self.customers.handle("go", self.code("go"))
        self.assertEqual(self.api.requests[0]["contacts_per_company"], 3)

    def test_follow_ups_are_still_approved_after_a_reset(self):
        person = self.launched()
        self.customers.handle("reset", "confirm")
        self.assertIsNone(self.customers.job())
        self.draft(person, 1)
        self.customers.advance()
        self.assertEqual(self.api.approved, ["d1"])
        self.assertIsNone(self.customers.job())

    def test_old_people_stop_being_watched(self):
        person = self.launched()
        self.customers.handle("reset", "confirm")
        self.customers.now = lambda: 40 * 86400 + __import__("time").time()
        self.draft(person, 1)
        self.customers.advance()
        self.assertEqual(self.api.approved, [])

    def test_a_campaign_uses_at_most_twenty_senders(self):
        self.api.senders = [f"acct-{n}" for n in range(30)]
        self.to_review()
        self.customers.handle("approve", self.code("approve"))
        self.assertEqual(len(self.api.campaigns[0]["allowed_account_ids"]), 20)

    def test_a_new_site_after_going_live_keeps_approving_follow_ups(self):
        person = self.launched()
        self.customers.handle("skip", "")
        self.customers.handle("skip", "")
        self.assertEqual(self.customers.job()["stage"], "live")
        self.assertIn("handover", self.customers.handle(SELLER, "").lower())
        self.draft(person, 1)
        self.customers.advance()
        self.assertEqual(self.api.approved, ["d1"])

    def test_an_email_too_long_for_leadpoet_loses_its_line_first(self):
        job_emails = [{"subject": "s", "body": "x" * 1000}, {"subject": "s", "body": "y"}, {"subject": "s", "body": "z"}]
        job = {"emails": job_emails, "sender": "Sam", "brief": {"product_name": "P"}}
        person = {"name": "A B", "company": "C", "id": "1"}
        job_emails[0]["body"] = "x" * 1000 + "\n[[PERSONAL]]"
        self.customers._finish_person(job, person, [{"line": "l" * 200, "said_by": "seller", "quote": "q", "url": "u"}], 0)
        self.assertEqual((person["state"], person["lines"], person["dropped"]), ("new", [], 1))
        job_emails[0]["body"] = "x" * 1200 + "\n[[PERSONAL]]"
        self.customers._finish_person(job, person, [], 0)
        self.assertEqual(person["state"], "held")

    def test_one_lock_holder_cannot_free_the_next_holders_lock(self):
        first = self.store.try_lock("x", 100, ttl=10)
        second = self.store.try_lock("x", 111, ttl=10)
        self.store.unlock("x", first)
        self.assertFalse(self.store.try_lock("x", 112, ttl=10))
        self.store.unlock("x", second)
        self.assertTrue(self.store.try_lock("x", 112, ttl=10))

    def test_a_failed_customers_check_does_not_lose_reply_alerts(self):
        from leadpoet_plugin import poll
        class Broken:
            def advance(self):
                raise KeyError("x")
        self.assertEqual(poll.advance(Broken(), self.store), "")
        self.assertEqual(poll.advance(Broken(), self.store), "")
        self.assertIn("stuck", poll.advance(Broken(), self.store))


class ProofCase(unittest.TestCase):
    QUOTE = "We now run 150 offices across Ohio and ship organic coffee to independent cafes"
    PERSON = {"company": "Acme"}

    def supported(self, line, quote=None):
        return line_is_supported(line, quote or self.QUOTE, self.PERSON)

    def test_a_number_must_match_a_whole_number(self):
        self.assertFalse(self.supported("You run 5 offices across Ohio."))
        self.assertFalse(self.supported("You run 15 offices across Ohio."))
        self.assertTrue(self.supported("You run 150 offices across Ohio."))

    def test_a_name_must_match_a_whole_word(self):
        self.assertFalse(self.supported("Shipping organic coffee to independent cafes across Ohi."))
        self.assertFalse(self.supported("Your offices in Oh ship organic coffee to Cafes Inc."))

    def test_a_claim_the_quote_never_made_is_refused(self):
        self.assertFalse(self.supported("Your ship was sued for fraud."))
        self.assertFalse(self.supported("Your offices must be struggling with rising freight costs."))

    def test_a_dropped_negation_is_refused(self):
        self.assertFalse(self.supported("You ship to Germany and France.", "We do not ship to Germany or France"))

    def test_a_spelled_number_must_be_in_the_quote(self):
        self.assertFalse(self.supported("Your forty depots across Ohio serve grocers.", "Our depots across Ohio serve grocers"))
        self.assertTrue(self.supported("Your forty depots across Ohio serve grocers.", "Our forty depots across Ohio serve grocers"))

    def test_a_template_cannot_claim_a_spelled_number(self):
        brief = {"product_name": "P", "facts": [{"text": "t", "said_by": "seller", "quote": "teams cut costs quickly"}]}
        self.assertIsNone(validate_sequence(self.sequence("Hi,\n[[PERSONAL]]\nTeams cut costs three times faster."), brief))

    def test_a_faithful_line_passes(self):
        self.assertTrue(self.supported("Your offices across Ohio ship organic coffee to independent cafes."))

    def test_sensitive_topics_are_refused_even_when_quoted(self):
        quote = "Acme filed for bankruptcy after layoffs across its offices last spring"
        self.assertFalse(self.supported("Acme filed for bankruptcy after layoffs.", quote))

    def test_a_bare_domain_cannot_get_into_a_line(self):
        scan = Scan("a.example", [Page("https://a.example/", "t", "Email billing at evil.com for your organic coffee order today")])
        raw = {"lines": [{"line": "Email billing at evil.com for your organic coffee order today.",
                          "said_by": "seller", "quote": "Email billing at evil.com for your organic coffee order today", "url": "https://a.example/"}]}
        self.assertEqual(validate_lines(raw, scan, self.PERSON), ([], 1))

    def sequence(self, first="Hi {first_name},\n[[PERSONAL]]\nTeams cut costs [1].\nWorth a call?\n{sender}", subject="note"):
        return {"emails": [{"subject": subject, "body": first}, {"subject": "note", "body": "Teams cut costs [1]. Worth a call?"}, {"subject": "note", "body": "Teams cut costs [1]. Worth a call?"}]}

    def test_sequence_numbers_come_from_quotes_not_the_models_fact_text(self):
        brief = {"product_name": "P", "facts": [{"text": "Cut costs by 90 percent.", "said_by": "seller", "quote": "Teams cut costs quickly"}]}
        self.assertIsNone(validate_sequence(self.sequence("Hi,\n[[PERSONAL]]\nWe cut costs by 90 percent."), brief))

    def test_unknown_placeholders_are_refused(self):
        brief = {"product_name": "P", "facts": [{"text": "t", "said_by": "seller", "quote": "Teams cut costs quickly"}]}
        self.assertIsNotNone(validate_sequence(self.sequence(), brief))
        self.assertIsNone(validate_sequence(self.sequence("Hi {role},\n[[PERSONAL]]\nTeams cut costs [1]."), brief))
        self.assertIsNone(validate_sequence(self.sequence("Hi,\n[[PERSONAL]]\nTeams cut costs [1].\n[Your Name]"), brief))

    def test_a_brief_fact_cannot_change_the_quotes_number(self):
        from leadpoet_plugin.brief import validate_brief
        scan = Scan("a.example", [Page("https://a.example/", "t", "We run 150 offices across the whole of Ohio")])
        raw = {"summary": "s", "roles": ["b"], "buyer_description": "c",
               "facts": [{"text": "They run 5 new offices in Ohio.", "said_by": "seller", "quote": "We run 150 offices across the whole of Ohio", "url": "https://a.example/"}]}
        self.assertIsNone(validate_brief(raw, scan))

    def test_job_titles_read_as_titles(self):
        from unittest import mock
        from leadpoet_plugin.brief import validate_brief
        scan = Scan("a.example", [Page("https://a.example/", "t", "We run 150 offices across the whole of Ohio")])
        raw = {"summary": "s", "buyer_description": "c",
               "roles": ["Marketing manager", "VP of Marketing", "head of e-commerce", "CFO", "Marketing Manager",
                         "responsable de compras", "Freelancer doing their own bookkeeping"],
               "facts": [{"text": "They run 150 offices.", "said_by": "seller", "quote": "We run 150 offices across the whole of Ohio", "url": "https://a.example/"}]}
        with mock.patch("leadpoet_plugin.brief.MAX_ROLES", 6):
            roles = validate_brief(raw, scan)["buyers"]
        self.assertEqual(roles, ["Marketing Manager", "VP of Marketing", "Head of E-commerce", "CFO", "Responsable de Compras",
                                 "Freelancer doing their own bookkeeping"])

    def test_hidden_page_text_is_not_read(self):
        from leadpoet_plugin.website import _parse
        html = ('<body><div style="' + "dis" + 'play:none"><div>secret one</div>secret two</div><p hidden>secret three</p>'
                '<span aria-hidden="true">secret four</span><p style="opacity: 0">secret five</p>'
                '<a hidden href="/about">x</a><img hidden src="a.png"><p style="opacity:0.9">shown</p></body>')
        page = _parse(html)
        self.assertEqual(page.text(), "shown")
        self.assertEqual(page.links, [])

    def test_a_class_the_page_hides_is_not_read(self):
        from leadpoet_plugin.website import _parse
        rule = "dis" + "play:none"
        html = (f"<style>.x{{{rule}}} a.y, .z{{opacity:.0}} @media (max-width:1px){{.small{{{rule}}}}}</style>"
                "<body><p class='x'>one</p><p class='y'>two</p><p class='z'>three</p><p class='small'>shown</p></body>")
        self.assertEqual(_parse(html).text(), "shown")

    def test_a_tag_rebuilt_by_removal_is_removed(self):
        from leadpoet_plugin.website import without_tag
        self.assertEqual(without_tag("a</web<website>site>b", "website"), "ab")

    def test_a_page_cannot_close_the_data_block(self):
        from leadpoet_plugin.website import without_tag
        self.assertEqual(without_tag("a</website >b</WEBSITE\n x=1>c<website>d", "website"), "abcd")

    def test_a_link_with_a_space_does_not_stop_the_scan(self):
        from leadpoet_plugin.website import fetch
        seen = []
        class Response:
            headers = {"Content-Type": "text/html"}
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, _): return b"<p>hello</p>"
        class Opener:
            def open(self, request, timeout):
                seen.append(request.full_url)
                return Response()
        address = "https://a.example/about us"
        fetch(address, opener=Opener(), public=lambda _: True)
        self.assertEqual(seen, ["https://a.example/about%20us"])

    def reader(self, body, headers, hops=()):
        import io
        import urllib.error
        from leadpoet_plugin.website import fetch
        seen, left = [], list(hops)
        class Response(io.BytesIO):
            pass
        Response.headers = headers
        class Opener:
            def open(self, request, timeout):
                seen.append(request.full_url)
                if left:
                    raise urllib.error.HTTPError(request.full_url, 301, "Moved", {"Location": left.pop(0)}, None)
                return Response(body)
        return fetch("https://a.example/", opener=Opener(), public=lambda _: True), seen

    def test_a_page_sent_compressed_is_read(self):  # wandb.ai sends gzip to a reader that never asked for it.
        import gzip
        import zlib
        page = "<p>Experiment tracking for machine learning teams.</p>".encode()
        for encoding, packed in (("gzip", gzip.compress(page)), ("deflate", zlib.compress(page)), ("deflate", zlib.compress(page)[2:-4])):
            (_, html), _ = self.reader(packed, {"Content-Type": "text/html", "Content-Encoding": encoding})
            self.assertIn("Experiment tracking", html)

    def test_a_missing_page_points_to_the_home_page_as_they_would_type_it(self):  # tebra.com/solutions said "www.tebra.com".
        import urllib.error
        from leadpoet_plugin.website import SiteError, fetch
        class Opener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
        with self.assertRaises(SiteError) as raised:
            fetch("https://www.a.example/solutions", opener=Opener(), public=lambda _: True)
        self.assertEqual(raised.exception.args[0], "That site answered with an error (404). Try the home page: /customers a.example")

    def test_a_page_packed_in_a_way_it_cannot_open_is_a_plain_error(self):
        from leadpoet_plugin.website import SiteError
        with self.assertRaises(SiteError):
            self.reader(b"\x1b\x03\x00", {"Content-Type": "text/html", "Content-Encoding": "br"})

    def test_a_page_in_another_character_set_is_read(self):
        (_, html), _ = self.reader("<p>Über 500 Kunden</p>".encode("iso-8859-1"), {"Content-Type": "text/html; charset=ISO-8859-1"})
        self.assertIn("Über 500 Kunden", html)
        (_, html), _ = self.reader("<p>Über</p>".encode(), {"Content-Type": "text/html; charset=no-such-set"})
        self.assertIn("Über", html)

    def test_a_redirect_that_spells_out_the_port_keeps_a_clean_source(self):  # factorialhr.es sends you to https://factorial.es:443/
        (final, _), seen = self.reader(b"<p>hello</p>", {"Content-Type": "text/html"}, hops=["https://b.example:443/hr"])
        self.assertEqual(final, "https://b.example/hr")
        self.assertEqual(seen[-1], "https://b.example/hr")
        (final, _), _ = self.reader(b"<p>hello</p>", {"Content-Type": "text/html"}, hops=["https://b.example:8443/hr"])
        self.assertEqual(final, "https://b.example:8443/hr")

    def test_a_broken_address_is_a_plain_error(self):
        from leadpoet_plugin.website import SiteError, normalize_site
        with self.assertRaises(SiteError):
            normalize_site("https://a.example:notaport/")


class TwoWorkspacesCase(unittest.TestCase):
    def test_jobs_never_mix(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Customers(Api(), Store(Path(tmp) / "one.db"), StockLlm(), stock_fetch)
            second = Customers(Api(), Store(Path(tmp) / "two.db"), StockLlm(), stock_fetch)
            first.start(SELLER)
            self.assertIsNone(second.job())
            self.assertIn("/customers yoursite.com", second.handle("", ""))


class ProgressTests(unittest.TestCase):
    def test_a_progress_note_that_fails_never_stops_the_job(self):
        def broken(_text):
            raise RuntimeError("the chat is gone")

        with tempfile.TemporaryDirectory() as folder:
            customers = Customers(StockApi(), Store(Path(folder) / "a.db"), StockLlm(), stock_fetch, say=broken)
            self.assertIn("/customers go", customers.start(SELLER))


class LineSubjectTests(unittest.TestCase):
    def test_a_line_that_opens_with_they_is_thrown_out(self):
        """A real model wrote this. In email 2 it sat alone under the greeting, so "they" meant nobody."""
        quote, url = "We roast small batches every morning", "https://roasters.example/"
        scan = Scan("roasters.example", [Page(url, "Home", f"{quote}, and vans bring it to you.")])
        person = {"company": "Harbor Lane Roasters"}
        raw = {"lines": [{"line": "They roast small batches every morning.", "said_by": "seller", "quote": quote, "url": url}]}
        self.assertEqual(validate_lines(raw, scan, person), ([], 1))
        raw["lines"][0]["line"] = "Harbor Lane Roasters roast small batches every morning."
        self.assertEqual(len(validate_lines(raw, scan, person)[0]), 1)


class EvidenceTests(unittest.TestCase):
    """Each input is a real miss from the 24-site audit or the red team."""
    PERSON = {"company": "Acme"}
    BRIEF = {"product_name": "Stripe", "facts": [{"text": "t", "said_by": "seller", "quote": "Accept payments online in minutes"}]}

    def test_a_fact_cannot_add_a_word_its_quote_lacks(self):
        scan = Scan("basecamp.com", [Page("https://b/", "t", "One project, free forever. Pro Unlimited has no limits on projects.")])
        def fact(text, quote):
            return {"text": text, "said_by": "seller", "quote": quote, "url": "https://b/"}
        raw = {"summary": "s", "roles": ["b"], "buyer_description": "c", "product_name": "Basecamp", "facts": [
            fact("Basecamp has a free plan for one personal project.", "One project, free forever."),
            fact("Pro Unlimited has no limits on projects.", "Pro Unlimited has no limits on projects.")]}
        self.assertEqual([row["text"] for row in validate_brief(raw, scan)["facts"]], ["Pro Unlimited has no limits on projects."])

    def test_lines_that_twist_the_quote_are_refused(self):
        for line, quote in (("Acme ran out of cash.", "Customers save cash and ran the numbers out loud"),
                            ("Acme ships to Germany, not France.", "We ship to France, not Germany"),
                            ("Acme stopped the Boston office.", "We started the Boston office"),
                            ("Acme ships twenty-five crates.", "We ship crates"),
                            ("Acme lay\u200boffs hit the Boston office.", "layoffs hit the Boston office"),
                            ("Acme: your next opportunity is here.", "your next opportunity is here")):
            self.assertFalse(line_is_supported(line, quote, self.PERSON), line)

    def emails(self, first):
        return {"emails": [{"subject": "payments", "body": first}, {"subject": "payments", "body": "Stripe lets you accept payments online [1]. Worth a call?"},
                           {"subject": "payments", "body": "Stripe lets you accept payments online [1]. Worth a call?"}]}

    def assert_refused(self, first):
        self.assertIsNone(validate_sequence(self.emails(first), self.BRIEF), first)

    def test_a_good_sequence_loses_its_proof_numbers(self):
        emails = validate_sequence(self.emails("Hi {first_name},\n\n[[PERSONAL]]\n\nStripe lets you accept payments online in minutes. [1]\n\nOpen to a short call?\n\nBest,\n{sender}"), self.BRIEF)
        self.assertNotIn("[1]", emails[0]["body"])
        self.assertIn("in minutes.", emails[0]["body"])

    def test_the_seller_is_not_named_twice_in_a_row(self):
        """Seen live, run 22: "Leadpoet lets you turn buying signals into conversations. Leadpoet is a B2B lead sourcing ... platform"."""
        brief = {"product_name": "Stripe", "facts": [{"text": "t", "said_by": "seller", "quote": "Accept payments online in minutes"},
                                                     {"text": "t", "said_by": "seller", "quote": "Stripe sends invoices"}]}
        once = "Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1]. It sends invoices [2].\nWorth a call?"
        self.assertTrue(validate_sequence(self.emails(once), brief))
        self.assertIsNone(validate_sequence(self.emails(once.replace("It sends", "Stripe sends")), brief))
        apart = "Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1].\nStripe sends invoices [2].\nWorth a call?"
        self.assertTrue(validate_sequence(self.emails(apart), brief))  # Two paragraphs may each open on the name.

    def test_only_one_email_asks_who_handles_it(self):
        """Seen live, run 22: email two asked "Who handles outbound prospecting?" and email three "Who handles lead research?"."""
        def mail(second, third):
            return {"emails": [{"subject": "payments", "body": "Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1].\nWorth a call?"},
                               {"subject": "payments", "body": f"Stripe lets you accept payments online [1]. {second}"},
                               {"subject": "payments", "body": f"Stripe lets you accept payments online [1]. {third}"}]}
        self.assertTrue(validate_sequence(mail("Who handles billing at {company}?", "Is checkout a priority this quarter?"), self.BRIEF))
        self.assertIsNone(validate_sequence(mail("Who handles billing at {company}?", "Who owns checkout on your team?"), self.BRIEF))

    def test_an_email_says_if_once(self):
        """Seen live: email one opened two paragraphs in a row with If, which reads like a form."""
        one = "Hi,\n[[PERSONAL]]\nIf your team takes card payments by phone, that is time away from buyers.\nStripe lets you accept payments online [1].\nWorth a call?"
        self.assertTrue(validate_sequence(self.emails(one), self.BRIEF))
        self.assert_refused(one.replace("Stripe lets", "If checkout is slow, Stripe lets"))

    def test_each_email_names_a_different_problem(self):
        """Seen live three runs in a row: email two said email one's "spend hours" problem again in other words."""
        from leadpoet_plugin.writer import check_sequence
        first = ("Hi,\n[[PERSONAL]]\nIf your reps spend hours researching before each first call, that is time away from buyers.\n"
                 "Stripe lets you accept payments online [1].\nWorth a call?")
        def emails(condition):
            return {"emails": [{"subject": "payments", "body": first},
                               {"subject": "payments", "body": f"{condition}\nStripe lets you accept payments online [1]. Who handles billing?"},
                               {"subject": "payments", "body": "Stripe lets you accept payments online [1]. Open to a short call?"}]}
        repeat = emails("If your reps lose hours researching before a buyer is ready to talk, that is selling time lost.")
        emails_back, problem = check_sequence(repeat, self.BRIEF)
        self.assertIsNone(emails_back)
        self.assertIn("email 1's problem", problem)
        self.assertTrue(check_sequence(repeat, self.BRIEF, safe_subjects=True)[0])  # Kept in hand rather than losing all three.
        self.assertTrue(validate_sequence(emails("If checkout drags at the till, that is time away from buyers."), self.BRIEF))
        self.assertTrue(validate_sequence(emails(""), self.BRIEF))

    def test_each_email_makes_a_different_claim(self):
        """Seen live: email three repeated email one's claim word for word. A short brief may reuse its one proof."""
        from leadpoet_plugin.writer import check_sequence
        brief = {"product_name": "Stripe", "facts": [{"text": "t", "said_by": "seller", "quote": quote} for quote in
                                                      ("Accept payments online", "Send invoices in minutes", "Pay out in 40 countries")]}
        def emails(third):
            return {"emails": [{"subject": "payments", "body": "Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1].\nWorth a call?"},
                               {"subject": "Question for {company}", "body": "Stripe lets you send invoices in minutes [2]. Who handles billing?"},
                               {"subject": "Question for {company}", "body": f"{third} Open to a short call?"}]}
        self.assertTrue(validate_sequence(emails("Stripe lets you pay out in 40 countries [3]."), brief))
        repeat = emails("If checkout drags, Stripe lets you accept payments online [1].")
        self.assertIsNone(validate_sequence(repeat, brief))
        self.assertTrue(check_sequence(repeat, brief, safe_subjects=True)[0])  # Kept in hand rather than losing all three.
        two = {**brief, "facts": brief["facts"][:2]}
        self.assertTrue(validate_sequence(emails("If checkout drags, Stripe lets you accept payments online [1]."), two))

    def test_no_email_only_names_what_the_seller_is(self):
        """Seen live: email three was only "Leadpoet is a B2B lead sourcing and outreach platform." and a question."""
        from leadpoet_plugin.writer import check_sequence
        brief = {"product_name": "Stripe", "facts": [{"text": "t", "said_by": "seller", "quote": quote} for quote in
                                                      ("Accept payments online", "Send invoices in minutes",
                                                       "a payments platform for internet businesses")]}
        def emails(third):
            return {"emails": [{"subject": "payments", "body": "Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1].\nWorth a call?"},
                               {"subject": "Question for {company}", "body": "Stripe lets you send invoices in minutes [2]. Who handles billing?"},
                               {"subject": "Question for {company}", "body": f"{third} Open to a short call?"}]}
        bare = emails("Stripe is a payments platform for internet businesses [3].")
        self.assertIsNone(validate_sequence(bare, brief))
        self.assertTrue(check_sequence(bare, brief, safe_subjects=True)[0])
        self.assertTrue(validate_sequence(emails("Stripe is a payments platform for internet businesses [3]. "
                                                 "It lets you send invoices in minutes [2]."), brief))

    def test_claims_with_no_source_never_reach_an_email(self):
        marker = "Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1].\nWorth a call?\n"
        for extra in ("Google, Stripe and Walmart all rely on us.", "Act fast and get started today.", "That keeps velocity up.",
                      "Stripe handles your billing [1].", "Would a sample list be useful?", "If this hurts, Stripe can help.",
                      "Want to see our product?", "Hi, {company} just lost its biggest client.",
                      "If account research takes up your week, these checks run without you.",  # From a real leadpoet.com draft.
                      "If your list is stale, you can fix that in minutes."):
            self.assert_refused(marker + extra)

    def test_each_email_reads_as_a_message(self):
        """A live test on 19 real sites sent these shapes: fragments, testimonials, emails with nothing to answer."""
        for bad in ("Hi,\n[[PERSONAL]]\nAccept payments online [1].\nWorth a call?",  # A command with no subject.
                    "Hi,\n[[PERSONAL]]\nWe accept payments online [1].\nWorth a call?",
                    "Hi,\n[[PERSONAL]]\nIt lets you accept payments online [1].\nWorth a call?",
                    "Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1]."):
            self.assert_refused(bad)

    def test_a_condition_can_lead_into_a_checked_claim(self):
        good = "Hi,\n[[PERSONAL]]\nIf billing drags at {company}, Stripe lets you accept payments online [1].\nWorth a call?"
        self.assertTrue(validate_sequence(self.emails(good), self.BRIEF))
        self.assert_refused(good.replace("accept payments", "accept all payments"))
        self.assert_refused(good.replace("drags at {company}, Stripe", "drags at {company} everyone loves Stripe and Stripe"))
        # Seen live, run 22: an If answered with what the seller is, not what it does about it.
        brief = {"product_name": "Stripe", "facts": [{"text": "t", "said_by": "seller", "quote": "Stripe is a platform to accept payments online"}]}
        said = "Hi,\n[[PERSONAL]]\nStripe is a platform to accept payments online [1].\nWorth a call?"
        self.assertTrue(validate_sequence(self.emails(said), brief))
        self.assertIsNone(validate_sequence(self.emails(said.replace("Stripe is", "If billing drags at {company}, Stripe is")), brief))

    def test_only_two_subjects_may_name_their_company(self):
        def subject(text):
            mail = self.emails("Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1].\nWorth a call?")
            mail["emails"][0]["subject"] = text
            return validate_sequence(mail, self.BRIEF)
        self.assertTrue(subject("Question for {company}"))
        self.assertTrue(subject("Stripe for {company}"))
        self.assertIsNone(subject("Accept payments across {company}"))

    def test_a_subject_cannot_stop_mid_phrase(self):
        mail = self.emails("Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1].\nWorth a call?")
        mail["emails"][0]["subject"] = "Stripe lets you"
        self.assertIsNone(validate_sequence(mail, self.BRIEF))

    def test_an_unfinished_if_is_refused(self):
        self.assert_refused("Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1].\nIf bounces keep hitting your sends.\nWorth a call?")

    def test_the_second_personal_line_goes_after_the_greeting(self):
        """It landed above "Hi Alex" when the model separated lines with one newline."""
        emails = [{"subject": "a", "body": "Hi {first_name},\n[[PERSONAL]]\nStripe does it.\n{sender}"},
                  {"subject": "b", "body": "Hi {first_name},\nStripe does it.\n{sender}"}, {"subject": "c", "body": "Hi {first_name},\nStripe does it.\n{sender}"}]
        lines = [{"line": "First line."}, {"line": "Second line."}]
        second = render(emails, {"name": "Alex Morgan", "company": "Clay"}, lines, "Sam")[1]["body"]
        self.assertTrue(second.startswith("Hi Alex,\n\nSecond line."), second)

    def test_a_personal_line_must_open_on_their_company(self):
        scan = Scan("a.example", [Page("https://a.example/", "t", "Remote switched their whole team to Linear this spring")])
        def lines(line):
            return validate_lines({"lines": [{"line": line, "said_by": "seller", "quote": "Remote switched their whole team to Linear this spring", "url": "https://a.example/"}]}, scan, {"company": "Linear"})[0]
        self.assertFalse(lines("Remote switched their whole team to Linear this spring."))
        self.assertFalse(lines("Linear, Remote switched their whole team to Linear this spring."))
        self.assertFalse(lines("Linear Remote switched their whole team to Linear this spring."))
        self.assertFalse(lines("Linear switched their whole team this spring."))  # Remote switched, to Linear.

    def test_a_quote_about_them_from_someone_else_never_becomes_their_line(self):
        """Seen live: a customer's "Our revenue went x6 thanks to Salesflare" became "Salesflare revenue went x6"."""
        def lines(quote, line, url="https://salesflare.com/", company="Salesflare"):
            scan = Scan("salesflare.com", [Page(url, "t", quote)])
            return validate_lines({"lines": [{"line": line, "quote": quote, "url": url}]}, scan, {"company": company})[0]
        self.assertFalse(lines("Our revenue went x6 thanks to Salesflare through better follow up",
                               "Salesflare revenue went x6 thanks to Salesflare through better follow up.", "https://salesflare.com/customers"))
        self.assertFalse(lines("Our revenue went x6 through better follow up", "Salesflare revenue went x6 through better follow up.",
                               "https://salesflare.com/customers/acme"))
        self.assertFalse(lines("20,000+ other sales teams run their entire outbound with lemlist",
                               "lemlist has 20,000+ other sales teams run their entire outbound.", company="lemlist"))
        self.assertTrue(lines("Salesflare customers close 3x more deals", "Salesflare customers close 3x more deals."))
        self.assertTrue(lines("We have implemented an AI trust framework", "Salesflare has implemented an AI trust framework.",
                              "https://salesflare.com/about-us"))
        self.assertTrue(lines("Trusted by 3,000+ businesses", "Salesflare is trusted by 3,000+ businesses."))
        # Seen live on outplay.ai/pricing: a customer's headline became "Outplay's outbound success rate increased by 30%".
        pricing = ("the response rate was within a few minutes no matter what time I reached out.\nAsh D\nBusiness Development Manager\n"
                   "Our outbound success rate increased by 30% in the last quarter alone.\n“Outplay has completely changed the game for me.")
        scan = Scan("outplay.ai", [Page("https://outplay.ai/pricing", "t", pricing)])
        self.assertFalse(validate_lines({"lines": [{"line": "Outplay's outbound success rate increased by 30% in the last quarter alone.",
                                                    "quote": "Our outbound success rate increased by 30% in the last quarter alone.",
                                                    "url": "https://outplay.ai/pricing"}]}, scan, {"company": "Outplay"})[0])
        home = ("Simplify your day-to-day with free tools from Jobber. Learn more\nJobber Grants\nOur $150,000 grant program recognizes "
                "excellence in home service. We want to recognize home service professionals' accomplishments.\nJobber Events")
        scan = Scan("getjobber.com", [Page("https://www.getjobber.com", "t", home)])
        self.assertTrue(validate_lines({"lines": [{"line": "Jobber's $150,000 grant program recognizes excellence in home service.",
                                                   "quote": "Our $150,000 grant program recognizes excellence in home service",
                                                   "url": "https://www.getjobber.com"}]}, scan, {"company": "Jobber"})[0])
        self.assertFalse(lines("Trusted & Loved by 10,000+ teams", "Persana AI has Trusted & Loved by 10,000+ teams.",
                               "https://persana.ai/", "Persana AI"))
        self.assertTrue(lines("Trusted & Loved by 10,000+ teams", "Persana AI is trusted & loved by 10,000+ teams.",
                              "https://persana.ai/", "Persana AI"))
        # Seen live: a heading's capitals went out mid-sentence as "Trumpet is Trusted by 15,000+ revenue teams".
        self.assertEqual(lines("Trusted by 15,000+ revenue teams", "Trumpet is Trusted by 15,000+ revenue teams.",
                               "https://www.sendtrumpet.com/", "Trumpet")[0]["line"], "Trumpet is trusted by 15,000+ revenue teams.")
        self.assertEqual(lines("Born from SpaceX, Relativity and Novelis", "Machina Labs is Born from SpaceX, Relativity and Novelis.",
                               "https://machinalabs.ai/", "Machina Labs")[0]["line"], "Machina Labs is born from SpaceX, Relativity and Novelis.")
        # Seen live: a strip of retailer logos became "Pod Foods has Erewhon, Raley’s, The Fresh Market, and Thrive Market."
        for quote in ("Erewhon, Raley’s, The Fresh Market, and Thrive Market", "Sprouts’ Farmers Market, Whole Foods Market, and Walmart"):
            self.assertFalse(lines(quote, f"Pod Foods has {quote}.", "https://www.podfoods.co/", "Pod Foods"), quote)
        self.assertTrue(lines("Sold at Erewhon, Raley’s and Whole Foods Market", "Pod Foods is sold at Erewhon, Raley’s and Whole Foods Market.",
                              "https://www.podfoods.co/", "Pod Foods"))
        # Seen live on bardeen.ai: their own positioning, said back to them.
        for quote in ("Bardeen is ideal for GTM teams across various roles including Sales (SDRs, AEs)",
                      "Bardeen is the leading AI agent for GTM teams", "Bardeen is built for GTM teams"):
            self.assertFalse(lines(quote, quote + ".", "https://www.bardeen.ai/", "Bardeen"), quote)
        self.assertTrue(lines("Bardeen is used by 200,000 GTM teams", "Bardeen is used by 200,000 GTM teams.",
                              "https://www.bardeen.ai/", "Bardeen"))
        # Seen live: a boast, said back to them.
        for quote, line, site, company in (
                ("Vareto usually pays for itself within the first few months.", "Vareto usually pays for itself within the first few months.",
                 "https://www.vareto.com/", "Vareto"),
                ("15M+ frontline jobs analyzed, an unmatched industrial data set", "Augmentir has 15M+ frontline jobs analyzed, an unmatched industrial data set.",
                 "https://www.augmentir.com/", "Augmentir")):
            self.assertFalse(lines(quote, line, site, company), quote)
        self.assertFalse(lines("15M+ frontline jobs analyzed", "Augmentir has 15M+ frontline jobs analyzed.", "https://www.augmentir.com/", "Augmentir"))
        # Found in review: a customer's result on a story list, or on a customer-stories page, or quoted without "our".
        result = "Response rates doubled within the first month"
        for url in ("https://acme.com/customer-stories/cortex", "https://acme.com/clients/cortex", "https://acme.com/customers?story=cortex"):
            self.assertFalse(lines(result, f"Acme {result.lower()}.", url, "Acme"), url)
        self.assertFalse(lines("Cortex cut onboarding time by 50 percent", "Acme cut onboarding time by 50 percent.",
                               "https://acme.com/customers", "Acme"))
        scan = Scan("acme.com", [Page("https://acme.com/", "t", f"Loved by teams\n“{result}.”\nJane Doe, VP Sales at Cortex")])
        self.assertFalse(validate_lines({"lines": [{"line": f"Acme {result.lower()}.", "quote": result, "url": "https://acme.com/"}]},
                                        scan, {"company": "Acme"})[0])
        # Seen live on mixmax.com/pricing: a carousel where each byline ends the quote's line, and no quote marks.
        carousel = ("I wish we had started using Mixmax a lot sooner. It makes my life much, much easier. Samantha Kennedy Director, "
                    "Enterprise Customer Success, Gong.io\nMixmax saves each user 1 day/month, and that savings translates into an "
                    "extra hire. Francisco Morales Engineering Recruitment Lead, Canva\n")
        scan = Scan("mixmax.com", [Page("https://www.mixmax.com/pricing", "t", carousel)])
        self.assertFalse(validate_lines({"lines": [{"line": "Mixmax saves each user 1 day/month.", "quote": "Mixmax saves each user 1 day/month",
                                                    "url": "https://www.mixmax.com/pricing"}]}, scan, {"company": "Mixmax"})[0])
        self.assertFalse(lines("10,000+ Sales Professionals", "Acme has 10,000+ sales professionals.", "https://acme.com/", "Acme"))
        # Seen live: the tile "5000+ Salespeople" counts Outplay's users, but became "Outplay has 5000+ Salespeople".
        self.assertFalse(lines("5000+ Salespeople", "Outplay has 5000+ Salespeople.", "https://outplay.ai/", "Outplay"))
        # Seen live: a customer result tile became "Spendflo has an 80% reduction in approval times", said to Spendflo's CEO.
        self.assertFalse(lines("80% reduction in approval times", "Spendflo has an 80% reduction in approval times.",
                               "https://www.spendflo.com/", "Spendflo"))
        self.assertFalse(lines("3x faster month-end close", "Rillet has 3x faster month-end close.", "https://www.rillet.com/", "Rillet"))
        self.assertFalse(lines("2,000+ hours reclaimed per year", "Spendflo has 2,000+ hours reclaimed per year.",
                               "https://www.spendflo.com/", "Spendflo"))
        self.assertFalse(lines("$2M in pipeline generated", "Acme has $2M in pipeline generated.", "https://acme.com/", "Acme"))
        self.assertTrue(lines("15 global offices", "Assembled has 15 global offices.", "https://www.assembled.com/", "Assembled"))
        # Seen live: Spendflo's product "Flo" became Spendflo: "Spendflo is live in 14 days."
        self.assertFalse(lines("Flo is live in 14 days.", "Spendflo is live in 14 days.", "https://www.spendflo.com/", "Spendflo"))
        self.assertFalse(lines("Ploger Increased Weekly Revenue Per Mile by 14.6%", "Optimal Dynamics has Ploger Increased Weekly Revenue Per Mile by 14.6%.",
                               "https://www.optimaldynamics.com/", "Optimal Dynamics"))
        self.assertFalse(lines("Jane Doe has joined as Chief Revenue Officer", "Acme has joined as Chief Revenue Officer.",
                               "https://acme.com/", "Acme"))
        for quote, line, company in (("Uptake has joined Bosch to scale fleet maintenance", "Uptake has joined Bosch to scale fleet maintenance.", "Uptake"),
                                     ("Cal.com is open source scheduling", "Cal.com is open source scheduling.", "Cal.com"),
                                     ("Rillet has arrived in the UK", "Rillet has arrived in the UK.", "Rillet"),
                                     ("It is live in 14 days", "Acme is live in 14 days.", "Acme")):
            self.assertTrue(lines(quote, line, f"https://{company.lower()}/", company), quote)
        self.assertTrue(lines("45K+ users creating better buying moments", "Chili Piper has 45K+ users creating better buying moments.",
                              "https://chilipiper.com/", "Chili Piper"))
        self.assertFalse(lines("Default is already powering real-time alerts and data shaping",
                               "Default is already powering real-time alerts and data shaping.",
                               "https://www.default.com/case-study/how-cortex-used-default", "Default"))
        # Seen live: two lines to Default's VP came from case studies about People Data Labs and Cortex.
        story = "https://www.default.com/case-study/how-people-data-labs-used-default"
        self.assertFalse(lines("Default replaced a clunky LeanData + Marketo + Salesforce setup",
                               "Default replaced a clunky LeanData + Marketo + Salesforce setup.", story, "Default"))
        self.assertFalse(lines("Default now sits as the orchestration layer between Cortex’s existing systems",
                               "Default now sits as the orchestration layer between Cortex’s existing systems.", story, "Default"))
        self.assertTrue(lines("Revenue Grid is trusted by 6,500+ sales teams", "Revenue Grid is trusted by 6,500+ sales teams.",
                              "https://revenuegrid.com/customers", "Revenue Grid"))
        self.assertTrue(lines("Default replaced a clunky LeanData + Marketo + Salesforce setup",
                              "Default replaced a clunky LeanData + Marketo + Salesforce setup.", "https://www.default.com/", "Default"))

    def test_a_negative_elsewhere_in_the_proof_does_not_block_a_subject(self):
        """One "no" on any page used to refuse every subject, and with it the whole sequence on 6 of 19 real sites."""
        brief = {"product_name": "Stripe", "facts": [{"text": "t", "said_by": "seller", "quote": "Accept payments online in minutes"},
                                                     {"text": "t", "said_by": "seller", "quote": "Stripe has no setup fees"}],
                 "problem": "Setup fees eat into margins."}
        mail = self.emails("Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1].\nWorth a call?")
        self.assertTrue(validate_sequence(mail, brief))
        mail["emails"][0]["subject"] = "No setup fees"
        self.assertTrue(validate_sequence(mail, brief))
        mail["emails"][0]["subject"] = "No payments"  # Each word is on a page, but no page says this.
        self.assertIsNone(validate_sequence(mail, brief))

    def test_a_word_in_the_sellers_name_is_not_the_seller(self):
        brief = {"product_name": "Blue Bottle Coffee", "facts": [{"text": "t", "said_by": "seller", "quote": "Free shipping on subscription orders"}]}
        first = "Hi,\n[[PERSONAL]]\nIf coffee runs low, that gets old.\nBlue Bottle has free shipping on subscription orders [1].\nWorth a call?"
        mail = {"emails": [{"subject": "Free shipping", "body": first}] + [{"subject": "Free shipping", "body": "Blue Bottle has free shipping [1]. Worth a call?"}] * 2}
        self.assertTrue(validate_sequence(mail, brief))

    def test_a_refused_draft_tells_the_model_what_to_fix(self):
        calls = []
        good = json.dumps(self.emails("Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1].\nWorth a call?"))
        bad = json.dumps(self.emails("Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1].\nAct fast.\nWorth a call?"))

        class Llm:
            def complete(self, messages, **_):
                calls.append(messages[1]["content"])
                return SimpleNamespace(text=bad if len(calls) == 1 else good)

        self.assertTrue(write_sequence(Llm(), {**self.BRIEF, "problem": "p", "voice": "v"}))
        self.assertIn("Act fast", calls[1])

    def test_a_seller_named_like_its_address_still_gets_its_emails(self):
        """Trigger.dev and Cal.com read as links, so every draft was refused and /customers write gave up."""
        brief = {"product_name": "Trigger.dev", "problem": "Long jobs hit serverless timeouts and lose progress on redeploys.", "facts": [
            {"text": "t", "said_by": "seller", "quote": "Build durable AI agents that survive refreshes, redeploys, and crashes."}]}
        body = ("Hi {first_name},\n[[PERSONAL]]\nIf long-running jobs lose progress on redeploy, Trigger.dev lets you build durable AI agents "
                "that survive refreshes, redeploys, and crashes [1].\nIs this worth a short call?\nBest,\n{sender}")
        later = "Hi {first_name},\nTrigger.dev lets you build durable AI agents [1].\nWho handles this at {company}?\nBest,\n{sender}"
        mail = {"emails": [{"subject": "Redeploys lose progress", "body": body}, {"subject": "Serverless timeouts", "body": later},
                           {"subject": "Serverless timeouts", "body": later.replace("Who handles this at {company}?", "Is this a priority at {company}?")}]}
        self.assertTrue(validate_sequence(mail, brief))
        from leadpoet_plugin.writer import check_sequence
        for address in ("Trigger.dev/docs", "app.trigger.dev", "hello@trigger.dev", "evil.example"):
            mail["emails"][1]["body"] = later.replace("Who handles this", f"Is {address} useful, and who handles this")
            self.assertIn("link", check_sequence(mail, brief)[1], address)

    def test_a_subject_may_end_on_a_plain_word_like_work(self):
        brief = {"product_name": "Stripe", "problem": "Billing by hand creates manual work and errors.",
                 "facts": [{"text": "t", "said_by": "seller", "quote": "Accept payments online in minutes"}]}
        mail = self.emails("Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1].\nWorth a call?")
        mail["emails"][0]["subject"] = "Manual work"
        self.assertTrue(validate_sequence(mail, brief))

    def test_asking_what_is_on_their_list_offers_nothing(self):
        ok = self.emails("Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1].\nIs this still on your list for this year?")
        self.assertTrue(validate_sequence(ok, self.BRIEF))
        self.assert_refused("Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1].\nWant the list?")

    def test_good_emails_with_a_refused_subject_are_kept_when_later_drafts_fail(self):
        """Linear's first draft only had a subject wrong. Three worse drafts later, the user had nothing."""
        first = self.emails("Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1].\nWorth a call?")
        first["emails"][0]["subject"] = "Stale rosters"
        worse = json.dumps(self.emails("Hi,\n[[PERSONAL]]\nStripe lets you accept payments online [1].\nAct fast.\nWorth a call?"))
        replies = [json.dumps(first), worse, worse, worse]
        llm = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text=replies.pop(0)))
        emails = write_sequence(llm, {**self.BRIEF, "problem": "p", "voice": "v"})
        self.assertEqual((emails[0]["subject"], replies), ("Question for {company}", []))

    def test_a_number_word_in_the_sellers_name_needs_no_source(self):
        """Robert Half lost every draft to "a number that no source has: half"."""
        brief = {"product_name": "Robert Half", "problem": "Hiring skilled staff is slow.",
                 "facts": [{"text": "t", "said_by": "seller", "quote": "Our specialized recruiters deliver top available talent."}]}
        body = "Hi {first_name},\n[[PERSONAL]]\nRobert Half recruiters deliver top available talent [1].\nWorth a short call?\n{sender}"
        mail = {"emails": [{"subject": "Top available talent", "body": body}] + [{"subject": "Top available talent", "body": body.replace("[[PERSONAL]]\n", "")}] * 2}
        self.assertTrue(validate_sequence(mail, brief))
        mail["emails"][1]["body"] = body.replace("[[PERSONAL]]\n", "If hiring takes half your week, that is slow.\n")
        self.assertIsNone(validate_sequence(mail, brief))

    def test_a_digit_in_the_sellers_name_needs_no_source(self):
        """E2B's claims were refused for "a number" whenever the quote did not happen to hold a 2."""
        brief = {"product_name": "E2B", "problem": "Agents need a place to run code.",
                 "facts": [{"text": "t", "said_by": "seller", "quote": "Billed by the second, with a free tier to start."}]}
        body = "Hi {first_name},\n[[PERSONAL]]\nE2B is billed by the second, with a free tier to start [1].\nWorth a short call?\n{sender}"
        mail = {"emails": [{"subject": "Billed by the second", "body": body}] + [{"subject": "Free tier", "body": body.replace("[[PERSONAL]]\n", "")}] * 2}
        self.assertTrue(validate_sequence(mail, brief))
        mail["emails"][2]["body"] = body.replace("[[PERSONAL]]\n", "").replace("is billed", "has 2 plans billed")
        self.assertIsNone(validate_sequence(mail, brief))

    def test_a_prospect_named_like_its_address_keeps_its_line(self):
        """Trigger.dev's funding and monday.com's new office were dropped as links, so the buying signal never opened the email."""
        from leadpoet_plugin.writer import signal_opener
        def opener(company, line, why):
            llm = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text=json.dumps({"line": line})))
            return [row["line"] for row in signal_opener(llm, {"company": company, "why": why})[0]]
        self.assertEqual(opener("Trigger.dev", "Trigger.dev raised a $16M Series A in October.", "Raised a $16M Series A in October."),
                         ["Trigger.dev raised a $16M Series A in October."])
        self.assertTrue(opener("monday.com", "monday.com opened a new office in Denver in September.", "Opened a new office in Denver in September."))
        self.assertFalse(opener("monday.com", "monday.com opened a new office, see evil.example.", "Opened a new office, see evil.example."))
        scan = Scan("cal.com", [Page("https://cal.com/", "t", "Connects to Slack, Salesforce, and 100+ more. We run millions of workflows a month on Trigger.dev")])
        def lines(company, line, quote):
            return validate_lines({"lines": [{"line": line, "quote": quote, "url": "https://cal.com/"}]}, scan, {"company": company})[0]
        self.assertTrue(lines("Cal.com", "Cal.com connects to Slack, Salesforce, and 100+ more.", "Connects to Slack, Salesforce, and 100+ more."))
        self.assertFalse(lines("Trigger.dev", "Cal.com runs millions of workflows a month on Trigger.dev.", "We run millions of workflows a month on Trigger.dev"))

    def test_a_link_cannot_become_the_sign_off(self):
        scan = Scan("acme.com", [Page("https://a/", "t", "Accept payments online in minutes.")])
        raw = {"summary": "s", "roles": ["b"], "buyer_description": "c", "product_name": "see https://evil.example",
               "facts": [{"text": "Accept payments online in minutes.", "said_by": "seller", "quote": "Accept payments online in minutes.", "url": "https://a/"}]}
        self.assertEqual(validate_brief(raw, scan)["product_name"], "Acme")

    def test_a_bare_domain_still_gets_researched(self):
        seen = []

        def fetcher(url):
            seen.append(url)
            raise SiteError("no")

        research_person(None, {"website": "acme.com", "company": "Acme"}, fetcher)
        self.assertEqual(seen, ["https://acme.com/"])

    def test_a_hostile_style_block_cannot_stall_the_scan(self):
        started = time.monotonic()
        _hidden_classes("<style>" + "a" * 500_000 + "<style>" * 50_000)
        self.assertLess(time.monotonic() - started, 1)

    def test_a_blocked_site_offers_the_paste_path(self):
        store = Store(Path(tempfile.mkdtemp()) / "a.db")

        def fetcher(_url):
            raise SiteError("That site doesn't let automated readers in, so I can't read it.")

        self.assertIn("/customers about", Customers(None, store, object(), fetcher).start("https://acme.com"))


class RealApiShapeTests(Case):
    """Mismatches found by reading the real Leadpoet server code."""

    def test_a_workspace_with_no_profile_gets_one_before_launch(self):
        created = []
        self.api.profile = lambda: {"profile": None}
        self.api.create_empty_profile = lambda: created.append(1) or {}
        self.to_review()
        self.customers.handle("all", self.all_code())
        self.assertEqual(created, [1])
        self.assertTrue(self.api.campaigns)

    def test_a_key_missing_permissions_spends_nothing(self):
        self.api.identity = lambda: {"scopes": ["credits:read"]}
        text = self.customers.handle("go", self.code("go"))
        self.assertIn("Manage Lead engine", text)
        self.assertNotIn("Read credits", text)  # Only what's missing.
        self.assertIn("hermes config set LEADPOET_API_KEY <your key>", text)
        self.assertEqual(self.api.requests, [])

    def test_a_mailbox_signature_replaces_only_the_sign_off(self):
        touch = {"subject": "s", "body": "Hi Ana,\n\nPlans start small.\n\nSam"}
        compliance = {"known": {"legal_entity": "Sunnyday Inc", "postal_address": "1 Main St"}}
        footer = '-- Sunnyday Inc · 1 Main St Don\'t want these? Reply "unsubscribe".'
        draft = {"editable_body": "Hi Ana,\n\nPlans start small."}
        match = self.customers._matches_with_signature
        self.assertTrue(match(touch, draft, f"Hi Ana, Plans start small. Sam Lee, CEO {footer}", compliance))
        self.assertFalse(match(touch, draft, "Hi Ana, Plans start small. Sam Lee, CEO", compliance))
        self.assertFalse(match(touch, {"editable_body": "Hi Ana, Buy now."}, f"Hi Ana, Buy now. {footer}", compliance))
        link = "https://cal.com/acme/intro"
        bound = {"editable_body": f"Hi Ana,\n\nPlans start small.\n\n{link}", "message_plan": {"campaign_booking_link": link}}
        self.assertTrue(match(touch, bound, f"Hi Ana, Plans start small. {link} Sam Lee, CEO {footer}", compliance))


class RecipientClaimTests(unittest.TestCase):
    brief = {"product_name": "P", "facts": [{"text": "Plans start small", "said_by": "seller", "quote": "Plans start small for every team"}]}

    def sequence(self, first_body):
        return {"emails": [{"subject": "note", "body": first_body},
                           {"subject": "note", "body": "Hi {first_name},\n\nPlans start small [1].\n\nWorth a call?\n\n{sender}"},
                           {"subject": "last note", "body": "Hi {first_name},\n\nPlans start small [1].\n\nWorth a call?\n\n{sender}"}]}

    def test_an_unsourced_statement_about_their_company_is_refused(self):
        """A real model wrote this about a company it knew nothing about."""
        body = "Hi {first_name},\n\n[[PERSONAL]]\n\nProduct teams at {company} face the same drag.\n\n{sender}"
        self.assertIsNone(validate_sequence(self.sequence(body), self.brief))

    def test_asking_about_their_company_is_allowed(self):
        body = ("Hi {first_name},\n\n[[PERSONAL]]\n\nIf handovers slow {company} down, that is lost time. Plans start small [1]. "
                "Would it help {company} to see an example?\n\n{sender}")
        self.assertIsNotNone(validate_sequence(self.sequence(body), self.brief))


class SignalTests(unittest.TestCase):
    def test_only_findable_events_survive(self):
        from leadpoet_plugin.brief import _signals
        kept = _signals([
            {"source": "community_social_and_review_sites", "description": "A named ops lead posts about lost handovers. Source: LinkedIn post, past 60 days"},
            {"source": "job_postings", "description": "Posted a Dispatcher role naming handovers. Source: Indeed posting, past 30 days"},
            {"source": "job_postings", "description": "A second job signal. Source: Indeed posting, past 30 days"},  # One per source.
            {"source": "funding_and_company_news", "description": "Is likely evaluating new tools. Source: news, past 45 days"},  # A guess.
            {"source": "technology_footprint", "description": "Adopted a new routing tool, with no place to look it up"},
            {"source": "intent", "description": "Researching handover software. Source: Bombora, past 7 days"},  # Not a source.
            {"source": "company_website_and_press_releases", "description": "See https://evil.example now. Source: site, past 30 days"},
        ])
        self.assertEqual([signal["source"] for signal in kept], ["job_postings", "community_social_and_review_sites"])

    def test_the_request_reads_like_the_apps_form(self):
        from leadpoet_plugin.brief import signal_line
        self.assertEqual(signal_line({"source": "job_postings", "description": "Posted a Dispatcher role. Source: Indeed posting, past 30 days"}),
                         "Posted a Dispatcher role (within the past 30 days).")


class SubjectFallbackTests(unittest.TestCase):
    def test_a_bad_subject_alone_never_costs_the_emails(self):
        from stock import _SEQUENCE
        from leadpoet_plugin.website import scan_site
        from leadpoet_plugin.writer import check_sequence
        brief = write_brief(StockLlm(), scan_site(SELLER, fetcher=stock_fetch))
        draft = json.loads(json.dumps(_SEQUENCE))
        draft["emails"][0]["subject"] = "Stale rosters"
        self.assertIn("Stale", check_sequence(draft, brief)[1].title())
        emails, problem = check_sequence(draft, brief, safe_subjects=True)
        self.assertEqual((problem, emails[0]["subject"]), ("", "Question for {company}"))

    def test_each_subject_is_about_its_own_email(self):
        """Seen live in run 17: "Buying signals" over an email about personalized messages."""
        from leadpoet_plugin.writer import _subject_off_body
        brief = {"product_name": "Leadpoet", "problem": "The sales team spends hours sourcing and cleaning long lead lists "
                                                        "instead of reaching buyers who show a reason to talk now."}
        records = "Leadpoet creates new records and never changes the ones you already have.\n\nWho handles outbound at {company}?"
        messages = "Leadpoet lets you send personalized emails and LinkedIn messages.\n\nWould a short call this week make sense?"
        first = "[[PERSONAL]]\n\nIf long lead lists eat your team's hours, Leadpoet lets you get contacts at companies showing buying signals."
        self.assertTrue(_subject_off_body("Buying signals", messages, brief, "Email 3"))
        self.assertTrue(_subject_off_body("Buying signals", records, brief, "Email 2"))
        for subject, body in (("Lead list hours", first), ("Buying signals", first), ("Personalized emails and messages", messages),
                              ("Cleaning lists", records), ("Reaching buyers now", messages), ("Question for {company}", records),
                              ("Leadpoet for {company}", messages), ("Quick question", messages)):
            self.assertEqual(_subject_off_body(subject, body, brief, "Email 1"), "", subject)


class SignalOpenerTests(unittest.TestCase):
    def line(self, text, why="Posted three SDR roles in the past month.", company="Ramp"):
        from leadpoet_plugin.writer import signal_opener
        llm = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text=json.dumps({"line": text})))
        return signal_opener(llm, {"company": company, "why": why})

    def test_the_opener_restates_the_note_and_nothing_more(self):
        self.assertEqual(self.line("Ramp posted three SDR roles in the past month")[0][0]["line"],
                         "Ramp posted three SDR roles in the past month.")
        self.assertEqual(self.line("Ramp posted three SDR roles in the past month to fix its pipeline."), ([], 1))
        self.assertEqual(self.line("Ramp posted five SDR roles in the past month."), ([], 1))
        self.assertEqual(self.line("Ramp posted three SDR roles.", why=""), ([], 0))

    def test_the_opener_never_drops_words_or_keeps_a_guess(self):
        self.assertEqual(self.line("Ramp raised $20M in March.", why="Ramp's competitor Beta raised $20M in March."), ([], 1))
        self.assertEqual(self.line("Ramp raised funding.", why="Ramp's former CEO left for Beta, which raised funding."), ([], 1))
        self.assertEqual(self.line("Ramp opens an office in Austin.", why="May open an office in Austin."), ([], 0))
        self.assertEqual(self.line("Ramp is likely hiring SDRs.", why="Is likely hiring SDRs."), ([], 0))
        self.assertEqual(self.line("Ramp is hiring a Head of Outbound.", why="Hiring a Head of Outbound.")[0][0]["line"],
                         "Ramp is hiring a Head of Outbound.")

    def test_a_full_length_note_opens_with_its_first_sentence(self):
        """Real notes run several sentences and end by saying the evidence doesn't show they're evaluating us."""
        note = ("Ramp is hiring two SDRs for outbound into mid-market finance teams. The postings ask reps to build lists by hand. "
                "The evidence confirms an outbound team; it does not show that Ramp is evaluating Leadpoet.")
        lines, refused = self.line("Ramp is hiring two SDRs for outbound into mid-market finance teams.", why=note)
        self.assertEqual((lines[0]["line"], lines[0]["quote"], refused),
                         ("Ramp is hiring two SDRs for outbound into mid-market finance teams.",
                          "Ramp is hiring two SDRs for outbound into mid-market finance teams.", 0))
        # Still word for word: a line that drops words is refused, and the note opens as written.
        self.assertEqual(self.line("Ramp is hiring two SDRs.", why=note)[0][0]["line"], "Ramp is hiring two SDRs for outbound into mid-market finance teams.")
        self.assertEqual(self.line("Ramp may be starting outbound.", why="Ramp may be starting outbound. It posted one role."), ([], 0))
        self.assertEqual(self.line("Ramp posted one AE role.", why="Ramp posted one AE role. The evidence is thin."), ([], 0))
        self.assertEqual(self.line("Ramp Inc. hired a CRO.", why="Ramp Inc. hired a CRO in May. It is building a team.")[0][0]["line"],
                         "Ramp Inc. hired a CRO in May.")

    def test_run_fifteen_notes_open(self):
        """Seen live in one run: four of ten notes lost their opener to how they were written, not what they said."""
        from leadpoet_plugin.customers import account_note
        from leadpoet_plugin.writer import signal_opener
        def opener(company, site, note, line):
            llm = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text=json.dumps({"line": line})))
            why = account_note(note, company=company)
            return why, [row["line"] for row in signal_opener(llm, {"company": company, "website": site, "why": why})[0]]
        tail = " The evidence confirms hiring; it does not show that they are evaluating Leadpoet."
        for company, site, note, line in (
                ("Vareto", "https://www.vareto.com", "vareto is hiring two SDRs in Seattle to sell planning software to finance leaders.",
                 "Vareto is hiring two SDRs in Seattle to sell planning software to finance leaders."),
                ("Arch Systems", "https://archsys.io", "In October 2026, Arch hired a VP of Sales — its first — and posted four SDR roles.",
                 "Arch hired a VP of Sales, its first, and posted four SDR roles."),
                ("Augmentir", "https://www.augmentir.com", "Augmentir is hiring SDRs &amp; AEs in Raleigh to sell software to plant managers.",
                 "Augmentir is hiring SDRs & AEs in Raleigh to sell software to plant managers."),
                ("Cube Software", "https://www.cubesoftware.com", "Cube posted three SDR roles on 30 October 2026.",
                 "Cube posted three SDR roles on 30 October 2026."),
                ("Finally", "https://www.finally.com", "Finally is hiring SDRs; its job post asks for cold calling experience.",  # Run 18.
                 "Finally is hiring SDRs, and its job post asks for cold calling experience."),
                ("Mercoa", "https://mercoa.com", "Mercoa (YC W23) raised a $12M seed and is hiring its first two SDRs.",  # Run 23.
                 "Mercoa raised a $12M seed and is hiring its first two SDRs."),
                ("Truewind", "https://www.truewind.ai", "Truewind posted three SDR roles at truewind.ai/careers in November 2026.",  # Run 23.
                 "Truewind posted three SDR roles in November 2026.")):
            why, lines = opener(company, site, note + tail, line)
            self.assertEqual(lines, [line], company)
            self.assertNotIn("&amp;", why)
            self.assertTrue(why.startswith(("Vareto", "In", "Augmentir", "Cube", "Finally", "Mercoa", "Truewind")), why)
        # A note saved before it was tidied still opens as it should read.
        llm = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text=json.dumps({"line": "Vareto is hiring two SDRs."})))
        self.assertEqual(signal_opener(llm, {"company": "Vareto", "why": "vareto is hiring two SDRs."})[0][0]["line"], "Vareto is hiring two SDRs.")
        llm = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text=json.dumps({"line": "vareto is hiring two SDRs."})))
        self.assertEqual(signal_opener(llm, {"company": "Vareto", "why": "vareto is hiring two SDRs."})[0][0]["line"], "Vareto is hiring two SDRs.")
        # Run 19: the model changed the tense one time in eight. The note itself still opens; an empty reply still means no event.
        why = "Dover grew its sales team by 40% this quarter and is hiring two more SDRs." + tail
        self.assertEqual(self.line("Dover has grown its sales team by 40% this quarter and is hiring two more SDRs.", why=why, company="Dover"),
                         ([{"line": "Dover grew its sales team by 40% this quarter and is hiring two more SDRs.",
                            "quote": "Dover grew its sales team by 40% this quarter and is hiring two more SDRs.", "url": ""}], 0))
        self.assertEqual(self.line("", why=why, company="Dover"), ([], 0))
        # Run 21: the opener for Tabs (a "Series B." note) and Finch (an en dash range) was refused.
        why = "Tabs is hiring SDRs after a $55M Series B." + tail
        self.assertEqual(self.line("Tabs is hiring SDRs after a $55M Series B.", why=why, company="Tabs")[0][0]["line"],
                         "Tabs is hiring SDRs after a $55M Series B.")
        why = "Finch is hiring SDRs who have sold to controllers at companies with $50M–$500M in revenue." + tail
        self.assertEqual(self.line("Finch is hiring SDRs who have sold to controllers at companies with $50M–$500M in revenue.",
                                   why=why, company="Finch")[0][0]["line"],
                         "Finch is hiring SDRs who have sold to controllers at companies with $50M to $500M in revenue.")
        # Planning to do something is still a guess, and a different word after their name is someone else.
        self.assertEqual(opener("Acme", "https://acme.com", "Acme is planning to open a Denver office.", "Acme is planning to open a Denver office.")[1], [])
        self.assertEqual(opener("Arch Systems", "https://archsys.io", "Arch Capital hired a VP of Sales.", "Arch Capital hired a VP of Sales.")[1], [])

    def test_a_note_that_writes_their_name_its_own_way_still_opens(self):
        """About 1,300 of 5,715 real notes shortened or punctuated the name ("Spark Health" for Spark Health Inc), and lost their opener."""
        from leadpoet_plugin.writer import signal_opener
        def opener(company, why, line):
            llm = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text=json.dumps({"line": line})))
            return [row["line"] for row in signal_opener(llm, {"company": company, "why": why})[0]]
        for company, sentence in (("Spark Health Inc", "Spark Health is hiring four SDRs in Denver."),
                                  ("Acme, Inc.", "Acme, Inc. opened a Denver office in May."),
                                  ("Acme", "Acme, a freight broker, posted two SDR roles in May."),
                                  ("Acme Group", "The Acme Group hired a CRO in May.")):
            self.assertEqual(opener(company, sentence + " It sells to banks.", sentence), [sentence])
        self.assertEqual(opener("Spark Health Inc", "Spark Health is hiring four SDRs in Denver.", "Spark Health is hiring SDRs."),
                         ["Spark Health is hiring four SDRs in Denver."])
        self.assertEqual(opener("Ramp", "Ramp's competitor Beta raised $20M.", "Ramp's competitor Beta raised $20M."), [])
        self.assertEqual(opener("Ramp", "Ramp may hire SDRs in May.", "Ramp may hire SDRs in May."), [])  # The month isn't a guess; may is.
        self.assertEqual(opener("Acme", "Acme Says It May Hire 50 Reps.", "Acme Says It May Hire 50 Reps."), [])
        self.assertEqual(opener("Hunter", "Hunter.io posted its first SDR role in May 2026.", "Hunter.io posted its first SDR role in May 2026."),
                         ["Hunter.io posted its first SDR role in May 2026."])
        self.assertEqual(opener("Acme", "Acme, Inc. opened a Denver office.", "Acme, Inc. opened a Denver office."), ["Acme, Inc. opened a Denver office."])
        # Records carry taglines and asides; notes write the name with or without them.
        for company, sentence in (("AG Adjustments, a Cadex Company", "AG Adjustments is hiring inside sales reps."),
                                  ("Alco Building Solutions (ABS)", "Alco Building Solutions (ABS) is hiring a sales lead."),
                                  ("Alco Building Solutions (ABS)", "Alco Building Solutions is hiring a sales lead.")):
            self.assertEqual(opener(company, sentence, sentence), [sentence])
        # A one-word bare name may be an ordinary word, and a short or joined record is not cut at all.
        for company, sentence in (("Alpine, an ITW Company", "Alpine Electronics hired a CRO."),
                                  ("Home | Acme Roofing", "Home sales in Denver rose 8% in May."),
                                  ("Kahuna | Skills & Competency Management SaaS Platform", "Kahuna is hiring SDR managers."),
                                  ("A / B Testing Partners", "A new VP of Sales joined Acme in May."),
                                  ("Clear / Point Health", "Clear demand for SDRs rose in May."),
                                  ("AC / DC Electric", "AC units sold out in Texas in May."),
                                  ("Re: Build Manufacturing", "Re-shoring grew 20% in May.")):
            self.assertEqual(opener(company, sentence, sentence), [], company)
        # A website named for the one word confirms it is their name: six of twenty records in two full runs lost
        # their signal this way. A site that isn't named for it, or names something longer, still refuses.
        def on_site(company, site, sentence):
            llm = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text=json.dumps({"line": sentence})))
            return [row["line"] for row in signal_opener(llm, {"company": company, "website": site, "why": sentence})[0]]
        for company, site, sentence in (("Rattle (Rattle Software Inc.)", "gorattle.com", "Rattle hired a VP of Sales in May."),
                                        ("Default - Inbound Automation", "https://www.default.com", "Default posted two SDR roles."),
                                        ("Outplay | Sales Engagement Platform", "outplayhq.com", "Outplay is hiring four SDRs."),
                                        ("Juro Online Ltd", "juro.com", "Juro hired a CRO on 3 October 2026."),
                                        ("Dealfront Systems, Inc.", "https://www.dealfront.com/", "Dealfront is hiring six SDRs in Boston."),
                                        ("Trumpet | Digital Sales Rooms", "https://www.sendtrumpet.com", "Trumpet posted two SDR roles."),
                                        ("Remote.com", "https://remote.com", "Remote acquired a payroll startup in October 2026.")):
            self.assertEqual(on_site(company, site, sentence), [sentence], company)
        # Seen live: "In October 2026, Kixie hired four SDRs" put the date first and lost its line. A place first still refuses.
        llm = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text=json.dumps({"line": "Kixie hired four SDRs in Denver."})))
        self.assertEqual([row["line"] for row in signal_opener(llm, {"company": "Kixie", "why": "In October 2026, Kixie hired four SDRs in Denver. More."})[0]],
                         ["Kixie hired four SDRs in Denver."])
        self.assertEqual(opener("Acme", "In Denver, Acme opened an office.", "Acme opened an office."), [])
        for company, site, sentence in (("Home | Acme Roofing", "acmeroofing.com", "Home sales in Denver rose 8% in May."),
                                        ("Home | Acme Roofing", "myhomeroofing.com", "Home sales in Denver rose 8% in May."),
                                        ("Alpine, an ITW Company", "alpine.com", "Alpine Electronics hired a CRO."),
                                        ("Bank of the West", "bankofthewest.com", "Bank of America acquired a fintech."),
                                        ("Acme Group", "acme.com", "Acme Health raised $20M."),
                                        ("Juro Online Ltd", "", "Juro hired a CRO on 3 October 2026."),
                                        ("Remote.com", "https://remotework.io", "Remote acquired a payroll startup.")):
            self.assertEqual(on_site(company, site, sentence), [], company)
        self.assertEqual(opener("Kahuna | Skills Platform", "Hiring SDR managers.", "Kahuna | Skills Platform is hiring SDR managers."), [])
        self.assertEqual(opener("Kahuna | Skills Platform", "Hiring SDR managers.", "Kahuna is hiring SDR managers."),
                         ["Kahuna is hiring SDR managers."])

    def test_another_companys_name_never_opens_as_theirs(self):
        """Matching part of the name must not hand a sister company's or a namesake's news to them."""
        from leadpoet_plugin.writer import signal_opener
        def opener(company, sentence):
            llm = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text=json.dumps({"line": sentence})))
            return signal_opener(llm, {"company": company, "why": sentence})[0]
        self.assertEqual(opener("Acme Group", "Acme Health raised $20M."), [])
        self.assertEqual(opener("Acme Group", "Acme Health, a sister company, raised $20M."), [])
        self.assertEqual(opener("Bank of the West", "Bank of America acquired a fintech."), [])
        self.assertEqual(opener("Acme", "Acme Communications hired a CRO."), [])
        self.assertEqual(opener("Linear", "Linear, Remote and Notion switched to Acme."), [])

    def test_a_signal_about_the_recipient_is_not_read_back_to_them(self):
        """Seen live: Lee Ann Ford-Harris's own email opened "Pod Foods hired Lee Ann Ford-Harris as Chief Revenue Officer"."""
        from leadpoet_plugin.writer import signal_opener
        def opener(name, sentence):
            llm = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text=json.dumps({"line": sentence})))
            return [row["line"] for row in signal_opener(llm, {"company": "Pod Foods", "name": name, "why": sentence})[0]]
        hired = "Pod Foods hired Lee Ann Ford-Harris as Chief Revenue Officer in October 2026."
        self.assertEqual(opener("Lee Ann Ford-Harris", hired), [])
        self.assertEqual(opener("Lee Ford-Harris", hired), [])
        # Someone else's news, or a namesake word, still opens.
        self.assertEqual(opener("Xena Holt", hired), [hired])
        self.assertEqual(opener("Ann Ford", hired), [hired])
        self.assertEqual(opener("Cher", "Pod Foods hired Cher as CRO in May."), ["Pod Foods hired Cher as CRO in May."])
        # Seen live: a title and surname, or their own role, is them too.
        def to(name, role, company, sentence):
            llm = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text=json.dumps({"line": sentence})))
            return [row["line"] for row in signal_opener(llm, {"company": company, "name": name, "role": role, "why": sentence})[0]]
        named = "Velo3D named Ms. Rossi Head of Sales in October 2026, and she is hiring four SDRs."
        self.assertEqual(to("Gia Rossi", "Head of Sales", "Velo3D", named), [])
        self.assertEqual(to("Eli Shaw", "VP of Sales", "Carbon", "Carbon hired a VP of Sales in October 2026."), [])
        self.assertEqual(to("Eli Shaw", "VP of Sales", "Carbon", "Carbon hired a Vice President of Sales in October 2026."), [])
        # Seen live: a longer title is someone else's.
        hired = "Optimal Dynamics hired a Head of Sales Development in October 2026."
        self.assertEqual(to("Quin Hays", "Head of Sales", "Optimal Dynamics", hired), [hired])
        self.assertEqual(to("Kim Tran", "Head of Sales", "Augury", "Augury hired a new Head of Growth in October 2026."),
                         ["Augury hired a new Head of Growth in October 2026."])
        self.assertEqual(to("Ana Rossi", "VP of Sales", "Velo3D", named.replace("Ms. Rossi", "Mr. Rossini")), [named.replace("Ms. Rossi", "Mr. Rossini")])

    def test_old_news_and_citations(self):
        """Seen live: "hired a VP of Sales in March 2025" opened an email 20 months later, and a "(site.com/careers)"
        citation kept a fresh posting from opening at all."""
        import datetime
        from leadpoet_plugin.writer import _is_old_news, signal_opener
        today = datetime.date(2026, 11, 12)
        for text in ("Relativity Space hired a VP of Sales in March 2025.", "Acme raised $70M in 2024.", "Acme hired a CRO on Oct. 6, 2025."):
            self.assertTrue(_is_old_news(text, today), text)
        for text in ("Acme raised $70M in October 2026.", "Acme plans to double outbound in Q4 2026.", "Acme is hiring four SDRs.",
                     "Acme hired a CRO in June 2026, and in March 2025 it opened Denver."):
            self.assertFalse(_is_old_news(text, today), text)
        this_month = datetime.date.today().strftime("%B %Y")
        note = f"Pickle Robot posted an SDR role (picklerobot.com/careers) in {this_month} that asks reps to find buyers by hand. More."
        line = f"Pickle Robot posted an SDR role in {this_month} that asks reps to find buyers by hand."
        llm = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text=json.dumps({"line": line})))
        self.assertEqual([row["line"] for row in signal_opener(llm, {"company": "Pickle Robot", "why": note})[0]], [line])
        # Seen live: "(see https://www.platformscience.com/careers)".
        line = "Platform Science is hiring SDRs to sell to fleet managers."
        llm = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text=json.dumps({"line": line})))
        note = "Platform Science is hiring SDRs (see https://www.platformscience.com/careers) to sell to fleet managers. More."
        self.assertEqual([row["line"] for row in signal_opener(llm, {"company": "Platform Science", "why": note})[0]], [line])

    def test_the_opener_stops_before_leadpoets_reading_of_the_event(self):
        """30% of real openers told the prospect "indicating a need to expand their sales efforts"."""
        lines, _ = self.line("Ramp is hiring two SDRs.", why="Ramp is hiring two SDRs, indicating a potential push into outbound. More.")
        self.assertEqual((lines[0]["line"], lines[0]["quote"]), ("Ramp is hiring two SDRs.", "Ramp is hiring two SDRs."))
        self.assertEqual(self.line("Ramp is hiring two SDRs, indicating a push into outbound.",
                                   why="Ramp is hiring two SDRs, indicating a push into outbound.")[0][0]["line"], "Ramp is hiring two SDRs.")
        listed = "Ramp sells to Indicative, Showpad and Highspot."  # A name in a list is not Leadpoet's reading.
        self.assertEqual(self.line(listed, why=listed)[0][0]["line"], listed)

    def test_the_greeting_uses_the_name_they_go_by(self):
        from leadpoet_plugin.writer import first_name
        for name, first in (("J. R. Ruiz", "J. R."), ("F. Scott Fitzgerald", "Scott"), ("J.R. Ruiz", "J.R."), ("J. Ruiz", "J."),
                            ("Mary-Kate Doyle", "Mary-Kate"), ("Kai Dunn", "Kai"), ("Cher", "Cher"), ("", "there"),
                            ("Dr. Amy Fox", "Amy"), ("Prof. Ana Lima", "Ana"), ("MR Tom Berg", "Tom"), ("Dr. J. R. Ruiz", "J. R."),
                            ("Drew Lane", "Drew"), ("Sir", "Sir"),
                            # Seen live, run 23: a list typed in one case greeted "Hi RAVI," and "Hi anna,".
                            ("RAVI SHAH", "Ravi"), ("anna kim", "Anna"), ("li wei", "Li"), ("JEAN-LUC MOREAU", "Jean-Luc"),
                            ("AJ SMITH", "AJ"), ("j. r. ruiz", "J. R."), ("DeShawn Ray", "DeShawn"), ("DR. AMY FOX", "Amy")):
            self.assertEqual(first_name({"name": name}), first, name)

    def test_the_first_sentence_survives_abbreviations(self):
        from leadpoet_plugin.writer import lead_sentence, short_note
        self.assertEqual(lead_sentence("Acme Inc. opened a U.S. office in May. It hires now."), "Acme Inc. opened a U.S. office in May.")
        # Seen live, run 24: the closing disclaimer after "U.K." was shown as the reason, and the email lost its line.
        self.assertEqual(lead_sentence("Landis opened an office in the U.K. The evidence confirms it."), "Landis opened an office in the U.K.")
        self.assertEqual(lead_sentence("Acme opened a U.S. office. It hires."), "Acme opened a U.S. office.")
        self.assertEqual(lead_sentence("Acme Inc. The Times said so."), "Acme Inc.")
        self.assertEqual(lead_sentence("Acme Co. Ltd. hired a CRO. More."), "Acme Co. Ltd. hired a CRO.")
        self.assertEqual(lead_sentence("Raised $9.1M led by J. Smith. More here."), "Raised $9.1M led by J. Smith.")
        # Seen live: "Mutiny named Priya Shah Head of Sales on Oct." went out as the whole line.
        self.assertEqual(lead_sentence("Acme named a CRO on Oct. 6, 2026, and is hiring. More."), "Acme named a CRO on Oct. 6, 2026, and is hiring.")
        self.assertEqual(lead_sentence("Acme hired a CRO in Oct. The team grew."), "Acme hired a CRO in Oct.")
        # Run 21: "Series B." ended the event; the disclaimer after it was shown as why the company fits.
        self.assertEqual(lead_sentence("Tabs is hiring SDRs after a $55M Series B. The evidence confirms the round."),
                         "Tabs is hiring SDRs after a $55M Series B.")
        self.assertEqual(lead_sentence("Acme closed its Series A in May. More."), "Acme closed its Series A in May.")
        self.assertTrue(short_note("word " * 80).endswith("…"))
        self.assertLessEqual(len(short_note("word " * 80)), 221)
        # Seen live: a long note was cut to "...write their own cold emails and…".
        long = ("CloudTalk posted nine SDR and account executive roles across Paris, New York, Sydney and Madrid in October "
                "2026, each asking reps to research accounts, build prospect lists from scratch, write their own cold emails "
                "and book meetings for enterprise account executives. More.")
        self.assertTrue(short_note(long).endswith("cold emails…"), short_note(long))

    def test_the_signal_opens_and_a_site_fact_stands_in_without_one(self):
        customers = Customers(StockApi(), Store(Path(tempfile.mkdtemp()) / "a.db"), StockLlm(), stock_fetch)
        lines, _ = research_person(StockLlm(), {**customers.api.pipeline_contacts(None)[0], "why": "Posted a night dispatcher role on Oct 28 that asks for clean shift handovers."}, stock_fetch)
        self.assertEqual([line["url"] for line in lines], [""])
        lines, _ = research_person(StockLlm(), customers.api.pipeline_contacts(None)[0], stock_fetch)
        self.assertEqual([line["url"] for line in lines], ["https://pinecrest-freight.example/"])


class ManySearchesCase(Case):
    """A second site starts while the first is finding. Both keep moving, and nothing acts on the wrong one."""

    OTHER = "https://other.example"

    def setUp(self):
        super().setUp()
        self.customers.fetcher = self.fetch
        self.customers.llm = self.Llm()
        self.customers.handle("go", self.code("go"))  # The first search spends credits and starts finding.
        self.api.credits_left = 100

    class Llm(StockLlm):
        def complete(self, messages, **kwargs):
            result = super().complete(messages, **kwargs)
            page = messages[-1]["content"].split("PAGE ")[1].split()[0].rstrip("/")
            return SimpleNamespace(text=result.text.replace(SELLER, page))

    def fetch(self, url):
        if not url.startswith(self.OTHER):
            return stock_fetch(url)
        return url if url.endswith("/") else url + "/", stock_fetch(SELLER)[1]

    def test_a_new_site_keeps_the_running_search(self):
        text = self.customers.handle(self.OTHER, "")
        self.assertIn("keeps running", text)
        self.assertIn("/customers open ", text)
        self.assertEqual(self.customers.job()["brief"]["domain"], "other.example")
        listing = self.customers.handle("list", "")
        self.assertIn("Other searches:", listing)
        self.assertIn(": finding contacts", listing)
        self.assertTrue(listing.startswith("other.example: ready to confirm. This is the one that's open: /customers"), listing)
        self.assertNotIn("Who buys", listing)  # A list is one line a search, not the whole card again.

    def test_news_from_a_background_search_points_to_it(self):
        first = self.customers.job()
        self.customers.handle(self.OTHER, "")
        self.api.state = "completed"
        ping = self.customers.advance()
        site = first["brief"]["domain"]
        self.assertTrue(ping.startswith(f"{site}: "), ping)
        self.assertIn(f"/customers open {site}", ping)
        self.assertNotIn("/customers write", ping)  # That would write for the open search, not this one.
        self.assertEqual(self.customers.job()["stage"], "scanned")
        self.assertIn("verified contacts are in", self.customers.handle("open", site))
        self.assertEqual(self.customers.job()["id"], first["id"])
        self.assertIn("Other searches:\nother.example: ready to confirm", self.customers.handle("list", ""))

    def test_open_without_a_match_lists_the_others(self):
        self.customers.handle(self.OTHER, "")
        self.assertIn("Your other searches: ", self.customers.handle("open", "nope.example"))

    def test_open_takes_the_short_name_of_a_site(self):
        first = self.customers.job()
        short = first["brief"]["domain"].split(".")[0]
        self.assertIn("You're on it already", self.customers.handle("open", short))
        self.customers.handle(self.OTHER, "")
        self.assertEqual(self.customers.job()["brief"]["domain"], "other.example")
        self.customers.handle("open", short)
        self.assertEqual(self.customers.job()["id"], first["id"])
        self.assertIn("You're on it already", self.customers.handle("open", short.upper()))

    def test_reset_keeps_a_paid_search_unless_confirmed(self):
        self.assertIn("keep it unless you're sure", self.customers.handle("reset", ""))
        self.assertEqual(self.customers.job()["stage"], "finding")
        fresh = self.customers.handle("reset", "confirm")
        # The request keeps running at Leadpoet, so the reply must not suggest the paid contacts are gone.
        self.assertIn("Leadpoet still finishes the search you paid for", fresh)
        self.assertIn("https://leadpoet.com/dashboard/lead-engine", fresh)
        self.assertIsNone(self.customers.job())

    def test_list_shows_people_still_running_from_an_earlier_search(self):
        rows = [{"id": f"p{n}", "name": f"Person {n}", "company": f"Co {n}", "state": "launched",
                 "retired_at": self.customers.now()} for n in range(12)]
        self.store.put("customers_past", json.dumps(rows))
        listing = self.customers.handle("list", "")
        self.assertIn("Still running from earlier searches:\nPerson 2, Co 2: running", listing)
        self.assertIn("Person 11, Co 11: running\nand 2 more", listing)
        self.assertNotIn("Person 1, Co 1:", listing)

    def test_there_is_a_limit(self):
        from leadpoet_plugin.customers import MAX_SEARCHES
        self.store.put("customers_parked", json.dumps([f"customers_job:x{n}" for n in range(MAX_SEARCHES - 1)]))
        for n in range(MAX_SEARCHES - 1):
            self.store.put(f"customers_job:x{n}", json.dumps({**self.customers.job(), "id": f"x{n}"}))
        self.assertIn("the most at once", self.customers.handle(self.OTHER, ""))


class WaitingCase(Case):
    """The days between asking and the contacts arriving, which is where most people first stop."""

    OTHER = ManySearchesCase.OTHER

    def setUp(self):
        super().setUp()
        self.customers.fetcher = lambda url: ManySearchesCase.fetch(self, url)
        self.customers.llm = ManySearchesCase.Llm()
        self.clock = [1_800_000_000.0]
        self.customers.now = lambda: self.clock[0]

    def go(self):
        self.text = self.customers.handle("go", self.customers.handle("", "").split("/customers go ")[1].split()[0])
        return self.customers.job()

    def second(self):
        """Another site, wanting different companies, so its request text is its own."""
        self.customers.handle(self.OTHER, "")
        job = self.customers.job()
        job["brief"]["company_fit"] = "Bakeries with two or more shops"
        self.customers.save(job)
        return self.go()

    def typed(self, job, name, day):
        return {"id": name, "icp_id": f"icp-{name}", "created_at": f"2099-01-0{day}T00:00:00Z",
                "request_description": description(job["brief"])}

    def test_two_unsent_searches_each_take_their_own_request(self):
        self.api.credits_left = 0
        first, second = self.go(), self.second()
        site = first["brief"]["domain"]
        self.api.app_requests = [self.typed(first, "app-first", 1)]
        ping = self.customers.advance()
        self.assertTrue(ping.startswith(f"{site}: I see your search in Leadpoet."), ping)
        self.assertIn("Leadpoet is on it. Each company", ping)  # Leadpoet's clock runs ahead here, and no wait is counted from it.
        look = self.customers.handle("", "")  # The open one is still waiting to be pasted, and says only that.
        self.assertNotIn("/customers use", look)
        self.assertIn("paste:", look)
        self.api.app_requests.append(self.typed(second, "app-second", 2))
        self.assertTrue(self.customers.advance().startswith("other.example: I see your search in Leadpoet."))
        self.assertEqual(self.customers.job()["query_id"], "app-second")
        self.customers.handle("open", site)
        self.assertEqual(self.customers.job()["query_id"], "app-first")

    def test_the_older_request_is_found_behind_a_newer_one(self):
        self.api.credits_left = 0
        first, second = self.go(), self.second()
        self.api.app_requests = [self.typed(first, "app-first", 1), self.typed(second, "app-second", 2)]
        self.customers.advance()
        self.assertEqual(self.customers.job()["query_id"], "app-second")
        self.customers.handle("open", first["brief"]["domain"])
        self.assertEqual((self.customers.job()["stage"], self.customers.job()["query_id"]), ("finding", "app-first"))

    def test_a_request_another_search_follows_is_never_offered(self):
        self.api.credits_left = 0
        first = self.go()
        self.api.app_requests = [{**self.typed(first, "app-odd", 1), "request_description": "Find dentists"}]
        code = self.customers.handle("", "").split("/customers use ")[1].split()[0]
        other = self.second()
        self.customers.save({**other, "stage": "finding", "query_id": "app-odd"})
        self.customers.handle("open", first["brief"]["domain"])
        text = self.customers.handle("use", code)
        self.assertIn("belongs to another of your searches", text)
        self.assertEqual(self.customers.job()["stage"], "needs_credits")

    def test_the_list_says_what_an_unsent_search_waits_on(self):
        self.api.credits_left = 0
        self.go()
        self.second()
        listing = self.customers.handle("list", "")
        self.assertEqual(listing.count("waiting for you to submit it in Leadpoet"), 2)
        self.assertNotIn("waiting for credits", listing)

    def test_the_wait_says_how_long_it_has_been(self):
        self.go()
        self.assertIn("Leadpoet is on it. Each company", self.text)
        self.clock[0] += 86400 + 60
        self.assertIn("Leadpoet is on it, a day in.", self.customers.handle("", ""))
        self.assertEqual(self.customers.advance(), "")
        self.clock[0] += 86400
        self.assertIn("Leadpoet is on it, 2 days in.", self.customers.handle("", ""))

    def test_a_long_wait_is_said_once(self):
        self.go()
        self.clock[0] += 3 * 86400 + 60
        ping = self.customers.advance()
        self.assertIn("still on it, 3 days in. That's longer than usual.", ping)
        self.assertIn("Nothing more is charged", ping)
        self.assertNotIn("While you wait", ping)
        self.assertEqual(self.customers.advance(), "")
        self.clock[0] += 2 * 86400
        self.assertEqual(self.customers.advance(), "")
        look = self.customers.handle("", "")
        self.assertIn("5 days in", look)
        self.assertNotIn("usually takes 1 to 2 days", look)
        self.api.state = "completed"
        self.assertIn("verified contacts are in", self.customers.advance())

    def test_the_wait_points_at_what_to_do_meanwhile(self):
        self.api.senders = []
        self.go()
        self.assertIn("While you wait:", self.text)
        self.assertIn("• Inbox: connect the email account your outreach will go from. https://leadpoet.com/dashboard/outreach", self.text)
        self.assertIn("• Your voice: paste a few lines you wrote. /customers voice <text>", self.text)
        self.api.senders = ["acct-1"]
        self.customers.handle("voice", "I write short and plain. One idea in each email, then one clear ask.")
        look = self.customers.handle("", "")
        self.assertNotIn("Inbox:", look)
        self.assertNotIn("Your voice:", look)
        self.assertNotIn("While you wait:", look)

    def test_a_tip_that_cannot_be_read_never_breaks_the_wait(self):
        self.go()
        def down():
            raise ApiError("Leadpoet returned HTTP 502.", status=502, ambiguous=True)
        self.api.email_accounts = down
        look = self.customers.handle("", "")
        self.assertIn("Leadpoet is on it.", look)
        self.assertNotIn("Inbox:", look)

    def test_credits_bought_in_the_app_are_announced(self):
        self.api.credits_left = 0
        self.go()
        self.assertEqual(self.customers.advance(), "")
        self.api.credits_left = 500
        ping = self.customers.advance()
        self.assertIn("Your credits are in.", ping)
        self.assertIn("/customers go ", ping)
        self.assertEqual(self.customers.advance(), "")  # Said once.
        self.assertEqual(self.api.requests, [])  # Announcing is not spending.

    def test_news_names_its_search_when_several_are_going(self):
        first = self.go()
        self.api.credits_left = 100
        self.second()
        self.api.state = "completed"
        ping = self.customers.advance()
        self.assertIn("other.example: 3 verified contacts are in.", ping)
        self.assertIn(f"{first['brief']['domain']}: 3 verified contacts are in.", ping)


class ReasoningBudgetTests(unittest.TestCase):
    def test_a_reasoning_model_still_finishes_the_brief(self):
        """Haiku 5.5 spent 1,384 of 1,400 tokens thinking, and the JSON stopped at '{"product_name":'."""
        reply = json.dumps({
            "summary": "Coffee subscriptions for offices.",
            "facts": [{"text": "It roasts its organic coffee weekly.", "said_by": "seller", "quote": "We roast organic coffee every week",
                       "url": "https://a.example/"}],
            "roles": ["Office Manager"], "buyer_description": "Offices with staff on site.",
        })

        class ThinkingLlm:
            def complete(self, _messages, *, max_tokens, purpose=""):
                visible = max(0, max_tokens - 1384) * 4  # About four characters to a token.
                return SimpleNamespace(text=reply[:visible])

        scan = Scan("a.example", [Page("https://a.example/", "t", "We roast organic coffee every week for offices.")])
        brief = write_brief(ThinkingLlm(), scan)
        self.assertIsNotNone(brief)
        self.assertEqual(brief["buyers"], ["Office Manager"])


class WalkFindingsCase(Case):
    """Paths a user walked into on a phone."""

    def test_resetting_a_card_that_spent_nothing_says_only_that(self):
        self.assertEqual(self.customers.handle("reset", ""), "Started fresh. Send /customers yoursite.com")

    def test_opening_a_site_whose_free_card_gave_way_offers_to_read_it_again(self):
        # Only one card that spent nothing is kept, so a site read before has no saved search to open.
        self.assertEqual(self.customers.handle("open", "gorgias.com"),
                         "No saved search for gorgias.com. Read it again: /customers gorgias.com")
        self.assertIn("You have no other searches", self.customers.handle("open", "gorgias"))

    def test_a_key_added_after_the_offer_shows_the_cost_before_spending(self):
        store = Store(Path(self.tmp.name) / "k.db")
        keyless = Customers(None, store, StockLlm(), stock_fetch)
        code = keyless.start(SELLER).split("/customers go ")[1].split()[0]
        keyless.handle("go", code)
        customers = Customers(self.api, store, StockLlm(), stock_fetch)
        text = customers.handle("go", code)
        self.assertIn("Cost: 100 credits", text)
        self.assertEqual(self.api.requests, [])
        customers.handle("go", code)
        self.assertEqual(len(self.api.requests), 1)

    def test_a_key_added_while_waiting_moves_on(self):
        store = Store(Path(self.tmp.name) / "w.db")
        keyless = Customers(None, store, StockLlm(), stock_fetch)
        keyless.handle("go", keyless.start(SELLER).split("/customers go ")[1].split()[0])
        self.assertIn("Your key is in", Customers(self.api, store, StockLlm(), stock_fetch).handle("", ""))

    def test_codes_typed_with_capitals_still_work(self):
        self.customers.handle("go", self.code("go").upper())
        self.assertEqual(len(self.api.requests), 1)

    def test_go_with_no_code_asks_for_it_briefly(self):
        text = self.customers.handle("go", "")
        self.assertIn(f"/customers go {self.code('go')}", text)
        self.assertLess(len(text), 120)

    def test_the_same_site_twice_does_not_start_a_second_search(self):
        self.customers.handle("go", self.code("go"))
        self.assertIn("already under way", self.customers.start(SELLER))
        self.assertEqual(len(self.api.requests), 1)

    def test_a_check_that_keeps_failing_is_counted(self):
        from leadpoet_plugin.poll import advance

        self.customers.handle("go", self.code("go"))

        def broken(_query_id):
            raise ApiError("boom", status=500)

        self.api.get_request = broken
        texts = [advance(self.customers, self.store) for _ in range(3)]
        self.assertIn("may be stuck", texts[-1])

    def test_a_search_that_finds_no_one_still_pings(self):
        self.customers.handle("go", self.code("go"))
        self.api.pipeline_contacts = lambda icp: []
        self.api.state = "completed"
        self.assertIn("can't see a contact", self.customers.advance())
        self.assertEqual(self.customers.advance(), "")

    def test_a_stranding_balance_is_explained_before_go(self):
        self.api.credits_left = 150
        text = Customers(self.api, Store(Path(self.tmp.name) / "s.db"), StockLlm(), stock_fetch).start(SELLER)
        self.assertIn("leave 50 credits", text)
        self.assertIn("Add 50 more", text)

    def test_approving_says_so_first(self):
        self.to_review()
        self.assertTrue(self.customers.handle("approve", self.code("approve")).startswith("Approved "))
        self.assertTrue(self.customers.handle("skip", "").startswith("Skipped "))

    def test_open_takes_a_www_address(self):
        self.customers.handle("go", self.code("go"))
        site = self.customers.job()["brief"]["domain"]
        self.assertIn("You're on it already", self.customers.handle("open", f"www.{site}"))

    def test_no_model_says_so_instead_of_blaming_the_site(self):
        text = Customers(self.api, Store(Path(self.tmp.name) / "m.db"), None, stock_fetch).start(SELLER)
        self.assertIn("model", text)


class BreadthFindingsCase(unittest.TestCase):
    """Real misses from reading 33 live sites with the real model."""

    QUOTE = "Accept payments online in minutes."

    def raw(self, **over):
        row = {"summary": "s", "roles": ["b"], "buyer_description": "c", "product_name": "Linear",
               "facts": [{"text": self.QUOTE, "said_by": "seller", "quote": self.QUOTE, "url": "https://a/"}]}
        row.update(over)
        return row

    def scan(self, domain="linear.app", title="Linear - Plan and build products"):
        return Scan(domain, [Page("https://a/", title, self.QUOTE)])

    def test_a_consumer_company_is_told_so(self):
        brief = validate_brief(self.raw(sells_to_businesses=False), self.scan("gentledental.com", "Gentle Dental"))
        self.assertTrue(brief["consumer"])
        store = Store(Path(tempfile.mkdtemp()) / "c.db")

        class Llm:
            def complete(self, *_args, **_kwargs):
                return SimpleNamespace(text=json.dumps(BreadthFindingsCase().raw(sells_to_businesses=False, product_name="Gentle Dental")))

        page = '<title>Gentle Dental</title><p>Accept payments online in minutes. We care for your smile.</p>' * 10
        text = Customers(None, store, Llm(), lambda url: (url, page)).start("gentledental.com")
        self.assertIn("sells to consumers", text)

    def test_a_feature_page_name_is_not_the_company(self):
        self.assertEqual(validate_brief(self.raw(product_name="Customer Requests"), self.scan())["product_name"], "Linear")
        self.assertEqual(validate_brief(self.raw(product_name="Sunnyday"), self.scan("getsunnyday.com", "Sunnyday"))["product_name"], "Sunnyday")

    def test_a_signal_listing_alternatives_is_dropped(self):
        signals = [{"source": "job_postings", "description": "Posted a role naming NICE or Five9 administration. Source: LinkedIn Jobs, past 30 days"},
                   {"source": "community_social_and_review_sites", "description": "A named ops lead posted about lost handovers. Source: LinkedIn post, past 60 days"}]
        kept = validate_brief(self.raw(signals=signals), self.scan())["signals"]
        self.assertEqual([signal["source"] for signal in kept], ["community_social_and_review_sites"])

    def test_a_problem_about_the_scan_is_dropped(self):
        brief = validate_brief(self.raw(problem="The site does not describe the problems buyers have."), self.scan())
        self.assertEqual(brief["problem"], "")

    def test_text_hidden_only_while_loading_is_read(self):
        from leadpoet_plugin.website import _parse
        html = "<style>.boot{visibility:hidden}</style><body class='x'><div class='boot'><p>" + "Real words here. " * 20 + "</p></div></body>"
        self.assertIn("Real words here.", _parse(html).text())

    def test_a_page_whose_body_starts_late_is_read(self):
        import io
        from leadpoet_plugin.website import scan_site
        html = ("<head><script>" + "var a=1;" * 110_000 + "</script></head><body><p>" + "Real words here. " * 20 + "</p></body>").encode()

        class Response(io.BytesIO):
            headers = {"Content-Type": "text/html"}

        class Opener:
            def open(self, request, timeout):
                return Response(html)

        from leadpoet_plugin.website import fetch
        scan = scan_site("https://big.example/", fetcher=lambda url: fetch(url, opener=Opener(), public=lambda _: True))
        self.assertIn("Real words here.", scan.pages[0].text)

    def test_a_card_drops_query_strings_and_long_country_lists(self):
        from leadpoet_plugin.brief import card
        brief = validate_brief(self.raw(countries=["A", "B", "C", "D", "E"]), self.scan())
        brief["facts"][0]["url"] = "https://headspace.com/about-us?origin=footer"
        shown = card(brief)
        self.assertIn("Proof from headspace.com/about-us:", shown)
        self.assertIn("Where: A, B, C and 2 more", shown)

    def test_a_regulatory_order_is_never_a_personal_line(self):
        from leadpoet_plugin.writer import _SENSITIVE
        self.assertTrue(_SENSITIVE.search("Austin ISD was released from the TEA Special Education Agreed Order."))

    def test_your_own_words_keep_your_company_name(self):
        scan = Scan("your-company", [Page("https://a/", "", self.QUOTE + " Pinecrest Freight runs it.")])
        self.assertEqual(validate_brief(self.raw(product_name="Pinecrest Freight"), scan)["product_name"], "Pinecrest Freight")
        self.assertEqual(validate_brief(self.raw(product_name="Basecamp"), self.scan("37signals.com", "Basecamp"))["product_name"], "Basecamp")

    def test_a_customers_words_are_never_the_sellers_proof(self):
        said = "Now we're speaking with buyers who are actively in the market."
        scan = Scan("linear.app", [Page("https://a/", "Linear - Plan", f"{self.QUOTE} {said} I love how my team ships.")])
        facts = [{"text": self.QUOTE, "quote": self.QUOTE, "url": "https://a/", "said_by": "seller"},
                 {"text": "Linear is speaking with buyers who are actively in the market", "quote": said, "url": "https://a/", "said_by": "customer"},
                 {"text": "Linear is speaking with buyers who are actively in the market", "quote": said, "url": "https://a/"},
                 {"text": "Linear ships", "quote": "I love how my team ships.", "url": "https://a/", "said_by": "seller"}]
        self.assertEqual([fact["quote"] for fact in validate_brief(self.raw(facts=facts), scan)["facts"]], [self.QUOTE])

    def test_a_price_is_never_proof(self):
        priced = "A high-intent account uses 10 credits, and you only pay for delivered accounts."
        scan = Scan("linear.app", [Page("https://a/", "Linear - Plan", f"{self.QUOTE} {priced} Plans from $49/mo.")])
        facts = [{"text": self.QUOTE, "quote": self.QUOTE, "url": "https://a/", "said_by": "seller"},
                 {"text": "A high-intent account uses 10 credits", "quote": priced, "url": "https://a/", "said_by": "seller"},
                 {"text": "Linear plans from $49/mo", "quote": "Plans from $49/mo.", "url": "https://a/", "said_by": "seller"}]
        self.assertEqual([fact["quote"] for fact in validate_brief(self.raw(facts=facts), scan)["facts"]], [self.QUOTE])

    def test_a_fee_per_sale_is_never_proof(self):
        fee = "30% per transaction when new customers find and buy through the discover marketplace."
        grew = "Teams grew their pipeline by 30% per quarter after switching."
        scan = Scan("linear.app", [Page("https://a/", "Linear - Plan", f"{self.QUOTE} {fee} {grew}")])
        facts = [{"text": fee, "quote": fee, "url": "https://a/", "said_by": "seller"},
                 {"text": grew, "quote": grew, "url": "https://a/", "said_by": "seller"}]
        self.assertEqual([fact["quote"] for fact in validate_brief(self.raw(facts=facts), scan)["facts"]], [grew])

    def test_the_product_text_says_the_name_once(self):
        brief = {"product_name": "n8n", "summary": "n8n is a platform for building AI agents.", "facts": []}
        self.assertEqual(product_text(brief), "n8n is a platform for building AI agents.")
        brief = {"product_name": "LangChain", "summary": "LangSmith is an agent engineering platform.", "facts": []}
        self.assertEqual(product_text(brief), "LangChain: LangSmith is an agent engineering platform.")

    def test_a_product_named_after_prices_or_a_numeral_keeps_its_facts(self):
        kept = ["Corporate credit cards for startups that close the books faster.",
                "Pricing intelligence for retail teams, refreshed every night.",
                "Built for Series I and Series II rounds, with one login per user."]
        scan = Scan("ramp.com", [Page("https://a/", "Ramp - Spend", " ".join(kept))])
        facts = [{"text": quote, "quote": quote, "url": "https://a/", "said_by": "seller"} for quote in kept]
        self.assertEqual([fact["quote"] for fact in validate_brief(self.raw(facts=facts), scan)["facts"]], kept)

    def test_a_signal_naming_the_product_itself_is_dropped(self):
        signals = [{"source": "job_postings", "description": "Posted an engineering role naming Linear and agent work. Source: LinkedIn Jobs posting, past 30 days"},
                   {"source": "community_social_and_review_sites", "description": "An engineering leader posted that handoffs get lost across Jira and Slack. Source: LinkedIn post, past 30 days"}]
        brief = validate_brief(self.raw(signals=signals, summary="Linear plans product work for software teams, on the home page."), self.scan())
        self.assertEqual([signal["source"] for signal in brief["signals"]], ["community_social_and_review_sites"])
        self.assertEqual(brief["summary"], "Linear plans product work for software teams.")

    def test_a_product_named_like_a_common_word_keeps_its_signals(self):
        signal = {"source": "job_postings", "description": "Posted a controller role to ramp up card coding. Source: LinkedIn Jobs posting, past 30 days"}
        brief = validate_brief(self.raw(product_name="Ramp", signals=[signal]), self.scan("ramp.com", "Ramp - Spend"))
        self.assertEqual(len(brief["signals"]), 1)

    def test_a_dropped_quote_says_why_so_the_retry_can_fix_it(self):
        said = "I saved ten hours a week with it."
        scan = Scan("linear.app", [Page("https://a/", "Linear - Plan", f"{self.QUOTE} {said} Plans from $49/mo.")])
        facts = [{"text": self.QUOTE, "quote": self.QUOTE, "url": "https://a/", "said_by": "seller"},
                 {"text": "Linear saves ten hours a week", "quote": said, "url": "https://a/", "said_by": "seller"},
                 {"text": "Linear plans from $49/mo", "quote": "Plans from $49/mo.", "url": "https://a/", "said_by": "seller"}]
        refused = []
        validate_brief(self.raw(facts=facts), scan, refused)
        self.assertEqual(len(refused), 2)
        self.assertIn("customer's words", refused[0])
        self.assertIn("price", refused[1])


class RequestPathFindingsCase(unittest.TestCase):
    """Real misses from running 24 live sites to the request step with the real model, Nov 11."""

    QUOTE = "Accept payments online in minutes."
    JOBS = "Posted a Billing Engineer role to build usage-based metering. Source: LinkedIn Jobs posting, past 30 days"
    POST = "Named head of finance's LinkedIn post on manual payout reconciliation. Source: LinkedIn post, past 90 days"

    def raw(self, **over):
        row = {"summary": "s", "roles": ["b"], "buyer_description": "c", "product_name": "Linear",
               "facts": [{"text": self.QUOTE, "said_by": "seller", "quote": self.QUOTE, "url": "https://a/"}]}
        row.update(over)
        return row

    def customers(self, said=None, replies=()):
        answers = list(replies)

        class Llm:
            def complete(self, *_args, **_kwargs):
                return SimpleNamespace(text=json.dumps(answers.pop(0)))

        page = f"<title>Linear</title><p>{self.QUOTE} Teams plan and build products here.</p>" * 10
        store = Store(Path(tempfile.mkdtemp()) / "c.db")
        return Customers(None, store, Llm(), lambda url: (url, page), say=said)

    def brief(self, draft, edit):
        """One fact is too few, so the draft is asked for twice, then edited."""
        answers = [draft, draft, edit]
        llm = SimpleNamespace(complete=lambda *_args, **_kwargs: SimpleNamespace(text=json.dumps(answers.pop(0))))
        return write_brief(llm, Scan("linear.app", [Page("https://a/", "Linear", self.QUOTE)]))

    def test_a_site_typed_the_way_chat_apps_send_it_is_read(self):
        from leadpoet_plugin.website import normalize_site, typed_site
        typed = {
            "Nousresearch.com.": "https://nousresearch.com/",
            "<https://leadpoet.com|leadpoet.com>": "https://leadpoet.com/",
            "[leadpoet.com](https://leadpoet.com)": "https://leadpoet.com/",
            "`leadpoet.com`": "https://leadpoet.com/",
            "**leadpoet.com**": "https://leadpoet.com/",
            "(leadpoet.com)": "https://leadpoet.com/",
            "leadpoet.com, thanks": "https://leadpoet.com/",
            "my site is foo.com": "https://foo.com/",
            "sub.domain.example.org/path_(x)": "https://sub.domain.example.org/path_(x)",
        }
        for said, url in typed.items():
            with self.subTest(said):
                self.assertEqual(normalize_site(typed_site(said)), url)

    def test_the_example_address_is_not_read_as_a_site(self):
        for said in ("yoursite.com", "<your website>", "your website", "https://www.yoursite.com/"):
            with self.subTest(said):
                self.assertEqual(self.customers().start(said), "That's the example address. Put your own website in its place.")

    def test_two_sites_or_an_email_address_are_still_refused(self):
        for said in ("foo.com bar.com", "write to me at sam@foo.com"):
            with self.subTest(said):
                self.assertIn("one website", self.customers().start(said))

    def test_a_social_profile_is_not_read_as_a_website(self):
        text = self.customers().start("https://x.com/NousResearch")  # No model reply is queued: none may be asked for.
        self.assertIn("profile on x.com", text)
        self.assertNotIn("consumers", text)

    def test_a_consumer_verdict_with_nothing_quoted_is_not_enough(self):
        scan = Scan("linear.app", [Page("https://a/", "Linear", self.QUOTE)])
        self.assertIsNone(validate_brief(self.raw(sells_to_businesses=False, facts=[]), scan))
        self.assertTrue(validate_brief(self.raw(sells_to_businesses=False), scan)["consumer"])

    def test_a_consumer_company_is_told_how_to_go_on(self):
        text = self.customers(replies=[self.raw(sells_to_businesses=False)]).start("linear.app")
        self.assertIn("sells to consumers", text)
        self.assertIn("/customers about <your text>", text)

    def test_the_progress_note_names_the_page_that_was_typed(self):
        notes = []
        self.customers(notes.append, [self.raw(sells_to_businesses=False)]).start("github.com/NousResearch/hermes-agent")
        self.assertIn("Reading github.com/NousResearch/hermes-agent:", notes[0])

    def test_the_drafts_checked_signal_stands_when_the_editors_rewrite_fails(self):
        signals = [{"source": "job_postings", "description": self.JOBS},
                   {"source": "community_social_and_review_sites", "description": self.POST}]
        edit = {"buyer_description": "c", "roles": ["b"], "signals": [
            {"source": "job_postings", "description": self.JOBS.replace("Billing Engineer", "Billing or Finance Engineer")},
            {"source": "community_social_and_review_sites", "description": self.POST.replace("manual", "slow manual")}]}
        kept = [signal["description"] for signal in self.brief(self.raw(signals=signals), edit)["signals"]]
        self.assertEqual(kept, [self.JOBS, self.POST.replace("manual", "slow manual")])

    def test_a_source_the_editor_left_out_stays_out(self):
        signals = [{"source": "job_postings", "description": self.JOBS},
                   {"source": "community_social_and_review_sites", "description": self.POST}]
        edit = {"buyer_description": "c", "roles": ["b"], "signals": [{"source": "job_postings", "description": self.JOBS}]}
        self.assertEqual([signal["source"] for signal in self.brief(self.raw(signals=signals), edit)["signals"]], ["job_postings"])

    def test_status_shows_the_next_step(self):
        customers = self.customers()
        self.assertEqual(customers.handle("status", ""), customers.handle("", ""))

    def test_pasted_words_have_no_address_so_the_one_page_read_is_the_source(self):
        """The real model answered "/" and "" for the page of an /customers about text, and every fact was refused."""
        words = ("We make Dockhand, scheduling software for small marinas. Marina managers use it to assign slips, bill "
                 "seasonal and transient boaters, and keep a waitlist. It replaces the paper slip map most marinas still run on.")
        facts = [{"text": "Marina managers use it to assign slips", "said_by": "seller", "url": url,
                  "quote": "Marina managers use it to assign slips, bill seasonal and transient boaters, and keep a waitlist."}
                 for url in ("/", "")]
        text = self.customers(replies=[self.raw(product_name="Dockhand", facts=facts)] * 2 + [{}]).handle("about", words)
        self.assertIn("Proof from your own description:", text)
        self.assertIn("/customers go", text)

    def test_a_quote_that_is_not_in_the_pasted_words_is_still_refused(self):
        words = "We make Dockhand, scheduling software for small marinas. " * 5
        facts = [{"text": "Trusted by 400 marinas across the country", "said_by": "seller", "url": "/",
                  "quote": "Trusted by 400 marinas across the country"}]
        text = self.customers(replies=[self.raw(product_name="Dockhand", facts=facts)] * 2).handle("about", words)
        self.assertIn("couldn't find enough", text)

    def test_a_brand_that_is_its_own_address_keeps_its_name(self):
        for domain, name in (("trigger.dev", "Trigger.dev"), ("cal.com", "Cal.com")):
            with self.subTest(domain):
                scan = Scan(domain, [Page("https://a/", name, self.QUOTE)])
                self.assertEqual(validate_brief(self.raw(product_name=name), scan)["product_name"], name)
        scan = Scan("linear.app", [Page("https://a/", "Linear", self.QUOTE)])
        self.assertEqual(validate_brief(self.raw(product_name="https://linear.app"), scan)["product_name"], "Linear")

    def test_a_search_waiting_on_a_key_is_saved_not_running(self):
        customers = self.customers(replies=[self.raw()] * 2 + [{}] + [self.raw(product_name="Other")] * 2 + [{}])
        first = customers.start("linear.app")
        customers.handle("go", first.split("/customers go ")[1].split()[0])
        text = customers.start("other.app")
        self.assertIn("linear.app is saved. Back to it: /customers open linear.app", text)
        self.assertNotIn("keeps running", text)

    def test_a_claim_lifted_from_mid_sentence_starts_with_a_capital(self):
        """cal.com, posthog.com and resend.com each showed a proof line starting small."""
        page = "With Resend you can start sending emails in minutes. sweetgreen serves warm bowls and plates every day."
        scan = Scan("resend.com", [Page("https://a/", "Resend", page)])
        facts = [{"text": "start sending emails in minutes", "said_by": "seller", "url": "https://a/",
                  "quote": "start sending emails in minutes"},
                 {"text": "sweetgreen serves warm bowls and plates every day", "said_by": "seller", "url": "https://a/",
                  "quote": "sweetgreen serves warm bowls and plates every day."}]
        kept = [fact["text"] for fact in validate_brief(self.raw(product_name="Resend", facts=facts), scan)["facts"]]
        self.assertEqual(kept[0], "Start sending emails in minutes")
        small = validate_brief(self.raw(product_name="sweetgreen", facts=facts[1:]), Scan("sweetgreen.com", scan.pages))
        self.assertEqual(small["facts"][0]["text"], "sweetgreen serves warm bowls and plates every day")


class BuyerCorrectionCase(Case):
    """Insight Global's card named contact-center operators, one corner of a general staffing firm, with no way to say so (Nov 11)."""

    WHO = "IT directors at US banks hiring contract engineers"

    def setUp(self):
        seen = self.seen = []

        class Llm(StockLlm):
            def complete(self, messages, **kwargs):
                result = super().complete(messages, **kwargs)
                said = messages[-1]["content"]
                seen.append((str(kwargs.get("purpose")), said))
                if "The founder adds: " not in said or not str(kwargs.get("purpose")).endswith(("brief", "brief check")):
                    return result
                raw = json.loads(result.text)
                raw["buyer_description"] = "US banks that staff engineering projects with contract engineers"
                return SimpleNamespace(text=json.dumps(raw))

        super().setUp()
        self.customers = Customers(self.api, self.store, Llm(), stock_fetch)

    def test_the_card_says_how_to_name_another_buyer(self):
        self.assertIn("Selling to someone else? Say who: /customers buyer <who buys>\n\nNext, find", self.text)
        self.assertIn("/customers buyer <who buys>: change the buyer on your card", self.customers.handle("help", ""))

    def test_saying_who_buys_reads_the_site_again_for_that_buyer(self):
        old = self.code("go")
        text = self.customers.handle("buyer", f"  {self.WHO}  ")
        self.assertIn("Who buys: US banks that staff engineering", text)
        told = [said for purpose, said in self.seen if purpose.endswith(("brief", "brief check"))]
        self.assertTrue(told and all(f"</website>\n\nThe founder adds: {self.WHO}" in said for said in told), told)  # Both the writer and its editor.
        job = self.customers.job()
        self.assertEqual((job["stage"], job["source"]), ("scanned", SELLER + "/"))
        self.assertEqual(self.customers._parked(), [])  # It takes the old card's place, not a place beside it.
        self.assertIn("isn't the one", self.customers.handle("go", old))  # The old card's code spends nothing.
        self.assertFalse(self.api.requests)

    def test_a_new_buyer_keeps_the_voice_and_says_a_search_has_one_buyer(self):
        """Ntiva with "law firms, dentists, and car dealerships" came back as law firms alone, and a voice pasted before it was lost."""
        voice = "I write short. No fluff, no exclamation marks. One idea per email and a plain ask."
        self.assertIn("Got it", self.customers.handle("voice", voice))
        text = self.customers.handle("buyer", self.WHO)
        self.assertEqual(self.customers.job()["voice_sample"], voice)
        self.assertIn("Each search is for one kind of buyer. To change it: /customers buyer <who buys>\n\nNext, find", text)
        self.assertNotIn("Selling to someone else?", text)
        self.assertIn("Each search is for one kind of buyer.", self.customers.handle("", ""))

    def test_it_works_until_leadpoet_has_been_asked(self):
        self.customers = Customers(None, self.store, self.customers.llm, stock_fetch)
        self.assertIn("free Leadpoet account", self.customers.handle("go", self.code("go")))
        self.assertEqual(self.customers.job()["stage"], "needs_key")
        self.assertIn("US banks that staff engineering", self.customers.handle("buyer", self.WHO))
        self.assertEqual(self.customers.job()["stage"], "scanned")

    def test_a_free_search_handed_to_the_app_can_still_change_its_buyer(self):
        self.api.credits = lambda: {"credits_remaining": 0, "plan": "free"}
        self.api.contact_settings = lambda: {"included_contacts_per_company": 1, "pricing": {"credits_per_company": 10}}
        self.text = self.customers.handle("", "")
        self.assertIn("paste", self.customers.handle("go", self.code("go")))
        self.assertEqual(self.customers.job()["stage"], "needs_credits")
        self.assertIn("US banks that staff engineering", self.customers.handle("buyer", self.WHO))
        self.assertEqual(self.customers.job()["stage"], "scanned")
        self.assertFalse(self.api.requests)

    def test_the_buyer_reads_as_a_sentence(self):
        """About one real card in five ended "Who buys" without its full stop."""
        brief = self.customers.job()["brief"]
        self.assertTrue(brief["company_fit"].endswith("."))
        from leadpoet_plugin.brief import _sentence
        self.assertEqual([_sentence(text) for text in ("US law firms", "US law firms.", "Who?", "")], ["US law firms.", "US law firms.", "Who?", ""])

    def test_a_search_under_way_keeps_its_buyer(self):
        self.customers.handle("go", self.code("go"))
        before = self.customers.job()
        text = self.customers.handle("buyer", self.WHO)
        self.assertIn("already went to Leadpoet", text)
        self.assertNotIn("/customers reset", text)  # Dropping it would throw away what was paid for.
        self.assertEqual(self.customers.job(), before)
        self.assertEqual(len(self.api.requests), 1)

    def test_too_little_to_go_on_asks_for_more_and_reads_nothing(self):
        calls = len(self.seen)
        for said in ("", "banks"):
            self.assertIn("Say who buys from you", self.customers.handle("buyer", said))
        self.assertEqual(len(self.seen), calls)
        self.customers.handle("reset", "")
        self.assertIn("Start with /customers yoursite.com", self.customers.handle("buyer", self.WHO))

    def test_a_search_from_pasted_words_is_told_to_add_the_buyer_there(self):
        job = self.customers.job()
        job["brief"]["domain"] = "your-company"
        self.customers.save({key: value for key, value in job.items() if key != "source"})
        card = self.customers.handle("", "")
        self.assertIn("/customers go ", card)
        self.assertNotIn("/customers buyer", card)
        self.assertIn("/customers about <your text>", self.customers.handle("buyer", self.WHO))

