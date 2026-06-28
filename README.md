# DC Bike Weather

Hour-by-hour cycling-suitability forecasts for Washington, DC — or anywhere you
point it — built by comparing every major global weather model, physics **and**
AI, on the things that actually matter on a bike: heat, rain, storms, wind, sun,
and light.

It ships in two forms that share one analysis engine:

- **A standalone Python script** that writes a multi-page PDF forecast.
- **A Quarto report** that renders the same analysis to HTML and PDF, fully
  parameterized so you can change location, units, models, and rider profile
  without touching code.

> ⚠️ These are planning estimates derived from public forecast data — not a
> substitute for official watches and warnings. Always check
> [weather.gov](https://www.weather.gov) before heading out.

---

## What's in here

| File | What it is |
|------|------------|
| `dc_bike_weather.py` | The analysis engine **and** a standalone PDF generator (~1,300 lines, no framework). |
| `dc_bike_weather_report.qmd` | A Quarto report (HTML + PDF) that imports the `.py` as a library. |

The `.qmd` does `import dc_bike_weather`, so **keep both files in the same
folder**.

---

## Features

- **Multi-model consensus.** Nine models side by side, with a consensus row so
  you can see where they agree and where they don't:
  GFS (US), ECMWF (EU), ICON (DE), GEM (CA), UKMO (UK), Météo-France, JMA (JP),
  plus two AI models — ECMWF **AIFS** and **GraphCast**.
- **Cycling-specific meteorology**, all computed from scratch:
  - Heat index (NWS Rothfusz), wet-bulb (Stull 2011), and an estimated full-sun
    "black-globe" temperature.
  - **WBGT** heat-stress index with athletic Green / Yellow / Red / Black flags.
  - **Wind chill** (NWS formula) for cold days.
  - UV index (WHO bands) and sunrise / sunset / civil-twilight (NOAA solar
    algorithm).
- **A 0–100 "bikeability" score** per hour and per model, with transparent
  additive penalties for heat, rain, storms, wind, and cold. Thunderstorm or
  extreme-instability hours are capped at "Avoid."
- **A best-ride-window recommendation** that's light-aware: if you ride with a
  good light and habitually go out early, darkness barely counts against the
  morning.
- **NWS active-alert** banner and per-day filtering (US only).
- **Conditional deep-dive panels** in the report: a WBGT heat-stress view
  appears on hot days, a wind-chill view on cold days — each with flags and
  ride guidance.
- **Fully parameterized report**: location, time zone, units (°F/°C), forecast
  days, model set, and rider profile are all knobs.

---

## Requirements

- **Python 3.9+** with:

  ```bash
  pip install requests pandas numpy matplotlib
  ```

- **For the Quarto report**, additionally:
  - [Quarto CLI](https://quarto.org/docs/download/)
  - **Jupyter** — this is what runs the report's Python cells:
    `pip install jupyter`
  - For PDF output, a LaTeX install: `quarto install tinytex` (one time)

---

## Quick start

### The PDF script

```bash
python dc_bike_weather.py
```

Writes `dc_bike_weather_<YYYYMMDD>.pdf` — three pages per day for today and
tomorrow — and prints a short text summary. Configure it by editing the
`CONFIG` block at the top of the file.

### The Quarto report

```bash
quarto render dc_bike_weather_report.qmd            # HTML + PDF
quarto render dc_bike_weather_report.qmd --to html  # HTML only
quarto render dc_bike_weather_report.qmd --to pdf   # PDF only
```

Both builds fetch live data at render time, so you need an internet connection.

---

## Configuring the report

Every knob lives in the **`parameters` cell** near the top of the `.qmd`. The
defaults describe Washington, DC. Override any of them at render time without
editing the file.

Single values on the command line:

```bash
quarto render dc_bike_weather_report.qmd -P units:metric -P forecast_days:1
```

Lists or several values via a YAML file (`params.yml`):

```yaml
location_name: "Boulder, CO"
lat: 40.0150
lon: -105.2705
tz: "America/Denver"
units: metric
forecast_days: 3
preferred_hours: [6, 10]
models: [gfs_seamless, ecmwf_ifs025, icon_seamless, ecmwf_aifs025]
```

```bash
quarto render dc_bike_weather_report.qmd --execute-params params.yml
```

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `location_name` | `"Washington, DC"` | Name shown in titles |
| `lat`, `lon` | DC | Forecast location |
| `tz` | `"America/New_York"` | IANA time zone |
| `forecast_days` | `2` | 1 = today; 2 = today + tomorrow; … |
| `units` | `"us"` | `"us"` → °F, mph · `"metric"` → °C, km/h |
| `have_light` | `true` | If true, after-dark hours get only a small penalty |
| `preferred_hours` | `[5, 9]` | No-darkness-penalty window `[start, end)`; `None` to disable |
| `ride_hours` | `3` | Length of the headline "best window" |
| `models` | `None` | `None` = all models, or a list of Open-Meteo model ids |
| `show_alerts` | `true` | Include active NWS alerts |
| `alert_email` | placeholder | Contact folded into the NWS API User-Agent |

### On Posit Connect Cloud

Connect Cloud has no `-P` UI — it injects **environment variables**, which the
`parameters` cell reads. Set them under **Advanced settings → Configure
variables → Add variable** when you publish, or later from the content's
**Settings** page (they're encrypted and applied when Connect Cloud renders your
repo). Each variable is `BIKE_` + the parameter name in upper case:

| Env variable | Overrides | Example value |
|---|---|---|
| `BIKE_LOCATION_NAME` | `location_name` | `Boulder, CO` |
| `BIKE_LAT`, `BIKE_LON` | `lat`, `lon` | `40.0150`, `-105.2705` |
| `BIKE_TZ` | `tz` | `America/Denver` |
| `BIKE_FORECAST_DAYS` | `forecast_days` | `3` |
| `BIKE_UNITS` | `units` | `metric` |
| `BIKE_HAVE_LIGHT` | `have_light` | `false` |
| `BIKE_PREFERRED_HOURS` | `preferred_hours` | `6,10`  (or `none`) |
| `BIKE_RIDE_HOURS` | `ride_hours` | `2` |
| `BIKE_MODELS` | `models` | `gfs_seamless,ecmwf_ifs025`  (or `none` = all) |
| `BIKE_SHOW_ALERTS` | `show_alerts` | `true` |
| `BIKE_ALERT_EMAIL` | `alert_email` | `you@example.com` |

Parsing notes: booleans accept `1/true/yes/on`; lists are comma-separated (or a
JSON array), and `none` clears them. Anything you don't set keeps the default
above. Precedence, later wins: **defaults → `BIKE_*` env vars → `-P` / params
file**. This applies only when Connect Cloud renders the source (publish from
GitHub or a source deploy); if you `quarto publish` locally rendered output, the
values bake in at your local render and the Cloud variables aren't consulted.

The scientific thresholds (WBGT, gust, and wet-bulb cutoffs) stay in the `.py`
as advanced settings.

---

## What the report contains

A short intro, an alerts callout (if any), then **one section per day, each
starting on a new page**:

1. **Recommended ride window** — the single best block, light-aware.
2. **Bikeability heatmap** — every model vs the consensus, hour by hour, with
   the best window starred.
3. **What the models say** — plain-language highlights.
4. **Consensus table** — every three hours (rich HTML on the web, a colour-coded
   image in the PDF).
5. **Heat stress (WBGT)** — *only when heat is a concern*: the flag reached,
   flag windows, wet-bulb danger, and ride advice, plus a focused WBGT chart.
6. **Wind chill** — *only when cold is a concern*: the feels-like low, the
   frostbite-risk band, and cold-weather advice, plus a focused chart.
7. **Temperature, heat stress & UV** and **Precipitation, storms & wind** — the
   detailed multi-panel charts.

A methodology section closes it out.

---

## How the bikeability score works

Each hour starts at 100 and loses points for:

- **Heat** — via WBGT where available, falling back to heat index, then air temp.
- **Rain** — by amount (mm) and cross-model agreement.
- **Severe weather** — CAPE thresholds + WMO thunderstorm codes + gusts; storm
  hours are capped at "Avoid."
- **Wind** — sustained speed and gusts.
- **Cold** — low temperatures.

Scores map to ratings: **Avoid · Poor · Fair · Good · Excellent**. Full
thresholds are documented in the report's methodology section and in the
`CONFIG` block of the script.

---

## Data sources

- **Forecasts:** [Open-Meteo](https://open-meteo.com) (CC BY 4.0) — all models
  through one API.
- **Alerts:** US [National Weather Service](https://www.weather.gov)
  (`api.weather.gov`).

Derived comfort metrics use published methods: NWS Rothfusz heat index, Stull
(2011) wet-bulb, the standard outdoor WBGT weighting, the NWS wind-chill
formula, and the NOAA solar-position algorithm.

---

## Troubleshooting

**`quarto: command not found` (or "not recognized" on Windows).**
Quarto installed, but your open terminal still has the old PATH. Close it and
open a new one (fully restart VS Code / RStudio). If it still can't be found,
the install didn't finish — re-run the installer from
[quarto.org](https://quarto.org/docs/download/), or render from R with
`quarto::quarto_render("dc_bike_weather_report.qmd")` (RStudio bundles its own
copy of Quarto).

**Quarto can't find Python / Jupyter.**
The report uses the Python engine, so Jupyter must be installed in the
interpreter Quarto uses. Run `quarto check jupyter` to see which one it picked.
If it's the wrong Python, point Quarto at the right one:

```bash
# macOS / Linux
export QUARTO_PYTHON=/path/to/python
# Windows PowerShell
$env:QUARTO_PYTHON = "C:\path\to\python.exe"
```

Or activate your conda/venv environment (with Jupyter installed) before
rendering.

**PDF render fails.** Install LaTeX once: `quarto install tinytex`.

**No data / network errors at render.** The setup cell fetches live data — you
need an internet connection, and `api.weather.gov` must be reachable for alerts.

---

## Notes

- The two outputs share one engine, so fixes and new metrics land in both.
- Wind chill is only defined for air ≤ 10 °C, so the wind-chill view simply
  doesn't appear when it's warm; likewise the WBGT view appears only once heat
  reaches the Yellow flag.
- All comfort numbers are estimates for planning. Check official forecasts and
  alerts before riding.
