# Leadpoet for Hermes

Send your website. Get customers.

```
/customers yoursite.com
```

**Find.** Hermes reads your site and works out who should buy. Every fact it uses comes with the quote and page that prove it. Leadpoet then finds verified contacts.

**Write.** Each person gets three emails in your voice. Hermes reads their company's site for something specific and quotes it. If the quote isn't really on that page, the line is thrown out. No source means no line.

**Launch.** You see each person's exact emails on your phone, with sources. One short code approves them.

**Answer.** Replies come back to `/desk`. Hermes drafts, you send.

## Try it now, with no account

```
/customers demo
```

It runs the whole job with fictional companies. Nothing can reach anyone, and it needs no key. One of the fictional lines is wrong on purpose, so you can watch it get thrown out.

Want to see it read your real site? `/customers demo yoursite.com`. The brief is real. The prospects are made up.

## Free to start

10 companies with verified contacts, email or LinkedIn outreach from one sender, and a CRM pipeline in Leadpoet that tracks every contact and reply. No card needed.

New Leadpoet accounts get 100 free credits, enough for those 10 companies. Hermes hands you the prepared request and picks it up from there.

## Install

```
hermes plugins install https://github.com/leadpoet/get-customers
hermes plugins enable leadpoet
```

Restart the gateway if it's running, so your phone sees `/customers`: `hermes gateway restart`.

For real contacts, create a key in [Leadpoet](https://leadpoet.com) under Settings, Developer, and add it to Hermes as `LEADPOET_API_KEY`.

Then turn on pings, so Hermes tells you when contacts land and replies arrive:

```
hermes leadpoet watch --every 5m --deliver telegram
```

It prints one `hermes cron create` line to run. The check uses no model, so it costs no tokens.

## Commands

| Command | What it does |
| --- | --- |
| `/customers yoursite.com` | Start a search. One already finding keeps running |
| `/customers` | Show the next step |
| `/customers voice <text>` | Match a few lines you really wrote |
| `/customers list` | See everyone, and your other searches |
| `/customers open <site>` | Switch to another search |
| `/customers all` | Read every email, then approve them together |
| `/customers check` | Refresh now |
| `/customers reset` | Start over |
| `/desk` | Answer the next reply |

## What keeps it safe

- **The agent can't spend, launch or send.** It can read progress and suggest reply text. There is no tool that does more. Those steps need you to type a code.
- **A code approves exact text.** It's a fingerprint of the words. Change a word and the old code stops working.
- **Later emails need the same text.** Hermes approves a queued email only when it matches what you approved. Leadpoet adds your booking link, if you've set one, and your legal footer. Anything else waits in Leadpoet Review.
- **A quote has to back up the sentence.** Numbers and names must be in the quote, and so must most of the other words. A line can't drop a "not", hidden page text doesn't count, and lawsuits, layoffs and the like never go in an opener.
- **Website text is treated as data.** Pages can't add links or web addresses to your emails.
- **No double charge.** A retry repeats the same request with the same key, and if Hermes can't confirm a search went through, it says so before you start another.
- **No private addresses.** The site reader checks every redirect and refuses anything not on the public internet.
- **Leadpoet still checks everything.** Opt-outs, send limits, and stop-on-reply all stay enforced on the server.
- **Group chats are off by default.** Add user ids to `allowed_user_ids` to allow them.

## What it touches

- **Network.** It calls the Leadpoet API at `leadpoet.com` with your key, and reads public websites you or your prospects own to find facts to quote. It refuses private addresses.
- **Your model.** Site text and reply text go to the model Hermes is set up to use.
- **Credentials.** It uses `LEADPOET_API_KEY` through Hermes. It stores no other secrets.
- **Local data.** Job state and replies are kept in the plugin's own Hermes data folder.
- **Background work.** Only if you set up `hermes leadpoet watch`. It writes one small script to your Hermes `scripts` folder and prints the `hermes cron create` line for you to run. Each run calls `hermes leadpoet check`. The plugin sends no telemetry.
- **Who can act.** Direct messages use your Hermes gateway's own allowlist. In a group chat, only ids in `allowed_user_ids` can use `/customers` or `/desk`. The model's tools can read a reply and save a draft, but only your own command can approve or send.

## Good to know

- The brief names one segment, its buyers and one or two buying signals a researcher can check, such as a posted role or a new office. Leadpoet looks for companies that show one recently.
- Finding contacts usually takes 1 to 2 days. Each company is checked against your profile and its buying signal, and every contact is verified. Hermes messages you when they land.
- A search is 10 companies, the smallest Leadpoet runs, which is exactly what the free credits buy.
- Each person becomes their own small campaign in Leadpoet, because every email is different.
- Sending needs a connected inbox and your sender details in Leadpoet. Hermes tells you if either is missing.
- Sites that block readers or only render in a browser can't be read. Describe the company with `/customers about <text>` instead.

MIT licensed.
