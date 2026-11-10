"""/customers: from a website to approved, sourced outreach, as a job that survives restarts.

The agent gets no way to spend credits, launch or approve. Those need a person to type a short
code tied to the exact content. Steps that wait on someone else resume from the scheduled check.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import brief as briefs
from .api import ApiError
from .website import Page, Scan, SiteError, normalize_site, scan_site
from .writer import fits, render, research_person, write_sequence

LEAD_ENGINE_URL = "https://leadpoet.com/dashboard/lead-engine"
OUTREACH_URL = "https://leadpoet.com/dashboard/outreach"
COMPANIES = 10  # Leadpoet's smallest paid search, and exactly what the free credits buy.
MAX_SENDERS = 20  # Leadpoet's limit for one campaign.
MAX_PAST_PEOPLE = 200
PAST_PEOPLE_SECONDS = 30 * 86400  # Long past the last step of a three-email sequence.
MAX_LAUNCHES_PER_RUN = 3  # Each is five writes, and the API allows twenty a minute.
RESEARCH_SECONDS = 75
ACTIVE_FIND = ("pending", "processing", "working")
SCOPE_NAMES = {"lead_engine:read": "Read Lead engine", "lead_engine:write": "Manage Lead engine", "pipeline:read": "Read pipeline",
               "credits:read": "Read credits", "outreach:read": "Read outreach", "outreach:write": "Manage outreach", "outreach:send": "Send outreach"}
SCOPES = tuple(SCOPE_NAMES)
OWN_WORDS = "your own description"
MAX_OWN_WORDS = 6000
MAX_SIGNATURE_CHARS = 1000
_PASTE_INSTEAD = "No problem. Paste a few sentences about what you sell: /customers about <your text>"
_LOCK = "customers"
_JOB = "customers_job"
_PARKED = "customers_parked"  # Other searches still running, so each one keeps moving.
MAX_SEARCHES = 5
_SETTLED = ("scanned", "done", "failed", "live")
_SLOW_DOWN = "Leadpoet asked me to slow down. I'll carry on at the next check."
# Leadpoet may clear these by itself, so a later check tries again without bothering anyone.
_TRY_APPROVAL_AGAIN = {"READY_CHECK_UNAVAILABLE", "AUTOMATIC_RETRY", "CONTACT_PAUSED", "CAMPAIGN_NOT_ACTIVE"}
_REPLY_OPT_OUT = 'Don\'t want these? Reply "unsubscribe".'
_LINK_OPT_OUT = re.compile(r"Don't want these\? Unsubscribe: https://\S+")
_CAMPAIGN_SEED = {
    "schema_version": "outreach-controlled-authoring-v1", "message_mode": "signal_led",
    "execution": {"enabled": True, "channels": ["email"]},
    "audience": {"intended_buyer_functions": [], "jurisdictions": [], "regulated_industries": [], "regulated_jurisdictions": []},
    "market_modules": [], "fulfillment": {"selections": []}, "contextual_learning": {"enabled": False},
    "campaign_context": {"version": 1, "mode": "fixed_sequence"}, "manual_sequence_authorized": True,
}
_STYLE = {"phrase_allowlist": [], "phrase_denylist": [], "positive_style_examples": [], "negative_style_examples": []}
HELP = """/customers yoursite.com: find your customers
/customers about <what you sell>: start from your own words
/customers: see the next step
/customers voice <a few lines you wrote>: match your writing
/customers list: see everyone, and your other searches
/customers open <site>: switch to another search
/customers all: read every email, then approve them together
/customers check: refresh now
/customers demo: try the whole thing, no account needed
/customers reset: start over"""


class _FewAtATime:
    """Hermes's model client takes parallel calls and limits them itself. This keeps a plugin's share small."""

    def __init__(self, llm: Any, limit: int = 4):
        self._llm, self._lock = llm, threading.BoundedSemaphore(limit)

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return self._llm.complete(*args, **kwargs)


class _Blocked(Exception):
    """Launching can't continue until the user fixes something. Approvals are kept."""


def affordable_companies(credits: int, price: int) -> int:
    """Leadpoet won't strand a small balance: a request leaves nothing usable, or at least 100."""
    left = credits - COMPANIES * price
    return COMPANIES if left >= 0 and (left < price or left >= 100) else 0


def code_for(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:4]


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def booking_link(draft: dict) -> str:
    """The booking link Leadpoet bound to this draft's campaign. It's the only link that can follow approved text."""
    plan = draft.get("message_plan")
    link = plan.get("campaign_booking_link") if isinstance(plan, dict) else None
    return _squash(link) if isinstance(link, str) else ""


def footer_is_known(extra: str, compliance: dict) -> bool:
    """Is this exactly the footer Leadpoet builds from the sender's own details? Nothing else may follow an approved email."""
    extra = _squash(extra)
    identity = " · ".join(_squash(re.sub(r"[ \t]*—[ \t]*", " - ", str(value)))
                          for value in (compliance.get("legal_entity"), compliance.get("postal_address")) if value)
    head = f"-- {identity}".strip()
    own_words = _squash(compliance.get("opt_out_text") or "")
    if own_words and extra == (f"{head} {own_words}" if identity else own_words):
        return True
    if identity and extra == head:
        return True
    if not extra.startswith(f"{head} "):
        return False
    rest = extra[len(head):].strip()
    return rest == _REPLY_OPT_OUT or bool(_LINK_OPT_OUT.fullmatch(rest))


def signature_then_footer(extra: str, compliance: dict) -> bool:
    """Leadpoet puts the mailbox's own signature between the approved text and its footer. Nothing else may be there."""
    extra = _squash(extra)
    own_words = _squash(compliance.get("opt_out_text") or "")
    starts = [match.start() for match in re.finditer(r"(?:^| )-- ", extra)]
    if own_words and extra.endswith(own_words):
        starts.append(len(extra) - len(own_words))
    return any(start <= MAX_SIGNATURE_CHARS and footer_is_known(extra[start:], compliance) for start in starts)



_FILLER = {"0", "x", "na", "nan", "nil", "none", "null", "undefined", "unknown", "pending",
           "tba", "tbd", "todo", "notapplicable", "notavailable"}


def _comparable(text: str) -> str:
    return re.sub(r"[.!;:,\s]+$", "", re.sub(r"\s+", " ", text.lower()).strip())


def account_note(note: object, *requests: dict | None) -> str:
    """Leadpoet's "why this account" note, as the app shows it.

    A new account starts with the request's own text as a placeholder, which says what
    was asked for, not why this company fits, so a copy of it never counts. Nor does filler.
    """
    if not isinstance(note, str):
        return ""
    text = _comparable(note)
    letters = re.sub(r"[\W_]", "", text)
    if not letters or letters in _FILLER:
        return ""
    asked = [_comparable(str((request or {}).get(key) or ""))
             for request in requests for key in ("request_description", "intent_signals")]
    if any(text in part for part in asked):
        return ""
    return re.sub(r"\s+", " ", note).strip()


def _arrivals(people: list[dict]) -> str:
    """Who arrived and, when Leadpoet published one, why each company fits."""
    noted = [person for person in people if person.get("why")]
    head = f"{len(people)} verified contacts are in."
    if not noted:
        return head
    rows = [f"• {person['company']}: {person['why']}" for person in noted]
    return head + " Why each company:\n" + "\n".join(rows)


class Customers:
    def __init__(self, api: Any, store: Any, llm: Any = None, fetcher: Any = None,
                 now: Callable[[], float] = time.time, say: Callable[[str], Any] | None = None, key: str = _JOB):
        self.api, self.store, self.llm, self.fetcher, self.now, self.say = api, store, llm, fetcher, now, say
        self.key = key

    def _progress(self, text: str) -> None:
        """A long model call shouldn't look like silence. A note that fails stops nothing."""
        try:
            if self.say:
                self.say(text)
        except Exception:
            pass

    # State -----------------------------------------------------------------------------------
    def job(self) -> dict | None:
        raw = self.store.get(self.key)
        try:
            return json.loads(raw) if raw else None
        except ValueError:  # Unreadable state must not trap someone: /customers reset has to work.
            return None

    def save(self, job: dict | None) -> None:
        self.store.put(self.key, json.dumps(job) if job else None)

    # Other searches ----------------------------------------------------------------------------
    def _parked(self) -> list[str]:
        try:
            keys = json.loads(self.store.get(_PARKED) or "[]")
        except ValueError:
            return []
        return [key for key in keys if isinstance(key, str) and self.store.get(key)]

    def _other(self, key: str) -> "Customers":
        return Customers(self.api, self.store, self.llm, self.fetcher, self.now, self.say, key=key)

    def _park(self, job: dict) -> None:
        """A search that has spent credits keeps running in the background when a new one starts."""
        key = f"{_JOB}:{job['id']}"
        self._other(key).save(job)
        self.store.put(_PARKED, json.dumps([*[k for k in self._parked() if k != key], key]))

    def _room_for_another(self, job: dict | None) -> str:
        if job and job.get("find_body"):
            return self._unconfirmed_find(job)
        if job and job["stage"] not in _SETTLED and len(self._parked()) >= MAX_SEARCHES - 1:
            return f"You have {MAX_SEARCHES} searches going, the most at once. See them with /customers list"
        return ""

    def _locked(self, work: Callable[[], str], busy: str) -> str:
        token = self.store.try_lock(_LOCK, self.now())
        if not token:
            return busy
        try:
            return work()
        finally:
            self.store.unlock(_LOCK, token)

    def _past(self) -> list[dict]:
        oldest = self.now() - PAST_PEOPLE_SECONDS
        try:
            rows = json.loads(self.store.get("customers_past") or "[]")
        except ValueError:
            return []
        return [row for row in rows if row.get("retired_at", 0) > oldest]

    def _retire(self, job: dict | None) -> None:
        """People already running stay watched, so their approved follow-ups still go out."""
        running = [{**row, "retired_at": self.now()} for row in (job or {}).get("people", []) if row["state"] == "launched"]
        if running:
            self.store.put("customers_past", json.dumps((self._past() + running)[-MAX_PAST_PEOPLE:]))

    # Commands --------------------------------------------------------------------------------
    def handle(self, action: str, argument: str) -> str:
        return self._locked(lambda: self._handle(action, argument),
                            "I'm still working on the last step. Try again in a minute.")

    def _handle(self, action: str, argument: str) -> str:
        if action == "reset":
            return self._reset(argument)
        if action == "voice":
            return self._voice(argument)
        if action == "about":
            return self._about(argument)
        job = self.job()
        if action == "":
            return self._next(job, refresh=True)
        if action == "check":
            return self._next(job, refresh=True)
        if action == "list":
            return self._listing(job)
        if action == "open":
            return self._open(job, argument)
        if action == "go":
            return self._go(job, argument)
        if action == "use":
            return self._use(job, argument)
        if action == "write":
            return self._write(job)
        if action == "approve":
            return self._approve(job, argument)
        if action == "all":
            return self._approve_all(job, argument)
        if action == "skip":
            return self._skip(job)
        if action in ("help", "?"):
            return HELP
        return self.start(" ".join(part for part in (action, argument) if part), job)

    def _reset(self, argument: str) -> str:
        job = self.job()
        if job and job.get("find_body") and argument.lower() != "confirm":
            return self._unconfirmed_find(job)
        if job and job["stage"] not in (*_SETTLED, "needs_key", "needs_credits") and argument.lower() != "confirm":
            return ("This search has spent credits, so I'll keep it unless you're sure.\n"
                    "To look at another company, just send /customers othersite.com and this one keeps running.\n"
                    "Drop it anyway: /customers reset confirm")
        self._retire(job)
        self.save(None)
        return "Started fresh. Send /customers yoursite.com"

    def _unconfirmed_find(self, job: dict) -> str:
        return ("I asked Leadpoet for this search and never heard back, so credits may already be spent. "
                f"Check with no double charge: /customers go {self._brief_code(job)}\n"
                "Or drop it anyway: /customers reset confirm")

    def start(self, site: str, job: dict | None = None, note: str = "") -> str:
        refused = self._room_for_another(job)
        if refused:
            return refused
        try:
            url = normalize_site(site)
        except SiteError as error:
            return error.args[0]
        try:
            host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
            self._progress(f"Reading {host}: working out who buys from you, who makes the call, "
                           "the buying signals that say now is the time, and the quotes that prove each claim. About 30 seconds.")
            scan = scan_site(url, fetcher=self.fetcher) if self.fetcher else scan_site(url)
        except SiteError as error:
            return f"{error.args[0]}\n\n{_PASTE_INSTEAD}"
        return self._start_from(scan, url, job, "Try a page that says what you sell: /customers yoursite.com/product")

    def _about(self, text: str) -> str:
        """Some sites turn readers away. The founder's own words are as good a source as their website."""
        job = self.job()
        refused = self._room_for_another(job)
        if refused:
            return refused
        if len(text) < 200:
            return "Paste a bit more: what you sell, who buys it, and any real numbers or customers.\n/customers about <your text>"
        self._progress("Reading what you wrote. This takes about 20 seconds.")
        scan = Scan(domain="your-company", pages=[Page(OWN_WORDS, "", text[:MAX_OWN_WORDS])])
        return self._start_from(scan, f"{OWN_WORDS}:{text[:200]}", job, "Add what the product does and who uses it, then send it again.")

    def _start_from(self, scan: Scan, source: str, job: dict | None, tip: str) -> str:
        brief = briefs.write_brief(self.llm, scan)
        if brief is None:
            return f"I couldn't find enough there that I can prove. {tip}"
        created = {"id": code_for(source, str(self.now()))[:4] + hashlib.sha256(source.encode()).hexdigest()[:8],
                   "stage": "scanned", "brief": brief, "people": [], "emails": None, "voice_sample": "",
                   "created_at": self.now()}
        if job and job["stage"] not in _SETTLED:
            self._park(job)
            note = f"{_site(job)} keeps running, and I'll ping you when it moves. Back to it: /customers open {_site(job)}\n\n"
        else:
            self._retire(job)
            note = ""
        self.save(created)
        return note + self._next(created)

    def _voice(self, sample: str) -> str:
        job = self.job()
        if not job:
            return "Start with /customers yoursite.com, then add your voice."
        if len(sample) < 40:
            return "Paste a few sentences you really wrote: /customers voice <text>"
        if job["stage"] not in ("scanned", "needs_key", "needs_credits", "finding", "ready"):
            return "The emails are already written, so that would change nothing. /customers reset starts over."
        job["voice_sample"] = sample[:1500]
        self.save(job)
        return "Got it. Your emails will follow that style."

    # Find ------------------------------------------------------------------------------------
    def _brief_code(self, job: dict) -> str:
        return code_for(job["id"], json.dumps(job["brief"], sort_keys=True))

    def _go(self, job: dict | None, code: str) -> str:
        if not job or job["stage"] not in ("scanned", "needs_key", "needs_credits"):
            return self._next(job)
        if code != self._brief_code(job):
            return "That code doesn't match this profile. Use the code shown here:\n\n" + self._next(job)
        if self.api is None:
            job["stage"] = "needs_key"
            self.save(job)
            return self._next(job)
        problem = self._key_problem() if not job.get("find_body") else ""
        return problem or self._spend(job)

    def _key_problem(self) -> str:
        """Check before spending, so a key that can't finish the job never starts it."""
        try:
            scopes = set(self.api.identity().get("scopes") or [])
        except ApiError as error:
            if error.status in (401, 403):
                return "Leadpoet didn't accept that API key. Create a new one in Settings, Developer, and add it as LEADPOET_API_KEY."
            raise
        missing = [scope for scope in SCOPES if scope not in scopes]
        return f"This API key is missing some permissions. Create a new one in Settings, Developer, with: {', '.join(SCOPE_NAMES[scope] for scope in missing)}" if missing else ""

    def _find_body(self, job: dict) -> dict | int | None:
        """The request to send, the balance if it's too small for one, or None with no price."""
        settings = self.api.contact_settings()
        price = int((settings.get("pricing") or {}).get("credits_per_company") or 0)
        included = int(settings.get("included_contacts_per_company") or 1)
        if price <= 0:
            return None
        remaining = int(self.api.credits().get("credits_remaining") or 0)
        companies = affordable_companies(remaining, price)
        if companies < 1:
            return remaining
        brief = job["brief"]
        signals = briefs.signals_text(brief)
        return {"lead_type": "high_intent" if signals else "target_fit", "icp_name": f"{brief['product_name']} (Hermes)"[:120],
                "request_description": briefs.description(brief), "product_description": briefs.product_text(brief),
                **({"intent_signals": signals} if signals else {}),
                "max_leads": companies, "contacts_per_company": included, "included_contacts_per_company": included,
                "credit_budget": companies * price, "contact_methods": {"email": True, "linkedin": False, "phone": False},
                "requested_roles": brief["buyers"], "company_sizes": brief["company_sizes"], "countries": brief["countries"]}

    def _spend(self, job: dict) -> str:
        # A retry has to repeat the same request exactly, even if the balance has dropped since.
        retry = bool(job.get("find_body"))
        body = job.get("find_body") or self._find_body(job)
        if body is None:
            return "Leadpoet didn't return a price, so I didn't spend anything. Try again shortly."
        if isinstance(body, int):
            return self._hand_off(job, body)
        quoted = job.get("quoted_credits")
        if not retry and quoted and body["credit_budget"] != quoted:  # Never charge a different amount than was shown.
            job.pop("quoted_credits", None)
            return "Leadpoet's price changed since you looked, so nothing was spent.\n\n" + self._offer(job)
        job["find_body"] = body
        self.save(job)
        try:
            data = self.api.create_request(body, self._find_key(job))
        except ApiError as error:
            if error.ambiguous:
                return "I couldn't confirm that went through. /customers go will safely check, with no double charge."
            if error.status == 402 or error.code == "insufficient_credits":
                # Leadpoet remembers this refusal against the key, so a top-up needs a new one. Nothing was charged.
                job.pop("find_body", None)
                job["find_attempt"] = job.get("find_attempt", 0) + 1
                return self._hand_off(job, 0)
            if retry or error.status == 409:
                return f"Leadpoet is still sorting out that request ({error.message}). /customers go checks again, with no double charge."
            job.pop("find_body", None)
            self.save(job)
            return f"Leadpoet didn't accept that: {error.message}"
        job.pop("find_body", None)
        job.update(stage="finding", query_id=data.get("query_id"), query_ids=data.get("query_ids") or [], icp_id=data.get("icp_id"),
                   companies=body["max_leads"], spent=body["credit_budget"], credits_left=data.get("credits_remaining"))
        self.save(job)
        return self._next(job)

    def _find_key(self, job: dict) -> str:
        return f"hermes-customers-{job['id']}-find" + (f"-{job['find_attempt']}" if job.get("find_attempt") else "")

    def _hand_off(self, job: dict, balance: int) -> str:
        job.setdefault("handed_off_at", self.now())  # The app request may already be there, so never move this later.
        job.update(stage="needs_credits", balance=balance)
        self.save(job)
        return self._next(job)

    def _adopt(self, job: dict) -> bool:
        """After the first request is made in the app, carry on with it, but only if it's this search."""
        since = job.get("handed_off_at", job["created_at"])
        wanted = _squash(briefs.description(job["brief"])).lower()
        for request in sorted(self.api.list_requests(), key=lambda row: str(row.get("created_at") or ""), reverse=True):
            created = _timestamp(request.get("created_at"))
            if created is None or created < since or not request.get("id"):
                continue
            typed = _squash(f"{request.get('request_description') or ''} {request.get('natural_language_query') or ''}").lower()
            if wanted in typed:
                self._adopt_request(job, request)
                return True
            # It may be this search with the text changed. Only the user can say.
            job["other_request"] = {"id": request["id"], "icp_id": request.get("icp_id")}
            self.save(job)
            return False
        return False

    def _adopt_request(self, job: dict, request: dict) -> None:
        job.pop("other_request", None)
        job.update(stage="finding", query_id=request["id"], icp_id=request.get("icp_id"))
        self.save(job)

    def _use(self, job: dict | None, code: str) -> str:
        other = (job or {}).get("other_request")
        if not job or not other or job["stage"] != "needs_credits":
            return self._next(job)
        if code != code_for(job["id"], other["id"]):
            return "That code doesn't match. Use the code shown here:\n\n" + self._next(job)
        self._adopt_request(job, other)
        return self._next(job)

    def _refresh_find(self, job: dict) -> None:
        request = self.api.get_request(job["query_id"])
        status = str(request.get("status") or "")
        state = str((request.get("progress") or {}).get("state") or "")
        if status in ("error", "failed") or state in ("failed", "archived"):
            job["stage"] = "failed"
            job["failure"] = (request.get("progress") or {}).get("message") or "Leadpoet couldn't finish this request."
        elif status in ("ready", "completed") or state == "ready":
            people = self._load_people(job, request)
            job["delivered_empty"] = not people
            if people:
                job.update(stage="ready", people=people)
        self.save(job)

    def _load_people(self, job: dict, request: dict) -> list[dict]:
        rows, seen, people = self.api.pipeline_contacts(job.get("icp_id")), set(), []
        # The live request is what the placeholder copied. The brief's own text covers a request adopted from the app.
        asked = (request, {"request_description": briefs.description(job["brief"]), "intent_signals": briefs.signals_text(job["brief"])})
        for row in rows:
            company = str(row.get("company") or "").strip().lower()
            if (row.get("query_id") not in {job["query_id"], *job.get("query_ids", [])} or not row.get("email") or not company or company in seen
                    or row.get("privacy_deleted_at") or row.get("archived_at")):
                continue
            seen.add(company)
            people.append({"id": row["id"], "name": row.get("name") or "", "role": row.get("role") or "",
                           "company": row.get("company") or "", "email": row["email"], "website": row.get("website") or "",
                           "why": account_note(row.get("icp_details"), *asked),
                           "state": "research", "lines": [], "dropped": 0})
        return people[:COMPANIES]

    # Write -----------------------------------------------------------------------------------
    def _write(self, job: dict | None) -> str:
        if not job or job["stage"] not in ("ready", "writing"):
            return self._next(job)
        if self.llm is None:
            return "Writing needs your Hermes model, and I can't reach it right now."
        job["stage"] = "writing"
        self._progress("Reading each company's site and writing their emails.")
        if not job.get("emails"):
            job["emails"] = write_sequence(self.llm, job["brief"], job.get("voice_sample", ""))
            if not job["emails"]:
                job["stage"] = "ready"
                self.save(job)
                return "The model's drafts kept breaking my rules, so I threw them out. Nothing was sent. Run /customers write to try again."
        self.save(job)
        deadline = self.now() + RESEARCH_SECONDS
        waiting = [person for person in job["people"] if person["state"] == "research"]
        self._line_llm = _FewAtATime(self.llm)
        # Fetching pages is slow and safe to overlap. Each person is saved as soon as they finish.
        with ThreadPoolExecutor(max_workers=4) as pool:
            for person, (lines, dropped) in zip(waiting, pool.map(self._research, waiting, [deadline] * len(waiting))):
                if lines is None:
                    continue
                current = self.job()
                if not current or current["stage"] != "writing":  # This run outlived its lock, and someone moved on.
                    return self._next(current)
                self._finish_person(job, person, lines, dropped)
                self.save(job)
        if any(person["state"] == "research" for person in job["people"]):
            done = sum(1 for person in job["people"] if person["state"] != "research")
            return f"{done} of {len(job['people'])} researched so far. /customers write continues."
        job["stage"] = "reviewing"
        self.save(job)
        return self._summary(job) + "\n\n" + self._next(job)

    def _research(self, person: dict, deadline: float) -> tuple[list[dict] | None, int]:
        if self.now() >= deadline:
            return None, 0
        return research_person(self._line_llm, person, self.fetcher)

    def _finish_person(self, job: dict, person: dict, lines: list[dict], dropped: int) -> None:
        touches = render(job["emails"], person, lines, self._sender_name(job))
        if lines and not fits(touches):
            dropped, lines = dropped + len(lines), []
            touches = render(job["emails"], person, lines, self._sender_name(job))
        person.update(lines=lines, dropped=dropped, state="new", touches=touches)
        if not fits(touches):
            person.update(state="held", held="These emails came out longer than Leadpoet allows, so I left this person out.")

    def _sender_name(self, job: dict) -> str:
        if "sender" not in job:
            try:
                profile = (self.api.profile() or {}).get("profile") or {}
            except ApiError:
                profile = {}
            job["sender"] = str((profile.get("voice") or {}).get("sender_name") or "").strip() or job["brief"]["product_name"]
        return job["sender"]

    def _summary(self, job: dict) -> str:
        people = job["people"]
        sourced = sum(1 for person in people if person["lines"])
        dropped = sum(person["dropped"] for person in people)
        text = f"Wrote for {len(people)} people. {sourced} have a personal line with a source."
        if dropped:
            text += f" I threw out {dropped} line{'s' if dropped != 1 else ''} the page didn't back up."
        return text

    def _person_code(self, job: dict, person: dict) -> str:
        return code_for(job["id"], person["id"], json.dumps([person["email"], person["name"], person["lines"], person["touches"]], sort_keys=True))

    def _all_code(self, job: dict, people: list[dict]) -> str:
        return code_for(job["id"], *[self._person_code(job, person) for person in people])

    def _emails(self, person: dict) -> list[str]:
        lines = [f"{person['name']}, {person['role']} at {person['company']}", person["email"]]
        if person.get("why"):
            lines.append(f"Why them: {person['why']}")
        lines.append("")
        if person["lines"]:
            for entry in person["lines"]:
                source = [f"Source: \"{entry['quote']}\"", entry["url"]] if entry["url"] else ["Source: Leadpoet's note above"]
                lines += [f"Personal line: {entry['line']}", *source, ""]
        else:
            lines += ["No personal line. I couldn't prove one, so there isn't one.", ""]
        for number, touch in enumerate(person["touches"], 1):
            when = "first" if touch["delay"] == 0 else f"{touch['delay']} business days after the last one"
            lines += [f"Email {number} ({when}): {touch['subject']}", touch["body"], ""]
        return lines

    def _card(self, job: dict, person: dict) -> str:
        new = [row for row in job["people"] if row["state"] == "new"]
        lines = self._emails(person)
        lines += ["Leadpoet adds your booking link, if you've set one, and your legal footer.",
                  f"Approve these exact emails: /customers approve {self._person_code(job, person)}", "Leave them out: /customers skip"]
        if len(new) > 1:
            lines += [f"Read all {len(new)} and approve them together: /customers all"]
        return "\n".join(lines)

    def _all_emails(self, job: dict, new: list[dict]) -> str:
        """The code for approving everyone is shown only under everything it approves."""
        blocks = ["\n".join(self._emails(person)) for person in new]
        return "\n---\n\n".join(blocks) + f"\nApprove all {len(new)} exactly as shown: /customers all {self._all_code(job, new)}"

    # Approve and launch ----------------------------------------------------------------------
    def _approve(self, job: dict | None, code: str) -> str:
        if not job or job["stage"] != "reviewing":
            return self._next(job)
        person = next((row for row in job["people"] if row["state"] == "new"), None)
        if person is None or code != self._person_code(job, person):
            return "That code doesn't match these emails. Use the code shown here:\n\n" + self._next(job)
        person["state"] = "approved"
        self.save(job)
        return self._after_review(job)

    def _approve_all(self, job: dict | None, code: str) -> str:
        if not job or job["stage"] != "reviewing":
            return self._next(job)
        new = [row for row in job["people"] if row["state"] == "new"]
        if not new:
            return self._next(job)
        if not code:
            return self._all_emails(job, new)
        if code != self._all_code(job, new):
            return "That code doesn't match these emails. Read them again with /customers all"
        for person in new:
            person["state"] = "approved"
        self.save(job)
        return self._after_review(job)

    def _skip(self, job: dict | None) -> str:
        if not job or job["stage"] != "reviewing":
            return self._next(job)
        person = next((row for row in job["people"] if row["state"] == "new"), None)
        if person:
            person["state"] = "skipped"
            self.save(job)
        return self._after_review(job)

    def _after_review(self, job: dict) -> str:
        """Each approval starts straight away, so walking off halfway loses nothing."""
        if any(row["state"] == "approved" for row in job["people"]):
            self._launch_some(job)
        notes: list[str] = []
        try:
            notes = self._approve_matching(job)
        except ApiError:
            pass  # The next check approves anything still waiting.
        self._settle(job)
        if job["stage"] == "done":
            return "You skipped everyone, so nothing launches. /customers reset starts over."
        return "\n\n".join([part for part in (job.get("blocked"), self._next(job) if job["stage"] != "launching" or not job.get("blocked") else "") if part] + notes)

    def _settle(self, job: dict) -> None:
        """With nobody left to review, the job moves on, even if a crash interrupted the launch."""
        if any(row["state"] == "new" for row in job["people"]):
            return
        if not any(row["state"] in ("approved", "launched", "held") for row in job["people"]):
            job["stage"] = "done"
        else:
            job["stage"] = "live" if not any(row["state"] == "approved" for row in job["people"]) else "launching"
        self.save(job)

    def _senders(self) -> list[str]:
        return [row["id"] for row in self.api.email_accounts()
                if row.get("channel") == "email" and row.get("status") == "active"
                and not row.get("paused") and not row.get("sending_pause")][:MAX_SENDERS]

    def _launch_some(self, job: dict) -> None:
        job.pop("blocked", None)
        ready = [row for row in job["people"] if row["state"] == "approved"]
        try:
            senders = self._senders()
            if ready and not senders:
                raise _Blocked(f"Connect an email inbox in Leadpoet so these can go out:\n{OUTREACH_URL}")
            if ready:
                self._ensure_profile(job)
            for person in ready[:MAX_LAUNCHES_PER_RUN]:
                self._launch(job, person, senders)
                self.save(job)
        except _Blocked as blocked:
            job["blocked"] = str(blocked)
        except ApiError as error:
            if error.status == 429 or error.ambiguous:
                job["blocked"] = _SLOW_DOWN
            else:
                job["blocked"] = f"Leadpoet stopped this launch: {error.message}"
        if job["stage"] == "launching" and not any(row["state"] == "approved" for row in job["people"]):
            job["stage"] = "live"
        self.save(job)

    def _ensure_profile(self, job: dict) -> None:
        """Leadpoet refuses a launch with no workspace profile, and signing up doesn't make one."""
        if job.get("profile_ready"):
            return
        if (self.api.profile() or {}).get("profile") is None:
            self.api.create_empty_profile()
        job["profile_ready"] = True
        self.save(job)

    def _launch(self, job: dict, person: dict, senders: list[str]) -> None:
        key = f"hermes-customers-{job['id']}-{person['id']}"
        if not person.get("snapshot_id"):
            audience = self.api.create_audience({"lead_ids": [person["id"]], "destination_requirement": "email"}, f"{key}-audience")
            if int(audience.get("admitted") or 0) < 1:
                person["state"] = "held"
                person["held"] = "Leadpoet held this contact back. They may have opted out or been contacted recently."
                return
            person["snapshot_id"] = audience["snapshot_id"]
            self.save(job)
        if not person.get("campaign_id"):
            try:
                created = self.api.create_campaign(self._campaign_body(person, senders), f"{key}-campaign")
            except ApiError as error:
                if error.status not in (400, 422):
                    raise
                # One person's emails shouldn't hold up everyone behind them.
                person.update(state="held", held=error.message)
                return
            person["campaign_id"] = created["strategy_id"]
            self.save(job)
        if person.get("launch_sent") and self._already_launched(person):
            return
        proposal = self.api.contract_proposal(person["campaign_id"])
        if not proposal.get("ready"):
            raise _Blocked("Leadpoet needs more before launching: " + ", ".join(map(str, proposal.get("reasons") or ["unknown"])) + f"\n{OUTREACH_URL}")
        preview = self.api.campaign_preview(person["campaign_id"])
        if preview.get("missingSenderDetails"):
            raise _Blocked("Add your sender details in Leadpoet (the law needs them in cold email):\n"
                           + ", ".join(map(str, preview["missingSenderDetails"])) + f"\n{OUTREACH_URL}")
        person["launch_sent"] = True
        self.save(job)
        try:
            self.api.launch_campaign(person["campaign_id"], {"preview_version": preview.get("preview_version"),
                                                             "fit_confirmation": preview.get("fit_confirmation"),
                                                             "campaign_contract_input": proposal.get("contract")})
        except ApiError as error:
            if error.code != "ALREADY_AUTHORIZED":
                if error.code == "NO_ELIGIBLE_MEMBERS":
                    person["state"] = "held"
                    person["held"] = error.message
                    return
                raise  # Usually something the user can fix, like sender details. Their approval is kept.
        person.update(state="launched", approved_touches=[], approved_steps=[])

    def _already_launched(self, person: dict) -> bool:
        """A launch whose answer was lost may have worked, and launching twice is refused."""
        try:
            status = (self.api.get_campaign(person["campaign_id"]).get("strategy") or {}).get("status")
        except ApiError as error:
            if error.status != 404:
                raise
            person.update(state="held", held="That campaign no longer exists in Leadpoet.")
            return True
        if status and status != "draft":
            person.update(state="launched", approved_touches=[], approved_steps=[])
            return True
        return False

    def _campaign_body(self, person: dict, senders: list[str]) -> dict:
        touches = [{"id": f"email-{index}", "channel": "email", "delay_business_days": touch["delay"],
                    "subject": touch["subject"], "body": touch["body"]} for index, touch in enumerate(person["touches"], 1)]
        authoring = dict(_CAMPAIGN_SEED)
        authoring["style"] = {"client_brand": None, "sender_voice": None,
                              "campaign_tone": {"version": 1, "controls": dict(_STYLE)},
                              "channel_formatting": [{"channel": "email", "version": 1, "controls": dict(_STYLE)}],
                              "locale_language": {"version": 1, "locale": "en-US", "language": "English"},
                              "learned_preferences": None}
        authoring["sequence"] = {"schema_version": "outreach-custom-sequence-v1", "mode": "custom", "touches": touches, "stop_on_reply": True}
        return {"name": f"Hermes: {person['name']} at {person['company']}"[:120], "audience_snapshot_id": person["snapshot_id"],
                "allowed_account_ids": senders, "mode": "copilot", "advanced_settings": {"controlled_campaign": authoring}}

    def _approve_matching(self, job: dict | None) -> list[str]:
        """Approve only drafts whose text is what the person approved. Anything else stays in Review."""
        past = self._past()
        people = {row["id"]: row for row in [*past, *(job["people"] if job else [])] if row["state"] == "launched"}
        if not people:
            return []
        approvals, owners, steps, notes = [], {}, {}, []
        compliance: dict[str, dict] = {}
        for draft in self.api.review_drafts():
            person = people.get(str(draft.get("lead_id") or (draft.get("lead") or {}).get("id")))
            if person is None or draft.get("id") in person.get("approved_touches", []):
                continue
            if str(draft.get("id")) in person.get("refused", {}) and person["refused"][str(draft.get("id"))] == draft.get("review_revision"):
                continue  # Leadpoet already said no to this version. Asking again won't help.
            step = self._matching_step(person, draft, compliance)
            if step is None:
                if not person.get("mismatch_alerted"):
                    person["mismatch_alerted"] = True
                    notes.append(f"A draft for {person['name']} doesn't match what you approved, so I didn't approve it. It's in Leadpoet Review.")
                continue
            if not draft.get("can_approve") or step in person.get("approved_steps", []) or step in steps.get(person["id"], ()):
                continue
            binding = {name: draft.get(name) for name in ("quality_attestation_id", "review_revision", "intended_send_start", "intended_send_end")}
            if draft.get("transport_content_hash"):
                binding["transport_content_hash"] = draft["transport_content_hash"]
            approvals.append({"message_id": draft["id"], "kind": "message", "binding": binding})
            owners[draft["id"]] = (person, step, draft.get("review_revision"))
            steps.setdefault(person["id"], set()).add(step)
        approved = refused = 0
        for start in range(0, len(approvals), 50):
            batch = approvals[start:start + 50]
            results = self.api.approve_drafts(batch)
            for approval in batch:
                result = results.get(approval["message_id"]) or {}
                status = result.get("status") if isinstance(result, dict) else result
                person, step, revision = owners[approval["message_id"]]
                if status in ("preparing", "authorized"):
                    person.setdefault("approved_touches", []).append(approval["message_id"])
                    person.setdefault("approved_steps", []).append(step)
                    approved += 1
                elif status and not (isinstance(result, dict) and result.get("code") in _TRY_APPROVAL_AGAIN):
                    person.setdefault("refused", {})[approval["message_id"]] = revision
                    refused += 1
        if approved:
            noun = f"{approved} approved email{'s' if approved != 1 else ''}"
            notes.append(f"{noun} would go out now, but this is the demo, so nothing did. Now try a reply: /desk"
                         if getattr(self.api, "demo", False) else f"{noun} on the way. Replies come to /desk.")
        if refused:
            notes.append(f"Leadpoet wants a person to check {refused} email{'s' if refused != 1 else ''} first, so {'they are' if refused != 1 else 'it is'} waiting in Review:\n{OUTREACH_URL}")
        if job:
            self.save(job)
        if past:
            self.store.put("customers_past", json.dumps(past))
        return notes

    def _footer_ok(self, extra: str, compliance: dict[str, dict]) -> bool:
        if "known" not in compliance:
            compliance["known"] = ((self.api.profile() or {}).get("profile") or {}).get("compliance") or {}
        return footer_is_known(extra, compliance["known"])

    def _matching_step(self, person: dict, draft: dict, compliance: dict[str, dict]) -> int | None:
        """Which approved email this draft is, or None. Only Leadpoet's own footer may follow the text."""
        subject = _squash(draft.get("rendered_subject") or draft.get("subject")).lower()
        body = _squash(draft.get("rendered_body") or draft.get("body"))
        first_subject = _squash(person["touches"][0]["subject"]).lower()
        if not subject:
            return None
        for step, touch in enumerate(person["touches"]):
            approved = _squash(touch["body"])
            # A follow-up may reply in the first email's thread, so its subject can differ.
            subjects = {_squash(touch["subject"]).lower(), *([f"re: {first_subject}"] if step else [])}
            extra = body[len(approved):] if body.startswith(approved) else None
            link = booking_link(draft)
            if extra is not None and link and re.match(rf" {re.escape(link)}(?: |$)", extra):
                extra = extra[len(link) + 1:]
            if subject not in subjects:
                continue
            if extra is not None and (not extra or (extra[0] == " " and self._footer_ok(extra, compliance))):
                return step
            if self._matches_with_signature(touch, draft, body, compliance):
                return step
        return None

    def _matches_with_signature(self, touch: dict, draft: dict, body: str, compliance: dict[str, dict]) -> bool:
        """With a mailbox signature, Leadpoet swaps the sign-off for it. The rest of the text must be untouched."""
        editable = _squash(draft.get("editable_body")).removesuffix(f" {booking_link(draft)}")
        paragraphs = [part for part in re.split(r"\n\s*\n", touch["body"]) if part.strip()]
        without_sign_off = _squash("\n\n".join(paragraphs[:-1]))
        if not editable or editable not in (_squash(touch["body"]), without_sign_off) or not body.startswith(editable):
            return False
        self._footer_ok("", compliance)  # Loads the sender's details.
        return signature_then_footer(body[len(editable):], compliance["known"])

    # The scheduled check. It uses no model -------------------------------------------------------
    def advance(self) -> str:
        return self._locked(self._advance, "")

    def _advance(self) -> str:
        texts = [self._advance_one(), *(self._other(key)._advance_one(parked=True) for key in self._parked())]
        return "\n\n".join(text for text in texts if text)

    def _advance_one(self, parked: bool = False) -> str:
        job = self.job()
        if self.api is None or (parked and not job):
            return ""
        if not job:
            try:
                return "\n\n".join(self._approve_matching(None))
            except ApiError:
                return ""
        before, told = job["stage"], job.get("blocked")
        try:
            notes = self._refresh(job)
        except ApiError:
            return ""  # A temporary failure. The next check tries again.
        if job["stage"] != before and job["stage"] in ("ready", "failed", "live"):
            notes.insert(0, self._parked_news(job) if parked else self._next(job))
        elif job.get("blocked") and job["blocked"] != told and job["blocked"] != _SLOW_DOWN:
            notes.insert(0, job["blocked"])
        if parked and notes:
            return f"{_site(job)}: " + "\n\n".join(notes)
        return "\n\n".join(notes)

    def _parked_news(self, job: dict) -> str:
        """Commands in a background search's news must not act on the search that's open."""
        if job["stage"] == "ready":
            return (_arrivals(job["people"]) + "\n"
                    f"Open it to write their emails: /customers open {_site(job)}")
        return self._stage_text(job)

    def _refresh(self, job: dict) -> list[str]:
        if job["stage"] == "needs_credits":
            self._adopt(job)
        if job["stage"] == "finding":
            self._refresh_find(job)
        if job["stage"] in ("reviewing", "launching") and any(row["state"] == "approved" for row in job["people"]):
            self._launch_some(job)
        if job["stage"] == "reviewing":
            self._settle(job)
        return self._approve_matching(job) if job["stage"] in ("reviewing", "launching", "live") or self._past() else []

    # What to show ----------------------------------------------------------------------------
    def _next(self, job: dict | None, refresh: bool = False) -> str:
        if not job:
            return "Send your website to begin: /customers yoursite.com\nOr try it first: /customers demo"
        notes: list[str] = []
        if refresh and self.api is not None and job["stage"] in ("needs_credits", "finding", "reviewing", "launching", "live"):
            try:
                notes = self._refresh(job)
                job = self.job() or job
            except ApiError as error:
                return f"Leadpoet didn't respond as expected. {error.message}"
        return "\n\n".join(part for part in (*notes, self._stage_text(job)) if part)

    def _stage_text(self, job: dict) -> str:
        stage = job["stage"]
        if stage == "scanned":
            return (briefs.card(job["brief"]) + "\n\n" + self._offer(job))
        if stage == "needs_key":
            return ("Finding real contacts needs a free Leadpoet account. New accounts get 100 free credits, "
                    "enough for 10 matching companies with verified contacts.\n"
                    f"1. Sign up: https://leadpoet.com\n2. Create an API key in Settings, Developer, with these permissions: {', '.join(SCOPE_NAMES.values())}\n"
                    "3. Add it as LEADPOET_API_KEY in Hermes\n"
                    f"Then: /customers go {self._brief_code(job)}")
        if stage == "needs_credits":
            if job.get("other_request"):
                return ("I see a new search in Leadpoet, but its text isn't the one I gave you, so I didn't assume it's this one.\n"
                        f"If it is: /customers use {code_for(job['id'], job['other_request']['id'])}")
            if job.get("balance"):
                return (f"Your balance of {job['balance']} credits doesn't fit a {COMPANIES}-company search, the smallest Leadpoet runs.\n"
                        f"Add credits here: {LEAD_ENGINE_URL}\nThen: /customers go {self._brief_code(job)}")
            return ("New accounts get 100 free credits with their first request, which Leadpoet takes in the app to keep bots out.\n"
                    f"Paste this there: {LEAD_ENGINE_URL}\n\n{briefs.description(job['brief'])}\n\n"
                    "I'll pick it up from there and ping you. /customers check looks now.")
        if stage == "finding" and getattr(self.api, "demo", False):
            return ("For real, this is where Leadpoet takes 1 to 2 days to find and verify every contact, and Hermes pings you when they land. "
                    "The demo skips the wait. Send /customers")
        if stage == "finding" and job.get("delivered_empty"):
            return "Leadpoet finished, but I can't see a contact with an email yet. Check Pipeline in Leadpoet. /customers check looks again."
        if stage == "finding":
            checked = "against your profile and for a recent buying signal" if briefs.signals_text(job["brief"]) else "against your profile"
            return (f"Leadpoet is on it. Each company is checked {checked}, "
                    "and every contact is verified, so this usually takes 1 to 2 days.\n\n"
                    "I'll message you here the moment they're in. Waiting costs no tokens.")
        if stage == "ready":
            return _arrivals(job["people"]) + "\nWrite their emails: /customers write"
        if stage == "writing":
            return "Still researching. /customers write continues."
        if stage == "reviewing":
            person = next((row for row in job["people"] if row["state"] == "new"), None)
            return self._card(job, person) if person else "No one is left to review. /customers reset starts a new one."
        if stage == "launching":
            done = sum(1 for row in job["people"] if row["state"] == "launched")
            total = sum(1 for row in job["people"] if row["state"] in ("approved", "launched"))
            return job.get("blocked") or f"Launching: {done} of {total} ready. I'll carry on and ping you."
        if stage == "live":
            launched = sum(1 for row in job["people"] if row["state"] == "launched")
            held = [row for row in job["people"] if row["state"] == "held"]
            if not launched:
                return "Leadpoet's safety checks held everyone back, so nothing launched. They may have opted out or been contacted recently."
            text = f"Live. {launched} sequence{'s' if launched != 1 else ''} running, stopped by any reply. Answer them with /desk."
            return text + (f"\n{len(held)} held back by Leadpoet's safety checks." if held else "")
        if stage == "failed":
            return f"{job.get('failure')} No more credits will be used. /customers reset starts over."
        return "All done. /customers reset starts a new one."

    def _offer(self, job: dict) -> str:
        code = self._brief_code(job)
        if self.api is None:
            return (f"Next, find {COMPANIES} companies with verified contacts.\n"
                    "New Leadpoet accounts get 100 free credits, enough for this.\n\n"
                    f"To confirm, reply:\n/customers go {code}")
        if getattr(self.api, "demo", False):
            return f"See how it finds prospects, using 3 made-up companies: /customers go {code}"
        try:
            price = int((self.api.contact_settings().get("pricing") or {}).get("credits_per_company") or 0)
            balance = int(self.api.credits().get("credits_remaining") or 0)
        except (ApiError, TypeError, ValueError):
            price = balance = 0
        if price <= 0:
            return f"Next, find {COMPANIES} companies with verified contacts.\n\nTo confirm, reply:\n/customers go {code}"
        cost = COMPANIES * price
        job["quoted_credits"] = cost
        self.save(job)
        return (f"Next, find {COMPANIES} companies with verified contacts.\n"
                f"Cost: {cost:,} credits. You have {balance:,}.\n\n"
                f"To confirm, reply:\n/customers go {code}")

    def _open(self, job: dict | None, site: str) -> str:
        wanted = re.sub(r"^https?://(www\.)?", "", site.strip().lower()).split("/")[0]
        others = [(key, self._other(key).job()) for key in self._parked()]
        match = next(((key, other) for key, other in others if other and wanted and _site(other).lower() == wanted), None)
        if match is None:
            names = ", ".join(_site(other) for _, other in others if other)
            return f"Your other searches: {names}\nOpen one: /customers open <site>" if names else "You have no other searches. Start one: /customers yoursite.com"
        key, target = match
        self.store.put(_PARKED, json.dumps([k for k in self._parked() if k != key]))
        self._other(key).save(None)
        if job and job["stage"] != "done":
            self._park(job)  # Switching never loses a search, even one that hasn't spent anything.
        self.save(target)
        return self._next(target)

    def _listing(self, job: dict | None) -> str:
        others = [other for other in (self._other(key).job() for key in self._parked()) if other]
        here = self._people(job)
        if not others:
            return here
        labels = {"scanned": "ready to confirm", "needs_key": "needs a Leadpoet key", "needs_credits": "waiting for credits", "finding": "finding contacts", "ready": "contacts are in",
                  "writing": "writing", "reviewing": "waiting for you", "launching": "launching", "live": "live",
                  "failed": "stopped", "done": "done"}
        lines = [f"{_site(other)}: {labels.get(other['stage'], other['stage'])}" for other in others]
        return (f"{here}\n\nOther searches:\n" + "\n".join(lines) + "\nOpen one: /customers open <site>")

    def _people(self, job: dict | None) -> str:
        if not job or not job["people"]:
            return self._next(job)
        labels = {"research": "researching", "new": "waiting for you", "approved": "approved", "skipped": "skipped", "launched": "running", "held": "held back"}
        return "\n".join(f"{row['name']}, {row['company']}: {labels.get(row['state'], row['state'])}"
                         f"{'' if row['lines'] else ' (no personal line)'}" for row in job["people"])


def _site(job: dict) -> str:
    brief = job.get("brief") or {}
    domain = str(brief.get("domain") or "")
    return domain if domain and domain != "your-company" else str(brief.get("product_name") or job["id"])


def _timestamp(value: Any) -> float | None:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
