# odyssey-70mm-watch

Pings your phone the moment AMC Lincoln Square releases new **IMAX 70mm** showtimes for **The Odyssey**.

It scrapes the theatre's server-rendered showtimes pages (no headless browser needed), tracks every showtime ID it has ever seen in `state.json`, and notifies you only about genuinely new ones — with a direct seat-selection link so you can grab tickets from the notification.

It correctly watches **both** listings AMC uses — the regular "The Odyssey" page *and* "The Odyssey – IMAX 70mm Event" — and ignores the Dolby / Laser / non-IMAX 70mm sections.

## Setup (5 minutes)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

**Notifications (pick one):**

- **ntfy (free):** install the ntfy app (iOS/Android), subscribe to a topic with a hard-to-guess name (topics are public namespaces), then:

  ```bash
  export NTFY_TOPIC="jimmy-odyssey-70mm-x7q2"
  ```

- **Pushover ($5 once, very reliable on iOS):** set `PUSHOVER_TOKEN` and `PUSHOVER_USER` instead (or additionally).

**First run:**

```bash
python monitor.py
```

The first run seeds `state.json` with everything currently listed and sends a single "monitoring started" ping. After that, you only hear about new drops.

## Scheduling

**Local cron (recommended — runs from your residential IP):**

```cron
*/20 * * * * cd $HOME/odyssey-70mm-watch && ./venv/bin/python monitor.py >> watch.log 2>&1
```

`crontab -e` and paste. On a Mac that sleeps, run it on any always-on box instead (old laptop, Raspberry Pi, $5 VPS), or keep the Mac awake with `caffeinate`. AMC has historically pushed new week-blocks midweek, so if you want to be surgical, tighten the interval Tuesday–Thursday mornings.

**GitHub Actions (zero infrastructure):** put `monitor.yml` at `.github/workflows/monitor.yml`, add `NTFY_TOPIC` as a repo secret, push. Caveat: Actions runners use datacenter IPs that AMC's CDN may block — if you see 403s in the run logs, fall back to local cron.

## Configuration (env vars)

| Variable | Default | Notes |
|---|---|---|
| `THEATRE_SHOWTIMES_URL` | Lincoln Square 13 | Point at any AMC theatre's `/showtimes` page |
| `MOVIE_PATTERN` | `odyssey` | Case-insensitive regex on the listing title |
| `FORMAT_PATTERN` | `imax\s*70` | Matches "IMAX 70MM"; use `(imax\s*70\|^70\s*mm)` to also watch the standard 70mm run |
| `DAYS_AHEAD` | `45` | Scan window |
| `EMPTY_STREAK_STOP` | `5` | Stop early after N consecutive dates with no listings |
| `DATE_URL_TEMPLATE` | auto | Manual override if auto-discovery fails (see below) |
| `REQUEST_DELAY` | `0.6` | Seconds between page fetches (+ jitter) |
| `STATE_FILE` | `state.json` | Where seen showtimes live |

## Troubleshooting

- **`Couldn't discover the per-date URL pattern`** — AMC changed their date navigation. Open the theatre page in Chrome, click a future date, copy the resulting URL (address bar, or the document request in DevTools → Network), replace the date with `{date}`, and export it as `DATE_URL_TEMPLATE`.
- **Scan finds 0 showtimes** — markup probably changed. Run `python monitor.py --dump`, then hand a file from `debug/` to Claude Code and ask it to update `parse_showtimes()` — it's ~40 lines and the only piece coupled to AMC's HTML.
- **403 / blocked** — you're likely on a datacenter IP (cloud VM, GitHub Actions). Run from home, or lengthen `REQUEST_DELAY` and reduce cadence.
- **Fresh start:** `python monitor.py --reset`.

## Ideas for v2

- **Refill alerts:** notify when a previously "Almost Full"/sold-out showtime opens back up (state already records `status` per showtime — diff it).
- **Multi-theatre:** loop over several `THEATRE_SHOWTIMES_URL`s with separate state files.
- **Cleaner data source:** AMC has an official developer API (`developers.amctheatres.com`, `GET /v2/theatres/{id}/showtimes/{date}` with a vendor key) that returns structured JSON including `isSoldOut` — worth switching to if you can get a key issued.

## Being a good citizen

Default settings make ~45 lightweight page requests every 20 minutes with delays and jitter — comparable to a person browsing the schedule. Keep it reasonable; this is for grabbing your own tickets, not scalping.
