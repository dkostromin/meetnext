#!/usr/bin/env python3
"""meetings.py — aggregate Apple Calendar events for a SwiftBar menu bar plugin.

Data path: Calendar.app via JXA (fast window query + rotating per-calendar
recurrence scan), cached on disk so the menu bar renders instantly.

Usage:
  meetings.py            SwiftBar output (from cache)
  meetings.py --next     next meeting as one line
  meetings.py --json     expanded events as JSON
  meetings.py --refresh  rebuild the cache (used by the background worker)
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

if os.environ.get("MEETNEXT_TZ"):
    os.environ["TZ"] = os.environ["MEETNEXT_TZ"]
    time.tzset()

HERE = os.path.dirname(os.path.abspath(__file__))
CALJSON = os.path.join(HERE, "caljson.js")
CACHE = os.path.expanduser("~/.cache/meetnext")
EVENTS_CACHE = os.path.join(CACHE, "events.json")
SERIES_STATE = os.path.join(CACHE, "series_state.json")
FEEDS_FILE = os.path.expanduser("~/.config/meetnext/feeds.txt")
NOTIFIED = os.path.join(CACHE, "notified.json")
FEED_STATE = os.path.join(CACHE, "feed_state.json")
FEED_STALE_HOURS = 6
EVENT_TTL = 180
SERIES_TTL = 45 * 60
WINDOW_HOURS = 96
LEAD_HOURS = 3

WINDOWS_TZ = {
    "UTC": "UTC",
    "Coordinated Universal Time": "UTC",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev",
    "Russian Standard Time": "Europe/Moscow",
    "Turkey Standard Time": "Europe/Istanbul",
    "Israel Standard Time": "Asia/Jerusalem",
    "Arabian Standard Time": "Asia/Dubai",
    "Georgian Standard Time": "Asia/Tbilisi",
    "Azerbaijan Standard Time": "Asia/Baku",
    "India Standard Time": "Asia/Kolkata",
    "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore",
    "Tokyo Standard Time": "Asia/Tokyo",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "Pacific Standard Time": "America/Los_Angeles",
    "Mountain Standard Time": "America/Denver",
    "Central Standard Time": "America/Chicago",
    "Eastern Standard Time": "America/New_York",
}
LINK_RE = re.compile(
    r"https?://[^\s<>\"']*(zoom\.us|meet\.google\.com|teams\.microsoft\.com|webex\.com|whereby\.com|meet\.jitsi\.org)[^\s<>\"']*",
    re.I,
)
TAG_RE = re.compile(r"<[^>]+>")
DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


# ---------- collection ----------

def jxa(mode, calendar=None):
    cmd = ["/usr/bin/osascript", "-l", "JavaScript", CALJSON, mode, str(WINDOW_HOURS)]
    if calendar:
        cmd.append(calendar)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return json.loads(out.stdout or "[]") if out.returncode == 0 else []
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return []


def jxa_range(start, end):
    cmd = ["/usr/bin/osascript", "-l", "JavaScript", CALJSON, "range",
           start.isoformat(), end.isoformat()]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return json.loads(out.stdout or "[]") if out.returncode == 0 else []
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return []


def feed_urls():
    """Read ~/.config/meetnext/feeds.txt — one URL per line, optional 'name = url'."""
    entries = []
    try:
        with open(FEEDS_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    name, url = line.split("=", 1)
                    entries.append((name.strip(), url.strip()))
                else:
                    entries.append((f"feed{len(entries) + 1}", line))
    except OSError:
        return []
    return entries


def _ics_dt(value, params):
    if params.get("VALUE") == "DATE":
        return datetime.strptime(value, "%Y%m%d").astimezone()
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone()
    naive = datetime.strptime(value, "%Y%m%dT%H%M%S")
    tzid = (params.get("TZID") or "").strip('"')
    if tzid:
        try:
            return naive.replace(tzinfo=ZoneInfo(WINDOWS_TZ.get(tzid, tzid))).astimezone()
        except (KeyError, ValueError):
            pass
    return naive.astimezone()


def _ics_unescape(value):
    return (value.replace("\\n", "\n").replace("\\N", "\n")
                 .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def _source_tz(params):
    tzid = (params.get("TZID") or "").strip('"')
    if not tzid:
        return None
    return WINDOWS_TZ.get(tzid, tzid)


def ics_events(text, calname):
    """Minimal iCalendar reader: VEVENT + RRULE + EXDATE, enough for Google feeds."""
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)

    blocks, current = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current is not None:
                blocks.append(current)
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            parts = key.split(";")
            params = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
            current.setdefault(parts[0].upper(), []).append((value, params))

    out = []
    for props in blocks:
        try:
            start_val, start_params = props["DTSTART"][0]
            start = _ics_dt(start_val, start_params)
            if "DTEND" in props:
                end_val, end_params = props["DTEND"][0]
                end = _ics_dt(end_val, end_params)
            else:
                end = start + timedelta(hours=1)
            exdates = [_ics_dt(v, p).isoformat() for v, p in props.get("EXDATE", [])]
            out.append({
                "title": _ics_unescape(props.get("SUMMARY", [("", {})])[0][0]) or "(no title)",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "allDay": start_params.get("VALUE") == "DATE",
                "recurrence": props["RRULE"][0][0] if "RRULE" in props else None,
                "location": _ics_unescape(props.get("LOCATION", [("", {})])[0][0]) or None,
                "notes": _ics_unescape(props.get("DESCRIPTION", [("", {})])[0][0]) or None,
                "url": _ics_unescape(props.get("URL", [("", {})])[0][0]) or None,
                "tz": _source_tz(start_params),
                "calendar": calname,
                "exdates": exdates,
            })
        except (KeyError, ValueError, IndexError):
            continue
    return out


def load_feed_state():
    try:
        with open(FEED_STATE) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def fetch_feeds():
    """Merge configured feeds and remember when each one last delivered data."""
    events = []
    state = load_feed_state()
    for name, url in feed_urls():
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                text = resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            entry = state.get(name) or {}
            entry["error"] = str(exc)[:120]
            state[name] = entry
            continue
        if "BEGIN:VCALENDAR" not in text:
            entry = state.get(name) or {}
            entry["error"] = "not an iCalendar payload"
            state[name] = entry
            continue
        state[name] = {"ts": time.time()}
        events.extend(ics_events(text, name))
    save_json(FEED_STATE, state)
    return events


def feed_warnings():
    """A feed that stopped refreshing is invisible otherwise — say it loudly."""
    out = []
    state = load_feed_state()
    for name, url in feed_urls():
        if url.startswith("file://"):
            try:
                age = time.time() - os.path.getmtime(url[7:])
            except OSError:
                out.append(f"⚠️ feed {name}: file missing")
                continue
        else:
            entry = state.get(name) or {}
            if not entry.get("ts"):
                error = entry.get("error")
                out.append(f"⚠️ feed {name}: never fetched" + (f" ({error})" if error else ""))
                continue
            age = time.time() - entry["ts"]
        if age > FEED_STALE_HOURS * 3600:
            entry = state.get(name) or {}
            hours = int(age // 3600)
            suffix = f" · {entry['error']}" if entry.get("error") else ""
            out.append(f"⚠️ feed {name}: last update {hours}h ago{suffix}")
    return out


def dedupe_events(events):
    """Calendar's scripting layer returns the same event several times; collapse it."""
    out, seen = [], set()
    for ev in events:
        key = (ev.get("title"), ev.get("start"), ev.get("end"), ev.get("recurrence"))
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def load_state():
    try:
        with open(SERIES_STATE) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_json(path, payload):
    os.makedirs(CACHE, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def refresh_one_series():
    """Scan the stalest calendar's recurrence series. One per cycle keeps it cheap."""
    names = jxa("names")
    if not names:
        return
    state = load_state()
    stale = [n for n in names if time.time() - state.get(n, {}).get("updated", 0) > SERIES_TTL]
    if not stale:
        return
    target = min(stale, key=lambda n: state.get(n, {}).get("updated", 0))
    events = jxa("series-one", target)
    state[target] = {"updated": time.time(), "events": events}
    save_json(SERIES_STATE, state)


def collect():
    win_start = datetime.now().astimezone() - timedelta(hours=1)
    win_end = datetime.now().astimezone() + timedelta(hours=WINDOW_HOURS)

    pooled = dedupe_events(jxa("window") + fetch_feeds())
    for entry in load_state().values():
        pooled.extend(dedupe_events(entry.get("events") or []))
    pooled = dedupe_events(pooled)

    rows = []
    for ev in pooled:
        if ev.get("allDay"):
            continue
        try:
            for s, e in occurrences(ev, win_start, win_end):
                rows.append({
                    "title": ev.get("title") or "(no title)",
                    "start": s,
                    "end": e,
                    "calendar": ev.get("calendar") or "",
                    "join": join_url(ev),
                    "notes": clean(ev.get("notes")),
                })
        except (KeyError, ValueError, TypeError):
            continue

    seen = set()
    unique = []
    for r in sorted(rows, key=lambda r: r["start"]):
        key = (r["title"].strip().lower(), r["start"].replace(second=0, microsecond=0))
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique


# ---------- recurrence ----------

def parse_dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()


def parse_until(value):
    if not value:
        return None
    m = re.search(r"(\d{8})", value)
    if not m:
        return None
    d = m.group(1)
    return datetime(int(d[:4]), int(d[4:6]), int(d[6:]), 23, 59, 59).astimezone()


def occurrences(ev, win_start, win_end):
    start = parse_dt(ev["start"])
    end = parse_dt(ev["end"])
    dur = end - start
    rule = ev.get("recurrence")
    if not rule:
        return [(start, end)] if end > win_start and start < win_end else []

    parts = {}
    for chunk in rule.split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip().upper()] = v.strip()
    freq = parts.get("FREQ", "").upper()
    interval = max(1, int(parts.get("INTERVAL", "1") or 1))
    byday = [d for d in parts.get("BYDAY", "").split(",") if d in DAYS]
    until = parse_until(parts.get("UNTIL"))
    limit = min(win_end, until) if until else win_end

    out = []
    # BYDAY is authored in the calendar's own zone, but JXA only gives us the
    # instant. Expanding in UTC keeps the original weekday; expanding in local
    # time shifts events that cross midnight (US evening -> MSK next morning).
    ref = start.astimezone(timezone.utc)
    if freq == "WEEKLY":
        week = ref - timedelta(days=ref.weekday())
        for step in range(0, 520, interval):
            base = week + timedelta(weeks=step)
            if base > limit.astimezone(timezone.utc):
                break
            days = byday or [list(DAYS)[ref.weekday()]]
            for code in days:
                s = (base + timedelta(days=DAYS[code])).replace(
                    hour=ref.hour, minute=ref.minute,
                    second=ref.second, microsecond=0).astimezone()
                if s < start or s > limit:
                    continue
                e = s + dur
                if e > win_start and s < win_end:
                    out.append((s, e))
    elif freq == "DAILY":
        for k in range(800):
            s = (ref + timedelta(days=k * interval)).astimezone()
            if s > limit:
                break
            e = s + dur
            if e > win_start and s < win_end:
                out.append((s, e))
    elif end > win_start and start < win_end:
        out.append((start, end))

    exdates = set()
    for marker in ev.get("exdates") or []:
        try:
            exdates.add(parse_dt(marker).replace(second=0, microsecond=0))
        except ValueError:
            continue
    if exdates:
        out = [(s, e) for s, e in out
               if s.replace(second=0, microsecond=0) not in exdates]
    return out


# ---------- formatting ----------

def clean(text):
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = TAG_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def join_url(ev):
    for candidate in (ev.get("url"), ev.get("location")):
        if candidate and re.match(r"https?://", candidate):
            if LINK_RE.search(candidate) or "zoom" in candidate.lower():
                return candidate.strip()
    m = LINK_RE.search(clean(ev.get("notes")))
    return m.group(0).rstrip(".,);") if m else None


def fmt_delta(delta):
    mins = abs(int(delta.total_seconds() // 60))
    if mins < 60:
        return f"{mins}m"
    return f"{mins // 60}h {mins % 60:02d}m"


def bar_text(title, limit=30):
    title = title.replace("|", "·").strip()
    if "·" in title:
        tail = title.split("·")[-1].strip()
        if tail:
            title = tail
    return title if len(title) <= limit else title[: limit - 1] + "…"


# ---------- cache ----------

def save_events(events):
    save_json(EVENTS_CACHE, [
        {**e, "start": e["start"].isoformat(), "end": e["end"].isoformat()}
        for e in events
    ])


def load_events():
    try:
        with open(EVENTS_CACHE) as fh:
            raw = json.load(fh)
        return [{**e, "start": parse_dt(e["start"]), "end": parse_dt(e["end"])} for e in raw], \
               time.time() - os.path.getmtime(EVENTS_CACHE)
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return None, 0


def spawn_refresh():
    lock = os.path.join(CACHE, "events.lock")
    if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 300:
        return
    os.makedirs(CACHE, exist_ok=True)
    open(lock, "w").close()
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--refresh"],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ---------- render ----------

def head_line(events, now, stale=False):
    flag = "⚠︎ " if stale else ""
    current = next((e for e in events if e["start"] <= now <= e["end"]), None)
    upcoming = [e for e in events if e["start"] > now]
    nxt = upcoming[0] if upcoming else None
    if current:
        return f"{flag}● {bar_text(current['title'], 24)} · {fmt_delta(current['end'] - now)} left"
    if nxt and nxt["start"] - now <= timedelta(hours=LEAD_HOURS):
        return f"{flag}▸ {bar_text(nxt['title'], 24)} · {nxt['start']:%H:%M} (in {fmt_delta(nxt['start'] - now)})"
    if nxt:
        return f"{flag}▸ Quiet · next {nxt['start']:%a %H:%M}"
    return f"{flag}▸ No meetings in 4 days"


def day_label(day, today):
    delta = (day - today).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if delta == -1:
        return "Yesterday"
    return day.strftime("%a %d %b")


def _osa_quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _post_notification(message, title, subtitle):
    script = (
        f"display notification {_osa_quote(message)}"
        f" with title {_osa_quote(title)}"
        f" subtitle {_osa_quote(subtitle)}"
    )
    result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"notify failed: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def _seen_notifications():
    try:
        with open(NOTIFIED) as fh:
            return set(json.load(fh))
    except (OSError, json.JSONDecodeError):
        return set()


def notify_due(events):
    """One notification per meeting, LEAD_HOURS before it starts."""
    now = datetime.now().astimezone()
    seen = _seen_notifications()
    sent = 0
    for ev in events:
        delta = ev["start"] - now
        if delta < timedelta(0) or delta > timedelta(hours=LEAD_HOURS):
            continue
        key = f"{ev['title']}|{ev['start'].isoformat()}"
        if key in seen:
            continue
        ok = _post_notification(
            f"{ev['start']:%H:%M} · {ev['calendar']}",
            ev["title"][:90],
            f"in {fmt_delta(delta)}",
        )
        if not ok:
            continue
        seen.add(key)
        sent += 1
    if sent:
        save_json(NOTIFIED, sorted(seen))
    return sent


def notify_stale_feeds():
    """Warn once a day if a feed stopped refreshing."""
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    seen = _seen_notifications()
    sent = 0
    for warning in feed_warnings():
        key = f"stale:{warning}:{today}"
        if key in seen:
            continue
        ok = _post_notification(warning[:120], "meetnext: календарь не обновляется", "данные могут быть устаревшими")
        if not ok:
            continue
        seen.add(key)
        sent += 1
    if sent:
        save_json(NOTIFIED, sorted(seen))
    return sent


def demo(day_str=None):
    """Replay a day: what the tool sees and what the menu bar would have shown."""
    if day_str:
        start = datetime.strptime(day_str, "%Y-%m-%d").astimezone()
    else:
        start = datetime.now().astimezone()
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    rows = []

    for ev in dedupe_events(jxa_range(start, end)):
        if ev.get("allDay"):
            continue
        try:
            s, e = parse_dt(ev["start"]), parse_dt(ev["end"])
        except (KeyError, ValueError):
            continue
        if s >= end or e <= start:
            continue
        rows.append({
            "title": ev.get("title") or "(no title)",
            "start": s,
            "end": e,
            "calendar": ev.get("calendar") or "",
            "join": join_url(ev),
            "source": "direct",
        })

    series_hits = 0
    for cal, entry in load_state().items():
        for ev in dedupe_events(entry.get("events") or []):
            if not ev.get("recurrence") or ev.get("allDay"):
                continue
            try:
                for s, e in occurrences(ev, start, end):
                    series_hits += 1
                    rows.append({
                        "title": ev.get("title") or "(no title)",
                        "start": s,
                        "end": e,
                        "calendar": ev.get("calendar") or "",
                        "join": join_url(ev),
                        "source": "series",
                    })
            except (KeyError, ValueError, TypeError):
                continue

    for ev in dedupe_events(fetch_feeds()):
        if ev.get("allDay"):
            continue
        try:
            for s, e in occurrences(ev, start, end):
                series_hits += 1
                rows.append({
                    "title": ev.get("title") or "(no title)",
                    "start": s,
                    "end": e,
                    "calendar": ev.get("calendar") or "",
                    "join": join_url(ev),
                    "source": "feed",
                })
        except (KeyError, ValueError, TypeError):
            continue

    deduped, seen = [], set()
    for r in sorted(rows, key=lambda r: (r["start"], r["source"] != "direct")):
        key = (r["title"].strip().lower(), r["start"].replace(second=0, microsecond=0))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    focus = tuple(c.strip() for c in os.environ.get("MEETNEXT_FOCUS", "").split(",") if c.strip())
    if not focus:
        focus = tuple(sorted({r["calendar"] for r in deduped}))

    print(f"=== Retro demo {start:%Y-%m-%d (%a)} — {len(deduped)} events ===")
    print(f"window: 00:00 → 24:00  ·  series occurrences considered: {series_hits}\n")

    print("By calendar:")
    for cal in focus:
        n = sum(1 for r in deduped if r["calendar"] == cal)
        note = ""
        if not n:
            events = (load_state().get(cal) or {}).get("events") or []
            try:
                last = max(parse_dt(e["start"]) for e in events if e.get("start"))
                note = f"   (last known event {last:%d %b %Y} — sync suspect)"
            except (KeyError, ValueError, TypeError):
                note = "   (no events)"
        print(f"  {cal:32} {n}{note}")
    others = {}
    for r in deduped:
        if r["calendar"] not in focus:
            others[r["calendar"]] = others.get(r["calendar"], 0) + 1
    for cal, n in sorted(others.items()):
        print(f"  {cal:32} {n}")
    print()

    print("Timeline:")
    if not deduped:
        print("  (nothing scheduled)")
    for r in deduped:
        mark = "*" if r["calendar"] in focus else " "
        join = "join✓" if r["join"] else "     "
        print(f" {mark} {r['start']:%H:%M}–{r['end']:%H:%M}  {r['title'][:44]:46} "
              f"{r['calendar'][:28]:30} {r['source']:7} {join}")
    print()

    print("Menu bar replay:")
    samples = {start.replace(hour=9)}
    for r in deduped:
        samples.add(r["start"] - timedelta(minutes=10))
        samples.add(r["start"] + timedelta(minutes=5))
    if deduped:
        samples.add(max(r["end"] for r in deduped) + timedelta(minutes=5))
    for t in sorted(samples):
        print(f"  {t:%H:%M}  {head_line(deduped, t)}")


def staleness_warnings(days=7, min_events=20):
    """Flag calendars that used to have events but stopped updating."""
    out = []
    now = datetime.now().astimezone()
    fed = [name.lower() for name, _ in feed_urls()]
    for cal, entry in load_state().items():
        if any(name in cal.lower() for name in fed):
            continue
        events = entry.get("events") or []
        if len(events) < min_events:
            continue
        try:
            latest = max(parse_dt(e["start"]) for e in events if e.get("start"))
        except (KeyError, ValueError, TypeError):
            continue
        if latest < now - timedelta(days=days):
            out.append(f"⚠️ macOS sync: {cal} has no events since {latest:%d %b}")
    return sorted(out)


def render_swiftbar(events):
    now = datetime.now().astimezone()
    current = next((e for e in events if e["start"] <= now <= e["end"]), None)
    feed_issues = feed_warnings()
    warnings = staleness_warnings() + feed_issues
    lines = [head_line(events, now, stale=bool(feed_issues)), "---"]
    for warning in warnings:
        lines.append(warning)
    upcoming = [e for e in events if e["end"] > now]
    if not upcoming:
        lines.append("Nothing left today or in the next 4 days")
    per_day = {}
    for ev in upcoming:
        per_day.setdefault(ev["start"].date(), 0)
        per_day[ev["start"].date()] += 1
    shown_day = None
    for ev in upcoming[:12]:
        day = ev["start"].date()
        if day != shown_day:
            if shown_day is not None:
                lines.append("---")
            count = per_day[day]
            noun = "meeting" if count == 1 else "meetings"
            lines.append(f"{day_label(day, now.date())} · {count} {noun} | size=11 color=#8e8e93")
            shown_day = day
        label = f"{ev['start']:%H:%M} {ev['title'].replace('|', '·')}"
        if ev is current:
            label = f"● {label} (now)"
        lines.append(f"{label} | href={ev['join']}" if ev.get("join") else label)
        zone = now.tzname() or ""
        sub = (f"-- {ev['start']:%H:%M}–{ev['end']:%H:%M} {zone}"
               f" · {ev['start'].astimezone(timezone.utc):%H:%M}Z · {ev['calendar']}")
        if ev.get("tz"):
            sub += f" · src {ev['tz']}"
        lines.append(sub)
        if ev.get("join"):
            lines.append(f"-- Join meeting | href={ev['join']}")
        if ev.get("notes"):
            for line in ev["notes"].splitlines():
                line = line.strip()
                if len(line) < 12 or re.fullmatch(r"[^\w]+", line):
                    continue
                low = line.lower()
                if low.startswith(("join ", "meeting ", "http")):
                    continue
                if "inviting you to a scheduled" in low:
                    continue
                lines.append(f"-- {line[:70]}")
                break
    lines += [
        "---",
        f"Timezone: {now.tzname()} ({os.environ.get('MEETNEXT_TZ') or 'system'}) | size=11 color=#8e8e93",
        "Refresh | refresh=true",
        "Open Calendar | bash=/usr/bin/open param1=-a param2=Calendar terminal=false",
    ]
    return "\n".join(lines)


def main():
    args = sys.argv[1:]

    if "--demo" in args:
        i = args.index("--demo")
        day = args[i + 1] if len(args) > i + 1 and not args[i + 1].startswith("--") else None
        demo(day)
        return

    if "--prime" in args:
        state = load_state()
        for name in jxa("names"):
            state[name] = {"updated": time.time(), "events": jxa("series-one", name)}
            save_json(SERIES_STATE, state)
        save_events(collect())
        return

    if "--notify" in args:
        notify_stale_feeds()
        events, age = load_events()
        if events is None:
            return
        if age > EVENT_TTL:
            spawn_refresh()
        notify_due(events)
        return

    if "--refresh" in args:
        try:
            save_events(collect())
            refresh_one_series()
        finally:
            lock = os.path.join(CACHE, "events.lock")
            if os.path.exists(lock):
                os.remove(lock)
        return

    events, age = load_events()
    if events is None:
        spawn_refresh()
        if not args:
            print("⏳ loading meetings…")
            return
        for _ in range(90):
            time.sleep(1)
            events, age = load_events()
            if events is not None:
                break
        events = events or []
    elif age > EVENT_TTL:
        spawn_refresh()

    if "--json" in args:
        print(json.dumps([
            {**e, "start": e["start"].isoformat(), "end": e["end"].isoformat()}
            for e in events
        ], indent=1))
        return
    if "--next" in args:
        now = datetime.now().astimezone()
        nxt = next((e for e in events if e["start"] > now), None)
        if not nxt:
            print("no upcoming meetings in 4 days")
            return
        url = f" {nxt['join']}" if nxt.get("join") else ""
        print(f"{nxt['start']:%a %H:%M} {nxt['title']} (in {fmt_delta(nxt['start'] - now)}){url}")
        return
    print(render_swiftbar(events))


if __name__ == "__main__":
    main()
