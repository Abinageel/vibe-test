# News agent

A small program that writes you a daily AI news briefing.

Each time you run it, it finds the ten most important new stories about AI from the last 24 hours, written for a tech lead. For each story you get:

- a one-line headline,
- two lines on why it matters to a tech lead,
- the link.

Every item is checked against the article it came from. If an item can't be checked, it's dropped. The last line of the briefing says how many sources were read and how many items were dropped.

## What it reads

It reads these five sources first:

| Source | Why it's there |
|---|---|
| Ars Technica (AI section) | Careful technical reporting |
| The Verge (AI section) | Wide coverage of product launches |
| TechCrunch (AI section) | Company, product and funding news |
| Hugging Face Blog | Open models and developer tools |
| Simon Willison's Weblog | Hands-on notes on new models and tools from a working engineer |

It skips opinion pieces, rumours and leaks, sponsored posts and deals, small funding rounds that come with no new product, gadget reviews, and podcast announcements.

You can change any of this in `news_agent/config.toml` (see "Changing what it looks for" below).

## What you need

- Python 3.11 or newer.
- An Anthropic API key (get one at https://console.anthropic.com), or an OpenRouter key (https://openrouter.ai). Each run costs money.
- An internet connection.

## Setting it up (once)

Open a terminal in this folder. Then run these commands one at a time.

Make a private Python space for the program:

```bash
python3 -m venv .venv
```

Switch to it (on Windows, run `.venv\Scripts\activate` instead):

```bash
source .venv/bin/activate
```

Install what it needs:

```bash
pip install -r requirements.txt
```

Give it your API key (replace the part in quotes with your own key):

```bash
export ANTHROPIC_API_KEY="your-key-here"
```

Or, if you use OpenRouter instead, give it your OpenRouter key:

```bash
export OPENROUTER_API_KEY="your-key-here"
```

If both are set, it uses the Anthropic key. Through OpenRouter, everything works the same except one thing: if Claude declines a request, there is no automatic retry on another model, and the item is simply dropped.

## Running it

```bash
python news_agent/news_agent.py
```

It takes a minute or two. The briefing is printed on screen and saved in the `briefings` folder, named by date (for example `briefings/2026-11-19.md`).

To also see which items were dropped and why, and which sources could not be reached:

```bash
python news_agent/news_agent.py --verbose
```

## What a briefing looks like

```
# Artificial intelligence briefing - 2026-11-19

## 1. Example Labs releases open-weights coding model under MIT licence

The model can be self-hosted, so teams can run it without sending code to a vendor.
Worth a trial against your current assistant for routine refactors.

Ars Technica - AI: https://arstechnica.com/...

...

---
Read 5 of 5 sources; dropped 2 items that could not be checked.
```

## How it works

1. **Reads the feeds.** It downloads each source's news feed and keeps only stories from the last 24 hours. Stories with words like "deal" or "podcast" in the title are thrown out straight away.
2. **Reads the articles.** It opens each story's web page and saves the article text.
3. **Picks the most important.** It sends the stories to Claude (Anthropic's AI model), with your topic, your reader and your skip rules. Claude picks the most important ones, in order, and writes the headline and the two lines. It's told to use only facts from the article. It picks a few spares in case some fail the next step.
4. **Checks every claim.** For each item, Claude is given the full article and the summary. It lists every fact in the summary and has to copy, word for word, the sentence in the article that backs it up. The program then checks for itself that the copied sentence really is in the article. If any fact isn't backed up, or the article couldn't be read, the item is dropped and the next spare takes its place.
5. **Writes the briefing.** The items that passed, in order of importance, followed by the count line.

If Claude declines a request, the program retries it on another Claude model. If that also declines, the item is dropped.

## Changing what it looks for

Open `news_agent/config.toml` in any text editor.

- **Topic and reader:** change `topic` and `reader`. For example `reader = "a CTO at a 50-person startup"`.
- **Number of items or time window:** change `max_items` and `lookback_hours`.
- **Sources:** each source is a block like this. Add or remove blocks. The address must be the site's RSS or Atom feed, not its home page. Most news sites list it as "RSS".
  ```toml
  [[sources]]
  name = "Example News"
  url  = "https://example.com/feed.xml"
  ```
- **What to skip:** `rules` are plain-English instructions for Claude. `title_keywords` are words that remove a story before Claude sees it, if they appear in its title.

## Running it every day

On a Mac or Linux machine you can schedule it with `cron`. Run `crontab -e` and add a line like this one, which runs at 7:00 every morning (use your own folder path and key):

```
0 7 * * * cd /path/to/this/folder && ANTHROPIC_API_KEY="your-key-here" .venv/bin/python news_agent/news_agent.py
```

## Good to know

- **A source can be down.** If a feed can't be reached, the program carries on with the rest. The last line shows how many were actually read ("Read 4 of 5 sources").
- **Some sites block automated readers.** If a site won't let the program read an article, it falls back to the text in the feed. If that's too short to check against, the item is dropped.
- **Fewer than ten items** means there weren't ten stories that passed the checks. That's on purpose: it would rather tell you less than tell you something it couldn't check.
- **The check covers facts, not opinions.** "Why it matters" is a judgement. The program checks that the facts in it come from the article, but whether it matters to you is up to you.

## Testing

The tests run without the internet or an API key, using made-up feeds and a stand-in for Claude:

```bash
python -m unittest discover tests
```

## Files

- `news_agent/news_agent.py` - the program.
- `news_agent/config.toml` - your settings.
- `tests/test_news_agent.py` - the tests.
- `requirements.txt` - the Python packages it needs.
- `briefings/` - where briefings are saved (made on first run; not saved to git).
