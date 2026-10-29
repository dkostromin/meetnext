# meetnext

One menu-bar list of every meeting across disconnected calendars: Google Workspace
accounts read through macOS Calendar, a workspace whose admin blocks calendar
sharing read through a logged-in browser, and a Microsoft 365 calendar read from
its published ICS feed.

## Files

| File | Role |
|------|------|
| `caljson.js` | JXA collector for Calendar.app (`window`, `series-one`, `range`, `names`) |
| `meetings.py` | merges sources, expands RRULEs, caches, renders SwiftBar, sends reminders |
| `centium_sync.mjs` | pulls a Google calendar via calendar export through BrowserOS cookies |
| `meetings.2m.sh` | SwiftBar plugin entry point (plugin directory) |
| `meetnext.swift` | EventKit experiment — unused, kept for history |
| `launchd/` | reference copies of the installed launch agents |

## Data flow

```
Calendar.app ──JXA──┐
centium.ics  ───────┼──> collect() ──> ~/.cache/meetnext/events.json ──> SwiftBar menu
published ICS ──────┘                                   └──> --notify (3 h reminder)
```

- `meetings.py --refresh` rebuilds the cache (window query + one recurring series
  per run + all feeds).
- `meetings.py` prints the SwiftBar menu from the cache and spawns a refresh when
  the cache is older than `EVENT_TTL`.
- `meetings.py --notify` posts one notification per meeting inside
  `LEAD_HOURS = 3`, plus a daily warning for a stale feed.

## Configuration

`~/.config/meetnext/centium.email` holds the Google account whose export
`centium_sync.mjs` pulls (or set `MEETNEXT_EMAIL`; `MEETNEXT_AUTHUSER` defaults
to `2`). `MEETNEXT_FOCUS` is an optional comma-separated list of calendar names
to highlight in `--demo`.

`~/.config/meetnext/feeds.txt` (mode 600), one entry per line:

```
name = https://…/calendar.ics
name = file:///Users/you/.cache/meetnext/centium.ics
```

Feeds older than `FEED_STALE_HOURS = 6` put `⚠︎` in the menu bar and are reported
in the dropdown and by notification.

## Install

1. Copy `meetings.2m.sh` to `~/Library/Application Support/SwiftBar/Plugins/`.
2. Install the launch agents from `launchd/` into `~/Library/LaunchAgents/`,
   fixing paths inside them, then
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<file>.plist`.
3. `node centium_sync.mjs` once to produce the local ICS.

## Notes

- Recurring events are expanded in **UTC**: JXA exposes instants only, and
  expanding in local time pushes events that cross midnight (US evening → MSK
  next morning) a day early.
- Windows timezone IDs from Outlook feeds are mapped to IANA.
- The browser-based export needs a signed-in BrowserOS profile; if it is closed
  at refresh time the previous snapshot stays and the feed is flagged stale.
- No credentials are stored in this repo. Feed URLs and cached events live
  outside it.
