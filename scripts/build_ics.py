#!/usr/bin/env python3
"""Build a calendar feed of the University of Toronto macro seminars.

Two sources are merged into one VCALENDAR:

  Department seminars (economics.utoronto.ca)
    1. The department's per-series .ics feed, authoritative for event ids,
       dates and times. It is a rolling window and it sometimes leaks events
       from other series, so events are filtered on CATEGORIES.
    2. The HTML listing of upcoming seminars, the only place the paper PDF
       link and the organizer names appear. Enrichment only: if this fetch
       fails the build still succeeds, it just loses those extras.

  Macro brown bag workshop (a link-shared Google Sheet)
    Exported as CSV. Rows whose Time column does not parse as a time range
    are schedule notes rather than talks ("Reading week", "Thanksgiving")
    and are skipped. If the sheet is unreachable the brown bags already in
    the published feed are carried over, so a Google outage cannot silently
    blank half the calendar.

Standard library only.
"""

import csv
import html
import io
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://www.economics.utoronto.ca/index.php/index"
UA = "uoft-macro-seminars/1.0 (+https://github.com/michaelboutros/uoft-macro-seminars)"
TZ = ZoneInfo("America/Toronto")

# Department seminar series: seriesId -> the name written into CATEGORIES.
# Add a line here to fold another series into the feed.
SERIES = {
    13: "Macroeconomics",
    17: "International macroeconomics",
}

# Macro brown bag workshop sign-up sheet.
SHEET_ID = "1KdPRBJ87OnCImU4pWxpfbdePqfeZp_151oFyBqFwRC8"
SHEET_GID = "0"
SHEET_CSV = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
BROWNBAG_CATEGORY = "Macro brown bag"

# The sheet's Comments column carries internal scheduling notes written for
# the organizers ("feel free to move me"), not for subscribers. Flip to True
# to publish them in the event description.
INCLUDE_SHEET_COMMENTS = False

# Link to the sign-up sheet from each brown bag event. Set False to keep the
# sheet out of the public feed.
LINK_TO_SHEET = True

CAL_NAME = "UofT Macro Seminars"
CAL_DESC = "Macroeconomics seminars and the macro brown bag workshop at the University of Toronto Department of Economics."
PRODID = "-//michaelboutros//UofT Macro Seminars//EN"
OUT = Path(__file__).resolve().parent.parent / "docs" / "uoft-macro-seminars.ics"

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------
# iCalendar text handling
# --------------------------------------------------------------------------

def unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))


def unescape_ics(value):
    out, i = [], 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def escape_ics(value):
    out = []
    for ch in value:
        if ch in ("\\", ";", ","):
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        else:
            out.append(ch)
    return "".join(out)


def fold(line):
    """Fold to 75 octets per RFC 5545 without splitting a UTF-8 character."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    chunks, start, limit = [], 0, 75
    while start < len(raw):
        end = min(start + limit, len(raw))
        while end > start and end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(raw[start:end].decode("utf-8"))
        start = end
        limit = 74  # continuation lines carry a leading space
    return "\r\n ".join(chunks)


def parse_vevents(ics_text):
    events = []
    for block in unfold(ics_text).split("BEGIN:VEVENT")[1:]:
        props = {}
        for line in block.split("END:VEVENT")[0].strip().split("\n"):
            if ":" not in line:
                continue
            name, _, value = line.partition(":")
            props[name.split(";")[0].strip().upper()] = unescape_ics(value.strip())
        if props.get("UID") and props.get("DTSTART"):
            events.append(props)
    return events


# --------------------------------------------------------------------------
# Department seminars
# --------------------------------------------------------------------------

def parse_description(desc):
    """Pull title / speaker / affiliation / joint-with / homepage apart."""
    fields, order = {}, ["Title", "Speaker", "Seminar series", "Web"]
    for i, key in enumerate(order):
        m = re.search(rf"^{re.escape(key)}:\s*(.*?)(?=\n\n(?:{'|'.join(order[i+1:])}):|\Z)",
                      desc, re.S | re.M)
        if m:
            fields[key] = re.sub(r"\s+", " ", m.group(1)).strip()

    speaker_line, joint = fields.get("Speaker", ""), ""
    m = re.search(r"\(Joint with:\s*(.*?)\)\s*$", speaker_line)
    if m:
        joint = m.group(1).strip()
        speaker_line = speaker_line[:m.start()].strip()
    name, _, affil = speaker_line.partition(", ")
    return {
        "title": fields.get("Title", "").strip() or "TBA",
        "speaker": re.sub(r"\s+", " ", name).strip(),
        "affiliation": affil.strip(),
        "joint": joint,
        "web": fields.get("Web", "").strip(),
        "series": fields.get("Seminar series", "").strip(),
    }


def strip_tags(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_listing(page):
    """Return {(YYYYMMDD, surname): {paper, organizers}} for one listing page."""
    found = {}
    for block in page.split("<!-- Presenters  -->")[1:]:
        date_m = re.search(r'font-weight:500">\s*(.*?)</span>', block, re.S)
        head_m = re.search(r"<h5>(.*?)</h5>", block, re.S)
        if not (date_m and head_m):
            continue
        d = re.match(r"\w+,\s+(\w+)\s+(\d{1,2})\s+(\d{4})", strip_tags(date_m.group(1)))
        if not d or d.group(1) not in MONTHS:
            continue
        key_date = f"{int(d.group(3)):04d}{MONTHS[d.group(1)]:02d}{int(d.group(2)):02d}"

        speaker = strip_tags(re.sub(r"\(.*?\)\s*$", "", strip_tags(head_m.group(1))))
        surname = speaker.split()[-1].lower() if speaker else ""
        if not surname:
            continue

        paper = ""
        title_m = re.search(r"<h6>(.*?)</h6>", block, re.S)
        if title_m:
            link = re.search(r'href="([^"]*downloadSeminarFile[^"]*)"', title_m.group(1))
            if link:
                paper = html.unescape(link.group(1))

        # The last block on the page runs into the site footer, so pull the
        # organizers out of their <a> tags rather than by scanning to the end.
        organizers, names = "", []
        org_m = re.search(r"Organizers?:", block)
        if org_m:
            names = [n for n in (strip_tags(t) for t in re.findall(
                r'<a[^>]+person/person/faculty/\d+"[^>]*>(.*?)</a>',
                block[org_m.end():], re.S)) if n]
        if len(names) > 1:
            organizers = ", ".join(names[:-1]) + " and " + names[-1]
        elif names:
            organizers = names[0]

        found[(key_date, surname)] = {"paper": paper, "organizers": organizers}
    return found


def department_events():
    wanted = set(SERIES.values())
    raw, listing = {}, {}
    for series_id, _ in SERIES.items():
        feed = fetch(f"{BASE}/calendar/icalendar_series/seminar_series-{series_id}.ics")
        for ev in parse_vevents(feed):
            if ev.get("CATEGORIES", "").strip() in wanted:
                raw[ev["UID"]] = ev
        try:
            listing.update(parse_listing(
                fetch(f"{BASE}/research/seminars?dateRange=future&seriesId={series_id}")))
        except (urllib.error.URLError, OSError) as exc:
            print(f"warning: listing fetch failed for series {series_id}: {exc}",
                  file=sys.stderr)

    events = []
    for ev in raw.values():
        info = parse_description(ev.get("DESCRIPTION", ""))
        surname = info["speaker"].split()[-1].lower() if info["speaker"] else ""
        extra = listing.get((ev["DTSTART"][:8], surname), {})

        speaker = info["speaker"] or "TBA"
        affil = f" ({info['affiliation']})" if info["affiliation"] else ""
        summary = f"Macro: {speaker}{affil}"
        if info["title"].upper() != "TBA":
            summary += f" — {info['title']}"

        desc = [info["title"], "", f"Speaker: {speaker}{affil}"]
        if info["joint"]:
            desc.append(f"Joint with: {info['joint']}")
        if extra.get("organizers"):
            desc.append(f"Organizer: {extra['organizers']}")
        if info["series"]:
            desc.append(f"Series: {info['series']}")
        desc.append("")
        if extra.get("paper"):
            desc.append(f"Paper: {extra['paper']}")
        if info["web"]:
            desc.append(f"Speaker page: {info['web']}")
        if ev.get("URL"):
            desc.append(f"Event page: {ev['URL']}")

        events.append({
            "uid": ev["UID"],
            "start": ev["DTSTART"],
            "end": ev.get("DTEND", ""),
            "summary": summary,
            "location": ev.get("LOCATION", ""),
            "description": "\n".join(desc).strip(),
            "url": ev.get("URL", ""),
            "category": ev.get("CATEGORIES", "Macroeconomics"),
        })
    return events


# --------------------------------------------------------------------------
# Brown bag workshop
# --------------------------------------------------------------------------

TIME_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*([ap])\.?m?\.?\s*[-–—]\s*(\d{1,2}):(\d{2})\s*([ap])\.?m?\.?\s*$",
    re.I)
TIME_RE_LOOSE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*(?:([ap])\.?m?\.?)?\s*[-–—]\s*(\d{1,2}):(\d{2})\s*(?:([ap])\.?m?\.?)?\s*$",
    re.I)


def _to24(hour, meridiem):
    """12-hour to 24-hour. With no meridiem, assume a daytime seminar slot."""
    if meridiem:
        m = meridiem.lower()
        if m == "p" and hour != 12:
            return hour + 12
        if m == "a" and hour == 12:
            return 0
        return hour
    return hour + 12 if hour <= 7 else hour


def parse_time_range(text):
    """'12:10-1:00pm' -> ((12, 10), (13, 0)). None if it isn't a time range."""
    m = TIME_RE_LOOSE.match(text or "")
    if not m:
        return None
    sh, sm, smer, eh, em, emer = (int(m[1]), int(m[2]), m[3],
                                  int(m[4]), int(m[5]), m[6])
    end_h = _to24(eh, emer)
    # An unmarked start usually shares the end's meridiem ("12:10-1:00pm").
    start_h = _to24(sh, smer or emer)
    if start_h * 60 + sm >= end_h * 60 + em and start_h < 12:
        start_h += 12
    if start_h * 60 + sm >= end_h * 60 + em:
        return None
    return (start_h, sm), (end_h, em)


def room_label(raw):
    raw = (raw or "").strip()
    if not raw:
        return "Max Gluskin House"
    m = re.match(r"^GE\s*[-–]?\s*(.+)$", raw, re.I)
    return f"Max Gluskin House, room {m.group(1).strip()}" if m else raw


def to_utc(year, month, day, hm):
    local = datetime(year, month, day, hm[0], hm[1], tzinfo=TZ)
    return local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def brownbag_events():
    rows = list(csv.DictReader(io.StringIO(fetch(SHEET_CSV))))
    events = []
    for row in rows:
        row = { (k or "").strip(): (v or "").strip() for k, v in row.items() }
        date_m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", row.get("Date", ""))
        if not date_m:
            continue
        month, day, year = int(date_m[1]), int(date_m[2]), int(date_m[3])

        # A Time cell that isn't a time range is a schedule note, not a talk:
        # "Reading week", "Thanksgiving", "Swapped with Macro workshop".
        span = parse_time_range(row.get("Time", ""))
        if not span:
            continue

        presenter = row.get("Presenter", "") or "TBA"
        title = row.get("Title", "") or "TBA"

        summary = f"Brown bag: {presenter}"
        if title.upper() != "TBA":
            summary += f" — {title}"

        desc = [title, "", f"Presenter: {presenter}", "Series: Macro brown bag workshop"]
        if INCLUDE_SHEET_COMMENTS and row.get("Comments"):
            desc.append(f"Note: {row['Comments']}")
        if LINK_TO_SHEET:
            desc += ["", f"Schedule and sign-up: {SHEET_URL}"]

        events.append({
            "uid": f"bb-{year:04d}{month:02d}{day:02d}@uoft-macro-seminars",
            "start": to_utc(year, month, day, span[0]),
            "end": to_utc(year, month, day, span[1]),
            "summary": summary,
            "location": room_label(row.get("Room", "")),
            "description": "\n".join(desc).strip(),
            "url": SHEET_URL if LINK_TO_SHEET else "",
            "category": BROWNBAG_CATEGORY,
        })
    return events


def published_events(category):
    """Brown bags already in the published feed, for use as a fallback."""
    if not OUT.exists():
        return []
    events = []
    for ev in parse_vevents(OUT.read_text()):
        if ev.get("CATEGORIES", "").strip() == category:
            events.append({
                "uid": ev["UID"],
                "start": ev["DTSTART"],
                "end": ev.get("DTEND", ""),
                "summary": ev.get("SUMMARY", ""),
                "location": ev.get("LOCATION", ""),
                "description": ev.get("DESCRIPTION", ""),
                "url": ev.get("URL", ""),
                "category": category,
            })
    return events


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def emit(ev, stamp):
    lines = [
        "BEGIN:VEVENT",
        f"UID:{ev['uid']}",
        f"DTSTAMP:{stamp}",
        f"LAST-MODIFIED:{stamp}",
        f"DTSTART:{ev['start']}",
    ]
    if ev["end"]:
        lines.append(f"DTEND:{ev['end']}")
    lines.append(f"SUMMARY:{escape_ics(ev['summary'])}")
    if ev["location"]:
        lines.append(f"LOCATION:{escape_ics(ev['location'])}")
    lines.append(f"DESCRIPTION:{escape_ics(ev['description'])}")
    if ev["url"]:
        lines.append(f"URL:{ev['url']}")
    lines.append(f"CATEGORIES:{escape_ics(ev['category'])}")
    lines.append("END:VEVENT")
    return lines


def main():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    try:
        seminars = department_events()
    except (urllib.error.URLError, OSError) as exc:
        print(f"error: could not fetch department seminars: {exc}", file=sys.stderr)
        return 1
    if not seminars:
        print("error: no department seminars found, refusing to rewrite the feed",
              file=sys.stderr)
        return 1

    try:
        brownbags = brownbag_events()
        if not brownbags:
            raise ValueError("sheet parsed but yielded no dated talks")
    except (urllib.error.URLError, OSError, ValueError, csv.Error) as exc:
        brownbags = published_events(BROWNBAG_CATEGORY)
        print(f"warning: brown bag sheet unusable ({exc}); "
              f"kept {len(brownbags)} already-published brown bag(s)", file=sys.stderr)

    events = sorted(seminars + brownbags, key=lambda e: (e["start"], e["uid"]))

    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_ics(CAL_NAME)}",
        f"X-WR-CALDESC:{escape_ics(CAL_DESC)}",
        "X-WR-TIMEZONE:America/Toronto",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        "X-PUBLISHED-TTL:PT12H",
    ]
    for ev in events:
        out += emit(ev, stamp)
    out.append("END:VCALENDAR")

    text = "\r\n".join(fold(line) for line in out) + "\r\n"

    # DTSTAMP/LAST-MODIFIED move on every run; ignore them when deciding
    # whether anything actually changed, so scheduled runs stay quiet.
    def significant(s):
        s = s.replace("\r\n", "\n")
        return re.sub(r"(?m)^(DTSTAMP|LAST-MODIFIED):.*\n", "", s)

    if OUT.exists() and significant(OUT.read_text()) == significant(text):
        print(f"no change ({len(events)} events)")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, newline="")
    upcoming = [e for e in events if e["start"] >= stamp]
    (OUT.parent / "events.json").write_text(json.dumps({
        "updated": stamp,
        "total": len(events),
        "upcoming": len(upcoming),
        "seminars": len(seminars),
        "brownbags": len(brownbags),
    }, indent=2) + "\n")
    print(f"wrote {OUT} ({len(events)} events: {len(seminars)} seminars, "
          f"{len(brownbags)} brown bags; {len(upcoming)} upcoming)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
