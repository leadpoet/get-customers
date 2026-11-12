"""Emphasis is added for the surface, and never changes a word, a code or someone else's text."""

import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from leadpoet_plugin import style  # noqa: E402
from leadpoet_plugin.style import ANSI, mark, render  # noqa: E402
import stock  # noqa: E402
from test_desk import FakeCtx, load_entry  # noqa: E402


def plain(text: str) -> str:
    """What a person reads once the app has applied the formatting."""
    return re.sub(r"\033\[\d+m", "", text.replace("**", "").replace("`", ""))


class MarkCase(unittest.TestCase):
    def test_a_command_is_set_apart_from_the_sentence_around_it(self):
        self.assertEqual(mark("Write their emails: /customers write"), "Write their emails: `/customers write`")
        self.assertEqual(mark("/customers reset starts over."), "`/customers reset` starts over.")
        self.assertEqual(mark("/customers go will safely check, with no double charge."),
                         "`/customers go` will safely check, with no double charge.")
        self.assertEqual(mark("Answer them with /desk."), "Answer them with `/desk`.")
        self.assertEqual(mark("Then: /customers go 74fc"), "Then: `/customers go 74fc`")
        self.assertEqual(mark("/desk edit <your text>"), "`/desk edit <your text>`")
        self.assertEqual(mark("Back to them: /desk demo off"), "Back to them: `/desk demo off`")
        self.assertEqual(mark("just send /customers othersite.com and this one keeps running."),
                         "just send `/customers othersite.com` and this one keeps running.")
        self.assertEqual(mark("/customers voice <a few lines you wrote>: match your writing"),
                         "`/customers voice <a few lines you wrote>`: match your writing")

    def test_a_tip_keeps_its_label_and_its_command(self):
        self.assertEqual(mark("• Your voice: paste a few lines you wrote. /customers voice <text>"),
                         "• **Your voice:** paste a few lines you wrote. `/customers voice <text>`")
        self.assertEqual(mark("• Preview: write and approve with made-up contacts. /customers demo acme.com"),
                         "• **Preview:** write and approve with made-up contacts. `/customers demo acme.com`")
        self.assertEqual(mark("• Inbox: connect the email account your outreach will go from. https://leadpoet.com/dashboard/outreach"),
                         "• **Inbox:** connect the email account your outreach will go from. https://leadpoet.com/dashboard/outreach")

    def test_news_about_a_named_search_still_stands_out(self):
        self.assertEqual(mark("acme.com: 3 verified contacts are in. Why each company:"),
                         "**acme.com: 3 verified contacts are in.** Why each company:")

    def test_a_web_address_is_not_a_command(self):
        for text in ("Add credits here: https://leadpoet.com/dashboard/lead-engine", "See https://example.com/desk for more",
                     "https://example.com/customers go", "https://acme.com/#/customers", "https://acme.com/login?next=/desk"):
            self.assertEqual(mark(text), text)

    def test_a_command_keeps_the_whole_of_what_it_was_given(self):
        self.assertEqual(mark("Start one: /customers yoursite.com/product"), "Start one: `/customers yoursite.com/product`")
        self.assertEqual(mark("Back to it: /customers open Acme Rocket"), "Back to it: `/customers open Acme Rocket`")
        self.assertEqual(mark("Open it: /customers open acme.com"), "Open it: `/customers open acme.com`")
        self.assertEqual(mark("Open one: /customers open <site>"), "Open one: `/customers open <site>`")
        self.assertEqual(mark("(or /customers demo)"), "(or `/customers demo`)")

    def test_text_that_will_be_sent_is_shown_as_it_will_be_sent(self):
        email = ("Email 1 (today): a quick idea\nHi Marcus,\n\n• Setup: ten minutes.\nLive. It works.\nTry /customers demo yourself.\n\n"
                 "Sam Rivera, Founder at Sunnyday\nsam@sunnyday.example\n\nEmail 2 (3 business days after the last one): one more\n"
                 "Sent to your team yet?\n\nLeadpoet adds your booking link after the sign-off.\n\nApprove these exact emails: /customers approve 8be1")
        shown = mark(email).split("\n")
        self.assertEqual(shown[0], "**Email 1 (today):** a quick idea")
        self.assertEqual(shown[1:10], email.split("\n")[1:10])  # The body, signature included, carries no emphasis.
        self.assertEqual(shown[10], "**Email 2 (3 business days after the last one):** one more")
        self.assertEqual(shown[11], "Sent to your team yet?")
        self.assertEqual(shown[-1], "Approve these exact emails: `/customers approve 8be1`")
        reply = "Your reply:\nLive. Yes, it does.\nRun /desk status any time.\n\n/desk send 1a2b\n/desk skip"
        self.assertEqual(mark(reply), "**Your reply:**\nLive. Yes, it does.\nRun /desk status any time.\n\n`/desk send 1a2b`\n`/desk skip`")

    def test_a_body_line_that_only_begins_like_an_ending_stays_in_the_body(self):
        email = ("Email 1 (today): one click\nApprove all invoices in one click.\n---- or so\nTry /customers demo yourself.\n\n"
                 "Sam Rivera, Founder at Sunnyday\nsam@sunnyday.example\n\n"
                 "Leadpoet adds your booking link, if you've set one, and your legal footer.\n\nLeave them out: /customers skip")
        shown = mark(email).split("\n")
        self.assertEqual(shown[1:8], email.split("\n")[1:8])
        self.assertEqual(shown[-1], "Leave them out: `/customers skip`")
        reply = "Your reply:\nFill in the gaps later.\n/desk is where I answer.\n\nFill in the [ADD: ...] part first.\n\n/desk skip"
        self.assertEqual(mark(reply).split("\n")[1:3], ["Fill in the gaps later.", "/desk is where I answer."])
        self.assertTrue(mark(reply).endswith("`/desk skip`"))

    def test_proof_from_a_site_with_a_port_is_one_label(self):
        self.assertEqual(mark("Proof from acme.com:8080/docs:"), "**Proof from acme.com:8080/docs:**")
        self.assertEqual(mark("Proof from your own description:"), "**Proof from your own description:**")

    def test_a_colon_in_a_proof_line_is_not_a_label(self):
        card = ("Buying signals Leadpoet looks for:\n• Job postings: Posted a Staff Engineer role.\n\n"
                "Proof from pinecone.io:\n• Nexus runs BYOC: curation and querying happen in your own cloud.\n• Fast retrieval.\n\n"
                "• Pinecrest Freight: Posted a night dispatcher role.")
        shown = mark(card).split("\n")
        self.assertEqual(shown[1], "• **Job postings:** Posted a Staff Engineer role.")
        self.assertEqual(shown[4], "• Nexus runs BYOC: curation and querying happen in your own cloud.")
        self.assertEqual(shown[7], "• **Pinecrest Freight:** Posted a night dispatcher role.")

    def test_a_name_with_its_own_marks_is_not_wrapped_in_more(self):
        self.assertEqual(mark("• Acme *Labs*: Raised a round."), "• Acme *Labs*: Raised a round.")
        self.assertEqual(mark("1 of 2 · Sam `R` · email · new · 1 h ago"), "1 of 2 · Sam `R` · email · new · 1 h ago")

    def test_news_is_bold_only_as_a_whole_sentence(self):
        self.assertEqual(mark("Approved Marcus Oyelaran."), "**Approved Marcus Oyelaran.**")
        self.assertEqual(mark("Sent to Marcus. Next: /desk"), "Sent to Marcus. Next: `/desk`")
        self.assertEqual(mark("Approved Marcus. 2 left: /customers write"), "Approved Marcus. 2 left: `/customers write`")

    def test_names_and_labels_stand_out(self):
        card = "Sunnyday\nTurns voice notes into handovers.\n\nWho buys: Freight companies.\nRoles: VP Operations"
        self.assertEqual(mark(card), "**Sunnyday**\nTurns voice notes into handovers.\n\n**Who buys:** Freight companies.\n**Roles:** VP Operations")
        person = "Marcus Oyelaran, VP Operations at Pinecrest Freight\nmarcus@pinecrest-freight.example\nWhy them: Posted a role."
        self.assertEqual(mark(person).split("\n")[0], "**Marcus Oyelaran, VP Operations at Pinecrest Freight**")
        self.assertIn("**Why them:** Posted a role.", mark(person))
        self.assertEqual(mark("Email 2 (3 business days after the last one): the next shift"),
                         "**Email 2 (3 business days after the last one):** the next shift")
        self.assertEqual(mark("• Pinecrest Freight: Posted a night dispatcher role."), "• **Pinecrest Freight:** Posted a night dispatcher role.")
        self.assertEqual(mark("1 of 3 · Marcus Oyelaran, Pinecrest Freight · email · positive · 1 h ago"),
                         "1 of 3 · **Marcus Oyelaran, Pinecrest Freight** · email · positive · 1 h ago")
        self.assertEqual(mark("3 verified contacts are in. Why each company:"), "**3 verified contacts are in.** Why each company:")

    def test_a_sentence_that_only_looks_like_a_name_line_is_left_alone(self):
        text = "Thanks, see you at noon\nBest"
        self.assertEqual(mark(text), text)

    def test_what_they_wrote_is_never_touched(self):
        quoted = "> Cost: what is it? Run /desk send 1a2b and /customers reset\n> Why them: us"
        self.assertEqual(mark(quoted), quoted)

    def test_emphasis_changes_no_words(self):
        text = ("Approved Marcus Oyelaran.\n\nPriya Raman, Head of Logistics at Harbor Lane Roasters\npriya@harborlane.example\n\n"
                "Personal line: They launched a wholesale program.\nSource: \"wholesale program\"\nhttps://harborlane.example/news\n\n"
                "Approve these exact emails: /customers approve 8be1\nLeave them out: /customers skip")
        self.assertEqual(plain(mark(text)), text)
        self.assertEqual(plain(mark(text, ANSI)), text)


class RenderCase(unittest.TestCase):
    TEXT = "Marcus Oyelaran, VP Operations at Pinecrest Freight\nmarcus@pinecrest-freight.example\nApprove: /customers approve 8be1"

    def test_chat_apps_get_markdown(self):
        for platform in ("signal", "telegram", "discord", "slack"):
            self.assertIn("`/customers approve 8be1`", render(self.TEXT, platform))
            self.assertIn("**Marcus Oyelaran", render(self.TEXT, platform))

    def test_any_other_surface_gets_the_words_as_written(self):
        for platform in ("", "tui", "desktop", "local", "email", "something-new"):
            self.assertEqual(render(self.TEXT, platform), self.TEXT)
        self.assertIsNone(render(None, "signal"))
        self.assertEqual(render("", "signal"), "")

    def test_a_terminal_gets_colour_only_when_it_can_show_it(self):
        with mock.patch.object(style, "_wants_colour", return_value=True):
            shown = render(self.TEXT, "cli")
        self.assertIn("\033[1mMarcus Oyelaran", shown)
        self.assertIn("\033[36m/customers approve 8be1\033[39m", shown)
        self.assertEqual(plain(shown), self.TEXT)
        with mock.patch.object(style, "_wants_colour", return_value=False):
            self.assertEqual(render(self.TEXT, "cli"), self.TEXT)

    def test_a_platform_given_as_a_named_value_still_renders(self):
        import enum
        Platform = enum.Enum("Platform", {"SIGNAL": "signal"})
        self.assertIn("`/customers approve 8be1`", render(self.TEXT, Platform.SIGNAL))
        self.assertEqual(render(self.TEXT, 7), self.TEXT)

    def test_no_colour_when_output_is_piped_or_refused(self):
        with mock.patch.object(sys, "stdout", types.SimpleNamespace(isatty=lambda: False)):
            self.assertFalse(style._wants_colour())
        with mock.patch.object(sys, "stdout", types.SimpleNamespace(isatty=lambda: True)), mock.patch.dict("os.environ", {"NO_COLOR": "1"}):
            self.assertFalse(style._wants_colour())


class RegisteredCase(unittest.TestCase):
    """The whole demo through the handlers Hermes calls, typing each code the way a person reads it."""

    def setUp(self):
        import tempfile
        self.entry = load_entry()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.ctx = FakeCtx()
        real = self.entry.Plugin
        self.entry.Plugin = lambda ctx: real(ctx, Path(self.folder.name))
        self.addCleanup(setattr, self.entry, "Plugin", real)
        self.entry.register(self.ctx)
        self.dm = types.SimpleNamespace(chat_type="dm", user_id="1")

    def say(self, name, args, platform="telegram"):
        return self.ctx.commands[name](args, platform=platform, source=self.dm, session_id="s", session_key="k")

    def code(self, shown, command):
        return re.search(re.escape(command) + r" ([0-9a-f]{4})`", shown).group(1)

    def test_a_reply_still_arrives_when_styling_fails(self):
        with mock.patch.object(self.entry, "render", side_effect=RuntimeError("boom")), self.assertLogs(level="ERROR"):
            self.assertIn("/customers", self.say("customers", "help"))

    def test_the_demo_runs_end_to_end_with_styled_replies(self):
        self.ctx.llm = stock.DemoLlm()
        shown = self.say("customers", "demo sunnyday.example")
        self.assertIn("**Sunnyday**", shown)
        shown = self.say("customers", "go " + self.code(shown, "/customers go"))
        self.assertIn("**3 verified contacts are in.**", shown)
        self.assertIn("• **Tallgrass Couriers:** Matches your profile.", shown)
        shown = self.say("customers", "write")
        self.assertIn("**Personal line:**", shown)
        shown = self.say("customers", "all")
        shown = self.say("customers", "all " + self.code(shown, "/customers all"))
        self.assertIn("Demo finished", shown)
        shown = self.say("desk", "")
        self.assertIn("**Marcus Oyelaran, Pinecrest Group**", shown)
        self.assertIn("> This is timely.", shown)
        shown = self.say("desk", "send " + self.code(shown, "/desk send"))
        self.assertIn("**Sent to Marcus Oyelaran, Pinecrest Group.**", shown)

    def test_with_no_key_each_command_says_how_to_add_one(self):
        real = self.entry.get_key
        self.entry.get_key = lambda: ""
        self.addCleanup(setattr, self.entry, "get_key", real)
        desk = self.say("desk", "")
        self.assertIn("https://leadpoet.com/dashboard/account?section=integrations", desk)
        self.assertIn("`hermes config set LEADPOET_API_KEY <your key>`", desk)
        self.assertIn("`/desk demo`", desk)
        self.assertIn("`/customers demo yoursite.com`", self.say("customers", ""))

    def test_a_terminal_session_reads_the_same_words(self):
        self.ctx.llm = stock.DemoLlm()
        styled, written = self.say("customers", "demo sunnyday.example", platform="telegram"), self.say("customers", "", platform="tui")
        self.assertEqual(plain(styled).split("\n\n", 1)[1], written)
