"""Offline tests: fake feeds, fake articles and a fake Claude. Run: python -m unittest discover tests"""

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "news_agent"))
import news_agent as na  # noqa: E402

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
FRESH = format_datetime(NOW - timedelta(hours=3))
STALE = format_datetime(NOW - timedelta(hours=40))

ARTICLE_A = "<html><body><article>" + "<p>Acme released Model X on Tuesday. It is open weights under the MIT licence.</p>" * 10 + "</article></body></html>"
ARTICLE_B = "<html><body><article>" + "<p>Beta Corp shipped a new code review tool for teams.</p>" * 10 + "</article></body></html>"

RSS = f"""<?xml version="1.0"?><rss><channel>
<item><title>Acme releases Model X</title><link>https://a.test/x</link><pubDate>{FRESH}</pubDate><description>Short.</description></item>
<item><title>Beta Corp code review tool</title><link>https://a.test/b</link><pubDate>{FRESH}</pubDate><description>Short.</description></item>
<item><title>Old news</title><link>https://a.test/old</link><pubDate>{STALE}</pubDate></item>
<item><title>Big deal on laptops</title><link>https://a.test/deal</link><pubDate>{FRESH}</pubDate></item>
</channel></rss>"""

ATOM = f"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Unreadable post</title><link href="https://b.test/u" rel="alternate"/>
<updated>{(NOW - timedelta(hours=1)).isoformat()}</updated><summary>tiny</summary></entry>
</feed>"""

PAGES = {
    "https://feed.test/rss": RSS,
    "https://feed.test/atom": ATOM,
    "https://a.test/x": ARTICLE_A,
    "https://a.test/b": ARTICLE_B,
}


class FakeSession:
    def get(self, url, timeout=None):
        if url not in PAGES:
            raise ConnectionError(url)
        body = PAGES[url]
        return SimpleNamespace(content=body.encode(), text=body, raise_for_status=lambda: None)


class FakeClaude:
    """Ranks every story; the checker rejects the Beta Corp item as unsupported."""

    def __init__(self):
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        prompt = kw["messages"][0]["content"]
        if "Pick the up to" in prompt:
            ids = [line.split('"')[1] for line in prompt.splitlines() if line.startswith("<story id=")]
            out = {"picks": [{"id": i, "headline": f"H{i}", "why_it_matters": "W."} for i in ids]}
        elif "Acme" in prompt:
            out = {"claims": [{"claim": "Acme released Model X", "supported": True,
                               "quote": "Acme released Model X on Tuesday."}]}
        else:
            out = {"claims": [{"claim": "Beta Corp raised $1B", "supported": False, "quote": ""}]}
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(out))])


CFG = {
    "topic": "AI: models", "reader": "a tech lead", "max_items": 10, "lookback_hours": 24,
    "model": "claude-opus-5-5",
    "sources": [{"name": "Feed A", "url": "https://feed.test/rss"},
                {"name": "Feed B", "url": "https://feed.test/atom"},
                {"name": "Feed C", "url": "https://feed.test/missing"}],
    "ignore": {"rules": ["deals"], "title_keywords": ["deal"]},
}


class NewsAgentTest(unittest.TestCase):
    def test_end_to_end(self):
        claude = FakeClaude()
        result = na.run(CFG, claude, FakeSession(), NOW)
        self.assertEqual(result.sources_read, 2)
        self.assertEqual([it.story.link for it in result.items], ["https://a.test/x"])
        reasons = dict(result.dropped)
        self.assertIn("not in source", reasons["Beta Corp code review tool"])
        self.assertIn("could not read", reasons["Unreadable post"])
        self.assertNotIn("Old news", reasons)           # outside 24 hours, never considered
        self.assertNotIn("Big deal on laptops", reasons)  # title keyword filter
        req = claude.calls[0]
        self.assertEqual(req["model"], "claude-opus-5-5")
        self.assertEqual(req["fallbacks"], "default")

        report = na.render(CFG, result, NOW)
        self.assertIn("## 1. Hs", report)
        self.assertIn("https://a.test/x", report)
        self.assertTrue(report.strip().endswith("Read 2 of 3 sources; dropped 2 items that could not be checked."))

    def test_fake_quote_is_caught(self):
        story = na.Story("s1", "A", "t", "https://a.test/x", NOW, article_text="Acme released Model X on Tuesday. " * 20)
        item = na.Item(story, "h", "w")
        liar = FakeClaude()
        liar.create = lambda **kw: SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(
            type="text", text=json.dumps({"claims": [{"claim": "c", "supported": True, "quote": "Acme bought Google"}]}))])
        liar.beta.messages.create = liar.create
        self.assertIn("quote not found", na.check(liar, "m", item))

    def test_refusal_drops_item(self):
        story = na.Story("s1", "A", "t", "https://a.test/x", NOW, article_text="x" * 500)
        c = FakeClaude()
        c.beta.messages.create = lambda **kw: SimpleNamespace(stop_reason="refusal", content=[])
        self.assertIn("declined", na.check(c, "m", na.Item(story, "h", "w")))


if __name__ == "__main__":
    unittest.main()
