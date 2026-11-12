"""Break every Leadpoet call in the real flow, one at a time, and check nothing is charged or launched twice."""

import tempfile
import unittest
from pathlib import Path

from leadpoet_plugin.api import ApiError
from leadpoet_plugin.customers import Customers
from stock import DemoCustomersApi, DemoLlm, SELLER, demo_fetch
from leadpoet_plugin.store import Store

WRITES = ("create_request", "create_audience", "create_campaign", "launch_campaign", "approve_drafts")
CALLS = WRITES + ("credits", "contact_settings", "get_request", "pipeline_contacts", "email_accounts", "contract_proposal",
                  "campaign_preview", "get_campaign", "review_drafts", "profile", "list_requests")


class Crash(BaseException):
    """The process died here."""


class FaultyApi(DemoCustomersApi):
    """Answers like Leadpoet: a repeated key returns the first answer, and a second launch is refused."""

    demo = False

    def __init__(self):
        super().__init__()
        self.count, self.at, self.kind = 0, -1, ""
        self.charges, self.launches, self.seen, self.bodies, self.approved_text = 0, {}, {}, {}, []
        for name in CALLS:
            setattr(self, name, self._wrap(name, getattr(self, name)))

    def _wrap(self, name, real):
        def call(*args, **kwargs):
            index, self.count = self.count, self.count + 1
            if index == self.at and self.kind == "lost_before":
                raise ApiError("timeout", ambiguous=True)
            if index == self.at and self.kind == "server_error":
                raise ApiError("boom", status=500)
            if index == self.at and self.kind == "busy":
                raise ApiError("slow down", status=429)
            if index == self.at and self.kind == "crash_before":
                raise Crash()
            result = real(*args, **kwargs)
            if index == self.at and self.kind == "lost_after":
                raise ApiError("timeout", ambiguous=True)
            if index == self.at and self.kind == "crash_after":
                raise Crash()
            return result
        return call

    def _once(self, key, body, make):
        if key in self.seen:
            assert self.bodies[key] == body, f"key {key} reused with a different body"
            return self.seen[key]
        self.bodies[key] = body
        self.seen[key] = make()
        return self.seen[key]

    def create_request(self, body, key):
        def make():
            self.charges += 1
            return DemoCustomersApi.create_request(self, body, key)
        return self._once(key, body, make)

    def create_audience(self, body, key):
        return self._once(key, body, lambda: DemoCustomersApi.create_audience(self, body, key))

    def create_campaign(self, body, key):
        return self._once(key, body, lambda: DemoCustomersApi.create_campaign(self, body, key))

    def launch_campaign(self, campaign_id, body):
        if campaign_id in self.launches:
            raise ApiError("already launched", status=409, code="ALREADY_AUTHORIZED")
        self.launches[campaign_id] = True
        return DemoCustomersApi.launch_campaign(self, campaign_id, body)


class FaultWalk(unittest.TestCase):
    def walk(self, at, kind):
        with tempfile.TemporaryDirectory() as folder:
            api, store, clock = FaultyApi(), Store(Path(folder) / "a.db"), [1000.0]
            make = lambda: Customers(api, store, DemoLlm(), demo_fetch, now=lambda: clock[0])
            customers = make()
            text = customers.start(SELLER)
            code = text.split("/customers go ")[1].split()[0]
            api.at, api.kind = at, kind

            def press(action, argument=""):
                nonlocal customers
                try:
                    return customers.handle(action, argument)
                except ApiError:  # The plugin's command wrapper turns this into a message.
                    return ""
                except Crash:
                    clock[0] += 400  # A crashed holder's lock has expired by the time anyone tries again.
                    customers = make()
                    return ""

            def run():
                nonlocal customers
                try:
                    customers.advance()
                except ApiError:  # The scheduled check counts it and tries again.
                    pass
                except Crash:
                    clock[0] += 400
                    customers = make()

            press("go", code)
            for _ in range(4):
                if customers.job()["stage"] in ("scanned", "needs_credits"):
                    press("go", code)
            for _ in range(4):
                run()
            for _ in range(4):
                if customers.job()["stage"] in ("ready", "writing"):
                    press("write")
            for _ in range(4):
                if customers.job()["stage"] == "reviewing" and any(row["state"] == "new" for row in customers.job()["people"]):
                    shown = press("all")
                    if "/customers all " in shown:
                        press("all", shown.rsplit("/customers all ", 1)[1].split()[0])
            for _ in range(6):
                run()
                press("")
            return api, customers.job()

    def test_clean_walk_reaches_live(self):
        api, job = self.walk(-1, "")
        self.assertEqual(job["stage"], "live")
        self.assertEqual(api.charges, 1)
        self.assertEqual(len(api.launches), 3)
        self.total = api.count

    def test_every_fault_at_every_call(self):
        total = self.walk(-1, "")[0].count
        self.assertGreater(total, 15)
        stuck = []
        for at in range(total + 2):
            for kind in ("lost_before", "lost_after", "server_error", "busy", "crash_before", "crash_after"):
                with self.subTest(at=at, kind=kind):
                    api, job = self.walk(at, kind)
                    self.assertLessEqual(api.charges, 1)
                    self.assertLessEqual(len(api.launches), 3)
                    self.assertLessEqual(len(api._campaigns), 3)
                    if job["stage"] != "live" or api.charges != 1 or len(api.launches) != 3:
                        stuck.append((at, kind, job["stage"], api.charges, len(api.launches)))
        self.maxDiff = None
        self.assertEqual(stuck, [], f"{len(stuck)} walks did not finish")


if __name__ == "__main__":
    unittest.main()
