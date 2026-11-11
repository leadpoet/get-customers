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
from leadpoet_plugin.customers_demo import DemoCustomersApi, DemoLlm, SELLER, demo_fetch
from leadpoet_plugin.store import Store
from leadpoet_plugin.website import Page, Scan
from leadpoet_plugin.brief import validate_brief
from leadpoet_plugin.website import SiteError, _hidden_classes
from leadpoet_plugin.writer import line_is_supported, render, research_person, validate_lines, validate_sequence, write_sequence


class Api(DemoCustomersApi):
    """The demo's Leadpoet, with switches for what goes wrong and a record of every write."""

    demo = False

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
        return {"query_id": "demo-find", "icp_id": "icp-1", "credits_remaining": 0}

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
        self.customers = Customers(self.api, self.store, DemoLlm(), demo_fetch)
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
        self.assertIn("doesn't match", self.customers.handle("go", "zzzz"))
        self.assertEqual(self.api.requests, [])

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
                                 {"id": "demo-find", "created_at": "2099-01-01T00:00:00Z", "icp_id": "icp-1",
                                  "request_description": description(self.customers.job()["brief"])}]
        self.customers.advance()
        self.assertEqual(self.customers.job()["query_id"], "demo-find")

    def test_no_key_shows_the_free_offer(self):
        customers = Customers(None, Store(Path(self.tmp.name) / "b.db"), DemoLlm(), demo_fetch)
        text = customers.start(SELLER)
        self.assertIn("first 10 companies are free", text)
        code = text.split("/customers go ")[1].split()[0]
        steps = customers.handle("go", code)
        # Where to sign up, where the key is made, the one command that adds it, and the way back.
        for needed in ("https://leadpoet.com/auth", "https://leadpoet.com/dashboard/account?section=integrations", "Create API key",
                       "Select all", "hermes config set LEADPOET_API_KEY <your key>", f"/customers go {code}"):
            self.assertIn(needed, steps)

    def test_a_key_added_after_the_offer_is_checked_before_it_is_welcomed(self):
        customers = Customers(None, Store(Path(self.tmp.name) / "c.db"), DemoLlm(), demo_fetch)
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
        self.assertIn("Free accounts get 100 credits", text)
        self.assertNotIn("doesn't cover", text)
        self.assertIn("Describe your ideal customer", self.customers.handle("go", self.code("go")))

    def test_a_paid_account_with_no_credits_is_told_what_it_is_short(self):
        self.api.credits_left = 0
        self.api.credits = lambda: {"credits_remaining": 0, "plan": "growth"}
        self.assertIn("doesn't cover", self.customers.handle("", ""))
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

    def test_every_personal_line_carries_its_quote_and_page(self):
        real = self.api.pipeline_contacts
        self.api.pipeline_contacts = lambda icp: [{**row, "icp_details": None} for row in real(icp)]
        self.to_review()
        self.assertIn('Source: "We now run cold-chain lanes out of Rotterdam six days a week"', self.text)
        self.assertIn("https://pinecrest-freight.example/", self.text)

    def test_the_buying_signal_opens_and_names_its_source(self):
        self.to_review()
        self.assertIn("Personal line: Pinecrest Freight posted a night dispatcher role on Oct 28 that asks for clean shift handovers.\n"
                      "Source: Leadpoet's note above", self.text)

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
            {"lead_id": "demo-marcus", "display_state": "sent", "scheduled_for": at(10)},
            {"lead_id": "demo-priya", "display_state": "scheduled", "scheduled_for": at(181)}]
        self.assertIn("The next email goes out in about 3 hours.", self.customers.handle("", ""))
        self.api.queued_messages = lambda: [{"lead_id": "demo-priya", "display_state": "queued", "scheduled_for": at(-15)}]
        self.assertIn("The next email is due now.", self.customers.handle("", ""))
        def broken():
            raise ApiError("down", 503)
        self.api.queued_messages = broken
        self.assertIn("Live.", self.customers.handle("", ""))

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
        self.api._drafts["changed"] = {"id": "changed", "lead_id": "demo-marcus", "can_approve": True,
                                       "rendered_subject": "x", "rendered_body": "A different email."}
        self.assertIn("doesn't match", self.customers.advance())
        self.assertNotIn("changed", self.api.approved)

    def test_a_draft_the_server_will_not_approve_is_left_alone(self):
        self.to_review()
        self.customers.handle("approve", self.code("approve"))
        draft = {"id": "d2", "lead_id": "demo-marcus", "can_approve": False,
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
        restarted = Customers(self.api, Store(Path(self.tmp.name) / "a.db"), DemoLlm(), demo_fetch)
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

    def test_a_broken_address_is_a_plain_error(self):
        from leadpoet_plugin.website import SiteError, normalize_site
        with self.assertRaises(SiteError):
            normalize_site("https://a.example:notaport/")


class TwoWorkspacesCase(unittest.TestCase):
    def test_jobs_never_mix(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Customers(Api(), Store(Path(tmp) / "one.db"), DemoLlm(), demo_fetch)
            second = Customers(Api(), Store(Path(tmp) / "two.db"), DemoLlm(), demo_fetch)
            first.start(SELLER)
            self.assertIsNone(second.job())
            self.assertIn("/customers yoursite.com", second.handle("", ""))


class ProgressTests(unittest.TestCase):
    def test_a_progress_note_that_fails_never_stops_the_job(self):
        def broken(_text):
            raise RuntimeError("the chat is gone")

        with tempfile.TemporaryDirectory() as folder:
            customers = Customers(DemoCustomersApi(), Store(Path(folder) / "a.db"), DemoLlm(), demo_fetch, say=broken)
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
        self.assertTrue(lines("Linear switched their whole team this spring."))

    def test_a_negative_elsewhere_in_the_proof_does_not_block_a_subject(self):
        """One "no" on any page used to refuse every subject, and with it the whole sequence on 6 of 19 real sites."""
        brief = {"product_name": "Stripe", "facts": [{"text": "t", "said_by": "seller", "quote": "Accept payments online in minutes"},
                                                     {"text": "t", "said_by": "seller", "quote": "Stripe has no setup fees"}]}
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
        from leadpoet_plugin.customers_demo import _SEQUENCE
        from leadpoet_plugin.website import scan_site
        from leadpoet_plugin.writer import check_sequence
        brief = write_brief(DemoLlm(), scan_site(SELLER, fetcher=demo_fetch))
        draft = json.loads(json.dumps(_SEQUENCE))
        draft["emails"][0]["subject"] = "Stale rosters"
        self.assertIn("Stale", check_sequence(draft, brief)[1].title())
        emails, problem = check_sequence(draft, brief, safe_subjects=True)
        self.assertEqual((problem, emails[0]["subject"]), ("", "Question for {company}"))


class SignalOpenerTests(unittest.TestCase):
    def line(self, text, why="Posted three SDR roles in the past month."):
        from leadpoet_plugin.writer import signal_opener
        llm = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text=json.dumps({"line": text})))
        return signal_opener(llm, {"company": "Ramp", "why": why})

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

    def test_the_signal_opens_and_a_site_fact_stands_in_without_one(self):
        customers = Customers(DemoCustomersApi(), Store(Path(tempfile.mkdtemp()) / "a.db"), DemoLlm(), demo_fetch)
        lines, _ = research_person(DemoLlm(), {**customers.api.pipeline_contacts(None)[0], "why": "Posted a night dispatcher role on Oct 28 that asks for clean shift handovers."}, demo_fetch)
        self.assertEqual([line["url"] for line in lines], [""])
        lines, _ = research_person(DemoLlm(), customers.api.pipeline_contacts(None)[0], demo_fetch)
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

    class Llm(DemoLlm):
        def complete(self, messages, **kwargs):
            result = super().complete(messages, **kwargs)
            page = messages[-1]["content"].split("PAGE ")[1].split()[0].rstrip("/")
            return SimpleNamespace(text=result.text.replace(SELLER, page))

    def fetch(self, url):
        if not url.startswith(self.OTHER):
            return demo_fetch(url)
        return url if url.endswith("/") else url + "/", demo_fetch(SELLER)[1]

    def test_a_new_site_keeps_the_running_search(self):
        text = self.customers.handle(self.OTHER, "")
        self.assertIn("keeps running", text)
        self.assertIn("/customers open ", text)
        self.assertEqual(self.customers.job()["brief"]["domain"], "other.example")
        listing = self.customers.handle("list", "")
        self.assertIn("Other searches:", listing)
        self.assertIn(": finding contacts", listing)

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

    def test_reset_keeps_a_paid_search_unless_confirmed(self):
        self.assertIn("keep it unless you're sure", self.customers.handle("reset", ""))
        self.assertEqual(self.customers.job()["stage"], "finding")
        self.assertIn("Started fresh", self.customers.handle("reset", "confirm"))
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

    def test_a_key_added_after_the_offer_shows_the_cost_before_spending(self):
        store = Store(Path(self.tmp.name) / "k.db")
        keyless = Customers(None, store, DemoLlm(), demo_fetch)
        code = keyless.start(SELLER).split("/customers go ")[1].split()[0]
        keyless.handle("go", code)
        customers = Customers(self.api, store, DemoLlm(), demo_fetch)
        text = customers.handle("go", code)
        self.assertIn("Cost: 100 credits", text)
        self.assertEqual(self.api.requests, [])
        customers.handle("go", code)
        self.assertEqual(len(self.api.requests), 1)

    def test_a_key_added_while_waiting_moves_on(self):
        store = Store(Path(self.tmp.name) / "w.db")
        keyless = Customers(None, store, DemoLlm(), demo_fetch)
        keyless.handle("go", keyless.start(SELLER).split("/customers go ")[1].split()[0])
        self.assertIn("Your key is in", Customers(self.api, store, DemoLlm(), demo_fetch).handle("", ""))

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
        text = Customers(self.api, Store(Path(self.tmp.name) / "s.db"), DemoLlm(), demo_fetch).start(SELLER)
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
        text = Customers(self.api, Store(Path(self.tmp.name) / "m.db"), None, demo_fetch).start(SELLER)
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
