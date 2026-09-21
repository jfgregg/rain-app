# Rain — personal forecast app

## What this is

A mobile-first weather app for one person's personal use. The core idea: standard
"20% chance of rain" numbers hide whether that 20% means "a shower will definitely
pass through, we just don't know which hour" vs "a storm has a 20% chance of
forming at all." This app makes that distinction visible using ensemble forecast
data instead of a single collapsed probability.

## Core concept — the two numbers

For any given hour, compute two probabilities from the ensemble:

- **hourly_pop**: fraction of ensemble members predicting rain in that specific hour
- **window_max_pop**: fraction of members predicting rain *at any point* within a
  surrounding window (start with a fixed 3-hour window centered on or following
  the hour — exact placement is a judgment call, reasonable default is fine)

The relationship between these two numbers tells you the *type*:

- **Shower** — window_max_pop is much higher than the hourly values within it.
  Rain is likely somewhere in the window; timing is what's uncertain.
- **Storm** — window_max_pop is close to the hourly values within it. Little
  uncertainty is "absorbed" by widening the window — either it happens
  (roughly) here, or it doesn't happen at all.

No fixed threshold is specified yet for calling something a "shower" vs "storm" —
start with something simple like `is_shower = (window_max_pop - hourly_pop) > 25
percentage points`, and treat that threshold as tunable once we see it against
real data.

## Data sources

- **NOAA GEFS** (Global Ensemble Forecast System, 30 members) — primary source
  for the ensemble spread calculation above. Access via the `herbie-data` Python
  package, which handles pulling GRIB files from NOAA's AWS Open Data bucket
  without needing to hand-roll NOMADS/AWS access.
- **NWS API** (`api.weather.gov`) — free, no key required. Use for:
  - Current conditions (actual station observation, not a model estimate)
  - Also returns the observing station's ID and coordinates, so we can compute
    and display distance from the requested location and how stale the reading is
- **Rain intensity**: derive from GEFS precipitation rate (QPF) per member,
  bucketed into light / moderate / heavy / intense — exact mm/hr thresholds
  are a reasonable-default judgment call for now.

## Multi-location support

Build this in from the start, not as a retrofit:

- A short list of saved locations: `{ name, lat, lon }`
- One marked as default/current
- All pipeline functions take `(lat, lon)` as parameters — never hardcode a
  single location internally

## Current conditions — be transparent about staleness/distance

Don't present current conditions as if it's an exact reading at the user's
coordinates. Always show, alongside the temperature/conditions:
- distance from the reporting station (miles)
- how long ago it was last updated

This matters because station data can genuinely disagree with what's happening
right outside the user's window — see design notes below.

## Design reference

See `weather-mockup.html` (in this same folder) for the target visual design —
a working static HTML mockup, already approved. Key elements to preserve:

- Hourly bars for `hourly_pop`, greyscale-coded by rain intensity
- "Shading" behind the bars for `window_max_pop`, on the *same percentage scale*
  as the bars (this is the whole trick — shower shading rises above its own
  bars, storm shading sits level with them)
- Small icon + percentage label on each shaded region; tap opens a detail panel
  with fuller text (type, most likely time within the window)
- Drag-to-select custom window (e.g. "7:00–8:00 commute") with saved chips
  (e.g. "Commute", "Weekend") for quick recall, showing window probability +
  type + intensity for that specific range
- Current conditions row: icon + temp + wind, minimal text
- Typefaces: Space Grotesk (headers), IBM Plex Mono (all numeric/data readouts)
- Visual language throughout: instrument/graph-paper feel, not cartoon weather
  icons — greys and slate-blue/amber accents, not bright primary colors

## Build order (suggested)

1. **Pipeline** (`fetch_forecast.py`, starter below): pull GEFS ensemble for a
   lat/lon, compute hourly_pop, window_max_pop, intensity buckets, shower/storm
   classification. Output clean JSON.
2. **Current conditions**: NWS station lookup + distance/staleness calc.
3. **API layer**: wrap the above behind a small local endpoint (FastAPI or
   Flask) so the frontend can fetch by lat/lon.
4. **Frontend**: build the mobile PWA from the mockup, wire it to the API,
   add the drag-to-select window and saved chips.
5. **v2 (later, not now)**: replace the fixed 3-hour window with a dynamic
   window size derived from how spread out the ensemble members actually are.

## Open questions to resolve while building (not blockers, just flag them)

- Exact window size and placement for `window_max_pop` (fixed 3hr is the v1 default)
- Exact percentage-point threshold for shower vs storm classification
- Precip rate thresholds for the 4 intensity buckets
- How far back "how long ago it was last updated" should trigger a visible
  warning (e.g. >90 min old)
