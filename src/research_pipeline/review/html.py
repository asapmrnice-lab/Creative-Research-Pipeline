"""Render the store as one self-contained HTML page.

Kept apart from the server so the markup can be tested without opening a
socket, and so no dependency beyond the standard library is needed: this
project declares no runtime dependencies, and a viewer is not a good reason
to acquire a web framework.

Every piece of post text is escaped before it reaches the page. Posts are
arbitrary text pulled off the internet -- treating them as trusted markup
would let a channel inject script into your own review tool.
"""

from __future__ import annotations

import html
import re
from datetime import datetime

from ..filtering.keywords import KeywordFilter
from ..storage.reader import ItemDetail, StoreStats

STYLE = """
:root {
  --bg: #f6f7f9; --card: #ffffff; --ink: #14161a; --muted: #6b7280;
  --line: #e3e6ea; --accent: #2563eb; --mark: #fde68a; --mark-ink: #3f2d00;
  --chip: #eef2f7; --note: #f0fdf4; --note-line: #86efac;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14161a; --card: #1c1f26; --ink: #e8eaed; --muted: #9aa3b2;
    --line: #2b3038; --accent: #7aa2f7; --mark: #7c5c00; --mark-ink: #ffe9a8;
    --chip: #262b34; --note: #16241b; --note-line: #2f6b45;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 1rem 4rem; background: var(--bg); color: var(--ink);
  font: 15px/1.55 -apple-system, "Segoe UI", Roboto, "Noto Sans", sans-serif;
}
.wrap { max-width: 900px; margin: 0 auto; }
header { padding: 1.5rem 0 1rem; }
h1 { font-size: 1.4rem; margin: 0 0 .35rem; }
.sub { color: var(--muted); font-size: .9rem; }
.tally { margin: .75rem 0 0; display: flex; flex-wrap: wrap; gap: .4rem; }
.chip {
  background: var(--chip); border: 1px solid var(--line); border-radius: 999px;
  padding: .15rem .6rem; font-size: .8rem; color: var(--muted);
}
.chip b { color: var(--ink); font-weight: 600; }
form.tools { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; margin: 1rem 0; }
input[type=search], textarea {
  background: var(--card); color: var(--ink); border: 1px solid var(--line);
  border-radius: 8px; padding: .5rem .7rem; font: inherit;
}
input[type=search] { flex: 1 1 260px; }
button {
  background: var(--accent); color: #fff; border: 0; border-radius: 8px;
  padding: .5rem .9rem; font: inherit; cursor: pointer;
}
button.ghost { background: transparent; color: var(--muted); border: 1px solid var(--line); }
label.toggle { display: flex; gap: .35rem; align-items: center; color: var(--muted); font-size: .9rem; }
article {
  background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 1rem 1.1rem; margin: 0 0 1rem;
}
article.unreviewed { border-left: 3px solid var(--accent); }
.meta { display: flex; flex-wrap: wrap; gap: .5rem; align-items: baseline; margin-bottom: .5rem; }
.meta .who { font-weight: 600; }
.meta .when, .meta .id { color: var(--muted); font-size: .85rem; }
.kw {
  background: var(--mark); color: var(--mark-ink); border-radius: 6px;
  padding: .05rem .45rem; font-size: .78rem; font-weight: 600;
}
.text { white-space: pre-wrap; overflow-wrap: anywhere; margin: .5rem 0 .75rem; }
mark { background: var(--mark); color: var(--mark-ink); border-radius: 3px; padding: 0 .1rem; }
.foot { display: flex; flex-wrap: wrap; gap: .75rem; align-items: center;
        border-top: 1px solid var(--line); padding-top: .6rem; font-size: .88rem; }
a { color: var(--accent); }
.note {
  background: var(--note); border-left: 3px solid var(--note-line);
  border-radius: 6px; padding: .5rem .7rem; margin: .5rem 0 0; font-size: .92rem;
}
.note .stamp { color: var(--muted); font-size: .78rem; }
details summary { cursor: pointer; color: var(--muted); font-size: .88rem; }
form.note { display: flex; gap: .5rem; margin-top: .6rem; }
form.note textarea { flex: 1; min-height: 2.4rem; resize: vertical; }
.empty { text-align: center; color: var(--muted); padding: 3rem 0; }
"""


def _e(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _when(value: str | None, width: int = 16) -> str:
    return (value or "").replace("T", " ")[:width]


def highlight(text: str, kf: KeywordFilter | None) -> str:
    """Escape post text, wrapping keyword hits in <mark>.

    The filter's own offsets point into normalized text, so they cannot be
    used to slice the original. Instead the same word pattern is re-applied
    to the raw text case-insensitively. Highlighting is cosmetic: a form that
    escapes it is still stored and still listed -- only the yellow is missed.
    """
    if not kf:
        return _e(text)

    stems = "|".join(
        pattern.pattern.replace("(?<![\\w-])", "").replace("(?![\\w-])", "")
        for _, pattern in kf._patterns
    )
    if not stems:
        return _e(text)

    marker = re.compile(rf"(?<![\w-])(?:{stems})(?![\w-])", re.IGNORECASE)
    out, last = [], 0
    for m in marker.finditer(text):
        out.append(_e(text[last : m.start()]))
        out.append(f"<mark>{_e(m.group(0))}</mark>")
        last = m.end()
    out.append(_e(text[last:]))
    return "".join(out)


def _card(item: ItemDetail, kf: KeywordFilter | None, query: str, unreviewed: bool) -> str:
    keywords = "".join(f'<span class="kw">{_e(k)}</span> ' for k in item.keywords)
    manual = [f for f in item.fields if f.origin == "human"]
    manual_html = (
        '<div class="foot">'
        + " ".join(f'<span class="chip"><b>{_e(f.name)}</b> {_e(f.value)}</span>' for f in manual)
        + "</div>"
        if manual
        else ""
    )
    notes_html = "".join(
        f'<div class="note">{_e(n.body)}<div class="stamp">{_e(_when(n.created_at))}</div></div>'
        for n in item.notes
    )
    link = (
        f'<a href="{_e(item.url)}" target="_blank" rel="noopener">open in Telegram →</a>'
        if item.url
        else ""
    )
    state = "" if item.notes else " unreviewed"
    # The form carries the current view back, so adding a note does not throw
    # you out of the search or filter you were reading in.
    keep = f'<input type="hidden" name="q" value="{_e(query)}">' \
           f'<input type="hidden" name="unreviewed" value="{"1" if unreviewed else ""}">'
    return f"""
<article class="item{state}" id="i{item.id}">
  <div class="meta">
    <span class="who">{_e(item.channel)}</span>
    <span class="when">{_e(_when(item.posted_at))}</span>
    <span class="id">#{item.id}</span>
    <span>{keywords}</span>
  </div>
  <div class="text">{highlight(item.text, kf)}</div>
  {manual_html}
  <div class="foot">{link}<span class="when">{len(item.notes)} note(s)</span></div>
  {notes_html}
  <form class="note" method="post" action="/note">
    <input type="hidden" name="id" value="{item.id}">{keep}
    <textarea name="body" placeholder="your note on this post…" required></textarea>
    <button type="submit">save</button>
  </form>
</article>"""


def render_page(
    stats: StoreStats,
    items: list[ItemDetail],
    *,
    kf: KeywordFilter | None = None,
    query: str = "",
    unreviewed: bool = False,
    db_label: str = "",
) -> str:
    """The whole page: header, tools, one card per item."""
    tally = "".join(
        f'<span class="chip"><b>{_e(word)}</b> {count}</span>'
        for word, count in stats.keywords.items()
    )
    showing = (
        f"{len(items)} of {stats.items} items"
        if len(items) != stats.items
        else f"{stats.items} items"
    )
    cards = (
        "".join(_card(i, kf, query, unreviewed) for i in items)
        or '<p class="empty">Nothing matches this view.</p>'
    )
    return f"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Research store — {stats.items} matched posts</title>
<style>{STYLE}</style>
</head><body><div class="wrap">
<header>
  <h1>Research store</h1>
  <div class="sub">{_e(showing)} · {_e(_when(stats.first_post, 10))} – {_e(_when(stats.last_post, 10))}
    · {stats.unreviewed} un-reviewed · {_e(db_label)}</div>
  <div class="tally">{tally}</div>
  <form class="tools" method="get" action="/">
    <input type="search" name="q" value="{_e(query)}" placeholder="search the collected posts…">
    <label class="toggle"><input type="checkbox" name="unreviewed" value="1"
      {"checked" if unreviewed else ""} onchange="this.form.submit()"> un-reviewed only</label>
    <button type="submit">search</button>
    <a href="/"><button type="button" class="ghost">reset</button></a>
  </form>
</header>
{cards}
<p class="sub">Generated {_e(datetime.now().strftime("%Y-%m-%d %H:%M"))} · read-only view;
notes you add are written to the store.</p>
</div></body></html>"""
