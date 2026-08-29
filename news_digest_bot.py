"""
Telegram "news digest" bot: once a day it collects the day's important
stories from the Netherlands (general + Amsterdam/AT5), Italy (general),
the world (BBC), global AI (VentureBeat + The Verge), global tech
(TechCrunch + Ars Technica + Hacker News) and global finance (Yahoo
Finance) - and sends them to Telegram **one at a time, every story as its
own message** - so each link gets its own preview (and "Instant View" where
the site supports it) instead of everything ending up glued into a single
block.

Send order (priority, top to bottom):
1. Netherlands (NOS general + AT5 Amsterdam)
2. Italy (ANSA)
3. World (BBC)
4. AI (VentureBeat + The Verge AI)
5. Tech (TechCrunch + Ars Technica + Hacker News) - AI and Tech are now
   adjacent, no longer separated by Finance
6. Finance (Yahoo Finance) - always last

"Only the important stories": every configured feed is already the
outlet's own editorial "top stories" selection (not a purely chronological
listing), so taking just the first N entries of each feed
(MAX_ITEMS_PER_CATEGORY) is equivalent to taking only the day's most
relevant stories according to that source.

"No duplicates": AI and Tech deliberately use several different outlets
(VentureBeat/The Verge/arXiv for AI, TechCrunch/Ars Technica/The
Guardian/Hacker News for tech) instead of several feeds from the same
outlet, which overlap by nature. On top of that there is a global
deduplication check (by exact link and by very similar title) that drops an
item when a practically identical story was already sent in an earlier
category of the same digest.

That check also applies BETWEEN one day and the next: the links and titles
that were sent are stored in `SENT_STATE_FILE` (default
`.state/sent.json`) and read back on the following run, expiring after
`SENT_STATE_TTL_DAYS` days. This matters because GitHub Actions schedules
drift and occasionally skip a run: without memory between runs, a digest
that started late would replay stories already read, and a skipped one
would lose others for good. With that memory in place `HOURS_LOOKBACK` can
stay generous (30h) and cover the gaps with no risk of repeating anything.

"AI papers (arXiv)": a bonus category, different from the others - not
journalism but the latest cs.AI academic papers published on arXiv (public
API, no key). Titles are often very technical; it is there for following
the research and not only the news.

"The Guardian" (Technology section): an extra editorial source for Tech,
through its public RSS feed - no key, no registration required (unlike the
Guardian's Open Platform API, which was deliberately not used for exactly
that reason).

How a story looks in the chat: every message is laid out like a news
channel post rather than a bare headline - the article's picture on top
(Telegram's own link preview, requested large and ABOVE the text through
`link_preview_options`), under it the title in bold, then a short excerpt
taken from the feed's own description (cleaned of HTML and boilerplate, cut
at a word boundary at SUMMARY_MAX_CHARS), and at the bottom a button that
opens the article - no URL in the text at all. Telegram adds its own
"Instant View" button to the preview when the site has a template for it;
the Bot API cannot ask for that, so the button below is the guaranteed way
in and Instant View comes on top of it where it exists. Sections are
separated by a header carrying a horizontal rule, the section name in bold
and the outlet plus the number of stories underneath, so the chat reads as
chapters instead of one unbroken column of messages. Messages are sent with
`parse_mode="HTML"`, which is why every piece of feed text goes through
`esc()` first.

For Dutch stories (NOS/AT5), every message carries both the original Dutch
title and its English translation (and the English excerpt too, unless
TRANSLATE_SUMMARIES is turned off). Translation tries three engines in
order (all through `deep-translator`): Google Translate -> MyMemory (both
free, no API key, but with shared quotas that run out from time to time)
-> DeepL (needs a free `DEEPL_API_KEY` in `.env`, far more reliable; if it
is not set this third attempt is skipped). If all three fail, the message
still goes out with the original title only.

"One notification a day": Telegram raises an alert (sound/vibration) for
every message received, unless the message is sent with
`disable_notification=True` (in which case it still lands in the chat, just
silently). To avoid 30+ alerts in a row, ONLY the first message of the
digest ("Digest notizie - ...") is sent with notifications on; every
message after it (category headers + individual stories) is silent. The
result: one notification a day, everything else arrives without
interrupting.

This is not a bot that "listens" for messages: it is a script that runs
ONCE and then exits - restarting it every morning is the job of the
`.github/workflows/daily-digest.yml` workflow on GitHub Actions (locally a
cron job / systemd timer / Windows Task Scheduler entry works fine, see
README.md).

IMPORTANT: this script has to run on a machine with real internet access
(your own PC, or a VPS such as Oracle Cloud) - it does not work from a
sandbox environment whose network is blocked towards api.telegram.org.

Quick setup:
1. pip install -r requirements.txt
2. Copy .env.example to .env and fill in the values (or use your existing .env)
3. python news_digest_bot.py   (single run, for testing)
4. Schedule it to run every morning on its own (see README.md).

Note on language: comments, docstrings and log messages are in English;
the strings actually sent to Telegram (the digest header, the category
labels in FEEDS) are deliberately kept in Italian, because that is what
the reader of the digest sees.
"""

import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("news-digest-bot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
# 30 and not 24 on purpose: GitHub Actions schedules drift (by half an hour
# and more) and every so often a run is skipped entirely. With exactly 24,
# a late run leaves behind a gap of stories no run ever looks at again. The
# 6 hours of overlap cover it, and stories already sent yesterday do not
# come back because the persistent dedup state filters them out (see
# SENT_STATE_FILE). Do not raise this without that state enabled.
HOURS_LOOKBACK = int(os.environ.get("HOURS_LOOKBACK", "30"))
# Kept low on purpose: every feed below is already the outlet's "top
# stories" selection (not a plain chronological order), so taking just the
# first few means taking only the day's most important stories.
MAX_ITEMS_PER_CATEGORY = int(os.environ.get("MAX_ITEMS_PER_CATEGORY", "3"))
SECONDS_BETWEEN_MESSAGES = float(os.environ.get("SECONDS_BETWEEN_MESSAGES", "1.0"))
# Above this title-similarity threshold (0-1), an item is considered a
# duplicate of one already sent and is discarded.
DUPLICATE_TITLE_THRESHOLD = float(os.environ.get("DUPLICATE_TITLE_THRESHOLD", "0.6"))
# Where to store the links/titles already sent, so they are not repeated on
# the next run. Without this file, deduplication only applies WITHIN a
# single digest, and "do not repeat" between one day and the next relies
# purely on HOURS_LOOKBACK matching the cron cadence - which stops being
# true the moment a run starts late. On GitHub Actions the file is carried
# between runs by actions/cache (see .github/workflows/daily-digest.yml);
# locally it simply sits in the project folder and is gitignored.
SENT_STATE_FILE = Path(os.environ.get("SENT_STATE_FILE", ".state/sent.json"))
# After how many days an already-sent story is forgotten. 7 is a
# compromise: long enough to cover runs skipped for days, but not so long
# that the file grows without bound (the title-similarity comparison is
# linear in the number of remembered entries).
SENT_STATE_TTL_DAYS = int(os.environ.get("SENT_STATE_TTL_DAYS", "7"))
# How many characters of the story's own summary (the description that
# comes with the feed entry) to keep under the title. The point of the
# excerpt is to tell you whether the story is worth opening without leaving
# the chat; past ~350 characters the card stops being a card and turns back
# into a wall of text. Set to 0 to send titles only, as the bot did before.
SUMMARY_MAX_CHARS = int(os.environ.get("SUMMARY_MAX_CHARS", "320"))
# Whether to translate the Dutch excerpt as well, not just the title. It
# costs one extra translation call per Dutch story; turn it off (0) if the
# free Google/MyMemory quotas start running out. With it off, Dutch stories
# keep the translated title and the excerpt stays in Dutch.
TRANSLATE_SUMMARIES = os.environ.get("TRANSLATE_SUMMARIES", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
# Optional: when set, enables DeepL as the third translation fallback
# (more reliable than Google/MyMemory). See .env.example for how to get one
# for free. Left empty, the third fallback is simply skipped.
DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY", "").strip()

# Verified sources - see the Notion document "Bot Telegram: guida, esempi
# e progetti" for the notes on each one (personal non-commercial use, etc.).
# "lang": "nl" turns on automatic translation into English for that category.
# "fetcher": "hackernews" skips RSS parsing and uses the official HN API.
#
# Each entry is split into "emoji" + "category" + "source" instead of one
# pre-formatted label, because the section header sent to Telegram formats
# the three differently: the name in bold on its own line, the outlet in
# italics underneath. Keeping them glued together would mean parsing the
# label back apart at send time.
#
# ORDER = SEND ORDER ON TELEGRAM. Keep this priority:
# Netherlands -> Italy -> World -> AI -> Tech -> Finance.
# AI and Tech are adjacent on purpose (several sources each, but grouped),
# and Finance stays last so it no longer splits them apart.
#
# The "category" and "source" labels are user-facing: they are sent to
# Telegram as the header of each section, so they stay in Italian.
FEEDS = [
    # --- 1. Netherlands ---
    {
        "emoji": "🇳🇱",
        "category": "Olanda — generale",
        "source": "NOS",
        "url": "https://feeds.nos.nl/nosnieuwsalgemeen",
        "lang": "nl",
    },
    {
        "emoji": "🏙️",
        "category": "Amsterdam",
        "source": "AT5",
        "url": "https://www.at5.nl/rss",
        "lang": "nl",
    },
    # --- 2. Italy ---
    {
        "emoji": "🇮🇹",
        "category": "Italia — generale",
        "source": "ANSA",
        "url": "https://www.ansa.it/sito/notizie/topnews/topnews_rss.xml",
        "lang": "it",
    },
    # --- 3. World ---
    {
        "emoji": "🌍",
        "category": "Mondo",
        "source": "BBC",
        "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "lang": "en",
    },
    # --- 4. AI (global, several sources so it does not depend on one outlet) ---
    {
        "emoji": "🤖",
        "category": "AI",
        "source": "VentureBeat",
        "url": "https://venturebeat.com/category/ai/feed/",
        "lang": "en",
    },
    {
        "emoji": "🤖",
        "category": "AI",
        "source": "The Verge",
        "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        "lang": "en",
    },
    # Bonus: research papers (not journalism) - arXiv exposes an Atom feed
    # through a query URL, which feedparser handles like ordinary RSS.
    {
        "emoji": "📄",
        "category": "Paper AI",
        "source": "arXiv",
        "url": "https://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&sortOrder=descending&max_results=10",
        "lang": "en",
    },
    # --- 5. Tech (global, adjacent to AI, no longer split off by Finance) ---
    {
        "emoji": "💻",
        "category": "Tech",
        "source": "TechCrunch",
        "url": "https://techcrunch.com/feed/",
        "lang": "en",
    },
    {
        "emoji": "💻",
        "category": "Tech",
        "source": "The Verge",
        "url": "https://www.theverge.com/rss/index.xml",
        "lang": "en",
    },
    {
        "emoji": "💻",
        "category": "Tech",
        "source": "Ars Technica",
        "url": "https://arstechnica.com/feed/",
        "lang": "en",
    },
    {
        "emoji": "💻",
        "category": "Tech",
        "source": "The Guardian",
        "url": "https://www.theguardian.com/uk/technology/rss",
        "lang": "en",
    },
    {
        "emoji": "💻",
        "category": "Tech",
        "source": "Hacker News",
        "fetcher": "hackernews",
        "lang": "en",
    },
    # --- 6. Finance (always last) ---
    {
        "emoji": "💰",
        "category": "Finanza globale",
        "source": "Yahoo Finance",
        "url": "https://finance.yahoo.com/news/rssindex",
        "lang": "en",
    },
]

# Italian names for the digest header date. `datetime.strftime("%A %d %B")`
# would give them in whatever locale the machine happens to have, which on
# GitHub Actions is English - and the header is the one line the reader
# sees first every morning.
WEEKDAYS_IT = [
    "lunedì",
    "martedì",
    "mercoledì",
    "giovedì",
    "venerdì",
    "sabato",
    "domenica",
]
MONTHS_IT = [
    "gennaio",
    "febbraio",
    "marzo",
    "aprile",
    "maggio",
    "giugno",
    "luglio",
    "agosto",
    "settembre",
    "ottobre",
    "novembre",
    "dicembre",
]
# Horizontal rule used to separate one section from the next. Telegram has
# no real divider, so a run of box-drawing characters is what gives the
# chat its "chapters" instead of an unbroken column of messages.
SECTION_RULE = "━" * 12

HACKER_NEWS_TOP_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HACKER_NEWS_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
HACKER_NEWS_ITEMS_TO_FETCH = 3


def fetch_feed_items(url: str) -> list[dict]:
    """Download an RSS feed and return the entries from the last
    HOURS_LOOKBACK hours (without applying the per-category cap yet: that
    happens AFTER deduplication, so a unique item is never dropped just
    because a duplicate happened to come before it).

    Each item carries the entry's own summary alongside title and link:
    that excerpt is what turns the Telegram message from a bare headline
    into a readable card. Feeds that ship no description simply produce an
    empty string and the message falls back to title + link."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_LOOKBACK)
    items = []
    try:
        parsed = feedparser.parse(url)
        for entry in parsed.entries:
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published:
                pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue
            title = re.sub(r"\s+", " ", entry.get("title", "")).strip()
            items.append(
                {
                    "title": title,
                    "link": entry.get("link", ""),
                    "summary": clean_summary(entry.get("summary", ""), title),
                }
            )
        if not parsed.entries:
            # An empty feed and a feed whose entries are all older than the
            # window look identical downstream: both send nothing. Only this
            # one means the URL itself stopped working.
            log.warning("Feed %s returned no entries at all (moved or down?).", url)
    except Exception as exc:  # noqa: BLE001
        log.warning("Feed %s failed: %s", url, exc)
    return items


def fetch_hacker_news_top() -> list[dict]:
    """Download the top Hacker News posts through the official API (free, no key).

    HN posts have no description to excerpt, so instead of an empty card
    they get a "footer" line with the two numbers that actually say
    something about a post there - points and comments - plus a link to the
    discussion, which is often the reason to open an HN item at all."""
    items = []
    try:
        top_ids = requests.get(HACKER_NEWS_TOP_URL, timeout=10).json()[:HACKER_NEWS_ITEMS_TO_FETCH]
        for item_id in top_ids:
            data = requests.get(HACKER_NEWS_ITEM_URL.format(id=item_id), timeout=10).json()
            discussion = f"https://news.ycombinator.com/item?id={item_id}"
            comments = data.get("descendants", 0)
            items.append(
                {
                    "title": data.get("title", ""),
                    "link": data.get("url") or discussion,
                    "summary": "",
                    "footer": f"▲ {data.get('score', 0)} punti · 💬 {comments} commenti",
                    "link_label": "📖 Leggi l'articolo",
                    "discussion_url": discussion,
                    "discussion_label": f"💬 Discussione ({comments})",
                }
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch Hacker News: %s", exc)
    return items


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def esc(text: str) -> str:
    """Escape the text that goes into a `parse_mode="HTML"` message. Titles
    and excerpts come from feeds we do not control, and a single stray `<`
    or `&` in them would make Telegram reject the whole message with a 400
    instead of just rendering it oddly."""
    return html.escape(text, quote=False)


# Boilerplate the feeds append to their own descriptions ("The post X
# appeared first on Y" on WordPress sites, "read more" teasers elsewhere).
# It is noise in an excerpt whose whole job is to be two readable lines.
_SUMMARY_NOISE = re.compile(
    r"(the post\s.+?appeared first on\s.+$"
    r"|continua a leggere.*$"
    r"|leggi (tutto|l['’]articolo).*$"
    r"|read more.*$"
    r"|lees (meer|verder|het hele artikel).*$"
    r"|\[…\]$"
    r"|\[\.\.\.\]$)",
    re.IGNORECASE | re.DOTALL,
)


def _truncate(text: str, limit: int) -> str:
    """Cut to at most `limit` characters on a word boundary, adding an
    ellipsis. Cutting mid-word reads like a bug; the ellipsis is what tells
    the reader the story continues behind the link."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.-–—")
    return f"{cut}…"


def clean_summary(raw: str, title: str) -> str:
    """Turn the HTML blob a feed calls a "description" into a plain-text
    excerpt fit for a Telegram message: tags stripped, entities decoded,
    whitespace collapsed, boilerplate removed, truncated to
    SUMMARY_MAX_CHARS.

    Returns an empty string when the result would add nothing: no
    description at all, excerpts turned off, or a "summary" that merely
    repeats the title (several feeds do exactly that, and repeating the
    headline under itself looks like a bug)."""
    if not raw or SUMMARY_MAX_CHARS <= 0:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _SUMMARY_NOISE.sub("", text).strip(" –—-·")
    if not text:
        return ""
    normalized_summary = _normalize_title(text)
    normalized_title = _normalize_title(title)
    if normalized_summary.startswith(normalized_title[:60]) and len(normalized_summary) <= len(
        normalized_title
    ) + 10:
        return ""
    return _truncate(text, SUMMARY_MAX_CHARS)


def _not_expired(entries: dict | None, cutoff: datetime) -> dict[str, str]:
    """Keep only the "key -> ISO send date" entries newer than `cutoff`. An
    entry with a missing or malformed date is dropped: if the file gets
    corrupted, resending an old story beats carrying around entries that
    would never expire."""
    kept: dict[str, str] = {}
    for key, sent_at in (entries or {}).items():
        try:
            if datetime.fromisoformat(sent_at) >= cutoff:
                kept[key] = sent_at
        except (TypeError, ValueError):
            continue
    return kept


class Deduplicator:
    """Tracks the links and titles already sent, to discard substantially
    identical stories reported by different sources (e.g. the same AI story
    on VentureBeat and The Verge on the same day).

    It remembers on two levels: within the current run (two sources running
    the same story this morning) and ACROSS runs - `load()` reads back what
    was sent on previous days and `save()` writes it out again, dropping
    entries older than `SENT_STATE_TTL_DAYS`. It is the second level that
    makes the digest immune to runs that drift or get skipped: without it,
    "do not repeat" relies purely on HOURS_LOOKBACK matching the cron
    cadence.

    If the state is missing or unreadable it starts from empty rather than
    failing: the worst outcome is a digest that repeats something, not a
    digest that never goes out."""

    def __init__(self, threshold: float) -> None:
        self.threshold = threshold
        # Maps of "key -> ISO send date" (not sets/lists) because we need to
        # know when an entry was sent in order to expire it.
        self.seen_links: dict[str, str] = {}
        self.seen_titles: dict[str, str] = {}

    def is_duplicate(self, title: str, link: str) -> bool:
        if link and link in self.seen_links:
            return True
        normalized = _normalize_title(title)
        for seen in self.seen_titles:
            if SequenceMatcher(None, normalized, seen).ratio() >= self.threshold:
                return True
        return False

    def remember(self, title: str, link: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if link:
            self.seen_links[link] = now
        self.seen_titles[_normalize_title(title)] = now

    def load(self, path: Path, ttl_days: int) -> None:
        """Read back the state of previous runs, ignoring expired entries."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.info("No state at %s: first run, or cache not restored.", path)
            return
        except (OSError, ValueError) as exc:  # noqa: BLE001
            log.warning("State at %s is unreadable (%s): starting from empty.", path, exc)
            return

        cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
        self.seen_links = _not_expired(raw.get("links"), cutoff)
        self.seen_titles = _not_expired(raw.get("titles"), cutoff)
        log.info(
            "State loaded: %s links and %s titles already sent in the last %s days.",
            len(self.seen_links),
            len(self.seen_titles),
            ttl_days,
        )

    def save(self, path: Path, ttl_days: int) -> None:
        """Write the state back out, pruned of expired entries. Never
        raises: by this point the digest has already been sent, and a failed
        save costs at most a few repeats tomorrow."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
        payload = {
            "links": _not_expired(self.seen_links, cutoff),
            "titles": _not_expired(self.seen_titles, cutoff),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            log.info("State saved to %s (%s links remembered).", path, len(payload["links"]))
        except OSError as exc:  # noqa: BLE001
            log.warning("Could not save state to %s: %s", path, exc)


# Language codes in the format MyMemory (the fallback) expects, which
# differs from Google Translate's (that one just uses "nl", "it", etc.).
_MYMEMORY_LANG_CODES = {"nl": "nl-NL", "it": "it-IT"}


def translate_to_english(text: str, source_lang: str) -> str | None:
    """Try, in order, Google Translate -> MyMemory -> DeepL (if
    `DEEPL_API_KEY` is set). The first two are free and need no API key but
    share public quotas that can run out or start blocking automated
    requests (this happens periodically; it is a limit of the free
    services, not a bug here). DeepL is optional but far more reliable
    (500,000 characters/month free with a personal key). Returns None only
    if ALL available attempts fail, so the caller can send just the
    original title instead of failing the whole digest."""
    try:
        from deep_translator import GoogleTranslator

        result = GoogleTranslator(source=source_lang, target="en").translate(text)
        if result:
            return result
    except Exception as exc:  # noqa: BLE001
        log.warning("Google Translate failed for %r: %s", text[:50], exc)

    try:
        from deep_translator import MyMemoryTranslator

        mm_source = _MYMEMORY_LANG_CODES.get(source_lang, source_lang)
        result = MyMemoryTranslator(source=mm_source, target="en-GB").translate(text)
        if result:
            return result
    except Exception as exc:  # noqa: BLE001
        log.warning("MyMemory fallback also failed for %r: %s", text[:50], exc)

    if DEEPL_API_KEY:
        try:
            from deep_translator import DeeplTranslator

            result = DeeplTranslator(
                api_key=DEEPL_API_KEY, source=source_lang, target="en", use_free_api=True
            ).translate(text)
            if result:
                return result
        except Exception as exc:  # noqa: BLE001
            log.warning("DeepL fallback also failed for %r: %s", text[:50], exc)

    return None


# Set to False the first time Telegram rejects `link_preview_options`, so
# the rest of the digest goes out with the old `disable_web_page_preview`
# flag instead of failing message after message. Only matters against a
# self-hosted Bot API server older than 7.0; the official one supports it.
_link_preview_options_supported = True


def send_telegram_message(
    text: str,
    silent: bool = True,
    preview_url: str | None = None,
    buttons: list[dict] | None = None,
) -> bool:
    """Send a single Telegram message, formatted as HTML. Handles the rate
    limit (429) by retrying after the delay Telegram asks for. Returns True
    if sent.

    `preview_url` is the link whose preview to show: asked for as a large
    image and placed ABOVE the text, so the message reads picture -> title
    -> excerpt. Messages without a link (the digest header, the section
    headers) pass None and get no preview at all.

    `buttons` is a single row of "open this link" buttons pinned under the
    message - the way out to the article, instead of a URL sitting in the
    text. Telegram adds its own "Instant View" button to the preview when
    the site has a template for it; the Bot API has no way to request that,
    which is why the button below says "read the article" and lets Telegram
    offer Instant View on top of it where it can.

    `silent=True` (the default) sends the message with
    `disable_notification`, so it raises no sound/vibration - only the
    first message of the digest is sent with `silent=False`, to get one
    notification a day instead of one per story."""
    global _link_preview_options_supported

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for _attempt in range(3):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_notification": "true" if silent else "false",
        }
        if preview_url and _link_preview_options_supported:
            payload["link_preview_options"] = json.dumps(
                {
                    "url": preview_url,
                    "prefer_large_media": True,
                    "show_above_text": True,
                }
            )
        else:
            payload["disable_web_page_preview"] = "false" if preview_url else "true"
            # Without link_preview_options there is no way to name the URL
            # to preview, so it has to be in the text for Telegram to find
            # it - the link lives in a button now, and buttons are invisible
            # to the preview builder.
            if preview_url and preview_url not in text:
                payload["text"] = f"{text}\n\n{preview_url}"[:4096]

        if buttons:
            payload["reply_markup"] = json.dumps({"inline_keyboard": [buttons]})

        resp = requests.post(url, data=payload, timeout=15)
        if resp.ok:
            return True
        if resp.status_code == 429:
            retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
            log.warning("Telegram rate limit, retrying in %s seconds...", retry_after)
            time.sleep(retry_after + 1)
            continue
        if (
            resp.status_code == 400
            and _link_preview_options_supported
            and "link_preview_options" in resp.text
        ):
            log.warning("link_preview_options rejected, falling back to plain previews.")
            _link_preview_options_supported = False
            continue
        log.error("Telegram send failed: %s %s", resp.status_code, resp.text)
        return False
    return False


def format_digest_header(now: datetime) -> str:
    """The one message of the day that rings: date spelled out in Italian,
    inside the same rules that separate the sections below it."""
    date_line = f"{WEEKDAYS_IT[now.weekday()]} {now.day} {MONTHS_IT[now.month - 1]} {now.year}"
    return (
        f"{SECTION_RULE}\n"
        f"📰 <b>DIGEST NOTIZIE</b>\n"
        f"🗓 <i>{esc(date_line)}</i>\n"
        f"{SECTION_RULE}"
    )


def format_category_header(feed: dict, count: int) -> str:
    """Section divider: a rule, the section name in bold next to its emoji,
    and the outlet plus how many stories follow in small italics. The rule
    is what gives the chat visible chapters - without it the section header
    reads as just another message in the column."""
    stories = "notizia" if count == 1 else "notizie"
    return (
        f"{SECTION_RULE}\n"
        f"{feed['emoji']} <b>{esc(feed['category'].upper())}</b>\n"
        f"<i>{esc(feed['source'])} · {count} {stories}</i>"
    )


def format_news_message(item: dict) -> str:
    """Lay out one story the way a news channel post reads: the picture on
    top (the caller asks Telegram for it through `preview_url`), then the
    headline in bold, then the excerpt. The blank line between title and
    excerpt is the whole point: it is what stops the two from running
    together. The link is not in the text at all - it lives in the button
    row built by `format_news_buttons`."""
    blocks = [f"<b>{esc(item['title'])}</b>"]
    if item.get("title_en"):
        blocks[0] += f"\n🇬🇧 <i>{esc(item['title_en'])}</i>"
    if item.get("summary"):
        blocks.append(esc(item["summary"]))
    if item.get("footer"):
        blocks.append(item["footer"])
    return "\n\n".join(blocks)


def format_news_buttons(item: dict, feed: dict) -> list[dict]:
    """The row of buttons under a story: the way out to the article, and on
    Hacker News the discussion next to it. A button beats a link in the
    text - it is a real tap target, and it keeps the message down to
    picture, title and excerpt.

    Instant View is not in here on purpose: the Bot API cannot request it.
    Telegram adds its own "Instant View" button to the preview above when
    the site has a template for it, and these buttons sit underneath
    whether it does or not."""
    buttons = []
    if item["link"]:
        buttons.append(
            {"text": item.get("link_label") or f"📖 Leggi su {feed['source']}", "url": item["link"]}
        )
    if item.get("discussion_url"):
        buttons.append({"text": item["discussion_label"], "url": item["discussion_url"]})
    return buttons


def send_category(feed: dict, items: list[dict], dedup: Deduplicator) -> bool:
    """Filter out duplicates, apply the per-category cap and send the
    messages (all silent: only the first message of the digest, in main(),
    raises a notification). Returns True if at least one story was sent."""
    fresh_items = []
    for item in items:
        if dedup.is_duplicate(item["title"], item["link"]):
            log.info("Dropped duplicate: %s", item["title"][:70])
            continue
        dedup.remember(item["title"], item["link"])
        fresh_items.append(item)
        if len(fresh_items) >= MAX_ITEMS_PER_CATEGORY:
            break

    if not fresh_items:
        log.info(
            "%s (%s): nothing sent - %s items in the window, all already sent earlier.",
            feed["category"],
            feed["source"],
            len(items),
        )
        return False

    send_telegram_message(format_category_header(feed, len(fresh_items)), silent=True)
    time.sleep(SECONDS_BETWEEN_MESSAGES)

    for item in fresh_items:
        if feed["lang"] == "nl":
            translate_dutch_item(item)
        send_telegram_message(
            format_news_message(item),
            silent=True,
            preview_url=item["link"] or None,
            buttons=format_news_buttons(item, feed),
        )
        time.sleep(SECONDS_BETWEEN_MESSAGES)

    return True


def translate_dutch_item(item: dict) -> None:
    """Add the English title (and, when TRANSLATE_SUMMARIES is on, the
    English excerpt) to a Dutch story, in place. Every failure is silent by
    design: the story still goes out in Dutch rather than not at all."""
    item["title_en"] = translate_to_english(item["title"], source_lang="nl")
    if item.get("summary") and TRANSLATE_SUMMARIES:
        translated = translate_to_english(item["summary"], source_lang="nl")
        if translated:
            item["summary"] = translated


def main() -> None:
    log.info("Collecting stories from the last %s hours...", HOURS_LOOKBACK)

    # The only "loud" message of the digest: it produces the single
    # notification of the day. Everything below it is sent silently.
    send_telegram_message(format_digest_header(datetime.now()), silent=False)
    time.sleep(SECONDS_BETWEEN_MESSAGES)

    dedup = Deduplicator(threshold=DUPLICATE_TITLE_THRESHOLD)
    dedup.load(SENT_STATE_FILE, SENT_STATE_TTL_DAYS)
    any_sent = False

    for feed in FEEDS:
        if feed.get("fetcher") == "hackernews":
            items = fetch_hacker_news_top()
        else:
            items = fetch_feed_items(feed["url"])
        if send_category(feed, items, dedup):
            any_sent = True

    if not any_sent:
        send_telegram_message(
            "<i>Nessuna notizia nuova trovata nelle ultime ore.</i>", silent=True
        )

    # Always after sending, never before: if the bot dies halfway through
    # the digest, repeating a few stories tomorrow beats marking as "sent"
    # items that never actually arrived.
    dedup.save(SENT_STATE_FILE, SENT_STATE_TTL_DAYS)

    log.info("Digest sent to Telegram (one notification, the rest silent, duplicates dropped).")


if __name__ == "__main__":
    main()
