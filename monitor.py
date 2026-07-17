#!/usr/bin/env python3
"""
odyssey-70mm-watch
==================
Monitors AMC Lincoln Square 13 for newly released showtimes of
"The Odyssey" in IMAX 70mm and sends a push notification when new
showtimes appear.

How it works
------------
1. Fetches the theatre's server-rendered showtimes page for each date in a
   rolling window (default: next 45 days).
2. Parses every showtime button (links to /showtimes/{id}) along with its
   movie title and premium-format heading (e.g. "IMAX 70MM").
   NOTE: AMC lists Odyssey IMAX 70mm shows under BOTH the regular
   "The Odyssey" listing and the "The Odyssey - IMAX 70mm Event" listing,
   so we match by title/format regex rather than a single movie id.
3. Diffs showtime IDs against state.json. Anything unseen -> notification
   via ntfy.sh and/or Pushover, with a direct seat-selection link.

First run seeds state silently (everything currently listed is "seen") and
sends a single "monitoring started" ping so you know it's alive.

Usage
-----
    python monitor.py                # normal run (use via cron)
    python monitor.py --dry-run      # scan + diff, but don't notify
    python monitor.py --dump         # save raw HTML to debug/ for inspection
    python monitor.py --reset        # wipe state and re-seed

Config is via environment variables -- see DEFAULTS below or README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cf

# --------------------------------------------------------------------------
# Configuration (override any of these with environment variables)
# --------------------------------------------------------------------------

DEFAULTS = {
    # The theatre showtimes page to watch.
    "THEATRE_SHOWTIMES_URL": (
        "https://www.amctheatres.com/movie-theatres/"
        "new-york-city/amc-lincoln-square-13/showtimes"
    ),
    # Case-insensitive regexes. Movie matches the listing title; format
    # matches the premium-format section heading. "imax\s*70" matches
    # "IMAX 70MM" but NOT the plain non-IMAX "70mm" section. To watch both,
    # set FORMAT_PATTERN='(imax\s*70|^70\s*mm)'.
    "MOVIE_PATTERN": r"odyssey",
    "FORMAT_PATTERN": r"imax\s*70",
    # How far ahead to scan, and when to stop early (N consecutive dates
    # with zero matching listings of any format -> assume run window ended).
    "DAYS_AHEAD": "45",
    "EMPTY_STREAK_STOP": "5",
    # If auto-discovery of the per-date URL fails, set this manually, e.g.
    # "https://.../showtimes?date={date}"  ({date} -> YYYY-MM-DD)
    "DATE_URL_TEMPLATE": "",
    # Politeness delay between page fetches (seconds; jitter added).
    "REQUEST_DELAY": "0.6",
    # State + notifications
    "STATE_FILE": "state.json",
    "NTFY_SERVER": "https://ntfy.sh",
    "NTFY_TOPIC": "",           # e.g. "jimmy-odyssey-70mm-x7q2"  (keep it unguessable)
    "PUSHOVER_TOKEN": "",       # optional alternative/addition to ntfy
    "PUSHOVER_USER": "",
}


def cfg(key: str) -> str:
    return os.environ.get(key, DEFAULTS[key])


# Section headings that denote a premium format block on AMC's pages.
# (Used to recognize headings generally; your FORMAT_PATTERN then selects
# which of them you actually care about.)
FORMAT_HEADING_RE = re.compile(
    r"(imax|dolby|laser|70\s*mm|prime at amc|open caption|real\s*d|d-?box|screenx|4dx|grand screen)",
    re.I,
)

SHOWTIME_HREF_RE = re.compile(r"/showtimes/(\d+)")
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*[ap]m\b", re.I)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Show:
    sid: str        # AMC showtime id -- globally unique, our dedupe key
    movie: str      # listing title, e.g. "The Odyssey - IMAX 70mm Event"
    fmt: str        # format heading, e.g. "IMAX 70MM"
    day: str        # YYYY-MM-DD of the schedule page it appeared on
    time: str       # e.g. "7:00pm" (falls back to raw label)
    status: str     # available | almost full | sold out
    url: str        # direct seat-selection link

    @property
    def pretty_day(self) -> str:
        d = date.fromisoformat(self.day)
        return d.strftime("%a %b %-d") if os.name != "nt" else d.strftime("%a %b %d")


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch(url: str) -> str:
    """GET a page while presenting a real-browser TLS/HTTP fingerprint."""
    r = cf.get(
        url,
        impersonate="chrome",
        timeout=30,
        headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    r.raise_for_status()
    return r.text


def polite_sleep() -> None:
    base = float(cfg("REQUEST_DELAY"))
    time.sleep(base + random.uniform(0, base))


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def parse_showtimes(html: str, day: str, base_url: str) -> list[Show]:
    """
    Linear scan of the document in DOM order:
      * an <a href="/movies/..."> sets the current movie (and resets format)
      * a heading (h1-h6) matching a premium-format label sets current format
      * an <a href="/showtimes/{id}"> is a showtime belonging to
        (current movie, current format)
    This avoids depending on class names, which AMC changes freely.
    Amenity chips like "IMAX at AMC" / "70mm" inside a section are NOT
    headings, so they don't clobber the current format.
    """
    soup = BeautifulSoup(html, "html.parser")
    shows: list[Show] = []
    movie: str | None = None
    fmt: str | None = None

    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "a"]):
        if el.name == "a":
            href = el.get("href") or ""
            if "/movies/" in href:
                title = el.get_text(" ", strip=True)
                if title:            # skip poster-image-only anchors
                    movie = title
                    fmt = None       # new movie card -> format unknown again
                continue
            m = SHOWTIME_HREF_RE.search(href)
            if m and movie and fmt:
                label = el.get_text(" ", strip=True)
                tm = TIME_RE.search(label)
                low = label.lower()
                status = (
                    "sold out" if "sold out" in low
                    else "almost full" if "almost full" in low
                    else "available"
                )
                shows.append(Show(
                    sid=m.group(1),
                    movie=movie,
                    fmt=fmt,
                    day=day,
                    time=tm.group(0).lower() if tm else label,
                    status=status,
                    url=urljoin(base_url, href.split("?")[0]),
                ))
        else:
            # Heading element. Movie-title headings contain a /movies/ link
            # (handled above via the anchor); theatre-name headings won't
            # match FORMAT_HEADING_RE.
            if el.find("a", href=re.compile("/movies/")):
                continue
            text = el.get_text(" ", strip=True)
            head = text.split(":")[0].strip()   # "IMAX 70MM: EXTRAORDINARY..." -> "IMAX 70MM"
            if head and len(head) <= 60 and FORMAT_HEADING_RE.search(head):
                fmt = head

    return shows


def dedupe(shows: list[Show]) -> list[Show]:
    seen: dict[str, Show] = {}
    for s in shows:
        seen.setdefault(s.sid, s)
    return list(seen.values())


# --------------------------------------------------------------------------
# Date-URL discovery
# --------------------------------------------------------------------------

CANDIDATE_TEMPLATES = [
    "{base}?date={date}",
    "{base}/all/{date}/{slug}/all",   # legacy AMC path style
    "{base}/{date}",
    "{base}?view-date={date}",
]


def showtime_ids(html: str) -> set[str]:
    return set(SHOWTIME_HREF_RE.findall(html))


def discover_date_template(base_url: str, base_html: str) -> str | None:
    """
    Figure out how to request a specific date's schedule.
    1) Prefer any date-bearing showtimes link present in the base page HTML.
    2) Otherwise try known URL shapes and keep the first one where two
       different dates return different showtime-id sets.
    """
    # 1) links embedded in the page (date picker), if server-rendered
    m = re.search(r'href="([^"]*showtimes[^"]*\d{4}-\d{2}-\d{2}[^"]*)"', base_html)
    if m:
        href = m.group(1)
        template = re.sub(r"\d{4}-\d{2}-\d{2}", "{date}", href)
        return urljoin(base_url, template)

    # 2) probe candidates
    slug = base_url.rstrip("/").split("/")[-2]  # e.g. amc-lincoln-square-13
    base_ids = showtime_ids(base_html)
    d1 = (date.today() + timedelta(days=1)).isoformat()
    d2 = (date.today() + timedelta(days=8)).isoformat()
    for tpl in CANDIDATE_TEMPLATES:
        template = tpl.format(base=base_url, slug=slug, date="{date}")
        try:
            polite_sleep()
            ids1 = showtime_ids(fetch(template.format(date=d1)))
            polite_sleep()
            ids2 = showtime_ids(fetch(template.format(date=d2)))
        except Exception:
            continue
        if ids1 and (ids1 != base_ids or ids2 != ids1):
            return template
    return None


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"seen": {}, "meta": {}}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def notify(title: str, body: str, click_url: str | None = None) -> None:
    sent = False
    topic = cfg("NTFY_TOPIC")
    if topic:
        headers = {"Title": title.encode("ascii", "ignore").decode().strip(), "Priority": "high", "Tags": "clapper"}
        if click_url:
            headers["Click"] = click_url
        try:
            cf.post(
                f"{cfg('NTFY_SERVER').rstrip('/')}/{topic}",
                data=body.encode(),
                headers=headers,
                timeout=15,
            )
            sent = True
        except Exception as e:
            print(f"[warn] ntfy send failed: {e}", file=sys.stderr)

    if cfg("PUSHOVER_TOKEN") and cfg("PUSHOVER_USER"):
        try:
            cf.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": cfg("PUSHOVER_TOKEN"),
                    "user": cfg("PUSHOVER_USER"),
                    "title": title,
                    "message": body,
                    "priority": 1,
                    **({"url": click_url, "url_title": "Pick seats"} if click_url else {}),
                },
                timeout=15,
            )
            sent = True
        except Exception as e:
            print(f"[warn] pushover send failed: {e}", file=sys.stderr)

    if not sent:
        print("[info] no notifier configured (set NTFY_TOPIC and/or Pushover vars); printing only")
    print(f"--- {title} ---\n{body}\n")


def format_new_shows(new: list[Show]) -> str:
    lines = []
    for s in sorted(new, key=lambda s: (s.day, s.time)):
        lines.append(f"{s.pretty_day} · {s.time} — {s.fmt} ({s.movie})")
        lines.append(f"  {s.url}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def scan(dump_dir: Path | None) -> list[Show]:
    base_url = cfg("THEATRE_SHOWTIMES_URL").rstrip("/")
    movie_re = re.compile(cfg("MOVIE_PATTERN"), re.I)
    fmt_re = re.compile(cfg("FORMAT_PATTERN"), re.I)
    days_ahead = int(cfg("DAYS_AHEAD"))
    empty_stop = int(cfg("EMPTY_STREAK_STOP"))

    today = date.today()
    base_html = fetch(base_url)
    if dump_dir:
        (dump_dir / "base.html").write_text(base_html)

    template = cfg("DATE_URL_TEMPLATE") or discover_date_template(base_url, base_html)
    if not template:
        sys.exit(
            "[error] Couldn't discover the per-date URL pattern.\n"
            "Open the theatre page in Chrome, click a future date, copy the URL\n"
            "from the address bar (or the request from DevTools > Network),\n"
            "replace the date with {date}, and set it as DATE_URL_TEMPLATE."
        )
    print(f"[info] date url template: {template}")

    all_shows: list[Show] = parse_showtimes(base_html, today.isoformat(), base_url)
    empty_streak = 0
    for i in range(1, days_ahead + 1):
        d = (today + timedelta(days=i)).isoformat()
        polite_sleep()
        try:
            html = fetch(template.format(date=d))
        except Exception as e:
            print(f"[warn] fetch failed for {d}: {e}", file=sys.stderr)
            continue
        if dump_dir:
            (dump_dir / f"{d}.html").write_text(html)
        day_shows = parse_showtimes(html, d, base_url)
        all_shows.extend(day_shows)

        # Early stop once the movie disappears from the schedule entirely
        # for `empty_stop` consecutive days (end of released window).
        if any(movie_re.search(s.movie) for s in day_shows):
            empty_streak = 0
        else:
            empty_streak += 1
            if empty_streak >= empty_stop:
                print(f"[info] no listings for {empty_streak} consecutive days; stopping at {d}")
                break

    matched = [s for s in dedupe(all_shows)
               if movie_re.search(s.movie) and fmt_re.search(s.fmt)]
    print(f"[info] scan complete: {len(matched)} matching showtimes currently listed")
    return matched


def main() -> None:
    ap = argparse.ArgumentParser(description="Watch AMC for new Odyssey IMAX 70mm showtimes")
    ap.add_argument("--dry-run", action="store_true", help="scan and diff but never notify")
    ap.add_argument("--dump", action="store_true", help="save fetched HTML to ./debug for inspection")
    ap.add_argument("--reset", action="store_true", help="clear state and re-seed")
    args = ap.parse_args()

    state_path = Path(cfg("STATE_FILE"))
    if args.reset and state_path.exists():
        state_path.unlink()

    dump_dir = None
    if args.dump:
        dump_dir = Path("debug")
        dump_dir.mkdir(exist_ok=True)

    state = load_state(state_path)
    first_run = not state["seen"]

    current = scan(dump_dir)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    new = [s for s in current if s.sid not in state["seen"]]
    for s in current:
        entry = state["seen"].setdefault(s.sid, {"first_seen": now})
        entry.update(asdict(s))
        entry["last_seen"] = now
    state["meta"]["last_run"] = now
    save_state(state_path, state)

    if first_run:
        days = sorted({s.day for s in current})
        span = f"{days[0]} → {days[-1]}" if days else "none yet"
        msg = (f"Monitoring started. Currently tracking {len(current)} "
               f"IMAX 70mm showtimes ({span}). You'll be pinged when new ones drop.")
        if not args.dry_run:
            notify("🎬 Odyssey 70mm watch is live", msg, cfg("THEATRE_SHOWTIMES_URL"))
        else:
            print(msg)
        return

    if new:
        title = f"🎬 {len(new)} new Odyssey IMAX 70mm showtime{'s' if len(new) > 1 else ''} — Lincoln Sq"
        body = format_new_shows(new)
        if not args.dry_run:
            notify(title, body, new[0].url)
        else:
            print(f"[dry-run] would notify:\n--- {title} ---\n{body}")
    else:
        print("[info] no new showtimes")


if __name__ == "__main__":
    main()
