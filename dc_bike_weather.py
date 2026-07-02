#!/usr/bin/env python3
"""
Biking suitability by hour for Washington, DC — multi-model weather analysis.

Pulls hourly forecasts from every major global weather model via the free
Open-Meteo API and writes a multi-page PDF that compares and contrasts the
models across the things that actually matter on a bike:

    * Temperature  — air (shade), heat index, estimated full-sun temperature,
                     wet-bulb temperature, and WBGT heat-stress index
    * Precipitation — amount and rain chance, model by model
    * Severe risk   — CAPE (thunderstorm energy), WMO thunderstorm codes, gusts
    * A composite hourly "bikeability" rating, shown per-model so you can see
      where the models agree and where they don't.

------------------------------------------------------------------------------
SETUP   :  pip install requests pandas numpy matplotlib
RUN     :  python dc_bike_weather.py
OUTPUT  :  dc_bike_weather_<YYYYMMDD>.pdf   (today, local DC time)
------------------------------------------------------------------------------

Methods / sources
  * Data: Open-Meteo (open-meteo.com), CC BY 4.0. Models requested below.
  * Heat index: U.S. NWS Rothfusz regression (+ low/high-RH adjustments).
  * Wet-bulb temperature: Stull (2011) psychrometric approximation,
    J. Appl. Meteor. Climatol. 50, 2267-2269 (sea-level pressure).
  * Full-sun ("black-globe") temperature: linearized sphere energy balance
    driven by shortwave radiation and wind. A black 150 mm globe in strong
    sun reads ~10-15 C above shade air temp at light wind; a MOVING cyclist
    sees a smaller uplift because the high relative airflow boosts convective
    cooling, so real ride exposure sits between the shade and full-sun curves.
  * WBGT (outdoor): 0.7*Tnwb + 0.2*Tg + 0.1*Ta, with natural wet bulb
    approximated by the psychrometric wet bulb (a mild simplification).
  * Severe proxy: CAPE thresholds + WMO codes 95/96/99 (thunder) + gusts.
  * Bikeability: transparent additive-penalty score (see THRESHOLDS); any hour
    with a thunderstorm flag or extreme instability is capped at "Avoid".

These derived comfort numbers are estimates for planning, not a substitute for
official watches/warnings. Check weather.gov before heading out.
"""

import sys
import warnings
import textwrap
import numpy as np
import pandas as pd
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
from matplotlib.colors import ListedColormap, BoundaryNorm
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Patch

# ============================ CONFIG ========================================
LAT, LON = 38.9072, -77.0369          # Washington, DC
PLACE = "Washington, DC"              # location name shown in titles
TZ = "America/New_York"
FORECAST_DAYS = 2                      # 1 = today only; 2 = today + tomorrow (one page set each)
UNITS = "us"                           # "us" -> degF, mph ; "metric" -> degC, km/h
OUTFILE = None                         # None -> auto-name with date

# Rider profile. With a powerful light, riding after dark is only a mild
# preference, not a blocker, so darkness barely discounts the best-ride window.
HAVE_LIGHT = True

# Hours (local, 24h) you habitually ride and are happy to ride in the dark, as
# (start, end) with end exclusive. Inside this window the best-ride-window pick
# applies NO darkness discount at all (weather penalties still apply). Wraps
# past midnight if start > end. Set to None to disable.
PREFERRED_HOURS = (5, 9)               # early-morning rides ~5am

# Typical ride length (hours) for the single headline "best ride window" pick.
# When a long stretch is rideable, the star window narrows to the best block of
# about this length (the full Good+ spans still appear in the summary).
RIDE_HOURS = 3

# Open-Meteo model id -> short display label. Comment out any you don't want.
# Physics (NWP) models plus two AI/ML models. The AI models forecast a core set
# of fields (temperature, humidity, wind, precipitation) but generally NOT CAPE,
# radiation or weather codes, so their severe/sun-stress cells may be blank and
# their heat score falls back to heat index / air temperature.
MODELS = {
    "gfs_seamless":         "GFS (US)",
    "ecmwf_ifs025":         "ECMWF (EU)",
    "icon_seamless":        "ICON (DE)",
    "gem_seamless":         "GEM (CA)",
    "ukmo_seamless":        "UKMO (UK)",
    "meteofrance_seamless": "Meteo-France",
    "jma_seamless":         "JMA (JP)",
    "ecmwf_aifs025":        "ECMWF AIFS (AI)",
    "gfs_graphcast025":     "GraphCast (AI)",
}

# NWS active-weather alerts (US only). Shown as a banner across the top of page 1.
SHOW_ALERTS = True
NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
# NWS asks API users to send a User-Agent identifying the app + a contact.
ALERT_UA = "dc-bike-weather/1.0 (personal use; replace-with-your-email@example.com)"

# Scoring thresholds (documented on the PDF methodology page).
WBGT_BLACK, WBGT_RED, WBGT_YELLOW = 32.2, 30.6, 27.8   # degC athletic heat flags
WETBULB_DANGER = 28.0                                   # degC, exertion danger
RAIN_MM_LIGHT, RAIN_MM_MOD, RAIN_MM_HEAVY = 0.1, 0.5, 2.5
CAPE_MARGINAL, CAPE_MOD, CAPE_STRONG, CAPE_EXTREME = 500, 1000, 2500, 4000
GUST_HIGH_MPH, GUST_MOD_MPH, WIND_HIGH_MPH = 40, 30, 25

# --- Rowing mode (singles / small-boat oriented; wind compared in mph) --------
MODE = "bike"                                      # "bike", "row", or "both"
ROW_WIND_MOD_MPH, ROW_WIND_HIGH_MPH = 10, 15       # sustained: chop / singles risk
ROW_GUST_MOD_MPH, ROW_GUST_HIGH_MPH = 17, 23       # gusts that upset a single
ROW_WHITECAP_MPH = 12                              # sustained wind that raises whitecaps
ROW_VIS_LOW_M, ROW_VIS_MOD_M = 1000, 3000          # metres; fog / collision risk
ROW_COLDWATER_SUM_F = 100                          # air+water below -> dress for immersion
ROW_COLDWATER_DANGER_F = 90                        # air+water below -> singles unsafe
ROW_WATER_COLD_C = 10.0                            # water below ~50 degF -> cold-shock risk
# Live river gauge (USGS NWIS instantaneous values). Default: Potomac at DC.
USGS_SITE = "01646500"                             # Potomac River near Washington, DC
FLOW_ELEVATED_CFS, FLOW_HIGH_CFS = 10000, 20000    # advisory only -> set to club rules
WATER_TEMP_C = None                                # manual fallback if gauge lacks temp
# ============================================================================

API_URL = "https://api.open-meteo.com/v1/forecast"
CORE_VARS = ["temperature_2m", "relative_humidity_2m", "precipitation",
             "precipitation_probability", "weather_code", "cloud_cover",
             "wind_speed_10m", "wind_gusts_10m", "wind_direction_10m",
             "shortwave_radiation", "uv_index", "visibility"]
BASE_VARS = CORE_VARS + ["cape"]

# UV index categories (WHO): upper bound, label, color.
UV_BANDS = [(2.5, "Low", "#4eb400"), (5.5, "Moderate", "#f7e400"),
            (7.5, "High", "#f85900"), (10.5, "Very High", "#d8001d"),
            (99.0, "Extreme", "#998cff")]

# Wind-chill (feels-like cold) categories: lower bound (degC), label, color.
# Bands approximate NWS frostbite-risk guidance for exposed skin.
WC_BANDS = [(5.0, "Cool", "#74c0fc"), (0.0, "Chilly", "#4dabf7"),
            (-10.0, "Cold", "#4263eb"), (-28.0, "Very cold", "#7048e8"),
            (-1.0e9, "Dangerous", "#d6336c")]
COLD_CONCERN_C = 5.0    # show the wind-chill view once feels-like reaches this

RATING_LABELS = ["Avoid", "Poor", "Fair", "Good", "Excellent"]
RATING_COLORS = ["#b2182b", "#ef8a62", "#fee08b", "#a6d96a", "#1a9850"]
RATING_BINS = [35, 50, 65, 80]          # np.digitize edges -> 0..4
MODEL_PALETTE = plt.cm.tab10(np.linspace(0, 1, 10))

US = (UNITS == "us")
TU = "\u00b0F" if US else "\u00b0C"
WU = "mph" if US else "km/h"

ALL_MODELS = dict(MODELS)   # immutable master, so model subsetting is repeatable


def configure(**kw):
    """Override CONFIG values from outside (e.g. a Quarto report) and recompute
    the few derived globals. Keys are the lower-case config names; a value of
    None means "leave unchanged" for the plain settings.

    Special handling:
      models          list of Open-Meteo model ids to include (order kept;
                      unknown ids keep the id as their label). None/empty = all.
      preferred_hours [start, end] (end exclusive) or None to disable the
                      no-darkness-penalty window. (None IS applied here.)
      location_name / place   name shown in figure/report titles.
      alert_email     contact address folded into the NWS User-Agent.
    Also accepts the scientific thresholds (e.g. gust_high_mph, wetbulb_danger)
    for power users. Returns a small dict summarising what was applied.
    """
    g = globals()
    simple = {
        "lat": "LAT", "lon": "LON", "tz": "TZ", "forecast_days": "FORECAST_DAYS",
        "units": "UNITS", "outfile": "OUTFILE", "have_light": "HAVE_LIGHT",
        "ride_hours": "RIDE_HOURS", "show_alerts": "SHOW_ALERTS",
        "alert_ua": "ALERT_UA", "nws_alerts_url": "NWS_ALERTS_URL",
        "wetbulb_danger": "WETBULB_DANGER", "gust_high_mph": "GUST_HIGH_MPH",
        "gust_mod_mph": "GUST_MOD_MPH", "wind_high_mph": "WIND_HIGH_MPH",
        "mode": "MODE", "usgs_site": "USGS_SITE", "water_temp_c": "WATER_TEMP_C",
        "flow_elevated_cfs": "FLOW_ELEVATED_CFS", "flow_high_cfs": "FLOW_HIGH_CFS",
        "row_wind_mod_mph": "ROW_WIND_MOD_MPH", "row_wind_high_mph": "ROW_WIND_HIGH_MPH",
        "row_gust_mod_mph": "ROW_GUST_MOD_MPH", "row_gust_high_mph": "ROW_GUST_HIGH_MPH",
    }
    for k, name in simple.items():
        if kw.get(k) is not None:
            g[name] = kw[k]
    place = kw.get("location_name") or kw.get("place")
    if place:
        g["PLACE"] = place
    if "preferred_hours" in kw:                       # None is meaningful here
        ph = kw["preferred_hours"]
        g["PREFERRED_HOURS"] = tuple(ph) if ph else None
    if kw.get("models"):
        g["MODELS"] = {m: ALL_MODELS.get(m, m) for m in kw["models"]}
    if kw.get("alert_email"):
        g["ALERT_UA"] = f"dc-bike-weather/1.0 (personal use; {kw['alert_email']})"
    g["US"] = (g["UNITS"] == "us")
    g["TU"] = "\u00b0F" if g["US"] else "\u00b0C"
    g["WU"] = "mph" if g["US"] else "km/h"
    return {"place": g["PLACE"], "units": g["UNITS"],
            "forecast_days": g["FORECAST_DAYS"], "models": list(g["MODELS"]),
            "mode": g["MODE"]}


# ---------------------------- unit helpers ----------------------------------
def Td(t_c):                       # temperature for display
    t_c = np.asarray(t_c, float)
    return t_c * 9 / 5 + 32 if US else t_c

def dTd(d_c):                      # temperature *difference* for display
    d_c = np.asarray(d_c, float)
    return d_c * 9 / 5 if US else d_c

def Wd(v_ms):                      # wind for display
    v_ms = np.asarray(v_ms, float)
    return v_ms * 2.23694 if US else v_ms * 3.6

def hlabel(ts):
    h = ts.hour % 12 or 12
    return f"{h}{'a' if ts.hour < 12 else 'p'}"

def hm(ts):
    """Hour:minute am/pm, e.g. 5:43a. Portable (no platform strftime codes)."""
    if ts is None:
        return "--"
    h = ts.hour % 12 or 12
    return f"{h}:{ts.minute:02d}{'a' if ts.hour < 12 else 'p'}"

def _hax(x, pos=None):
    t = mdates.num2date(x)
    h = t.hour % 12 or 12
    return f"{h}{'a' if t.hour < 12 else 'p'}"

def uv_band(uv):
    """(label, color) for a UV index value."""
    if uv is None or np.isnan(uv):
        return ("--", "#999999")
    for hi, label, color in UV_BANDS:
        if uv < hi:
            return (label, color)
    return UV_BANDS[-1][1:]

def sun_times(lat, lon, d):
    """Sunrise, sunset and civil dawn/dusk for a date, via the NOAA solar
    algorithm. Returns tz-aware Timestamps (or None where the sun never
    reaches the angle, e.g. polar day/night). lon is positive-east."""
    import math
    N = d.timetuple().tm_yday
    g = 2 * math.pi / 365 * (N - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    latr = math.radians(lat)
    base = pd.Timestamp(d).tz_localize("UTC")

    def event(zenith_deg, morning):
        cosH = ((math.cos(math.radians(zenith_deg)) - math.sin(latr) * math.sin(decl))
                / (math.cos(latr) * math.cos(decl)))
        if cosH < -1 or cosH > 1:
            return None
        ha = math.degrees(math.acos(cosH))
        tmin = 720 - 4 * (lon + (ha if morning else -ha)) - eqtime
        # Open-Meteo returns naive *local* timestamps, so match that here.
        return (base + pd.Timedelta(minutes=tmin)).tz_convert(TZ).tz_localize(None)

    sr, ss = event(90.833, True), event(90.833, False)
    daylight = (ss - sr) if (sr is not None and ss is not None) else None
    return {"dawn": event(96.0, True), "sunrise": sr, "sunset": ss,
            "dusk": event(96.0, False), "daylight": daylight}


# ---------------------------- meteorology -----------------------------------
def heat_index_f(T, RH):
    """NWS heat index in degF from air temp (degF) and RH (%)."""
    T = np.asarray(T, float); RH = np.asarray(RH, float)
    HI = 0.5 * (T + 61.0 + (T - 68.0) * 1.2 + RH * 0.094)   # simple form
    m = HI >= 80.0
    if np.any(m):
        Tm, Rm = T[m], RH[m]
        full = (-42.379 + 2.04901523 * Tm + 10.14333127 * Rm
                - 0.22475541 * Tm * Rm - 0.00683783 * Tm * Tm
                - 0.05481717 * Rm * Rm + 0.00122874 * Tm * Tm * Rm
                + 0.00085282 * Tm * Rm * Rm - 0.00000199 * Tm * Tm * Rm * Rm)
        a1 = (Rm < 13) & (Tm > 80) & (Tm < 112)
        full = np.where(a1, full - ((13 - Rm) / 4.0)
                        * np.sqrt(np.clip((17 - np.abs(Tm - 95.0)) / 17.0, 0, None)),
                        full)
        a2 = (Rm > 85) & (Tm > 80) & (Tm < 87)
        full = np.where(a2, full + ((Rm - 85.0) / 10.0) * ((87.0 - Tm) / 5.0), full)
        HI[m] = full
    return HI

def wet_bulb_c(Ta_c, RH):
    """Stull (2011) wet-bulb temperature, degC, from air temp (degC) and RH (%)."""
    Ta = np.asarray(Ta_c, float)
    R = np.clip(np.asarray(RH, float), 1.0, 99.0)
    return (Ta * np.arctan(0.151977 * np.sqrt(R + 8.313659))
            + np.arctan(Ta + R) - np.arctan(R - 1.676331)
            + 0.00391838 * R ** 1.5 * np.arctan(0.023101 * R) - 4.686035)

def globe_temp_c(Ta_c, S_wm2, V_ms):
    """Estimated black-globe (full-sun) temperature, degC."""
    Ta = np.asarray(Ta_c, float)
    S = np.clip(np.asarray(S_wm2, float), 0, None)
    V = np.clip(np.asarray(V_ms, float), 0.3, None)   # natural-convection floor
    sigma, alpha, eps = 5.670374419e-8, 0.95, 0.95
    TaK = Ta + 273.15
    h_c = 6.3 * V ** 0.6                  # forced convection, 150 mm sphere
    h_r = 4 * eps * sigma * TaK ** 3       # linearized radiative coefficient
    dT = (alpha * S / 4.0) / (h_c + h_r)   # mean absorbed flux over sphere = aS/4
    return Ta + dT

def severe_level(cape, wcode, gust_ms):
    """0=none .. 4=extreme, from CAPE + thunderstorm codes + gusts."""
    cape = np.asarray(cape, float)
    g = np.asarray(gust_ms, float) * 2.23694
    with np.errstate(invalid="ignore"):
        lvl = np.select(
            [cape >= CAPE_EXTREME, cape >= CAPE_STRONG,
             cape >= CAPE_MOD, cape >= CAPE_MARGINAL],
            [4, 3, 2, 1], 0).astype(float)
    thunder = np.isin(np.asarray(wcode, float), [95, 96, 99])
    lvl = np.where(thunder, np.maximum(lvl, 3), lvl)        # storms -> >= High
    lvl = np.where(g > GUST_HIGH_MPH, np.maximum(lvl, 2), lvl)
    return lvl

def penalty_components(df):
    """Per-hour penalty pieces behind the bikeability score, plus the raw drivers.

    suitability() is defined in terms of this, so any explanation built from these
    components describes exactly the penalties that produced the score."""
    t_c = df["t_c"].values
    t_f = t_c * 9 / 5 + 32
    wbgt, tw = df["wbgt_c"].values, df["tw_c"].values
    hi_c = df["hi_c"].values
    hi_f = hi_c * 9 / 5 + 32
    precip = df["precip"].fillna(0).values
    gms, wms = df["gust_ms"].values, df["wind_ms"].values
    gmph, wmph = gms * 2.23694, wms * 2.23694
    sev, thunder = df["severe"].values, df["thunder"].values

    # Heat penalty: prefer WBGT (needs sun + humidity); fall back to heat index
    # (needs humidity); then to plain air temp. Keeps AI models scored fairly.
    with np.errstate(invalid="ignore"):
        h_wbgt = np.select([wbgt >= WBGT_BLACK, wbgt >= WBGT_RED,
                            wbgt >= WBGT_YELLOW], [70, 45, 20], 0)
        h_hi = np.select([hi_f >= 125, hi_f >= 103, hi_f >= 90, hi_f >= 80],
                         [70, 55, 35, 15], 0)          # NWS heat-index bands (F)
        h_air = np.select([t_f >= 100, t_f >= 95, t_f >= 90, t_f >= 85],
                          [55, 40, 25, 12], 0)
    heat = np.where(~np.isnan(wbgt), h_wbgt,
                    np.where(~np.isnan(hi_f), h_hi, h_air))
    heat = np.maximum(heat, np.where(tw >= WETBULB_DANGER, 70, 0))
    cold = np.select([t_f < 32, t_f < 40, t_f < 50], [40, 20, 5], 0)
    rain = np.select([precip >= RAIN_MM_HEAVY, precip >= RAIN_MM_MOD,
                      precip >= RAIN_MM_LIGHT], [45, 25, 12], 0)
    sevp = np.select([sev >= 4, sev >= 3, sev >= 2, sev >= 1], [80, 55, 30, 10], 0)
    wind = (np.where(gmph > GUST_HIGH_MPH, 25, np.where(gmph > GUST_MOD_MPH, 12, 0))
            + np.where(wmph > WIND_HIGH_MPH, 10, 0))
    return {
        "heat": heat, "cold": cold, "rain": rain, "severe": sevp, "wind": wind,
        "capped": (np.asarray(thunder, bool) | (sev >= 3)),        # storms -> Avoid
        "wbgt_c": wbgt, "tw_c": tw, "hi_c": hi_c, "t_c": t_c,
        "precip": precip, "gust_ms": gms, "wind_ms": wms,
        "sev": sev, "thunder": np.asarray(thunder, bool),
    }

def suitability(df):
    """Composite 0-100 bikeability score for one model's hourly frame."""
    c = penalty_components(df)
    score = np.clip(100 - (c["heat"] + c["cold"] + c["rain"]
                           + c["severe"] + c["wind"]), 0, 100)
    cap = np.where(c["capped"], 25, 100)                    # storms -> Avoid
    return np.minimum(score, cap)

def wbgt_flag(w):
    return ("Black" if w >= WBGT_BLACK else "Red" if w >= WBGT_RED
            else "Yellow" if w >= WBGT_YELLOW else "Green")


def _reason_phrases(c, i):
    """Ranked [(penalty, text, category), ...] for what dragged hour i's score
    down, biggest penalty first. Text uses display units to match the charts."""
    out = []
    # Storms / instability: this is what caps an hour at Avoid.
    if bool(c["thunder"][i]):
        out.append((100.0, "thunderstorms in the forecast", "storm"))
    elif c["severe"][i] > 0:
        names = {4: "extreme instability (very high CAPE)",
                 3: "strong instability (high CAPE)",
                 2: "moderate instability", 1: "marginal instability"}
        out.append((float(c["severe"][i]),
                    names.get(int(c["sev"][i]), "instability"), "storm"))
    # Heat: name whichever metric actually drove the penalty.
    if c["heat"][i] > 0:
        w, tw = c["wbgt_c"][i], c["tw_c"][i]
        if w == w and w >= WBGT_YELLOW:                      # w==w screens NaN
            txt = (f"heat stress \u2014 WBGT in the {wbgt_flag(w)} zone "
                   f"({Td(w):.0f}{TU})")
        elif tw == tw and tw >= WETBULB_DANGER:
            txt = f"dangerous humidity (wet-bulb {Td(tw):.0f}{TU})"
        elif c["hi_c"][i] == c["hi_c"][i]:
            txt = f"heat index {Td(c['hi_c'][i]):.0f}{TU}"
        else:
            txt = f"high temperature ({Td(c['t_c'][i]):.0f}{TU})"
        out.append((float(c["heat"][i]), txt, "heat"))
    # Cold.
    if c["cold"][i] > 0:
        word = ("hard freeze" if c["cold"][i] >= 40
                else "freezing cold" if c["cold"][i] >= 20 else "cold")
        out.append((float(c["cold"][i]), f"{word} ({Td(c['t_c'][i]):.0f}{TU})", "cold"))
    # Rain (qualitative, so it reads the same in either unit system).
    if c["rain"][i] > 0:
        word = ("heavy rain" if c["rain"][i] >= 45
                else "moderate rain" if c["rain"][i] >= 25 else "light rain")
        out.append((float(c["rain"][i]), word, "rain"))
    # Wind: gusts and/or sustained.
    if c["wind"][i] > 0:
        parts = []
        if c["gust_ms"][i] * 2.23694 > GUST_HIGH_MPH:
            parts.append(f"strong gusts ({Wd(c['gust_ms'][i]):.0f}{WU})")
        elif c["gust_ms"][i] * 2.23694 > GUST_MOD_MPH:
            parts.append(f"gusty wind ({Wd(c['gust_ms'][i]):.0f}{WU})")
        if c["wind_ms"][i] * 2.23694 > WIND_HIGH_MPH:
            parts.append("strong sustained wind")
        out.append((float(c["wind"][i]), " and ".join(parts) if parts else "wind", "wind"))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def _low_rating_notes(panel, idx, score_col, comp_fn, phrase_fn):
    """Generic 'why is it rated low' builder shared by cycling and rowing.

    Returns short markdown strings, each covering a contiguous block of hours,
    saying how many models rate it Poor-or-worse and the dominant reason(s). The
    worst-scoring model at each hour supplies the reason (it sets the lowest
    rating shown on the heatmap)."""
    models = list(panel)
    if not models:
        return []
    N = len(models)
    comps = {m: comp_fn(panel[m]) for m in models}
    scores = np.array([panel[m][score_col].values for m in models])   # (N, H)
    cats = np.digitize(scores, RATING_BINS)                           # 0..4
    H = scores.shape[1]

    per_hour = []
    for i in range(H):
        bad = cats[:, i] <= 1                          # Poor (1) or Avoid (0)
        if not bad.any():
            per_hour.append(None); continue
        wm = int(np.argmin(scores[:, i]))              # worst model drives it
        ph = phrase_fn(comps[models[wm]], i)
        pen, txt, cat = ph[0] if ph else (0.0, "poor conditions", "other")
        per_hour.append({"i": i, "n_bad": int(bad.sum()),
                         "worst": RATING_LABELS[int(cats[:, i].min())],
                         "pen": pen, "text": txt, "cat": cat,
                         "sec": [(t, cc) for _, t, cc in ph[1:]]})

    # Merge consecutive hours sharing scope (all vs some), rating and reason kind.
    groups = []
    for h in per_hour:
        if h is None:
            continue
        scope = "all" if h["n_bad"] == N else "some"
        key = (scope, h["worst"], h["cat"])
        if groups and groups[-1]["key"] == key and h["i"] == groups[-1]["j"] + 1:
            g = groups[-1]; g["j"] = h["i"]
            if h["pen"] > g["pen"]:                    # keep the worst hour's text
                g["pen"], g["text"] = h["pen"], h["text"]
            for t, cc in h["sec"]:
                g["sec"].setdefault(cc, t)
        else:
            sec = {}
            for t, cc in h["sec"]:
                sec.setdefault(cc, t)
            groups.append({"key": key, "i": h["i"], "j": h["i"], "scope": scope,
                           "worst": h["worst"], "pen": h["pen"],
                           "text": h["text"], "cat": h["cat"], "sec": sec})

    notes = []
    for g in groups:
        mask = np.zeros(len(idx), bool); mask[g["i"]:g["j"] + 1] = True
        win = window_text(idx, mask)
        who = f"all {N} models" if g["scope"] == "all" else "some models"
        reasons = g["text"]
        extras = [t for cc, t in g["sec"].items() if cc != g["cat"]]
        if extras:
            reasons += "; also " + ", ".join(extras)
        notes.append(f"**{win} \u2014 {g['worst']}** ({who}): {reasons}.")
    return notes


def low_rating_notes(panel, idx):
    """Explain every hour any model rates cycling Poor or worse."""
    return _low_rating_notes(panel, idx, "score", penalty_components, _reason_phrases)


# =========================== rowing (singles) ================================

def _fog_risk(df):
    """(visibility_m array, low_bool, moderate_bool) per hour. Uses the visibility
    field when present, else a dewpoint-depression proxy from temp + humidity."""
    n = len(df)
    vis = df["vis"].values.astype(float) if "vis" in df else np.full(n, np.nan)
    have = ~np.isnan(vis)
    low = have & (vis < ROW_VIS_LOW_M)
    mod = have & (vis >= ROW_VIS_LOW_M) & (vis < ROW_VIS_MOD_M)
    if not have.all():                                 # dewpoint fallback
        t = df["t_c"].values
        rh = np.clip(df["rh"].values.astype(float), 1, 100)
        a, b = 17.625, 243.04
        gamma = np.log(rh / 100.0) + a * t / (b + t)
        dew = b * gamma / (a - gamma)
        dep = t - dew                                  # small spread -> fog likely
        low = low | ((~have) & (dep < 2.0))
        mod = mod | ((~have) & (dep >= 2.0) & (dep < 3.5))
    return vis, low, mod


def row_penalty_components(df, water_c=None):
    """Per-hour penalties behind the rowing score, plus raw drivers. Singles/small
    boats: wind and chop, gusts, fog/visibility, and cold-water immersion risk
    (per-hour air temp combined with the current water temperature)."""
    t_c = df["t_c"].values
    t_f = t_c * 9 / 5 + 32
    wms, gms = df["wind_ms"].values, df["gust_ms"].values
    wmph, gmph = wms * 2.23694, gms * 2.23694
    sev = df["severe"].values
    thunder = np.asarray(df["thunder"].values, bool)
    vis, fog_low, fog_mod = _fog_risk(df)

    wind = np.select([wmph >= ROW_WIND_HIGH_MPH, wmph >= ROW_WIND_MOD_MPH,
                      wmph >= ROW_WHITECAP_MPH], [55, 30, 18], 0)
    gust = np.where(gmph >= ROW_GUST_HIGH_MPH, 35,
                    np.where(gmph >= ROW_GUST_MOD_MPH, 18, 0))
    fog = np.where(fog_low, 60, np.where(fog_mod, 25, 0))
    if water_c is None or water_c != water_c:
        cold = np.zeros(len(df)); summ = np.full(len(df), np.nan)
    else:
        summ = t_f + (water_c * 9 / 5 + 32)
        cold = np.select([summ < ROW_COLDWATER_DANGER_F, water_c < ROW_WATER_COLD_C,
                          summ < ROW_COLDWATER_SUM_F], [70, 55, 30], 0)
    return {"wind": wind, "gust": gust, "fog": fog, "cold": cold,
            "capped": (thunder | (sev >= 3)),
            "wind_ms": wms, "gust_ms": gms, "wmph": wmph, "gmph": gmph,
            "vis": vis, "fog_low": fog_low, "fog_mod": fog_mod,
            "t_c": t_c, "water_c": (np.nan if water_c is None else water_c),
            "sum_f": summ, "sev": sev, "thunder": thunder}


def row_suitability(df, water_c=None):
    """Composite 0-100 rowing score for one model's hourly frame."""
    c = row_penalty_components(df, water_c)
    score = np.clip(100 - (c["wind"] + c["gust"] + c["fog"] + c["cold"]), 0, 100)
    cap = np.where(c["capped"], 25, 100)                    # storms -> Avoid
    return np.minimum(score, cap)


def add_row_scores(panel, water_c=None):
    """Attach a 'row_score' column to each model frame (needs current water temp
    for the cold-water rule; pass None to skip that term)."""
    for m in panel:
        panel[m]["row_score"] = row_suitability(panel[m], water_c)
    return panel


def _row_reason_phrases(c, i):
    """Ranked [(penalty, text, category), ...] for what makes rowing hour i low."""
    out = []
    if bool(c["thunder"][i]):
        out.append((100.0, "thunderstorms \u2014 get off the water", "storm"))
    elif c["sev"][i] > 0:
        names = {4: "extreme instability", 3: "strong instability",
                 2: "moderate instability", 1: "marginal instability"}
        out.append((float(80 if c["sev"][i] >= 4 else 55),
                    names.get(int(c["sev"][i]), "instability"), "storm"))
    if c["cold"][i] > 0:
        w = c["water_c"]
        txt = "cold water" + (f" ({Td(w):.0f}{TU})" if w == w else "")
        out.append((float(c["cold"][i]), txt + " \u2014 immersion risk", "cold"))
    if c["fog"][i] > 0:
        if c["fog_low"][i]:
            v = c["vis"][i]
            txt = "fog / low visibility" + (f" ({v / 1000:.1f} km)" if v == v else "")
        else:
            txt = "reduced visibility"
        out.append((float(c["fog"][i]), txt, "fog"))
    if c["wind"][i] > 0 or c["gust"][i] > 0:
        bits = []
        if c["wmph"][i] >= ROW_WHITECAP_MPH:
            bits.append(f"whitecaps likely (wind {Wd(c['wind_ms'][i]):.0f}{WU})")
        elif c["wind"][i] > 0:
            bits.append(f"choppy (wind {Wd(c['wind_ms'][i]):.0f}{WU})")
        if c["gmph"][i] >= ROW_GUST_MOD_MPH:
            bits.append(f"gusts {Wd(c['gust_ms'][i]):.0f}{WU}")
        out.append((float(max(c["wind"][i], c["gust"][i])),
                    " and ".join(bits) if bits else "wind", "wind"))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def row_low_rating_notes(panel, idx, water_c=None):
    """Explain every hour any model rates rowing Poor or worse."""
    return _low_rating_notes(panel, idx, "row_score",
                             lambda df: row_penalty_components(df, water_c),
                             _row_reason_phrases)


def river_notes(river):
    """(callout_kind, headline, [lines]) summarising live USGS river conditions,
    or None when nothing was returned. Flow/stage/water temperature are 'now',
    not a forecast."""
    if not river or (river.get("flow_cfs") is None and river.get("water_c") is None):
        return None
    kind, lines = "note", []
    flow, stage, w = river.get("flow_cfs"), river.get("stage_ft"), river.get("water_c")
    if flow is not None:
        trend = f", {river['flow_trend']}" if river.get("flow_trend") else ""
        lvl = ("high" if flow >= FLOW_HIGH_CFS else
               "elevated" if flow >= FLOW_ELEVATED_CFS else "normal")
        extra = f"; stage {stage:.1f} ft" if stage is not None else ""
        lines.append(f"Flow **{flow:,.0f} cfs** ({lvl}{trend}){extra}.")
        if flow >= FLOW_HIGH_CFS:
            kind = "important"
            lines.append("High water \u2014 fast current and debris; singles not advised.")
        elif flow >= FLOW_ELEVATED_CFS:
            kind = "warning"
            lines.append("Elevated flow \u2014 stronger current than usual; stay near shore.")
    if w is not None:
        lines.append(f"Water temperature **{Td(w):.0f}{TU}**.")
        if w < ROW_WATER_COLD_C:
            kind = "important"
            lines.append(f"Cold water (<{Td(ROW_WATER_COLD_C):.0f}{TU}) \u2014 dress for "
                         "immersion; a buddy or launch is strongly advised.")
    return kind, "River conditions now", lines


def row_methodology_text():
    return (
        "Rowing view (singles / small boats). Score starts at 100 and subtracts "
        "penalties for wind and chop (sustained \u2265" f"{ROW_WHITECAP_MPH} mph raises "
        f"whitecaps; heavier above {ROW_WIND_MOD_MPH}/{ROW_WIND_HIGH_MPH} mph), gusts "
        f"(\u2265{ROW_GUST_MOD_MPH}/{ROW_GUST_HIGH_MPH} mph), fog / low visibility, and "
        "cold-water immersion risk. Cold water uses the club-style air+water rule: "
        f"below {ROW_COLDWATER_SUM_F}\u00b0F combined dress for immersion, below "
        f"{ROW_COLDWATER_DANGER_F}\u00b0F (or water under {Td(ROW_WATER_COLD_C):.0f}{TU}) "
        "singles are unsafe. Any thunder or strong instability caps the hour at Avoid. "
        "Flow, stage and water temperature are live from the USGS gauge and reflect "
        "current conditions, not a forecast; the flow bands are advisory \u2014 set them "
        "to your club's rules.")


def wind_chill_c(t_c, w_kmh):
    """NWS wind-chill (feels-like cold), degC, from air temp (degC) and wind
    (km/h). Returns NaN where wind chill is not defined (air > 10 degC), falls
    back to the air temp when wind is negligible, and never reads warmer than
    the air."""
    t = np.asarray(t_c, float); v = np.asarray(w_kmh, float)
    vp = np.power(np.clip(v, 0, None), 0.16)
    wc = 13.12 + 0.6215 * t - 11.37 * vp + 0.3965 * t * vp
    wc = np.where(v < 4.8, t, wc)
    return np.where(t <= 10.0, np.minimum(wc, t), np.nan)


def _runs(mask):
    """List of (start_i, end_i) inclusive index spans where mask is True."""
    mask = np.asarray(mask, bool); runs = []; s = None
    for i, v in enumerate(mask):
        if v and s is None:
            s = i
        elif not v and s is not None:
            runs.append((s, i - 1)); s = None
    if s is not None:
        runs.append((s, len(mask) - 1))
    return runs


def window_text(idx, mask):
    """'1p-6p' style label(s) for the True spans of mask (end exclusive)."""
    rr = _runs(mask)
    if not rr:
        return None
    return ", ".join(f"{hlabel(idx[a])}\u2013{hlabel(idx[b] + pd.Timedelta(hours=1))}"
                     for a, b in rr)


def wc_band(wc):
    """(label, color) for a wind-chill value (degC)."""
    for lo, lab, col in WC_BANDS:
        if wc >= lo:
            return (lab, col)
    return WC_BANDS[-1][1:]


# Cyclist-facing guidance, keyed by worst flag/band reached (unit-agnostic).
HEAT_ADVICE = {
    "Yellow": ["Hydrate before and during \u2014 roughly a bottle an hour.",
               "Ease off on climbs; let effort, not pace, set the limit.",
               "Take a shaded breather if the ride runs long."],
    "Red": ["Keep it short and easy \u2014 skip intervals and long climbs.",
            "Ride early morning or after sunset, not midday.",
            "Carry extra water plus electrolytes; douse yourself to cool.",
            "Watch for heat illness (dizziness, nausea, chills, no sweat) and stop if it shows."],
    "Black": ["Best to move the ride indoors or to another day.",
              "If you must go: dawn only, very short and easy, near shade and water."],
}
COLD_ADVICE = {
    "Cool": ["Long sleeves and light gloves \u2014 you'll warm up after a few minutes."],
    "Chilly": ["Thermal base layer, full-finger gloves, and an ear band.",
               "A windproof vest takes the bite off descents."],
    "Cold": ["Windproof jacket and tights with insulated full-finger gloves.",
             "Cover ears and neck; add toe covers or thicker socks.",
             "Descents feel far colder than climbs \u2014 dress for the downhill."],
    "Very cold": ["Cover all exposed skin \u2014 balaclava and clear eye protection.",
                  "Insulated gloves and shoe covers; frostbite is possible on long exposure.",
                  "Keep rides shorter and stay near a warm bailout."],
    "Dangerous": ["Frostbite can set in within ~30 minutes \u2014 better to skip or ride indoors.",
                  "If you do ride, fully cover skin and keep it brief."],
}


def heat_flags(idx, agg):
    """Heat-stress assessment from consensus WBGT. concern=True once the day
    reaches at least the Yellow athletic flag."""
    wb, tw = agg["wb_mean"], agg["tw_mean"]
    out = {"concern": False, "recommendations": []}
    if np.all(np.isnan(wb)):
        return out
    ip = int(np.nanargmax(wb)); peak = float(wb[ip])
    out.update(peak_wbgt_c=peak, peak_hour=idx[ip], peak_flag=wbgt_flag(peak),
               concern=peak >= WBGT_YELLOW)
    out["windows"] = {lvl: window_text(idx, wb >= thr) for lvl, thr in
                      (("Yellow", WBGT_YELLOW), ("Red", WBGT_RED), ("Black", WBGT_BLACK))}
    twmax = float(np.nanmax(tw)) if not np.all(np.isnan(tw)) else float("nan")
    out["wetbulb_max_c"] = twmax
    out["wetbulb_danger"] = twmax == twmax and twmax >= WETBULB_DANGER
    if out["concern"]:
        worst = "Yellow"
        for lvl in ("Red", "Black"):
            if out["windows"].get(lvl):
                worst = lvl
        out["recommendations"] = HEAT_ADVICE[worst]
    return out


def cold_flags(idx, agg):
    """Wind-chill assessment from consensus air temp + wind. concern=True when
    the feels-like drops to COLD_CONCERN_C or below."""
    air = agg["air_mean"]
    wc = wind_chill_c(air, agg["wind_mean"] * 3.6)
    out = {"concern": False, "wc": wc, "recommendations": []}
    if np.all(np.isnan(wc)):
        return out
    im = int(np.nanargmin(wc)); wmin = float(wc[im])
    gap = np.where(np.isnan(wc), -np.inf, air - wc)
    ig = int(np.nanargmax(gap))
    out.update(min_wc_c=wmin, min_hour=idx[im], band=wc_band(wmin)[0],
               max_gap_c=float(air[ig] - wc[ig]) if np.isfinite(wc[ig]) else 0.0,
               gap_hour=idx[ig], concern=wmin <= COLD_CONCERN_C)
    if out["concern"]:
        out["recommendations"] = COLD_ADVICE.get(out["band"], COLD_ADVICE["Cool"])
    return out


# ---------------------------- data plumbing ---------------------------------
def _get(varlist):
    params = {"latitude": LAT, "longitude": LON, "hourly": ",".join(varlist),
              "models": ",".join(MODELS), "timezone": TZ,
              "forecast_days": FORECAST_DAYS, "wind_speed_unit": "ms"}
    r = requests.get(API_URL, params=params, timeout=30)
    j = r.json()
    if isinstance(j, dict) and j.get("error"):
        raise RuntimeError(j.get("reason", "Open-Meteo error"))
    r.raise_for_status()
    return j

def fetch():
    try:
        return _get(BASE_VARS)
    except Exception as e:                      # some models lack CAPE
        print(f"[note] retrying without CAPE ({e})")
        return _get(CORE_VARS)

def _to_local(s):
    """Parse an ISO8601 timestamp to the configured local timezone."""
    if not s:
        return None
    try:
        t = pd.to_datetime(s)
        return t.tz_convert(TZ) if t.tzinfo is not None else t.tz_localize(TZ)
    except Exception:
        return None

def fmt_alert_window(onset, ends):
    o, e = _to_local(onset), _to_local(ends)
    now = pd.Timestamp.now(tz=TZ)
    if e is None:
        return ""
    if o is not None and o > now + pd.Timedelta(minutes=30):
        return f"{hlabel(o)} {o.strftime('%a')} \u2013 {hlabel(e)} {e.strftime('%a')}"
    return f"until {hlabel(e)} {e.strftime('%a')}"

def fetch_alerts():
    """Active NWS alerts for the point. Returns [] on any failure (e.g. non-US)."""
    if not SHOW_ALERTS:
        return []
    try:
        r = requests.get(NWS_ALERTS_URL, params={"point": f"{LAT},{LON}"},
                         headers={"User-Agent": ALERT_UA,
                                  "Accept": "application/geo+json"}, timeout=20)
        r.raise_for_status()
        out = []
        for f in r.json().get("features", []):
            p = f.get("properties", {}) or {}
            out.append({"event": p.get("event", "Weather Alert"),
                        "severity": p.get("severity", "Unknown"),
                        "onset": p.get("onset") or p.get("effective"),
                        "ends": p.get("ends") or p.get("expires")})
        return out
    except Exception as e:
        print(f"[note] NWS alerts unavailable ({e})")
        return []

USGS_IV_URL = "https://waterservices.usgs.gov/nwis/iv/"

def fetch_river(site=None):
    """Latest USGS gauge readings for river conditions: discharge (cfs), gage
    height (ft) and water temperature (degC). Returns a dict; any field may be
    None if the gauge does not report it. Never raises (mirrors fetch_alerts)."""
    site = site or USGS_SITE
    out = {"site": site, "name": None, "flow_cfs": None, "stage_ft": None,
           "water_c": None, "flow_trend": None, "time": None}
    try:
        r = requests.get(USGS_IV_URL, params={
            "format": "json", "sites": site, "period": "P1D",
            "parameterCd": "00060,00065,00010", "siteStatus": "all"},
            headers={"User-Agent": ALERT_UA}, timeout=30)
        r.raise_for_status()
        series = r.json()["value"]["timeSeries"]
    except Exception as e:
        print(f"[note] USGS river data unavailable ({e})")
        if WATER_TEMP_C is not None:
            out["water_c"] = float(WATER_TEMP_C)
        return out
    by_code = {}
    for ts in series:
        try:
            code = ts["variable"]["variableCode"][0]["value"]
            pts = ts["values"][0]["value"]
            vals = [(p["dateTime"], float(p["value"])) for p in pts
                    if p.get("value") not in (None, "", "-999999", "-999999.0")]
            if vals:
                by_code[code] = vals
                out["name"] = ts["sourceInfo"]["siteName"]
        except Exception:
            continue
    if "00060" in by_code:
        s = by_code["00060"]; out["flow_cfs"], out["time"] = s[-1][1], s[-1][0]
        if len(s) >= 2:
            out["flow_trend"] = ("rising" if s[-1][1] > s[-2][1] * 1.02
                                 else "falling" if s[-1][1] < s[-2][1] * 0.98
                                 else "steady")
    if "00065" in by_code:
        out["stage_ft"] = by_code["00065"][-1][1]
    if "00010" in by_code:
        out["water_c"] = by_code["00010"][-1][1]
    elif WATER_TEMP_C is not None:
        out["water_c"] = float(WATER_TEMP_C)
    return out

def to_panel(js):
    """dict[model_id] -> enriched hourly DataFrame (models with no data dropped)."""
    h = js["hourly"]
    idx = pd.to_datetime(h["time"])
    rename = {"temperature_2m": "t_c", "relative_humidity_2m": "rh",
              "precipitation": "precip", "precipitation_probability": "pop",
              "weather_code": "wcode", "cloud_cover": "cloud",
              "wind_speed_10m": "wind_ms", "wind_gusts_10m": "gust_ms",
              "wind_direction_10m": "wdir",
              "shortwave_radiation": "swr", "cape": "cape", "uv_index": "uv",
              "visibility": "vis"}
    panel = {}
    for mid in MODELS:
        df = pd.DataFrame(index=idx)
        for v in BASE_VARS:
            key = f"{v}_{mid}"
            if key in h and h[key] is not None:
                df[rename[v]] = pd.to_numeric(pd.Series(h[key], index=idx),
                                              errors="coerce")
            else:
                df[rename[v]] = np.nan
        if df["t_c"].notna().any():
            panel[mid] = enrich(df)
    return panel

def enrich(df):
    df["hi_c"] = (heat_index_f(df["t_c"] * 9 / 5 + 32, df["rh"]) - 32) * 5 / 9
    df["tw_c"] = wet_bulb_c(df["t_c"], df["rh"])
    df["tg_c"] = globe_temp_c(df["t_c"], df["swr"], df["wind_ms"])
    df["wbgt_c"] = 0.7 * df["tw_c"] + 0.2 * df["tg_c"] + 0.1 * df["t_c"]
    df["severe"] = severe_level(df["cape"], df["wcode"], df["gust_ms"])
    df["thunder"] = pd.Series(df["wcode"], index=df.index).isin([95, 96, 99])
    df["score"] = suitability(df)
    return df

def stack(panel, col):
    return np.vstack([panel[m][col].values.astype(float) for m in panel])

def nanmean0(a):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        allnan = np.all(np.isnan(a), axis=0)
        out = np.where(allnan, np.nan, np.nanmean(a, axis=0))
    return out


# ---------------------------- narrative -------------------------------------
def runs(mask):
    mask = np.asarray(mask, bool); out = []; i = 0; n = len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j + 1 < n and mask[j + 1]:
                j += 1
            out.append((i, j)); i = j + 1
        else:
            i += 1
    return out

def fmt_runs(mask, idx):
    parts = []
    for i, j in runs(mask):
        end = idx[j] + pd.Timedelta(hours=1)
        parts.append(f"{hlabel(idx[i])}\u2013{hlabel(end)}")
    return ", ".join(parts) if parts else "none"

def compass(deg):
    if deg is None or (isinstance(deg, float) and np.isnan(deg)):
        return "?"
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg + 22.5) // 45) % 8]

def in_preferred(hour):
    if PREFERRED_HOURS is None:
        return False
    a, b = PREFERRED_HOURS
    return (a <= hour < b) if a <= b else (hour >= a or hour < b)

def daylight_penalty(idx, sun):
    """Per-hour soft penalty for riding in darkness. Small if HAVE_LIGHT, so
    weather dominates the best-window pick; larger otherwise. Hours inside
    PREFERRED_HOURS get no darkness penalty (you ride then by choice)."""
    night = 6 if HAVE_LIGHT else 30
    twi = 2 if HAVE_LIGHT else 12
    pen = np.zeros(len(idx))
    dawn, sr, ss, dusk = sun["dawn"], sun["sunrise"], sun["sunset"], sun["dusk"]
    if sr is None or ss is None or dawn is None or dusk is None:
        return pen
    for k, ts in enumerate(idx):
        if in_preferred(ts.hour):                  # habitual ride time -> no ding
            continue
        c = ts + pd.Timedelta(minutes=30)          # hour centre
        if c < dawn or c > dusk:
            pen[k] = night
        elif c < sr or c > ss:
            pen[k] = twi
    return pen

def best_ride_window(sc_mean, idx, sun, agg):
    """Pick the best contiguous block to ride, balancing the weather score with
    a (light-aware) daylight nudge. Returns ((i, j), description)."""
    n = len(idx)
    adj = sc_mean - daylight_penalty(idx, sun)

    def best_run(thr):
        best, i = None, 0
        while i < n:
            if adj[i] >= thr:
                j = i
                while j + 1 < n and adj[j + 1] >= thr:
                    j += 1
                key = (adj[i:j + 1].mean(), j - i + 1)
                if best is None or key > best[0]:
                    best = (key, (i, j))
                i = j + 1
            else:
                i += 1
        return best[1] if best else None

    marginal = False
    win = best_run(65) or best_run(50)
    if win is None:                                 # no good block all day
        L = min(2, n)
        sums = [adj[k:k + L].mean() for k in range(n - L + 1)]
        k = int(np.argmax(sums)); win = (k, k + L - 1); marginal = True

    wi, wj = win
    span = wj - wi + 1
    if span > RIDE_HOURS:                            # narrow to the best block
        L = RIDE_HOURS
        bk, bs = wi, -1e9
        for k in range(wi, wj - L + 2):
            s = adj[k:k + L].mean()
            if s > bs:
                bs, bk = s, k
        wi, wj = bk, bk + L - 1

    start, end = idx[wi], idx[wj] + pd.Timedelta(hours=1)
    lo = Td(np.nanmin(agg["air_mean"][wi:wj + 1]))
    hot = Td(np.nanmax(agg["hi_mean"][wi:wj + 1]))
    bits = [f"{lo:.0f}\u2013{hot:.0f}{TU}"]
    if not np.all(np.isnan(agg["gust_mean"][wi:wj + 1])):
        gd = float(np.nanmax(agg["gust_mean"][wi:wj + 1]))
        wd = compass(agg["prevailing_dir"]) if agg.get("prevailing_dir") is not None else None
        bits.append(f"wind to {Wd(gd):.0f} {WU}" + (f" from {wd}" if wd else ""))
    uvw = agg["uv_mean"][wi:wj + 1]
    if not np.all(np.isnan(uvw)):
        bits.append(f"UV \u2264{np.nanmax(uvw):.0f}")
    txt = (f"\u2605 Best ride window: {hlabel(start)}\u2013{hlabel(end)} "
           f"({wj - wi + 1}h) \u2014 " + ", ".join(bits) + ".")
    if marginal:
        txt += " Best available \u2014 conditions are marginal all day."
    if daylight_penalty(idx, sun)[wi:wj + 1].max() > 0:
        txt += " Partly after dark; your light covers it."
    return (wi, wj), txt

def summarize(panel, idx, agg, sun):
    n = len(idx)
    sc = stack(panel, "score"); sc_mean = sc.mean(0)
    bullets = []
    win, win_txt = best_ride_window(sc_mean, idx, sun, agg)
    bullets.append(win_txt)

    if sun["sunrise"] is not None and sun["sunset"] is not None:
        dl = sun["daylight"]
        dl_txt = f" ({int(dl.total_seconds()//3600)}h{int(dl.total_seconds()%3600//60):02d}m daylight)" if dl is not None else ""
        bullets.append(f"Daylight: sunrise {hm(sun['sunrise'])}, sunset "
                       f"{hm(sun['sunset'])}{dl_txt}; civil dawn {hm(sun['dawn'])}, "
                       f"dusk {hm(sun['dusk'])} (lights outside that).")

    best = fmt_runs(sc_mean >= 65, idx)
    bullets.append(f"Best windows (consensus Good or better): {best}.")

    avoid = fmt_runs(sc_mean < 35, idx)
    bullets.append(f"Avoid (storms / extreme heat): {avoid}.")

    ih = int(np.nanargmax(agg["hi_mean"]))
    bullets.append(f"Peak heat ~{hlabel(idx[ih])}: heat index "
                   f"{Td(agg['hi_mean'][ih]):.0f}{TU}, "
                   f"WBGT {Td(agg['wb_mean'][ih]):.0f}{TU} "
                   f"({wbgt_flag(agg['wb_mean'][ih])} flag).")

    iw = int(np.nanargmax(agg["tw_mean"]))
    bullets.append(f"Max wet-bulb {Td(agg['tw_mean'][iw]):.0f}{TU} ~{hlabel(idx[iw])} "
                   f"(>{Td(WETBULB_DANGER):.0f}{TU} is dangerous for hard efforts).")

    gap = agg["tg_mean"] - agg["air_mean"]
    ig = int(np.nanargmax(gap))
    bullets.append(f"Sun vs shade: up to +{dTd(gap[ig]):.0f}{TU} in full sun "
                   f"~{hlabel(idx[ig])}; moving on the bike trims this (airflow).")

    uvm = agg["uv_mean"]
    if not np.all(np.isnan(uvm)):
        iu = int(np.nanargmax(uvm))
        bullets.append(f"Peak UV {uvm[iu]:.0f} ({uv_band(uvm[iu])[0]}) ~{hlabel(idx[iu])} "
                       f"\u2014 sunscreen / sunglasses on long midday rides.")

    rain_mask = (agg["agree"] >= 40) | (agg["pr_mean"] >= 0.2)
    bullets.append(f"Rain likely: {fmt_runs(rain_mask, idx)} "
                   f"(peak model agreement {agg['agree'].max():.0f}%).")

    if not np.all(np.isnan(agg["gust_mean"])):
        ig2 = int(np.nanargmax(agg["gust_mean"]))
        wtxt = (f"Windiest ~{hlabel(idx[ig2])}: {Wd(agg['wind_mean'][ig2]):.0f} {WU} "
                f"sustained, gusts {Wd(agg['gust_mean'][ig2]):.0f} {WU}")
        if agg.get("prevailing_dir") is not None:
            wtxt += f", prevailing from the {compass(agg['prevailing_dir'])}"
        bullets.append(wtxt + ".")

    thunder_any = np.zeros(n, bool); sev_max = np.zeros(n)
    for m in panel:
        thunder_any |= panel[m]["thunder"].values
        sev_max = np.maximum(sev_max, panel[m]["severe"].values)
    sev_mask = thunder_any | (sev_max >= 3)
    bullets.append(f"Severe/thunder risk flagged: {fmt_runs(sev_mask, idx)}.")

    rng = sc.max(0) - sc.min(0)
    isp = int(np.argmax(rng))
    lo = RATING_LABELS[int(np.digitize(sc.min(0)[isp], RATING_BINS))]
    hi = RATING_LABELS[int(np.digitize(sc.max(0)[isp], RATING_BINS))]
    bullets.append(f"Models disagree most ~{hlabel(idx[isp])} "
                   f"(ratings span {lo} to {hi}) \u2014 treat that hour as uncertain.")
    return bullets, win


# ---------------------------- plotting --------------------------------------
def setup_style():
    plt.rcParams.update({"font.size": 9, "axes.titlesize": 11,
                         "axes.titleweight": "bold", "figure.dpi": 110,
                         "axes.grid": True, "grid.alpha": 0.25})

def model_color(i):
    return MODEL_PALETTE[i % 10]

def day_label(d):
    today = pd.Timestamp.now(tz=TZ).date()
    delta = (d - today).days
    name = {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get(delta)
    pretty = pd.Timestamp(d).strftime("%A, %B ") + str(d.day) + pd.Timestamp(d).strftime(", %Y")
    return f"{name} \u00b7 {pretty}" if name else pretty

def alert_overlaps_day(a, d):
    """True if an alert's active window intersects calendar day d (local)."""
    o, e = _to_local(a.get("onset")), _to_local(a.get("ends"))
    start = pd.Timestamp(d, tz=TZ)
    end = start + pd.Timedelta(days=1)
    if o is None and e is None:
        return True
    o = o or start
    e = e or end
    return (o < end) and (e > start)

def shade_daylight(ax, sun, idx):
    """Shade night (dark) and civil-twilight (light) spans on a time-axis chart."""
    x0, x1 = idx[0], idx[-1] + pd.Timedelta(hours=1)
    dawn, sr, ss, dusk = sun["dawn"], sun["sunrise"], sun["sunset"], sun["dusk"]
    night, twi = "#1a1a2e", "#7a7a99"
    spans_night, spans_twi = [], []
    if dawn is not None:
        spans_night.append((x0, dawn)); spans_twi.append((dawn, sr))
    if dusk is not None:
        spans_twi.append((ss, dusk)); spans_night.append((dusk, x1))
    for a, b in spans_night:
        ax.axvspan(max(a, x0), min(b, x1), color=night, alpha=0.10, lw=0, zorder=0)
    for a, b in spans_twi:
        ax.axvspan(max(a, x0), min(b, x1), color=twi, alpha=0.12, lw=0, zorder=0)

def draw_alerts(fig, alerts, max_show=3):
    """Draw NWS alert bands across the very top of the figure. Returns the
    figure-fraction y below which the rest of the page should begin."""
    if not alerts:
        return 0.985
    rank = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3, "Unknown": 4}
    colors = {"Extreme": "#6a1b1a", "Severe": "#b2182b", "Moderate": "#e8590c",
              "Minor": "#d9a000", "Unknown": "#666666"}
    ordered = sorted(alerts, key=lambda a: rank.get(a["severity"], 5))
    shown = ordered[:max_show]
    y, band, gap = 0.988, 0.034, 0.006
    for a in shown:
        col = colors.get(a["severity"], "#666666")
        ax = fig.add_axes([0.03, y - band, 0.94, band]); ax.axis("off")
        ax.add_patch(plt.Rectangle((0, 0), 1, 1, transform=ax.transAxes,
                                   facecolor=col, edgecolor="none"))
        ax.text(0.012, 0.5, f"\u26a0  {a['event']}", transform=ax.transAxes,
                color="white", fontsize=10.5, fontweight="bold", va="center")
        win = fmt_alert_window(a["onset"], a["ends"])
        if win:
            ax.text(0.988, 0.5, win, transform=ax.transAxes, color="white",
                    fontsize=8.5, va="center", ha="right")
        y -= (band + gap)
    extra = len(ordered) - len(shown)
    if extra > 0:
        fig.text(0.5, y, f"+{extra} more active alert(s) \u2014 see weather.gov",
                 ha="center", va="top", fontsize=7.5, color="#b2182b")
        y -= 0.018
    return y - 0.004

def draw_heatmap(ax, panel, idx, agg, sun, win=None, legend_anchor=(0.5, -0.35),
                 score_col="score", title="Hourly bikeability \u2014 each model vs the consensus"):
    """Draw the per-model-vs-consensus suitability heatmap onto an axis.
    Shared by the PDF overview page, the report figure, and the rowing view."""
    sc = stack(panel, score_col)
    rows = list(panel.keys())
    mat = np.vstack([sc, sc.mean(0)])
    cats = np.digitize(mat, RATING_BINS)
    cmap = ListedColormap(RATING_COLORS)
    norm = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5, 4.5], cmap.N)
    ax.imshow(cats, aspect="auto", cmap=cmap, norm=norm, interpolation="nearest")
    ylabels = [MODELS[m] for m in rows] + ["CONSENSUS"]
    ax.set_yticks(range(len(ylabels))); ax.set_yticklabels(ylabels, fontsize=8)
    ax.get_yticklabels()[-1].set_fontweight("bold")
    ax.axhline(len(rows) - 0.5, color="white", lw=3)
    ax.set_xticks(range(0, len(idx), 2))
    ax.set_xticklabels([hlabel(idx[i]) for i in range(0, len(idx), 2)], fontsize=8)
    ax.set_title(title, loc="left")
    ax.tick_params(length=0)
    for t in (sun["sunrise"], sun["sunset"]):
        if t is not None:
            xp = (t - idx[0]).total_seconds() / 3600.0
            if -0.5 <= xp <= len(idx) - 0.5:
                ax.axvline(xp, color="white", lw=1.4, ls=(0, (4, 3)), alpha=0.9)
    if win is not None:
        wi, wj = win; crow = len(rows)
        ax.add_patch(plt.Rectangle((wi - 0.5, crow - 0.5), wj - wi + 1, 1,
                                   fill=False, edgecolor="#111", lw=2.6, zorder=5))
        ax.text((wi + wj) / 2.0, crow, "\u2605", ha="center", va="center",
                fontsize=10, color="#111", zorder=6)
    legend = [Patch(facecolor=RATING_COLORS[i], label=RATING_LABELS[i]) for i in range(5)]
    ax.legend(handles=legend, ncol=5, fontsize=8, loc="upper center",
              bbox_to_anchor=legend_anchor, frameon=False)

def fig_heatmap(panel, idx, agg, sun, win=None, score_col="score",
                title="Hourly bikeability \u2014 each model vs the consensus"):
    """Standalone suitability heatmap figure (cycling by default; pass
    score_col='row_score' with a rowing title for the rowing view)."""
    rows = list(panel.keys())
    fig, ax = plt.subplots(figsize=(11, max(3.2, 0.34 * (len(rows) + 1) + 1.4)))
    fig.subplots_adjust(left=0.12, right=0.97, bottom=0.2, top=0.86)
    draw_heatmap(ax, panel, idx, agg, sun, win, legend_anchor=(0.5, -0.22),
                 score_col=score_col, title=title)
    return fig

def consensus_rows(idx, agg):
    """Header + 3-hourly consensus table rows + per-row rating index.
    Shared by the PDF table and the report's HTML table."""
    sel = list(range(0, len(idx), 3))
    head = ["Hr", "Air", "HI", "WBlb", "Sun", "UV", "Rn%", "CAPE", "Rating"]
    cells, rate_idx = [], []
    for i in sel:
        rate_i = int(np.digitize(agg["sc_mean"][i], RATING_BINS))
        cape_v = agg["cape_mean"][i]; uv_v = agg["uv_mean"][i]
        cells.append([
            hlabel(idx[i]), f"{Td(agg['air_mean'][i]):.0f}",
            f"{Td(agg['hi_mean'][i]):.0f}", f"{Td(agg['tw_mean'][i]):.0f}",
            f"{Td(agg['tg_mean'][i]):.0f}",
            "-" if np.isnan(uv_v) else f"{uv_v:.0f}",
            f"{agg['agree'][i]:.0f}",
            "-" if np.isnan(cape_v) else f"{cape_v:.0f}",
            RATING_LABELS[rate_i],
        ])
        rate_idx.append(rate_i)
    return head, cells, rate_idx

def fig_table(idx, agg):
    """Standalone colour-coded consensus table as a figure (used by the PDF
    build of the report, where raw HTML tables don't render)."""
    head, cells, rate_idx = consensus_rows(idx, agg)
    fig, ax = plt.subplots(figsize=(8.5, 0.34 * (len(cells) + 1) + 0.3))
    ax.axis("off")
    tab = ax.table(cellText=cells, colLabels=head, loc="center", cellLoc="center")
    tab.auto_set_font_size(False); tab.set_fontsize(9); tab.scale(1, 1.35)
    for c in range(len(head)):
        tab[(0, c)].set_facecolor("#333"); tab[(0, c)].set_text_props(color="white")
    for r, ci in enumerate(rate_idx, start=1):
        tab[(r, len(head) - 1)].set_facecolor(RATING_COLORS[ci])
    fig.subplots_adjust(left=0.02, right=0.98, top=0.98, bottom=0.02)
    return fig

def methodology_text():
    """Plain-text methodology paragraph (no leading label), shared by PDF + report."""
    return (
        "Air = 2 m temperature (shade).  Heat index: NWS Rothfusz.  "
        "Wet-bulb: Stull (2011).  Full-sun: black-globe energy-balance estimate from "
        "shortwave radiation + wind (a moving cyclist sits below it).  "
        "WBGT = 0.7\u00b7Twb + 0.2\u00b7Tglobe + 0.1\u00b7Tair.  When the day reaches the "
        "Yellow athletic flag a dedicated heat-stress panel appears with the flag "
        "windows (Yellow \u2265" f"{WBGT_YELLOW:.0f}, Red \u2265{WBGT_RED:.0f}, Black \u2265{WBGT_BLACK:.0f}\u00b0C) "
        "and ride guidance.  Wind chill (NWS formula) is a feels-like cold value "
        "for air \u226410\u00b0C; when it drops to about "
        f"{COLD_CONCERN_C:.0f}\u00b0C or below a wind-chill panel appears with frostbite-risk "
        "bands and cold-weather guidance.  "
        "Rain% = share of models with \u2265" f"{RAIN_MM_LIGHT} mm that hour (per-model POP is GFS-only).  "
        "Severe = CAPE + WMO thunder codes + gusts.  Bikeability = additive penalties for "
        "heat, rain, severe weather, wind and cold; thunderstorm or extreme-instability hours "
        "are capped at Avoid.  AI models (AIFS, GraphCast) supply temp/precip/wind but not "
        "CAPE or radiation, so their severe/sun cells may be blank and heat falls back to heat "
        "index.  UV index is the WHO scale (Low<3, Moderate, High, Very High, Extreme>10).  "
        "Wind penalties: sustained >" f"{WIND_HIGH_MPH} mph, gusts >{GUST_MOD_MPH}/{GUST_HIGH_MPH} mph; "
        "the wind panel shows actual speeds so you can judge effort.  "
        "\u2605 Best ride window = best contiguous block of consensus score, with a "
        f"{'small (you have a light)' if HAVE_LIGHT else 'large (no light set)'} after-dark nudge"
        + (f" (waived {PREFERRED_HOURS[0]}:00\u2013{PREFERRED_HOURS[1]}:00, your usual ride time)" if PREFERRED_HOURS else "")
        + ".  "
        "Sun/twilight times from the NOAA solar algorithm (civil twilight = sun 6\u00b0 "
        "below horizon).  Estimates for planning \u2014 check weather.gov for official alerts.")

def methodology_src():
    return ("Data: Open-Meteo (open-meteo.com), CC BY 4.0; alerts from NWS api.weather.gov.  "
            "Models: " + ", ".join(MODELS.values()))

def page_overview(pdf, panel, idx, agg, bullets, date_str, alerts, sun, win=None):
    rows = list(panel.keys())
    fig = plt.figure(figsize=(11, 8.5))
    top = draw_alerts(fig, alerts)             # banner across the very top
    title_y = top - 0.010
    sub_y = title_y - 0.038
    sun_y = sub_y - 0.026
    gs_top = sun_y - 0.050
    gs = fig.add_gridspec(3, 2, height_ratios=[0.82, 1.55, 0.63],
                          hspace=0.55, wspace=0.12,
                          left=0.07, right=0.97, top=gs_top, bottom=0.06)
    fig.suptitle(f"Biking suitability by hour \u2014 {PLACE}",
                 fontsize=16, y=title_y)
    fig.text(0.5, sub_y, f"{date_str}   \u00b7   "
             f"{len(rows)} weather models   \u00b7   units: {TU}, {WU}",
             ha="center", fontsize=10, color="#444")
    if sun["sunrise"] is not None and sun["sunset"] is not None:
        fig.text(0.5, sun_y,
                 f"\u2600 sunrise {hm(sun['sunrise'])}  \u00b7  sunset {hm(sun['sunset'])}"
                 f"   |   civil dawn {hm(sun['dawn'])}  \u00b7  dusk {hm(sun['dusk'])}"
                 f"  (use lights outside this)",
                 ha="center", fontsize=8.5, color="#7a6a00")

    # --- suitability heatmap (models x hours) ---
    axh = fig.add_subplot(gs[0, :])
    draw_heatmap(axh, panel, idx, agg, sun, win, legend_anchor=(0.5, -0.35))

    # --- narrative bullets ---
    axt = fig.add_subplot(gs[1, 0]); axt.axis("off")
    axt.set_title("What the models say", loc="left")
    axt.text(0, 0.99, "\n".join("\u2022 " + b for b in bullets),
             va="top", ha="left", fontsize=7.3, wrap=True, linespacing=1.5,
             transform=axt.transAxes)

    # --- compact summary table (every 3 h) ---
    axtab = fig.add_subplot(gs[1, 1]); axtab.axis("off")
    axtab.set_title("Consensus by hour", loc="left")
    head, cells, cellcol = consensus_rows(idx, agg)
    tab = axtab.table(cellText=cells, colLabels=head, loc="center",
                      cellLoc="center")
    tab.auto_set_font_size(False); tab.set_fontsize(7.0); tab.scale(1, 1.25)
    for c in range(len(head)):
        tab[(0, c)].set_facecolor("#333"); tab[(0, c)].set_text_props(color="white")
    for r, ci in enumerate(cellcol, start=1):
        tab[(r, len(head) - 1)].set_facecolor(RATING_COLORS[ci])

    # --- methodology footer ---
    axm = fig.add_subplot(gs[2, :]); axm.axis("off")
    method = (textwrap.fill("Method  \u2014  " + methodology_text(), width=178) + "\n"
              + textwrap.fill(methodology_src(), width=178))
    axm.text(0, 0.95, method, va="top", ha="left", fontsize=6.6,
             color="#555", transform=axm.transAxes)
    pdf.savefig(fig); plt.close(fig)

def fig_thermal(panel, idx, agg, date_str, sun):
    fig = plt.figure(figsize=(11, 8.5))
    gs = fig.add_gridspec(3, 1, height_ratios=[1.0, 1.0, 0.55], hspace=0.32,
                          left=0.08, right=0.95, top=0.91, bottom=0.07)
    ax1, ax2, ax3 = (fig.add_subplot(gs[0]), fig.add_subplot(gs[1]),
                     fig.add_subplot(gs[2]))
    fig.suptitle(f"Temperature, heat stress & sun \u2014 {PLACE}",
                 fontsize=15, y=0.965)
    fig.text(0.5, 0.93, date_str, ha="center", fontsize=9, color="#444")

    # air temperature, one line per model -> shows model spread
    shade_daylight(ax1, sun, idx)
    for i, m in enumerate(panel):
        ax1.plot(idx, Td(panel[m]["t_c"].values), color=model_color(i),
                 lw=1.4, alpha=0.85, label=MODELS[m])
    ax1.plot(idx, Td(agg["air_mean"]), color="black", lw=2.6, label="Mean")
    ax1.set_ylabel(f"Air temp ({TU})")
    ax1.set_title("Air temperature (shade) \u2014 each model", loc="left")
    ax1.legend(fontsize=7, ncol=5, loc="upper left", framealpha=0.9)

    # derived heat metrics (multi-model means) + sun/shade band
    shade_daylight(ax2, sun, idx)
    ax2.fill_between(idx, Td(agg["air_mean"]), Td(agg["tg_mean"]),
                     color="#f4a300", alpha=0.18, label="Sun-vs-shade gap")
    ax2.plot(idx, Td(agg["tg_mean"]), color="#e8590c", lw=2, label="Full-sun (est.)")
    ax2.plot(idx, Td(agg["hi_mean"]), color="#c92a2a", lw=2, ls="--", label="Heat index")
    ax2.plot(idx, Td(agg["air_mean"]), color="black", lw=2, label="Air (shade)")
    ax2.plot(idx, Td(agg["wb_mean"]), color="#1c7ed6", lw=2, label="WBGT")
    ax2.plot(idx, Td(agg["tw_mean"]), color="#0c8599", lw=2, ls=":", label="Wet-bulb")
    ax2.axhline(Td(WETBULB_DANGER), color="#0c8599", lw=0.8, ls=":", alpha=0.6)
    ax2.set_ylabel(f"Temperature ({TU})")
    ax2.set_title("Feels-like & heat-stress metrics (model mean)", loc="left")
    ax2.legend(fontsize=7, ncol=3, loc="upper left", framealpha=0.9)

    # UV index with WHO category bands
    shade_daylight(ax3, sun, idx)
    uv = agg["uv_mean"]
    prev = 0.0
    for hi, lab, col in UV_BANDS:
        ax3.axhspan(prev, min(hi, 13), color=col, alpha=0.16, lw=0)
        prev = hi
    if not np.all(np.isnan(uv)):
        ax3.plot(idx, uv, color="#222", lw=2.2)
        iu = int(np.nanargmax(uv))
        ax3.annotate(f"max {uv[iu]:.0f} ({uv_band(uv[iu])[0]})", (idx[iu], uv[iu]),
                     textcoords="offset points", xytext=(0, 6), fontsize=7.5,
                     ha="center", color="#222")
        ax3.set_ylim(0, max(2, np.nanmax(uv) * 1.25))
    else:
        ax3.text(0.5, 0.5, "UV index unavailable", transform=ax3.transAxes,
                 ha="center", va="center", color="#999")
    ax3.set_ylabel("UV index")
    ax3.set_title("UV index (model mean)", loc="left")

    for ax in (ax1, ax2, ax3):
        ax.set_xlim(idx[0], idx[-1])
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(_hax))
    ax3.set_xlabel(f"Hour (local, {TZ})")
    return fig

def page_thermal(pdf, panel, idx, agg, date_str, sun):
    fig = fig_thermal(panel, idx, agg, date_str, sun)
    pdf.savefig(fig); plt.close(fig)

def fig_wbgt(panel, idx, agg, date_str, sun):
    """Focused heat-stress chart: consensus WBGT against the athletic flag
    bands (Green/Yellow/Red/Black), with wet-bulb for context."""
    wb, tw = agg["wb_mean"], agg["tw_mean"]
    fig, ax = plt.subplots(figsize=(11, 3.9))
    fig.subplots_adjust(left=0.08, right=0.85, top=0.84, bottom=0.17)
    fig.suptitle(f"Heat stress \u2014 WBGT vs athletic flags \u2014 {PLACE}",
                 fontsize=13, x=0.08, ha="left", y=0.98)
    fig.text(0.08, 0.9, date_str, ha="left", fontsize=8.5, color="#555")
    shade_daylight(ax, sun, idx)
    lo = min(WBGT_YELLOW - 4, np.nanmin(wb) - 1)
    hi = max(WBGT_BLACK + 2, np.nanmax(wb) + 1)
    for a, b, col, lab in ((lo, WBGT_YELLOW, "#2f9e44", "Green"),
                           (WBGT_YELLOW, WBGT_RED, "#f59f00", "Yellow"),
                           (WBGT_RED, WBGT_BLACK, "#e8590c", "Red"),
                           (WBGT_BLACK, hi, "#495057", "Black")):
        if b > a:
            ax.axhspan(Td(a), Td(b), color=col, alpha=0.14, lw=0)
            ax.text(1.012, Td((a + b) / 2.0), lab, transform=ax.get_yaxis_transform(),
                    va="center", ha="left", fontsize=8, color=col, fontweight="bold")
    ax.plot(idx, Td(wb), color="#1d3557", lw=2.8, label="WBGT (consensus)")
    if not np.all(np.isnan(tw)):
        ax.plot(idx, Td(tw), color="#0c8599", lw=1.5, ls=":", label="Wet-bulb")
        ax.axhline(Td(WETBULB_DANGER), color="#0c8599", lw=0.9, ls=":", alpha=0.7)
    ip = int(np.nanargmax(wb))
    ax.annotate(f"peak {Td(wb[ip]):.0f}{TU} ({wbgt_flag(wb[ip])})",
                (idx[ip], Td(wb[ip])), textcoords="offset points", xytext=(0, 7),
                ha="center", fontsize=8.5, fontweight="bold", color="#1d3557")
    ax.set_ylim(Td(lo), Td(hi)); ax.set_ylabel(f"WBGT ({TU})")
    ax.legend(fontsize=7.5, loc="upper left", framealpha=0.9)
    ax.set_xlim(idx[0], idx[-1])
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(_hax))
    ax.set_xlabel(f"Hour (local, {TZ})")
    return fig

def fig_windchill(panel, idx, agg, date_str, sun):
    """Focused wind-chill chart: consensus air temp vs feels-like wind chill,
    with frostbite-risk bands and the wind-driven drop shaded."""
    air = agg["air_mean"]
    wc = wind_chill_c(air, agg["wind_mean"] * 3.6)
    fig, ax = plt.subplots(figsize=(11, 3.9))
    fig.subplots_adjust(left=0.08, right=0.85, top=0.84, bottom=0.17)
    fig.suptitle(f"Wind chill \u2014 feels-like cold vs frostbite risk \u2014 {PLACE}",
                 fontsize=13, x=0.08, ha="left", y=0.98)
    fig.text(0.08, 0.9, date_str, ha="left", fontsize=8.5, color="#555")
    shade_daylight(ax, sun, idx)
    fin = wc[np.isfinite(wc)]
    lo = min((fin.min() - 2) if fin.size else 0.0, 0.0)
    hi = max(np.nanmax(air) + 1, 12.0)
    prev_hi = hi
    for lo_b, lab, col in WC_BANDS:
        seg_hi, seg_lo = prev_hi, max(lo_b, lo)
        if seg_hi > seg_lo:
            ax.axhspan(Td(seg_lo), Td(seg_hi), color=col, alpha=0.13, lw=0)
            ax.text(1.012, Td((seg_lo + seg_hi) / 2.0), lab,
                    transform=ax.get_yaxis_transform(), va="center", ha="left",
                    fontsize=8, color=col, fontweight="bold")
        prev_hi = lo_b
    ax.plot(idx, Td(air), color="#495057", lw=1.8, label="Air (shade)")
    ax.fill_between(idx, Td(wc), Td(air), where=~np.isnan(wc), color="#4263eb",
                    alpha=0.16, label="Wind-chill drop")
    ax.plot(idx, Td(wc), color="#1d3557", lw=2.8, label="Wind chill (feels like)")
    if np.isfinite(wc).any():
        im = int(np.nanargmin(wc))
        ax.annotate(f"low {Td(wc[im]):.0f}{TU} ({wc_band(wc[im])[0]})",
                    (idx[im], Td(wc[im])), textcoords="offset points",
                    xytext=(0, -13), ha="center", fontsize=8.5, fontweight="bold",
                    color="#1d3557")
    ax.set_ylim(Td(lo), Td(hi)); ax.set_ylabel(f"Temperature ({TU})")
    ax.legend(fontsize=7.5, loc="upper right", framealpha=0.9)
    ax.set_xlim(idx[0], idx[-1])
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(_hax))
    ax.set_xlabel(f"Hour (local, {TZ})")
    return fig

def fig_conditions(panel, idx, agg, date_str, sun):
    n = len(idx)
    fig = plt.figure(figsize=(11, 8.5))
    gs = fig.add_gridspec(3, 1, height_ratios=[1.0, 1.0, 0.78], hspace=0.42,
                          left=0.08, right=0.93, top=0.92, bottom=0.07)
    ax1, ax2, ax3 = (fig.add_subplot(gs[0]), fig.add_subplot(gs[1]),
                     fig.add_subplot(gs[2]))
    fig.suptitle(f"Precipitation, storms & wind \u2014 {PLACE}",
                 fontsize=15, y=0.965)
    fig.text(0.5, 0.93, date_str, ha="center", fontsize=9, color="#444")

    # precip amount per model (mm) + rain chance on twin axis
    shade_daylight(ax1, sun, idx)
    for i, m in enumerate(panel):
        ax1.plot(idx, panel[m]["precip"].fillna(0).values, color=model_color(i),
                 lw=1.3, alpha=0.8, label=MODELS[m])
    ax1.plot(idx, agg["pr_mean"], color="black", lw=2.4, label="Mean mm")
    ax1.set_ylabel("Precip (mm/hr)")
    ax1.set_title("Precipitation amount \u2014 each model", loc="left")
    ax1.legend(fontsize=6.5, ncol=5, loc="upper left", framealpha=0.9)

    axp = ax1.twinx()
    axp.fill_between(idx, agg["agree"], color="#4c6ef5", alpha=0.12)
    axp.plot(idx, agg["agree"], color="#4c6ef5", lw=1.8, label="Model agreement %")
    if agg["pop_gfs"] is not None:
        axp.plot(idx, agg["pop_gfs"], color="#7048e8", lw=1.4, ls="--",
                 label="GFS POP %")
    axp.set_ylabel("Rain chance (%)"); axp.set_ylim(0, 100)
    axp.legend(fontsize=7, loc="upper right", framealpha=0.9)

    # severe: CAPE per model + thunder shading
    shade_daylight(ax2, sun, idx)
    has_cape = False
    for i, m in enumerate(panel):
        c = panel[m]["cape"].values
        if np.isfinite(c).any():
            ax2.plot(idx, c, color=model_color(i), lw=1.3, alpha=0.8, label=MODELS[m])
            has_cape = True
    if has_cape:
        ax2.plot(idx, agg["cape_mean"], color="black", lw=2.2, label="Mean CAPE")
    for thr, lab in [(CAPE_MOD, "t-storms possible"), (CAPE_STRONG, "severe possible")]:
        ax2.axhline(thr, color="#888", lw=0.8, ls="--")
        ax2.text(idx[0], thr, f" {lab}", fontsize=6.5, color="#666", va="bottom")
    thunder_any = np.zeros(n, bool)
    for m in panel:
        thunder_any |= panel[m]["thunder"].values
    for i, j in runs(thunder_any):
        ax2.axvspan(idx[i], idx[j] + pd.Timedelta(hours=1), color="#b2182b", alpha=0.12)
    ax2.set_ylabel("CAPE (J/kg)")
    title = "Thunderstorm energy (CAPE)  \u2014  red bands = a model flags thunder"
    if not has_cape:
        title = "Severe risk \u2014 CAPE unavailable here; red bands = thunder codes"
    ax2.set_title(title, loc="left")
    if has_cape:
        ax2.legend(fontsize=6.5, ncol=5, loc="upper left", framealpha=0.9)

    # wind: sustained + gusts, with penalty thresholds and direction arrows
    shade_daylight(ax3, sun, idx)
    mphd = (lambda v: v) if US else (lambda v: v * 1.60934)
    g, w = Wd(agg["gust_mean"]), Wd(agg["wind_mean"])
    gmax = np.nanmax(g) if not np.all(np.isnan(g)) else mphd(GUST_MOD_MPH)
    ax3.set_ylim(0, max(gmax * 1.4, mphd(GUST_MOD_MPH) * 1.1))
    ax3.plot(idx, g, color="#d9480f", lw=1.8)
    ax3.plot(idx, w, color="#1864ab", lw=2.4)
    if not np.all(np.isnan(g)):
        ig = int(np.nanargmax(g))
        ax3.annotate("gusts", (idx[ig], g[ig]), xytext=(0, 4),
                     textcoords="offset points", fontsize=7.5, color="#d9480f", ha="center")
    if not np.all(np.isnan(w)):
        iw = int(np.nanargmax(w))
        ax3.annotate("sustained", (idx[iw], w[iw]), xytext=(0, -11),
                     textcoords="offset points", fontsize=7.5, color="#1864ab", ha="center")
    for thr, lab, col in [(WIND_HIGH_MPH, "strong", "#1864ab"),
                          (GUST_HIGH_MPH, "hazardous gusts", "#d9480f")]:
        if mphd(thr) < ax3.get_ylim()[1]:
            ax3.axhline(mphd(thr), color=col, lw=0.8, ls=":", alpha=0.6)
            ax3.text(idx[-1], mphd(thr), f"{lab} ", fontsize=6.2, color=col,
                     va="bottom", ha="right")
    wdir = agg.get("wdir_hourly")
    if wdir is not None and not np.all(np.isnan(wdir)):
        yt = ax3.get_ylim()[1] * 0.9
        for k in range(0, n, 3):
            if not np.isnan(wdir[k]):
                R = 90 - ((wdir[k] + 180) % 360)        # blow-to bearing -> screen angle
                ax3.text(idx[k], yt, "\u2192", rotation=R, rotation_mode="anchor",
                         ha="center", va="center", fontsize=12, color="#555")
    prevail = compass(agg.get("prevailing_dir")) if agg.get("prevailing_dir") is not None else None
    ax3.set_ylabel(f"Wind ({WU})")
    ax3.set_title("Wind \u2014 sustained & gusts"
                  + (f"  (prevailing from {prevail}; arrows = direction)" if prevail else ""),
                  loc="left")

    for ax in (ax1, ax2, ax3):
        ax.set_xlim(idx[0], idx[-1])
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(_hax))
    ax3.set_xlabel(f"Hour (local, {TZ})")
    return fig

def page_precip_severe(pdf, panel, idx, agg, date_str, sun):
    fig = fig_conditions(panel, idx, agg, date_str, sun)
    pdf.savefig(fig); plt.close(fig)


# ---------------------------- main ------------------------------------------
def build_aggregates(panel):
    air = stack(panel, "t_c")
    pr = np.nan_to_num(stack(panel, "precip"), nan=0.0)
    ws = stack(panel, "wind_ms")
    wd = stack(panel, "wdir")
    # vector-mean wind direction across models, weighted by speed (per hour)
    th = np.deg2rad(wd)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        su = np.nansum(np.sin(th) * ws, axis=0)
        sv = np.nansum(np.cos(th) * ws, axis=0)
        wdir_hourly = np.where((su == 0) & (sv == 0), np.nan,
                               np.rad2deg(np.arctan2(su, sv)) % 360)
        prevailing = (float(np.rad2deg(np.arctan2(np.nansum(np.sin(th) * ws),
                                                  np.nansum(np.cos(th) * ws))) % 360)
                      if np.isfinite(wd).any() else None)
    agg = {
        "air_mean": np.nanmean(air, 0),
        "hi_mean": nanmean0(stack(panel, "hi_c")),
        "tg_mean": nanmean0(stack(panel, "tg_c")),
        "tw_mean": nanmean0(stack(panel, "tw_c")),
        "wb_mean": nanmean0(stack(panel, "wbgt_c")),
        "pr_mean": pr.mean(0),
        "agree": 100.0 * (pr >= RAIN_MM_LIGHT).mean(0),
        "cape_mean": nanmean0(stack(panel, "cape")),
        "uv_mean": nanmean0(stack(panel, "uv")),
        "wind_mean": nanmean0(ws),
        "gust_mean": nanmean0(stack(panel, "gust_ms")),
        "wdir_hourly": wdir_hourly,
        "prevailing_dir": prevailing,
        "sc_mean": stack(panel, "score").mean(0),
        "pop_gfs": (panel["gfs_seamless"]["pop"].values
                    if "gfs_seamless" in panel
                    and panel["gfs_seamless"]["pop"].notna().any() else None),
    }
    return agg

def split_by_day(panel):
    """dict[date] -> per-day sub-panel (dict of model -> day DataFrame)."""
    idx = panel[next(iter(panel))].index
    days = sorted(set(idx.date))
    out = []
    for d in days:
        sub = {m: panel[m][panel[m].index.date == d] for m in panel}
        if len(sub[next(iter(sub))]) >= 12:        # skip stub days with few hours
            out.append((d, sub))
    return out

def worst_alert(alerts):
    if not alerts:
        return None
    rank = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3, "Unknown": 4}
    return sorted(alerts, key=lambda a: rank.get(a["severity"], 5))[0]

def main():
    setup_style()
    print("Fetching multi-model forecast from Open-Meteo ...")
    try:
        js = fetch()
    except Exception as e:
        sys.exit(f"Failed to fetch data: {e}")

    panel = to_panel(js)
    if not panel:
        sys.exit("No model data returned.")

    print("Checking NWS alerts ...")
    alerts = fetch_alerts()
    days = split_by_day(panel)

    out = OUTFILE or f"dc_bike_weather_{days[0][0].strftime('%Y%m%d')}.pdf"
    printed = []
    with PdfPages(out) as pdf:
        for d, sub in days:
            didx = sub[next(iter(sub))].index
            sun = sun_times(LAT, LON, d)
            agg = build_aggregates(sub)
            bullets, win = summarize(sub, didx, agg, sun)
            day_alerts = [a for a in alerts if alert_overlaps_day(a, d)]
            label = day_label(d)
            page_overview(pdf, sub, didx, agg, bullets, label, day_alerts, sun, win)
            page_thermal(pdf, sub, didx, agg, label, sun)
            page_precip_severe(pdf, sub, didx, agg, label, sun)
            printed.append((label, bullets))

    n_models = len(panel)
    print(f"Wrote {out}  ({n_models} models, {len(days)} day(s), 3 pages each)")

    wa = worst_alert(alerts)
    if wa:
        print(f"\n>>> TOP ALERT: {wa['event']} [{wa['severity']}] "
              f"{fmt_alert_window(wa['onset'], wa['ends'])}")
        if len(alerts) > 1:
            print(f"    (+{len(alerts) - 1} more active \u2014 see weather.gov)")
    else:
        print("\nNo active NWS alerts.")

    for label, bullets in printed:
        print(f"\n=== {label} ===")
        for b in bullets:
            print("  - " + b)


if __name__ == "__main__":
    main()
