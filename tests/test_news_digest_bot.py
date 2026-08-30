"""Unit tests for news_digest_bot.

Everything here runs offline: no real Telegram, RSS or translation calls.
`requests.post` / `requests.get` and the module's own senders are
monkeypatched. Run with:

    pip install -r requirements-dev.txt
    pytest
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import news_digest_bot as bot


class DummyResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, ok=True, status_code=200, payload=None, text=""):
        self.ok = ok
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch):
    """Non-empty credentials + no real sleeping between messages."""
    monkeypatch.setattr(bot, "TELEGRAM_BOT_TOKEN", "TESTTOKEN")
    monkeypatch.setattr(bot, "TELEGRAM_CHAT_ID", "-100999")
    monkeypatch.setattr(bot.time, "sleep", lambda *_: None)


def _feed(**over):
    base = {
        "emoji": "💰",
        "category": "Finanza globale",
        "source": "Yahoo Finance",
        "url": "https://example.test/rss",
        "lang": "en",
        "group": bot.GROUP_FINANCE,
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# _normalize_title / _not_expired / clean_summary
# --------------------------------------------------------------------------

def test_normalize_title_lowercases_and_strips_punctuation():
    assert bot._normalize_title("OpenAI's GPT-5: Released!  ") == "openais gpt5 released"


def test_not_expired_keeps_recent_drops_old_and_malformed():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    entries = {
        "fresh": now.isoformat(),
        "stale": (now - timedelta(days=30)).isoformat(),
        "broken": "not-a-date",
        "missing": None,
    }
    assert set(bot._not_expired(entries, cutoff)) == {"fresh"}


def test_not_expired_handles_none_input():
    assert bot._not_expired(None, datetime.now(timezone.utc)) == {}


def test_clean_summary_drops_one_that_only_repeats_the_title():
    title = "Markets rally as inflation cools"
    assert bot.clean_summary("<p>Markets rally as inflation cools</p>", title) == ""


def test_clean_summary_strips_html_and_boilerplate():
    out = bot.clean_summary(
        "<p>A real excerpt here.</p> The post Foo appeared first on Bar", "Different title"
    )
    assert out == "A real excerpt here."


# --------------------------------------------------------------------------
# Deduplicator
# --------------------------------------------------------------------------

def test_deduplicator_matches_exact_link():
    d = bot.Deduplicator(threshold=0.6)
    d.remember("Some headline", "https://example.com/a")
    assert d.is_duplicate("A completely unrelated headline", "https://example.com/a")


def test_deduplicator_matches_similar_title_across_different_links():
    d = bot.Deduplicator(threshold=0.6)
    d.remember("OpenAI releases a new reasoning model", "https://a.test/1")
    assert d.is_duplicate("OpenAI releases new reasoning model", "https://b.test/2")


def test_deduplicator_lets_unrelated_stories_through():
    d = bot.Deduplicator(threshold=0.6)
    d.remember("OpenAI releases a new reasoning model", "https://a.test/1")
    assert not d.is_duplicate("Amsterdam city council approves budget", "https://c.test/3")


def test_deduplicator_load_ignores_expired(tmp_path):
    now = datetime.now(timezone.utc)
    state = tmp_path / "sent.json"
    state.write_text(json.dumps({
        "links": {"https://old": (now - timedelta(days=99)).isoformat(),
                  "https://new": now.isoformat()},
        "titles": {"fresh title": now.isoformat()},
    }))
    d = bot.Deduplicator(threshold=0.6)
    d.load(state, ttl_days=7)
    assert "https://new" in d.seen_links and "https://old" not in d.seen_links


def test_deduplicator_load_missing_file_starts_empty(tmp_path):
    d = bot.Deduplicator(threshold=0.6)
    d.load(tmp_path / "nope.json", ttl_days=7)
    assert d.seen_links == {} and d.seen_titles == {}


# --------------------------------------------------------------------------
# FEEDS integrity
# --------------------------------------------------------------------------

def test_every_feed_declares_a_known_group():
    known = {key for key, _, _ in bot.TOPIC_DEFINITIONS}
    for feed in bot.FEEDS:
        assert feed.get("group") in known, feed.get("source")


def test_topic_definitions_cover_the_three_expected_groups():
    keys = {key for key, _, _ in bot.TOPIC_DEFINITIONS}
    assert keys == {bot.GROUP_GENERAL, bot.GROUP_TECH_AI, bot.GROUP_FINANCE}


def test_finance_is_its_own_group_and_last():
    assert bot.FEEDS[-1]["group"] == bot.GROUP_FINANCE
    assert sum(f["group"] == bot.GROUP_FINANCE for f in bot.FEEDS) == 1


# --------------------------------------------------------------------------
# send_telegram_message threading
# --------------------------------------------------------------------------

def test_send_message_includes_thread_id_when_given(monkeypatch):
    captured = {}
    monkeypatch.setattr(bot.requests, "post",
                        lambda url, data, timeout: captured.update(data) or DummyResponse(ok=True))
    assert bot.send_telegram_message("hello", thread_id=42) is True
    assert captured["message_thread_id"] == 42
    assert captured["chat_id"] == "-100999"


def test_send_message_omits_thread_id_when_none(monkeypatch):
    captured = {}
    monkeypatch.setattr(bot.requests, "post",
                        lambda url, data, timeout: captured.update(data) or DummyResponse(ok=True))
    assert bot.send_telegram_message("hello", thread_id=None) is True
    assert "message_thread_id" not in captured


def test_send_message_retries_on_429_then_succeeds(monkeypatch):
    calls = []

    def fake_post(url, data, timeout):
        calls.append(1)
        if len(calls) == 1:
            return DummyResponse(ok=False, status_code=429,
                                 payload={"parameters": {"retry_after": 0}})
        return DummyResponse(ok=True)

    monkeypatch.setattr(bot.requests, "post", fake_post)
    assert bot.send_telegram_message("hi") is True
    assert len(calls) == 2


def test_send_message_returns_false_on_hard_error(monkeypatch):
    monkeypatch.setattr(bot.requests, "post",
                        lambda url, data, timeout: DummyResponse(ok=False, status_code=400, text="bad"))
    assert bot.send_telegram_message("hi") is False


# --------------------------------------------------------------------------
# create_forum_topic
# --------------------------------------------------------------------------

def test_create_forum_topic_returns_thread_id(monkeypatch):
    monkeypatch.setattr(bot.requests, "post", lambda url, data, timeout: DummyResponse(
        payload={"ok": True, "result": {"message_thread_id": 55, "name": data["name"]}}))
    assert bot.create_forum_topic("📰 Generali", 0x6FB9F0) == 55


def test_create_forum_topic_none_when_chat_is_not_a_forum(monkeypatch):
    monkeypatch.setattr(bot.requests, "post", lambda url, data, timeout: DummyResponse(
        ok=False, status_code=400, text="Bad Request: the chat is not a forum"))
    assert bot.create_forum_topic("x", 1) is None


def test_create_forum_topic_none_on_network_error(monkeypatch):
    def boom(url, data, timeout):
        raise bot.requests.RequestException("connection reset")

    monkeypatch.setattr(bot.requests, "post", boom)
    assert bot.create_forum_topic("x", 1) is None


def test_create_forum_topic_none_when_result_has_no_thread_id(monkeypatch):
    monkeypatch.setattr(bot.requests, "post",
                        lambda url, data, timeout: DummyResponse(payload={"ok": True, "result": {}}))
    assert bot.create_forum_topic("x", 1) is None


# --------------------------------------------------------------------------
# resolve_group_threads
# --------------------------------------------------------------------------

def test_resolve_group_threads_all_none_when_disabled(monkeypatch):
    monkeypatch.setattr(bot, "TELEGRAM_TOPICS", False)
    threads = bot.resolve_group_threads()
    assert threads == {bot.GROUP_GENERAL: None, bot.GROUP_TECH_AI: None, bot.GROUP_FINANCE: None}


def test_resolve_group_threads_creates_then_caches(tmp_path, monkeypatch):
    state = tmp_path / "topics.json"
    monkeypatch.setattr(bot, "TELEGRAM_TOPICS", True)
    monkeypatch.setattr(bot, "TOPIC_STATE_FILE", state)

    created = []

    def fake_create(name, color):
        created.append(name)
        return 100 + len(created)

    monkeypatch.setattr(bot, "create_forum_topic", fake_create)

    first = bot.resolve_group_threads()
    assert first == {bot.GROUP_GENERAL: 101, bot.GROUP_TECH_AI: 102, bot.GROUP_FINANCE: 103}
    assert json.loads(state.read_text()) == {
        bot.GROUP_GENERAL: 101, bot.GROUP_TECH_AI: 102, bot.GROUP_FINANCE: 103}

    created.clear()
    second = bot.resolve_group_threads()
    assert created == []
    assert second == first


def test_resolve_group_threads_failed_group_stays_none_and_is_not_persisted(tmp_path, monkeypatch):
    state = tmp_path / "topics.json"
    monkeypatch.setattr(bot, "TELEGRAM_TOPICS", True)
    monkeypatch.setattr(bot, "TOPIC_STATE_FILE", state)

    def fake_create(name, color):
        return None if "Finanza" in name else 7

    monkeypatch.setattr(bot, "create_forum_topic", fake_create)

    threads = bot.resolve_group_threads()
    assert threads[bot.GROUP_FINANCE] is None
    assert threads[bot.GROUP_GENERAL] == 7
    assert bot.GROUP_FINANCE not in json.loads(state.read_text())


def test_load_topic_ids_skips_non_integer_values(tmp_path):
    p = tmp_path / "topics.json"
    p.write_text(json.dumps({"generali": 5, "tech_ai": "oops", "finanza": None}))
    assert bot.load_topic_ids(p) == {"generali": 5}


# --------------------------------------------------------------------------
# send_category routing
# --------------------------------------------------------------------------

def test_send_category_routes_every_message_to_the_thread(monkeypatch):
    sent = []

    def fake_send(text, silent=True, preview_url=None, buttons=None, thread_id=None):
        sent.append(thread_id)
        return True

    monkeypatch.setattr(bot, "send_telegram_message", fake_send)

    d = bot.Deduplicator(threshold=0.6)
    items = [{"title": "Markets rally", "link": "https://x.test/1", "summary": ""},
             {"title": "Bonds slide", "link": "https://x.test/2", "summary": ""}]
    ok = bot.send_category(_feed(), items, d, thread_id=9)

    assert ok is True
    assert sent and all(t == 9 for t in sent)   # header + every story


def test_send_category_returns_false_and_sends_nothing_when_empty(monkeypatch):
    sent = []
    monkeypatch.setattr(bot, "send_telegram_message", lambda *a, **k: sent.append(1) or True)
    d = bot.Deduplicator(threshold=0.6)
    assert bot.send_category(_feed(), [], d, thread_id=1) is False
    assert sent == []


def test_send_category_skips_duplicates(monkeypatch):
    sent = []
    monkeypatch.setattr(bot, "send_telegram_message", lambda *a, **k: sent.append(1) or True)
    d = bot.Deduplicator(threshold=0.6)
    d.remember("Already sent story", "https://dup.test/1")
    items = [{"title": "Already sent story", "link": "https://dup.test/1", "summary": ""}]
    assert bot.send_category(_feed(), items, d) is False
    assert sent == []


def test_send_category_respects_the_per_category_cap(monkeypatch):
    monkeypatch.setattr(bot, "MAX_ITEMS_PER_CATEGORY", 2)
    stories = []

    def fake_send(text, silent=True, preview_url=None, buttons=None, thread_id=None):
        if preview_url:
            stories.append(preview_url)
        return True

    monkeypatch.setattr(bot, "send_telegram_message", fake_send)
    d = bot.Deduplicator(threshold=0.6)
    titles = [
        "Central bank holds rates steady",
        "Tech shares tumble on earnings miss",
        "Oil prices climb after supply cut",
        "Housing starts beat forecasts",
        "Retail sales slump in July",
    ]
    items = [{"title": t, "link": f"https://x.test/{i}", "summary": ""} for i, t in enumerate(titles)]
    bot.send_category(_feed(), items, d, thread_id=None)
    assert len(stories) == 2
