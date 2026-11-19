"""News agent: the most important new items on a topic, checked against their sources.

Run:  python news_agent/news_agent.py
See README.md for setup.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
import unicodedata
import warnings
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import anthropic
import requests
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

HERE = Path(__file__).resolve().parent
USER_AGENT = "Mozilla/5.0 (compatible; news-agent/1.0)"
MIN_SOURCE_CHARS = 300          # shorter than this and there is nothing to check against
MAX_SOURCE_CHARS = 60_000       # longest article text sent to Claude for checking
MAX_CANDIDATES = 60             # most stories passed to the ranking step
FALLBACK_BETA = "server-side-fallback-2026-07-01"


# --------------------------------------------------------------------------- data


@dataclass
class Story:
    id: str
    source: str
    title: str
    link: str
    published: datetime
    feed_text: str = ""
    article_text: str = ""

    @property
    def text(self) -> str:
        """The best text we have for this story: the full article, else the feed's copy."""
        best = max(self.article_text, self.feed_text, key=len)
        return best[:MAX_SOURCE_CHARS]


@dataclass
class Item:
    story: Story
    headline: str
    why: str


@dataclass
class RunResult:
    items: list[Item]
    sources_read: int
    sources_failed: list[str]
    dropped: list[tuple[str, str]] = field(default_factory=list)  # (title, reason)


# --------------------------------------------------------------------------- feeds


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(el: ET.Element, *names: str) -> str:
    for child in el:
        if _strip_ns(child.tag) in names and (child.text or "").strip():
            return child.text.strip()
    return ""


def _parse_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)              # RSS style
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))  # Atom style
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def html_to_text(html: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(html or "", "html.parser").get_text(" ")).strip()


def parse_feed(xml_bytes: bytes, source: str) -> list[Story]:
    root = ET.fromstring(xml_bytes)
    stories = []
    for el in root.iter():
        kind = _strip_ns(el.tag)
        if kind not in ("item", "entry"):
            continue
        title = html_to_text(_child_text(el, "title"))
        link = _child_text(el, "link")
        if not link:  # Atom puts the address in an attribute
            for child in el:
                if _strip_ns(child.tag) == "link" and child.get("rel", "alternate") == "alternate":
                    link = child.get("href", "")
                    break
        published = _parse_date(_child_text(el, "pubDate", "published", "updated", "date"))
        body = _child_text(el, "encoded", "content") or _child_text(el, "description", "summary")
        if title and link and published:
            stories.append(Story("", source, title, link.strip(), published, html_to_text(body)))
    return stories


def fetch_feeds(sources: list[dict], session: requests.Session) -> tuple[list[Story], int, list[str]]:
    stories, read, failed = [], 0, []
    for src in sources:
        try:
            resp = session.get(src["url"], timeout=20)
            resp.raise_for_status()
            stories.extend(parse_feed(resp.content, src["name"]))
            read += 1
        except Exception as exc:  # one bad feed must not stop the run
            failed.append(f"{src['name']} ({type(exc).__name__})")
    return stories, read, failed


def fetch_article(story: Story, session: requests.Session) -> None:
    try:
        resp = session.get(story.link, timeout=20)
        resp.raise_for_status()
    except Exception:
        return
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    body = soup.find("article") or soup.find("main") or soup.body or soup
    paragraphs = [p.get_text(" ", strip=True) for p in body.find_all(["p", "li", "h2", "h3"])]
    story.article_text = re.sub(r"\s+", " ", " ".join(paragraphs)).strip()


def select_recent(stories: list[Story], hours: int, title_keywords: list[str], now: datetime) -> list[Story]:
    cutoff = now - timedelta(hours=hours)
    seen, keep = set(), []
    banned = [k.lower() for k in title_keywords]
    for s in sorted(stories, key=lambda s: s.published, reverse=True):
        key = s.link.split("?")[0].rstrip("/")
        if s.published < cutoff or s.published > now + timedelta(hours=1) or key in seen:
            continue
        if any(k in s.title.lower() for k in banned):
            continue
        seen.add(key)
        keep.append(s)
    keep = keep[:MAX_CANDIDATES]
    for i, s in enumerate(keep, 1):
        s.id = f"s{i}"
    return keep


# --------------------------------------------------------------------------- Claude


OPENROUTER_URL = "https://openrouter.ai/api"


def make_client():
    """Anthropic directly if ANTHROPIC_API_KEY is set, else OpenRouter if OPENROUTER_API_KEY is set."""
    if os.environ.get("ANTHROPIC_API_KEY") or not os.environ.get("OPENROUTER_API_KEY"):
        return anthropic.Anthropic()
    client = anthropic.Anthropic(
        base_url=OPENROUTER_URL, auth_token=os.environ["OPENROUTER_API_KEY"], api_key=None
    )
    client.via_openrouter = True
    return client


def model_name(client, model: str) -> str:
    """OpenRouter names models differently: claude-opus-5-5 -> anthropic/claude-opus-5.5."""
    if getattr(client, "via_openrouter", False) and "/" not in model:
        return "anthropic/" + re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", model)
    return model


def ask_claude(client, model: str, prompt: str, schema: dict, effort: str) -> dict | None:
    """One Claude call that must answer as JSON matching `schema`. None if Claude declined."""
    extra = {}
    if not getattr(client, "via_openrouter", False):  # OpenRouter does not accept this setting
        # If the model declines, the API retries the request on a fallback model.
        extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
    response = client.beta.messages.create(
        model=model_name(client, model),
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
        **extra,
    )
    if response.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in response.content if b.type == "text")
    return json.loads(text)


RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "headline": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                },
                "required": ["id", "headline", "why_it_matters"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["picks"],
    "additionalProperties": False,
}

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "supported": {"type": "boolean"},
                    "quote": {"type": "string"},
                },
                "required": ["claim", "supported", "quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}


def rank(client, cfg: dict, stories: list[Story], want: int) -> list[Item]:
    listing = "\n\n".join(
        f"<story id=\"{s.id}\">\nSource: {s.source}\nPublished: {s.published:%Y-%m-%d %H:%M} UTC\n"
        f"Title: {s.title}\nText: {s.text[:1500]}\n</story>"
        for s in stories
    )
    ignore = "\n".join(f"- {r}" for r in cfg["ignore"]["rules"])
    prompt = f"""You are editing a daily news briefing.

Topic: {cfg['topic']}
Reader: {cfg['reader']}

Below are stories published in the last {cfg['lookback_hours']} hours. Pick the up to {want} most important ones for this reader, most important first. Importance means: how much it should change what this reader decides or does this week.

Leave out any story that matches these rules:
{ignore}

If two stories cover the same event, pick only the one with the fuller text. Leave out stories that are off-topic. It is fine to return fewer than {want}.

For each pick give:
- id: the story id.
- headline: one line, plain and factual, under 15 words, using only facts stated in that story's text.
- why_it_matters: exactly two short sentences on why it matters to this reader. Any fact you state must come from that story's text; do not add numbers, names or dates that are not in it.

Stories:
{listing}"""
    data = ask_claude(client, cfg["model"], prompt, RANK_SCHEMA, effort="high")
    if data is None:
        raise RuntimeError("Claude declined the ranking request.")
    by_id = {s.id: s for s in stories}
    items, used = [], set()
    for p in data["picks"]:
        if p["id"] in by_id and p["id"] not in used:
            used.add(p["id"])
            items.append(Item(by_id[p["id"]], p["headline"].strip(), p["why_it_matters"].strip()))
    return items


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"[^a-z0-9%$.']+", " ", text).strip()


def check(client, model: str, item: Item) -> str | None:
    """Check the item against its source. Returns None if it passes, or the reason it fails."""
    source = item.story.text
    if len(source) < MIN_SOURCE_CHARS:
        return "could not read enough of the source to check it"
    prompt = f"""Check a news summary against its source article.

<summary>
Headline: {item.headline}
Why it matters: {item.why}
</summary>

<source title="{item.story.title}" url="{item.story.link}">
{source}
</source>

Split the summary into its factual claims (who did what, numbers, names, dates, what a product does). Judgements about why something matters to a reader are not factual claims; skip them unless they slip in a new fact.

For each factual claim:
- supported: true only if the source states it. Not "likely", not "implied".
- quote: copy, word for word, the shortest sentence or phrase from the source that supports it. Empty if not supported."""
    data = ask_claude(client, model, prompt, CHECK_SCHEMA, effort="medium")
    if data is None:
        return "the checker declined to review it"
    if not data["claims"]:
        return "no checkable claims found"
    haystack = _normalise(source)
    for c in data["claims"]:
        if not c["supported"]:
            return f"not in source: {c['claim']}"
        if _normalise(c["quote"]) not in haystack:  # the quote must really be in the article
            return f"supporting quote not found in source: {c['claim']}"
    return None


# --------------------------------------------------------------------------- run


def run(cfg: dict, client, session: requests.Session, now: datetime | None = None) -> RunResult:
    now = now or datetime.now(timezone.utc)
    want = cfg["max_items"]

    raw, read, failed = fetch_feeds(cfg["sources"], session)
    stories = select_recent(raw, cfg["lookback_hours"], cfg["ignore"].get("title_keywords", []), now)
    result = RunResult([], read, failed)
    if not stories:
        return result

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda s: fetch_article(s, session), stories))

    ranked = rank(client, cfg, stories, want + 5)  # a few spares in case some fail the check

    # Check in rank order; replace any that fail with the next spare.
    queue = list(ranked)
    while queue and len(result.items) < want:
        batch, queue = queue[: want - len(result.items)], queue[want - len(result.items):]
        with ThreadPoolExecutor(max_workers=5) as pool:
            verdicts = list(pool.map(lambda it: _safe_check(client, cfg["model"], it), batch))
        for item, reason in zip(batch, verdicts):
            if reason is None:
                result.items.append(item)
            else:
                result.dropped.append((item.story.title, reason))
    result.items.sort(key=lambda it: ranked.index(it))
    return result


def _safe_check(client, model: str, item: Item) -> str | None:
    try:
        return check(client, model, item)
    except Exception as exc:
        return f"check failed ({type(exc).__name__})"


def render(cfg: dict, result: RunResult, now: datetime) -> str:
    lines = [f"# {cfg['topic'].split(':')[0].strip().capitalize()} briefing - {now:%Y-%m-%d}", ""]
    if not result.items:
        lines += ["No items passed the checks today.", ""]
    for n, it in enumerate(result.items, 1):
        lines += [f"## {n}. {it.headline}", "", it.why, "", f"{it.story.source}: {it.story.link}", ""]
    lines.append("---")
    lines.append(
        f"Read {result.sources_read} of {len(cfg['sources'])} sources; "
        f"dropped {len(result.dropped)} item{'s' if len(result.dropped) != 1 else ''} that could not be checked."
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daily news briefing, checked against its sources.")
    ap.add_argument("--config", default=str(HERE / "config.toml"), help="settings file")
    ap.add_argument("--out", default=str(HERE.parent / "briefings"), help="folder to save briefings in")
    ap.add_argument("--verbose", action="store_true", help="also list dropped items and failed sources")
    args = ap.parse_args(argv)

    cfg = tomllib.loads(Path(args.config).read_text())
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    now = datetime.now(timezone.utc)

    result = run(cfg, make_client(), session, now)
    report = render(cfg, result, now)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{now:%Y-%m-%d}.md"
    out_file.write_text(report)
    print(report)
    if args.verbose:
        for name in result.sources_failed:
            print(f"[source failed] {name}", file=sys.stderr)
        for title, reason in result.dropped:
            print(f"[dropped] {title}: {reason}", file=sys.stderr)
    print(f"Saved to {out_file}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
