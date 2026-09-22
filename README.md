# UofT Macro Seminars — calendar subscription

A self-refreshing iCalendar feed of the macroeconomics seminars at the University of Toronto Department of Economics.

**Subscribe:** `https://michaelboutros.github.io/uoft-macro-seminars/uoft-macro-seminars.ics`

Landing page with one-click subscribe buttons: <https://michaelboutros.github.io/uoft-macro-seminars/>

- Apple Calendar / Outlook: use the `webcal://` form of that URL, or File → New Calendar Subscription.
- Google Calendar: Other calendars → From URL → paste the `https://` URL.

Subscribing (rather than importing) means added, moved or retitled seminars show up on their own.

## How it works

`scripts/build_ics.py` reads two things from `economics.utoronto.ca` and merges them:

1. The department's per-series `.ics` feed — the authoritative source for event ids, dates and times. It is a rolling window and it sometimes leaks events from other series, so events are filtered on `CATEGORIES`.
2. The HTML listing of upcoming seminars — the only place the paper PDF link and the organizer names appear. If this fetch fails the build still succeeds; it just loses those extras.

The output uses `Macro: Speaker (Affiliation) — Title` as the event summary, so the calendar is readable at a glance in month view, and puts the title, coauthors, organizer, paper link and speaker homepage in the description.

The script is standard library only — no dependencies to install.

## Refreshing

`.github/workflows/refresh.yml` runs the build twice a day (07:20 and 19:20 UTC) and on demand via **Actions → Refresh seminar calendar → Run workflow**. It commits `docs/` only when the feed actually changes; `DTSTAMP` and `LAST-MODIFIED` are ignored when comparing, so unchanged runs are silent.

GitHub disables scheduled workflows after 60 days without repository activity. If nothing has changed for 30 days the job pushes an empty keepalive commit, so a quiet summer will not switch the schedule off.

## Changing which series are included

Edit the `SERIES` dictionary at the top of `scripts/build_ics.py`:

```python
SERIES = {
    13: "Macroeconomics",
    17: "International macroeconomics",
}
```

The keys are the department's `seriesId` values (visible in the URL when you filter the seminar listing) and the values are the names the department writes into `CATEGORIES`. Series 17 is currently dormant but is included so the feed picks it up if it restarts.

## Running it locally

```sh
python3 scripts/build_ics.py
```

Writes `docs/uoft-macro-seminars.ics` and `docs/events.json`.
