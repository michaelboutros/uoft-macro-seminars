# UofT Macro Seminars — calendar subscription

A self-refreshing iCalendar feed of the macroeconomics seminars **and the macro brown bag workshop** at the University of Toronto Department of Economics.

**Subscribe:** `https://michaelboutros.github.io/uoft-macro-seminars/uoft-macro-seminars.ics`

Landing page with one-click subscribe buttons: <https://michaelboutros.github.io/uoft-macro-seminars/>

- Apple Calendar / Outlook: use the `webcal://` form of that URL, or File → New Calendar Subscription.
- Google Calendar: Other calendars → From URL → paste the `https://` URL.

Subscribing (rather than importing) means added, moved or retitled seminars show up on their own.

## How it works

`scripts/build_ics.py` merges two sources into one calendar. It is standard library only — nothing to install.

**Department seminars**, from `economics.utoronto.ca`:

1. The department's per-series `.ics` feed — the authoritative source for event ids, dates and times. It is a rolling window and it sometimes leaks events from other series, so events are filtered on `CATEGORIES`.
2. The HTML listing of upcoming seminars — the only place the paper PDF link and the organizer names appear. If this fetch fails the build still succeeds; it just loses those extras.

**Macro brown bag workshop**, from the link-shared [sign-up sheet](https://docs.google.com/spreadsheets/d/1KdPRBJ87OnCImU4pWxpfbdePqfeZp_151oFyBqFwRC8/edit), exported as CSV. A row is a talk only if its `Time` cell parses as a time range; rows like `Reading week`, `Thanksgiving` or `Swapped with Macro workshop` are schedule notes and are skipped. Times are read as `America/Toronto` and converted to UTC, so the autumn DST change is handled. Event UIDs are keyed on the date, which means editing a presenter or title in the sheet updates the existing calendar entry instead of creating a duplicate.

If the sheet is unreachable, the brown bags already in the published feed are carried over and the run warns, so a Google outage cannot silently blank half the calendar. The build aborts rather than rewriting the feed if the department source yields nothing.

Summaries are `Macro: Speaker (Affiliation) — Title` and `Brown bag: Presenter — Title`, so the calendar is readable at a glance in month view, with the full title, coauthors, organizer, paper link and speaker homepage in the description.

### Two things you may want to change

Both are flags at the top of `scripts/build_ics.py`:

- `INCLUDE_SHEET_COMMENTS` (default `False`) — the sheet's `Comments` column holds internal scheduling notes written for the organizers, so they are kept out of the public feed.
- `LINK_TO_SHEET` (default `True`) — each brown bag links back to the sign-up sheet. Set it to `False` to keep the sheet out of the feed entirely.

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

The keys are the department's `seriesId` values (visible in the URL when you filter the seminar listing) and the values are the names the department writes into `CATEGORIES`. Series 17 is currently dormant but is included so the feed picks it up if it restarts. `International trade` (series 10) is deliberately not included.

To point the brown bags at a different sheet, change `SHEET_ID` and `SHEET_GID`. The sheet has to be readable by anyone with the link, since the Action fetches it unauthenticated.

## Running it locally

```sh
python3 scripts/build_ics.py
```

Writes `docs/uoft-macro-seminars.ics` and `docs/events.json`.
