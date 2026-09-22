#!/usr/bin/env python3
"""Build a clean iCalendar feed of the University of Toronto macro seminars.

Sources (both from economics.utoronto.ca, stdlib only, no dependencies):

  1. The department's own per-series .ics feed. This is the authoritative
     source for event ids, dates and times, but it is a rolling window and it
     occasionally leaks events belonging to other seminar series.
  2. The HTML listing of upcoming seminars for the same series, which is where
     the paper PDF link and the organizer names live.

Events are filtered to the configured series, enriched from the HTML listing
where a match is found, and written out as a single VCALENDAR.
"""

import html
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://www.economics.utoronto.ca/index.php/index"
UA = "uoft-macro-seminars/1.0 (+https://github.com/michaelboutros/uoft-macro-seminars)"

# Seminar series to include: department seriesId -> the name the department
# writes into CATEGORIES. Add a line here to fold another series into the feed.
SERIES = {
    13: "Macroeconomics",
    17: "International macroeconomics",
}

CAL_NAME = "UofT Macro Seminars"
CAL_DESC = "Macroeconomics seminars at the University of Toronto Department of Economics."
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
# iCalendar parsing
# --------------------------------------------------------------------------

def unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))


def unescape_ics(value):
    out, i = [], 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append({"n": "\n", "N": "\n"}.get(nxt, nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def escape_ics(value):
    out = []
    for ch in value:
        if ch in "\\;,":
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
    chunks, start = [], 0
    limit = 75
    while start < len(raw):
        end = min(start + limit, len(raw))
        while end > start and end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(raw[start:end].decode("utf-8"))
        start = end
        limit = 74  # continuation lines carry a leading space
    return ("\r\n ").join(chunks)


def parse_events(ics_text):
    events = []
    for block in unfold(ics_text).split("BEGIN:VEVENT")[1:]:
        block = block.split("END:VEVENT")[0]
        props = {}
        for line in block.strip().split("\n"):
            if ":" not in line:
                continue
            name, _, value = line.partition(":")
            props[name.split(";")[0].strip().upper()] = unescape_ics(value.strip())
        if props.get("UID") and props.get("DTSTART"):
            events.append(props)
    return events


def parse_description(desc):
    """Pull title / speaker / affiliation / joint-with / homepage apart."""
    fields, order = {}, ["Title", "Speaker", "Seminar series", "Web"]
    for i, key in enumerate(order):
        m = re.search(rf"^{re.escape(key)}:\s*(.*?)(?=\n\n(?:{'|'.join(order[i+1:])}):|\Z)",
                      desc, re.S | re.M)
        if m:
            fields[key] = re.sub(r"\s+", " ", m.group(1)).strip()

    speaker_line = fields.get("Speaker", "")
    joint = ""
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


# --------------------------------------------------------------------------
# HTML listing (supplies paper links and organizers for upcoming seminars)
# --------------------------------------------------------------------------

def strip_tags(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_listing(page):
    """Return {(YYYYMMDD, speaker surname): {paper, organizers}} for one page."""
    found = {}
    for block in page.split("<!-- Presenters  -->")[1:]:
        date_m = re.search(r'font-weight:500">\s*(.*?)</span>', block, re.S)
        head_m = re.search(r"<h5>(.*?)</h5>", block, re.S)
        if not (date_m and head_m):
            continue
        date_text = strip_tags(date_m.group(1))
        d = re.match(r"\w+,\s+(\w+)\s+(\d{1,2})\s+(\d{4})", date_text)
        if not d or d.group(1) not in MONTHS:
            continue
        key_date = f"{int(d.group(3)):04d}{MONTHS[d.group(1)]:02d}{int(d.group(2)):02d}"

        speaker = strip_tags(re.sub(r"\(.*?\)\s*$", "", strip_tags(head_m.group(1))))
        surname = speaker.split()[-1].lower() if speaker else ""

        paper = ""
        title_m = re.search(r"<h6>(.*?)</h6>", block, re.S)
        if title_m:
            link = re.search(r'href="([^"]*downloadSeminarFile[^"]*)"', title_m.group(1))
            if link:
                paper = html.unescape(link.group(1))

        # The last block on the page runs into the site footer, so pull the
        # organizers out of their <a> tags rather than by scanning to the end.
        organizers = ""
        org_m = re.search(r"Organizers?:", block)
        if org_m:
            names = [strip_tags(t) for t in re.findall(
                r'<a[^>]+person/person/faculty/\d+"[^>]*>(.*?)</a>',
                block[org_m.end():], re.S)]
            names = [n for n in names if n]
            if len(names) > 1:
                organizers = ", ".join(names[:-1]) + " and " + names[-1]
            elif names:
                organizers = names[0]

        if surname:
            found[(key_date, surname)] = {"paper": paper, "organizers": organizers}
    return found


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def build_event(ev, extra, stamp):
    info = parse_description(ev.get("DESCRIPTION", ""))
    speaker = info["speaker"] or "TBA"
    affil = f" ({info['affiliation']})" if info["affiliation"] else ""
    title = info["title"]

    summary = f"Macro: {speaker}{affil}"
    if title and title.upper() != "TBA":
        summary += f" — {title}"

    desc = [f"{title}", ""]
    desc.append(f"Speaker: {speaker}{affil}")
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

    lines = [
        "BEGIN:VEVENT",
        f"UID:{ev['UID']}",
        f"DTSTAMP:{stamp}",
        f"LAST-MODIFIED:{stamp}",
        f"DTSTART:{ev['DTSTART']}",
    ]
    if ev.get("DTEND"):
        lines.append(f"DTEND:{ev['DTEND']}")
    lines.append(f"SUMMARY:{escape_ics(summary)}")
    if ev.get("LOCATION"):
        lines.append(f"LOCATION:{escape_ics(ev['LOCATION'])}")
    lines.append(f"DESCRIPTION:{escape_ics(chr(10).join(desc).strip())}")
    if ev.get("URL"):
        lines.append(f"URL:{ev['URL']}")
    if ev.get("CATEGORIES"):
        lines.append(f"CATEGORIES:{escape_ics(ev['CATEGORIES'])}")
    lines.append("END:VEVENT")
    return lines


def main():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    wanted = set(SERIES.values())

    events, listing = {}, {}
    for series_id, series_name in SERIES.items():
        try:
            feed = fetch(f"{BASE}/calendar/icalendar_series/seminar_series-{series_id}.ics")
        except (urllib.error.URLError, OSError) as exc:
            print(f"error: could not fetch ics for series {series_id}: {exc}", file=sys.stderr)
            return 1
        for ev in parse_events(feed):
            # The departmental feed occasionally includes events from other
            # series; keep only the ones we actually asked for.
            if ev.get("CATEGORIES", "").strip() in wanted:
                events[ev["UID"]] = ev

        try:
            page = fetch(f"{BASE}/research/seminars?dateRange=future&seriesId={series_id}")
            listing.update(parse_listing(page))
        except (urllib.error.URLError, OSError) as exc:
            # Enrichment only: a failure here costs paper links, not events.
            print(f"warning: could not fetch listing for series {series_id}: {exc}",
                  file=sys.stderr)

    if not events:
        print("error: no events found, refusing to overwrite the feed", file=sys.stderr)
        return 1

    ordered = sorted(events.values(), key=lambda e: e["DTSTART"])
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
    for ev in ordered:
        date_key = ev["DTSTART"][:8]
        info = parse_description(ev.get("DESCRIPTION", ""))
        surname = info["speaker"].split()[-1].lower() if info["speaker"] else ""
        out += build_event(ev, listing.get((date_key, surname), {}), stamp)
    out.append("END:VCALENDAR")

    text = "\r\n".join(fold(line) for line in out) + "\r\n"

    # DTSTAMP/LAST-MODIFIED change on every run; ignore them when deciding
    # whether anything actually changed, so the scheduled job stays quiet.
    def significant(s):
        s = s.replace("\r\n", "\n")
        return re.sub(r"(?m)^(DTSTAMP|LAST-MODIFIED):.*\n", "", s)

    if OUT.exists() and significant(OUT.read_text()) == significant(text):
        print(f"no change ({len(ordered)} events)")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, newline="")
    upcoming = [e for e in ordered if e["DTSTART"] >= stamp]
    (OUT.parent / "events.json").write_text(json.dumps({
        "updated": stamp,
        "total": len(ordered),
        "upcoming": len(upcoming),
    }, indent=2) + "\n")
    print(f"wrote {OUT} ({len(ordered)} events, {len(upcoming)} upcoming)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
