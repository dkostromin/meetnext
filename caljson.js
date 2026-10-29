// caljson.js — dump calendar events as JSON for meetings.py
// Usage: osascript -l JavaScript caljson.js <mode> [arg] [arg2]
//   window <hoursForward>
//   series-one <hoursForward> <calendarName>
//   range <startISO> <endISO>
//   names

function run(argv) {
  const mode = argv[0] || "window";
  const forward = Number(argv[1] || 48);
  const only = argv[2] || null;

  const C = Application("Calendar");
  const skip = /holiday|праздник|birthday|siri suggestions|scheduled reminders/i;
  const seen = {};
  const names = [];
  C.calendars().forEach(c => {
    const n = c.name();
    if (!skip.test(n) && !seen[n]) { seen[n] = 1; names.push(n); }
  });

  if (mode === "names") return JSON.stringify(names);

  const now = new Date();
  let end, floor;
  if (mode === "range") {
    floor = new Date(argv[1]);
    end = new Date(argv[2]);
  } else {
    end = new Date(now.getTime() + forward * 3600 * 1000);
    floor = mode === "window"
      ? new Date(now.getTime() - 3 * 3600 * 1000)
      : new Date(now.getTime() - 400 * 24 * 3600 * 1000);
  }

  const targets = mode === "series-one" && only ? [only] : names;
  const out = [];
  targets.forEach(name => {
    let c;
    try { c = C.calendars.whose({ name: name })()[0]; } catch (e) { return; }
    let evs;
    try {
      evs = c.events.whose({ startDate: { _lessThan: end, _greaterThan: floor } })();
    } catch (e) { return; }
    evs.forEach(e => {
      try {
        out.push({
          title: e.summary(),
          start: e.startDate().toISOString(),
          end: e.endDate().toISOString(),
          allDay: !!e.alldayEvent(),
          recurrence: e.recurrence() || null,
          location: e.location() || null,
          notes: e.description() || null,
          url: e.url() ? String(e.url()) : null,
          calendar: name
        });
      } catch (err) {}
    });
  });

  return JSON.stringify(out);
}
