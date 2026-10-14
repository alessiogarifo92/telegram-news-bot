# News Digest Bot — Telegram

Sends a daily news digest to Telegram, in this priority order: Netherlands
(NOS) → Italy (ANSA) → World (BBC) → AI (The Decoder + The Verge AI +
arXiv papers) → Tech (TechCrunch + The Verge + Ars Technica + The Guardian
+ Hacker News) → Finance (Bloomberg Markets). AI and Tech sit next to each
other on purpose; Finance is always last.

**Amsterdam (AT5) was removed on 2026-08-29.** `https://www.at5.nl/rss`
answers HTTP 200 with an HTML page where the feed used to be, and neither
that page nor `at5.nl` advertises a feed any more, so there is no
replacement URL to switch to — the entry would have failed silently every
morning. Amsterdam stories of national weight still come through NOS. If
AT5 publishes a feed again, the entry goes back in `FEEDS`, where a comment
marks the spot.

The Verge appears twice on purpose: its AI section under AI, and its main
feed under Tech, so its reporting outside AI is not lost. Stories that show
up in both go out once — the second copy is dropped by the same
deduplication that handles two outlets running the same story.

This is **not** a listening bot. It is a script that runs once and exits.
In production it is started once a day by GitHub Actions — see
[How it is set up right now](#how-it-is-set-up-right-now).

**One notification per day.** Every headline is still sent as its own
Telegram message, so each link gets its own preview (and Instant View where
the site supports it). But only the very first message of the digest makes
your phone buzz — everything after it is sent with `disable_notification`.
You see all the news separated in the chat, and the phone rings once.

**Optional "folders".** Set `TELEGRAM_TOPICS=true` and point
`TELEGRAM_CHAT_ID` at a supergroup with Topics enabled, and the digest is
split across three threads — 📰 Generali, 🤖 Tech & AI, 💰 Finanza — each
mutable on its own. See [Folders (Telegram Topics)](#folders-telegram-topics).

---

## What a story looks like in the chat

Each story is laid out like a news channel post, not a bare headline:

```
━━━━━━━━━━━━
🇳🇱 OLANDA — GENERALE          ← section header: rule + name in bold
NOS · 3 notizie                  ← outlet and how many stories follow
```

```
   ┌───────────────────────┐
   │  article picture      │     ← Telegram's own link preview, asked for
   └───────────────────────┘        large and ABOVE the text

Politie houdt 40 mensen aan…     ← title, in bold
🇬🇧 Police arrest 40 people…      ← English translation (Dutch sources only)

Vrijdagmiddag was Brussel het    ← excerpt from the feed's own description,
toneel van een grote operatie…      cleaned up and cut at a word boundary

   [ 📖 Leggi su NOS ]           ← button, the way out to the article
```

The picture on top comes from `link_preview_options`
(`prefer_large_media` + `show_above_text`), so nothing is uploaded — it is
the preview Telegram already builds for that URL, just placed above the
text instead of below it. Messages are sent with `parse_mode="HTML"`; feed
text is HTML-escaped before it goes in.

The link is **not** in the text: it sits in an inline button under the
message (`reply_markup`), which is a real tap target and keeps the message
down to picture, title and excerpt. **Instant View** cannot be requested
through the Bot API — Telegram attaches its own "Instant View" button to
the preview when the site has a template for it, and the button below is
there either way. Hacker News stories get a second button for the
discussion thread.

The excerpt is the feed's own `description`, stripped of tags and of the
boilerplate feeds append to it ("The post … appeared first on …",
"Continua a leggere", "Lees meer"), then truncated at a word boundary to
`SUMMARY_MAX_CHARS` (320 by default; set it to `0` to go back to
titles-only messages). A description that merely repeats the title is
dropped rather than printed twice. Hacker News posts have no description,
so they get a points/comments line and a second button for the discussion
instead.

---

## How it works

Each run goes through four stages:

1. **Fetch** — every entry in the `FEEDS` list in `news_digest_bot.py` is
   pulled and parsed. Hacker News is the exception: it uses the official
   Firebase API instead of RSS. Entries older than `HOURS_LOOKBACK` are
   dropped.
2. **Deduplicate** — an item is discarded if its link was already sent, or
   if its title is at least `DUPLICATE_TITLE_THRESHOLD` similar (0–1,
   default 0.6) to one already sent. This runs across the whole digest, so
   the same story reported by The Decoder and The Verge only goes out once.
   It also runs **across days** — see
   [What counts as "new"](#what-counts-as-new).
3. **Cap** — at most `MAX_ITEMS_PER_CATEGORY` surviving items per category
   are kept (10 by default). It is a ceiling, not a quota: a category with
   four fresh stories sends four. Every configured feed is already that
   outlet's own "top stories" selection rather than a raw chronological
   list, so the whole window is worth reading. The cap is applied *after*
   deduplication, so a unique item is never dropped just because a
   duplicate happened to come before it.
4. **Send** — one Telegram message per headline, ~1s apart, with retry on
   HTTP 429 (rate limit), each formatted as the card described in
   [What a story looks like in the chat](#what-a-story-looks-like-in-the-chat).
   Dutch stories (NOS) are sent with both the original title and an
   English translation — and, unless `TRANSLATE_SUMMARIES=0`, with the
   excerpt translated too.

**Translation fallback chain** for Dutch headlines: Google Translate →
MyMemory → DeepL. The first two are free and need no key, but share public
quotas that run out periodically. DeepL is optional and much more reliable;
it is skipped entirely if `DEEPL_API_KEY` is not set. If all three fail the
message still goes out, with the original Dutch title only.

Nothing in the pipeline is allowed to abort the digest: a dead feed, a
failed translation, or an unwritable state file produces a warning in the
log and the run continues.

---

## How it is set up right now

The bot runs on **GitHub Actions** in this private repository. No server,
no VPS, no machine that has to stay on. Two workflows:

### `.github/workflows/daily-digest.yml`

Sends the digest. Schedule: `17 6 * * *` (06:17 UTC = 08:17 CEST in summer,
07:17 CET in winter).

Two deliberate choices in that cron expression:

- **Morning.** The 30-hour window closing at that time already contains the
  entire evening peak of US AI/tech announcements from the day before
  (which tails off around 23:00 CET). Nothing is missed by running early —
  the news simply arrives together at breakfast instead of scattered
  through the day.
- **`:17`, not `:00`.** GitHub queues scheduled workflows across all
  repositories, and round times are the most congested. An odd minute
  measurably reduces the typical delay.

**The schedule is only the fallback.** In practice GitHub starts it 4–9
hours late (August–October 2026: runs began between 10:30 and 15:30 UTC),
so the digest arrived at lunch, not breakfast. The on-time start now comes
from an external cron, a free **cron-job.org** job that calls
`workflow_dispatch` every day at 08:17 Europe/Rome; dispatched runs start
within seconds. The job, configured on cron-job.org:

- URL: `https://api.github.com/repos/alessiogarifo92/telegram-news-bot/actions/workflows/daily-digest.yml/dispatches`
- Method `POST`, body `{"ref":"main","inputs":{"mode":"send"}}`
- Headers: `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`,
  `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`
- Token: a fine-grained personal access token limited to this repository,
  permission **Actions: Read and write** only. GitHub answers `204` on
  success. When the token expires the call fails and the fallback below
  takes over until it is renewed. The late scheduled run still fires, but its first job
(`already-sent`) checks whether a run of this workflow already succeeded
today (UTC) and, if so, skips sending. A `concurrency` group queues the two
if they ever overlap, so the check always sees the earlier run's result.
If the external call misses a day, the scheduled run sends as before, just late.

The job also restores and saves `.state/` via `actions/cache`, which is
what gives the bot memory between runs:

```yaml
- name: Restore deduplication state
  uses: actions/cache@v6
  with:
    path: .state
    key: digest-state-${{ github.run_id }}
    restore-keys: |
      digest-state-
```

The key contains `run_id`, so it never exists at restore time. The restore
therefore always falls through to `restore-keys` (a prefix match, which
picks the most recent cache), and the post-job save always writes a fresh
entry. This is the standard pattern for a cache that *rolls forward*
instead of freezing on its first write.

### `.github/workflows/keepalive.yml`

Schedule: `0 3 1 * *` (03:00 UTC on the 1st of each month). GitHub disables
`schedule` triggers on a repository after **60 days without activity**.
This job writes a timestamp into `.github/.keepalive` and commits it, which
counts as activity and resets that timer for *every* scheduled workflow in
the repo — including the digest. It needs `permissions: contents: write`.

Do not delete this workflow. Without it the digest silently stops after two
quiet months, with no error anywhere.

### Cost

Actions minutes are only billed for private repositories. A run takes
roughly 70 seconds, so about **35–40 minutes per month** against the 2,000
free minutes on the Free plan. Making the repo public would make it free,
at the cost of publishing the code (never the secrets).

---

## Setting up GitHub Secrets

The bot never reads `.env` on GitHub — secrets are injected as environment
variables by the workflow. Three are used:

| Secret | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from `@BotFather` |
| `TELEGRAM_CHAT_ID` | Yes | Where to send the digest (personal ID, or a `-100…` supergroup ID for Topics) |
| `TELEGRAM_TOPICS` | No | `true` to split the digest across three forum topics (default `false`) |
| `DEEPL_API_KEY` | No | Third translation fallback for Dutch headlines |

If `DEEPL_API_KEY` is not set, the workflow passes an empty string and the
bot simply skips that fallback. It does not fail.

> There is no `GUARDIAN_API_KEY`. The Guardian is read through its public
> RSS feed, which needs no key — the Open Platform API was deliberately not
> used for exactly that reason.

### Option A — from the command line (`gh`)

Requires the [GitHub CLI](https://cli.github.com/), authenticated with
`gh auth login`. Run from inside the repository, so `gh` infers which repo
to target from the `origin` remote:

```bash
gh secret set TELEGRAM_BOT_TOKEN
```

The command then waits and reads the value from stdin — paste it and press
Enter. Piping works too, and keeps the value out of your shell history:

```bash
printf '%s' "123456:ABC-your-token-here" | gh secret set TELEGRAM_BOT_TOKEN
```

To read the values straight out of your local `.env` without ever printing
them:

```bash
set -a && . ./.env && set +a
printf '%s' "$TELEGRAM_BOT_TOKEN" | gh secret set TELEGRAM_BOT_TOKEN
printf '%s' "$TELEGRAM_CHAT_ID"   | gh secret set TELEGRAM_CHAT_ID
printf '%s' "$DEEPL_API_KEY"      | gh secret set DEEPL_API_KEY
```

Verify — this lists names and update times only, never values:

```bash
gh secret list
```

### Option B — from the web interface

1. Open the repository on GitHub.
2. **Settings** → **Secrets and variables** → **Actions**.
3. **New repository secret**.
4. Enter the exact name (`TELEGRAM_BOT_TOKEN`) and paste the value.
5. **Add secret**. Repeat for each one.

### Things worth knowing about secrets

- They are **write-only**. Once saved, nobody — including you — can read a
  secret back through the UI or the API. To change one, overwrite it.
- Actions **masks** them in logs: a secret that reaches the output appears
  as `***`. Do not rely on this as your only protection — masking matches
  exact strings, so a value that gets transformed (base64-encoded, split
  across lines) can leak.
- Adding, changing, or removing a secret takes effect on the **next** run.
  Runs already in progress keep the old values.
- Secrets are **not** available to workflows triggered by pull requests
  from forks. Irrelevant here, but it is the usual reason a workflow
  suddenly cannot see them.
- Names are case-sensitive and must match the workflow exactly. A typo
  produces an empty variable, not an error — which is why the bot fails
  loudly at startup on a missing `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID`
  (`raise SystemExit`) rather than sending nothing in silence.

---

## Setting up GitHub Actions

If you are wiring this up on a fresh repository:

```bash
# 1. Create the private repo and push (from the project folder)
git init
git add -A
git status          # confirm .env is NOT listed — it must stay untracked
git commit -m "Initial commit"
git branch -M main
gh repo create telegram-news-bot --private --source=. --remote=origin --push

# 2. Add the secrets (see the section above)
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
gh secret set DEEPL_API_KEY

# 3. Confirm both workflows are registered and active
gh workflow list
```

Actions is enabled by default on new repositories. If `gh workflow list`
comes back empty, check **Settings** → **Actions** → **General** →
*Allow all actions and reusable workflows*.

Two constraints that catch people out:

- A workflow only becomes visible and schedulable once its file exists on
  the **default branch** (`main` here). A workflow living only on a feature
  branch will not run on a schedule and cannot be dispatched.
- `schedule` triggers **never fire on a brand-new repository until the
  workflow file has been pushed to the default branch**, and they are
  disabled again after 60 days of inactivity — which is what
  `keepalive.yml` exists to prevent.

Scheduled runs are best-effort: GitHub explicitly does not guarantee the
exact minute, and drops runs entirely under heavy load. The bot is designed
around that rather than against it — see below.

---

## Running it manually

### Locally, on your machine

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then fill in the values
python news_digest_bot.py
```

Dependencies are `feedparser`, `requests`, `python-dotenv` and
`deep-translator`. To reinstall or update them later:

```bash
pip install -r requirements.txt --upgrade
```

A local run reads `.env` (via `python-dotenv`) and writes its dedup state
to `.state/sent.json` in the project folder. Both are gitignored. If you
want a local run that does **not** interfere with the memory of the
scheduled runs, point it somewhere else:

```bash
SENT_STATE_FILE=/tmp/test-state.json python news_digest_bot.py
```

### Triggering the GitHub workflow by hand

Both workflows declare `workflow_dispatch`, so they can be started on
demand without waiting for the schedule:

```bash
gh workflow run daily-digest.yml
```

Give it a few seconds, then check the result:

```bash
gh run list --workflow=daily-digest.yml --limit 1
```

`gh run watch` and `gh run view` both take a **run ID**. Without one,
`gh run view` opens an interactive picker and `gh run watch` fails
outright, so grab the ID of the latest run first:

```bash
RUN_ID=$(gh run list --workflow=daily-digest.yml --limit 1 --json databaseId --jq '.[0].databaseId')
```

Then follow it until it finishes, exiting non-zero if the run failed:

```bash
gh run watch "$RUN_ID" --exit-status
```

Or read its full log:

```bash
gh run view "$RUN_ID" --log
```

That log is how you confirm the dedup memory is working. You want
`State loaded: N links ...` near the start (or `No state at ...` on the
very first run, when there is no cache yet) and `Cache saved with key:
digest-state-...` in the post-job cleanup:

```bash
gh run view "$RUN_ID" --log | grep -Ei 'state loaded|no state at|state saved|cache saved|cache not found'
```

From the web interface: **Actions** tab → *Daily news digest* in the left
sidebar → **Run workflow** → **Run workflow**.

Note that a manual run sends a real digest to your Telegram chat. Because
the dedup state persists, triggering it twice in a row will correctly send
almost nothing the second time — that is the feature working, not a bug.

**Resetting the memory.** That same feature is why a section can go quiet:
if everything a source published in the window was already sent by an
earlier run, the whole section is skipped (the log says so, per category:
`Tech (The Verge): nothing sent - all 10 items in the window were already
sent`). The manual run takes a **mode** for that:

| Mode | What it does |
|---|---|
| `send` | The normal digest. What the daily schedule runs. |
| `reset-and-send` | Forgets everything, then sends a full digest right away. |
| `reset-only` | Forgets everything and sends nothing. |

`reset-only` is the one to use before a scheduled run: it clears the memory
without firing a second digest at the moment you press the button, so the
next morning's digest is the one that arrives full. Either way the reset
sticks — the empty state is saved to the cache the next run restores.

---

## Folders (Telegram Topics)

By default every headline lands in one flat chat. If that is too much in a
single stream, turn on **Topics**: the digest is then split across three
threads, each with its own mute switch.

| Topic | Contains |
|---|---|
| 📰 **Generali** | Olanda (NOS) · Italia (ANSA) · Mondo (BBC) |
| 🤖 **Tech & AI** | AI (The Decoder, The Verge) · Paper arXiv · Tech (TechCrunch, The Verge, Ars Technica, The Guardian, Hacker News) |
| 💰 **Finanza** | Bloomberg Markets |

Inside each topic the per-source section headers still separate the
sub-sections, so "Generali" is itself divided into Olanda / Italia / Mondo.

### Setup

1. **Create a group** in Telegram, then in its settings turn on **Topics**.
   A personal chat cannot have topics — it has to be a group, which
   Telegram promotes to a *supergroup* automatically.
2. **Add your bot to the group and make it an administrator**, with the
   *Manage Topics* permission. If the bot does not show up when you try to
   add it: open **@BotFather → /mybots → your bot → Bot Settings → Allow
   Groups?** and make sure it is enabled.
3. **Get the group's chat ID** — add **@getidsbot** (or **@RawDataBot**) to
   the group, read the `-100…` number it prints, then remove it. Set that
   as `TELEGRAM_CHAT_ID` (locally in `.env`, on Actions as the secret).
4. **Set `TELEGRAM_TOPICS=true`** — env var locally; add it as a repository
   secret for Actions (the workflow already passes it through).
5. **Run once.** The bot creates the three topics itself and writes their
   IDs to `.state/topics.json` (`TOPIC_STATE_FILE`), carried between runs
   by `actions/cache` the same way as `sent.json`. Later runs reuse them.
6. **Pin the IDs.** Copy the three `message_thread_id` values from the
   run's log (`Created topic '📰 Generali' (message_thread_id=…)`) into
   `TELEGRAM_TOPIC_IDS` in `.github/workflows/daily-digest.yml`, e.g.
   `{"generali": 3190, "tech_ai": 3191, "finanza": 3192}`. GitHub deletes
   a cache nobody has used for 7 days, so after a week of failed runs
   `topics.json` is gone and, without pinned IDs, the bot would create a
   second set of topics.

If anything in step 2 is missing — bot not admin, *Manage Topics* off, the
chat not a forum — `createForumTopic` fails, the bot logs a warning and
sends that group to the "General" thread instead. The digest still goes
out; it just is not split.

The `reset-only` / `reset-and-send` workflow modes forget the *sent
stories*, not the topics: `topics.json` is left in place so a reset does
not spawn a second set of same-named topics. To rebuild the topics from
scratch, delete `topics.json` (locally the file; on Actions, clear the
cache), empty `TELEGRAM_TOPIC_IDS` and run again.

---

## What counts as "new"

`HOURS_LOOKBACK` controls how far back to look in each feed. **The default
is 30 hours, not 24**, even though the bot runs once a day.

The extra 6 hours are deliberate. GitHub Actions schedules do not start at
the exact minute: they queue up and drift, and occasionally a run is
skipped outright. With a window of exactly 24 hours, a run that starts an
hour late leaves an hour of news uncovered — and no later run ever goes
back for it. The overlap closes that gap.

The overlap does not cause repeats, because the bot **remembers what it
already sent between runs**: links and normalised titles are written to
`SENT_STATE_FILE` (default `.state/sent.json`) and read back on the next
run, expiring after `SENT_STATE_TTL_DAYS` days (default 7). On GitHub
Actions that file survives through `actions/cache`; locally it just sits in
the project folder and is gitignored.

The state is saved **after** the digest has been sent, never before: if the
bot dies halfway through, it is better to repeat a few headlines tomorrow
than to mark as "sent" items that never arrived. A missing or corrupted
state file logs a warning and starts from empty — the worst case is a
repeated digest, not a digest that fails to go out.

The two settings go together: **if you disable the persistent state, set
`HOURS_LOOKBACK` back to 24**, otherwise every digest will replay the last
6 hours of the previous day.

### Configuration reference

All of these are read from the environment (`.env` locally, workflow `env:`
on Actions) and all have defaults:

| Variable | Default | What it does |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | **Required.** Missing value fails immediately. |
| `TELEGRAM_CHAT_ID` | — | **Required.** Missing value fails immediately. |
| `TELEGRAM_TOPICS` | `false` | `true` splits the digest across three forum topics. |
| `TOPIC_STATE_FILE` | `.state/topics.json` | Where the created topic IDs are remembered. |
| `TELEGRAM_TOPIC_IDS` | *(empty)* | JSON `{"generali": …, "tech_ai": …, "finanza": …}`: fixed topic IDs, used instead of `TOPIC_STATE_FILE`. |
| `DEEPL_API_KEY` | *(empty)* | Optional third translation fallback. |
| `HOURS_LOOKBACK` | `30` | How far back a feed entry can be. |
| `MAX_ITEMS_PER_CATEGORY` | `10` | Ceiling on stories per category, after dedup. |
| `SECONDS_BETWEEN_MESSAGES` | `1.0` | Pause between Telegram sends. |
| `DUPLICATE_TITLE_THRESHOLD` | `0.6` | Title similarity (0–1) that counts as a duplicate. |
| `SENT_STATE_FILE` | `.state/sent.json` | Where cross-run memory is stored. |
| `SENT_STATE_TTL_DAYS` | `7` | How long a sent item is remembered. |
| `SUMMARY_MAX_CHARS` | `320` | Length of the excerpt under each title. `0` = titles only. |
| `TRANSLATE_SUMMARIES` | `1` | Also translate the Dutch excerpt, not just the title. `0` saves translation quota. |

---

## Things only you can do (with your Telegram account)

1. **Create the bot** — open Telegram, message `@BotFather`, send
   `/newbot`, give the bot a name and a username, and copy the **token**
   it returns.
2. **Find your chat ID** — message `@userinfobot` on Telegram and copy the
   number it gives you (your personal numeric ID). Alternatively send
   `/start` to your new bot and read the script's logs on first run.
3. **(Optional, recommended) DeepL key** for more reliable NL→EN
   translation: go to https://www.deepl.com/pro#developer, create a free
   "DeepL API Free" account (no card required), copy the key and set it as
   `DEEPL_API_KEY`. Without it the bot uses only Google Translate and
   MyMemory, and falls back to the untranslated Dutch title if both fail.

---

## Where it can run

The script needs **real internet access** to `api.telegram.org` and to the
RSS sources. GitHub Actions runners have it. Your own machine has it. A
free VPS (Oracle Cloud Always Free) has it. Restricted sandbox environments
generally do not — the connection to Telegram, and often to the feeds
themselves, is blocked there.

### Running it on your own machine instead of Actions

Only needed if you would rather not use GitHub Actions at all.

**cron (Linux/macOS)**, e.g. daily at 07:00:

```bash
crontab -e
# add this line, adjusting the paths:
0 7 * * * cd /home/ubuntu/telegram-news-bot && .venv/bin/python news_digest_bot.py >> digest.log 2>&1
```

**systemd timer** (better on a Linux VPS — `Persistent=true` catches up on
a run missed while the machine was off, which plain cron will not do):

```ini
# /etc/systemd/system/news-digest-bot.service
[Unit]
Description=News Digest Bot

[Service]
Type=oneshot
WorkingDirectory=/home/ubuntu/telegram-news-bot
ExecStart=/home/ubuntu/telegram-news-bot/.venv/bin/python news_digest_bot.py
EnvironmentFile=/home/ubuntu/telegram-news-bot/.env
```

```ini
# /etc/systemd/system/news-digest-bot.timer
[Unit]
Description=Run the News Digest Bot every morning at 7:00

[Timer]
OnCalendar=*-*-* 07:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now news-digest-bot.timer
```

If you do this *instead of* Actions, disable the `daily-digest.yml`
schedule (`gh workflow disable daily-digest.yml`) so you do not get two
digests a day from two different machines with two separate memories.

---

## Extending it

- **More sources**: add entries to the `FEEDS` list in
  `news_digest_bot.py`. The order of that list **is** the send order on
  Telegram: keep the priority Netherlands → Italy → World → AI → Tech →
  Finance, with Finance always last. Other solid AI/tech feeds beyond the
  ones already configured: MIT Technology Review
  (`https://www.technologyreview.com/topic/artificial-intelligence/feed`
  for AI, `https://www.technologyreview.com/feed/` for tech), Wired
  (`https://www.wired.com/feed/tag/ai/latest/rss` for AI,
  `https://www.wired.com/feed/rss` for general tech), The Register
  (`https://www.theregister.com/headlines.atom`).
- **Check a feed before adding it**: sandboxed environments cannot test
  this (the network is blocked to RSS sites too, not just Telegram). Try it
  from a real terminal first:

  ```bash
  python -c "import feedparser; print(feedparser.parse('URL').entries[:2])"
  ```

- **Finance**: Bloomberg's free Markets feed is used because Yahoo
  Finance's general feed is gone (404). Some Bloomberg articles are
  paywalled, but the headline is the point of the digest. Free
  alternatives that also work: CNBC
  (`https://www.cnbc.com/id/100003114/device/rss/rss.html`) and the FT
  markets feed (`https://www.ft.com/markets?format=rss`, paywalled). Alpha
  Vantage has a free tier if you need real market data.
- **A different time, or more than one digest a day**: change the `cron`
  expression in `daily-digest.yml`. It is always in UTC and does **not**
  follow daylight saving, so the local time shifts by an hour twice a year.
  More than one run a day is safe now that dedup state persists — the
  second run of the day will only send what appeared since the first.
