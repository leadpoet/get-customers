# Leadpoet for Hermes

Send your website. Get customers.

```
/customers yoursite.com
```

**Find.** Hermes reads your site and fills Leadpoet's full lead request: who buys, the roles that decide, and the buying signals that say now is the time. Every fact it uses comes with the quote and page that prove it. Leadpoet then finds verified contacts at companies showing a recent signal.

**Write.** Each person gets three emails in your voice. The first opens on the buying signal Leadpoet found, such as a role they just posted. Without one, Hermes reads their company's site for something specific and quotes it. If the quote isn't really on that page, the line is thrown out. No source means no line.

**Launch.** You see each person's exact emails on your phone, with sources. One short code approves them.

**Answer.** Replies come back to `/desk`. Hermes drafts, you send.

## Try it now, with no account

```
/customers demo
```

It runs the whole job with fictional companies. Nothing can reach anyone, and it needs no key. One of the fictional lines is wrong on purpose, so you can watch it get thrown out.

Want to see it read your real site? `/customers demo yoursite.com`. The brief is real. The prospects are made up.

## Try it for real

10 companies with verified contacts, email or LinkedIn outreach from one sender, and a CRM pipeline in Leadpoet that tracks every contact and reply. No card needed.

A new free account's 100 credits cover those 10 companies. Leadpoet takes that request in its app, so Hermes hands you the text for each box, then picks the search up and pings you when your contacts land.

## Install

Leadpoet is in the Hermes plugin catalog:

```
hermes plugins install leadpoet
hermes plugins enable leadpoet
```

The catalog pins a reviewed release, so it can trail this repo by a version. For the newest one, or on Hermes 0.23.0 without `hermes update`, install from the repo:

```
hermes plugins install https://github.com/leadpoet/get-customers
hermes plugins enable leadpoet
```

Restart the gateway if it's running, so your phone sees `/customers`: `hermes gateway restart`.

For real contacts, add a Leadpoet API key. The account is free:

1. Sign up at [leadpoet.com/auth](https://leadpoet.com/auth).
2. Open [Integrations](https://leadpoet.com/dashboard/account?section=integrations), then Developer, API keys, Create API key. Name it, choose Select all, and copy the key.
3. Add it where Hermes runs:

```
hermes config set LEADPOET_API_KEY <your key>
```

Hermes picks the key up on your next message. If you skip this, `/customers` and `/desk` show these same steps when a key is needed.

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
| `/customers buyer <who buys>` | Change the buyer on your card, before anything is spent |
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

- If the card names a buyer you don't sell to, `/customers buyer <who buys>` reads the site again for that buyer. Your words set the buyer, the roles and the signals. Every fact still has to be on your site.
- The brief names one kind of buyer, the roles that decide, and 3 to 5 buying signals, at most one from each place a researcher can check: job postings, funding and news, technology, the company's own site, and community or review sites. Each has a time window, such as a role posted in the past 30 days. Guesses like "is evaluating tools" are thrown out.
- Finding contacts usually takes 1 to 2 days. Each company is checked against your profile and its buying signal, and every contact is verified. Hermes messages you when they land, with Leadpoet's note on why each company fits.
- A search is 10 companies, the smallest Leadpoet runs, which is exactly what the free credits buy.
- Each person becomes their own small campaign in Leadpoet, because every email is different.
- Sending needs a connected inbox and your sender details in Leadpoet. Hermes tells you if either is missing.
- Sites that block readers or only render in a browser can't be read. Describe the company with `/customers about <text>` instead.

MIT licensed.
