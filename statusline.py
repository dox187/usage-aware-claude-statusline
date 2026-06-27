#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Claude Code Custom Status Line Script
======================================

WHAT IT DOES
  Renders a multi-line status line for Claude Code from the session JSON that
  Claude Code pipes in on stdin. The layout (templates) and the weather
  location live in an external config file (statusline_config.json) so you can
  tweak them without editing this script. Colors live in the COLORS dict below.

SETUP (settings.json)
  Add a statusLine block to your Claude Code settings.json. Write the script
  path with FORWARD SLASHES or ~ on every platform:

    "statusLine": {
      "type": "command",
      "command": "uv run ~/.claude/statusline.py",
      "padding": 0
    }

  - macOS / Linux : "uv run ~/.claude/statusline.py"
                    (or "python3 ~/.claude/statusline.py" if you don't use uv)
  - Windows       : "uv run ~/.claude/statusline.py"
                    (the drive form "uv run /c/Users/<you>/.claude/statusline.py" also works)

  WINDOWS PATH RULE: use forward slashes or ~, NEVER backslashes. On Windows
  Claude Code runs the command through Git Bash when it is installed (otherwise
  through PowerShell). Git Bash treats a backslash as an escape character, so a
  path like C:\Users\<you>\... is mangled and the command fails silently. Git
  Bash translates /c/... to C:\... and expands ~ to your home directory. With no
  Git Bash installed, use a PowerShell wrapper instead:
    "powershell -NoProfile -File C:/Users/<you>/.claude/statusline.ps1"

REQUIREMENTS
  - Python 3.7+  Only the standard library is imported, so no pip install is
                 ever needed. 3.7 is the floor (datetime.fromisoformat is used).
  - uv           OPTIONAL. Just a convenient launcher; since there are no
                 third-party deps, "python3 statusline.py" works equally well.
  - Git          OPTIONAL. Only the branch / (+added,-removed) segment needs it;
                 it degrades gracefully outside a repo or when git is absent.
  - Internet     OPTIONAL. Only live weather (Open-Meteo) uses it, and ONLY when
                 an active template line actually references {weather} or {sun} -
                 if neither is used, no weather request is ever made. Results are
                 cached in ~/.claude/weather_cache.json for 10 minutes; offline
                 it falls back to the stale cache, then to a blank weather line.
  - Terminal     Needs 24-bit truecolor and an emoji-capable Unicode font.
                 Nerd Fonts are NOT required (templates use the ASCII "(git)/"
                 label plus standard emoji).

CONFIG FILE (statusline_config.json)
  Must sit in the SAME directory as this script (its path is derived from the
  script's own location). If it is missing or invalid the built-in
  DEFAULT_WEATHER / DEFAULT_TEMPLATES below are used, so the status line keeps
  working. Keys starting with "_" (e.g. "_comment") are ignored. Shape:

    {
      "weather":     { "name": "<city>", "latitude": <lat>, "longitude": <lon>,
                       "show_name": true, "show_icon": true, "show_temp": true,
                       "show_humidity": true, "show_wind": true },
      "templates":   { "line1": "...", "line2": "...", "line3": "..." },
      "emoji_width": 2,
      "ctx_bar_empty": "░",
      "colors":      { "model": "#RRGGBB", "ctx_bar": "#RRGGBB", ... }
    }

  - weather     : city label shown on the weather line + the coordinates sent to
                  the Open-Meteo API (decimal degrees; look them up at
                  https://open-meteo.com, any map, or the TUI editor's "Look up
                  coordinates" action which geocodes the city name for you).
                  The show_* booleans pick which parts of the {weather} string
                  appear: show_name (city), show_icon (weather emoji), show_temp
                  (°C), show_humidity (💧 %), show_wind (🍃 km/h). All default to
                  true. Weather is fetched ONLY when {weather} or {sun} is used in
                  an active line.
  - templates   : one entry per line, printed in order (line1, line2, ...).
                  Remove a line to hide it; up to 10 lines are supported.
  - emoji_width : terminal columns per emoji, 1 or 2 (default 2). Only affects
                  the line1<->bar alignment math. Set 1 if your terminal renders
                  emoji single-width and the bar looks horizontally shifted.
  - ctx_bar_empty : glyph for the bar's unfilled cells (default "░"); any single-
                  column character (e.g. ░ ▒ · - or a space).
  - colors      : per-key "#RRGGBB" overrides for the COLORS dict above. Any key
                  you omit keeps the default; invalid values are ignored.

  TEMPLATE PLACEHOLDERS
    {c.NAME} apply a COLORS color   {r} reset
    {time} {version} {model} {effort}     {sun} {weather} {peak_label}
    {ctx} {ctx_percent} {ctx_bar} {align_pad}   {path} {branch} {added} {removed}
    {total} {input} {output} {cached}

  {ctx_bar} draws a context-usage gauge; the trailing cell uses Unicode
  eighth-blocks (▏▎▍▌▋▊▉) so the fill is accurate to 1/8 of a cell. The bar
  colors itself from COLORS in token-usage bands: "ctx_bar" up to 150K, then
  "ctx_bar_mid" (150-250K), "ctx_bar_high" (250-300K), "ctx_bar_crit" (300-500K)
  and "ctx_bar_max" (500K+); the empty ░ track uses "ctx_bar_track". The bands
  are defined in CTX_BAR_BANDS. Flank it with bracket glyphs via {c.ctx_bracket}.

  ALIGNMENT: a bare {ctx_bar} on line2 auto-sizes so the bracketed bar lines up
  with the line1 segment from {effort} to the end of line1 (its width and
  position follow line1's live content). Put {align_pad} right before the bar to
  emit the leading spaces. So line2 typically reads:
      {c.weather}{weather}{r}{align_pad}{c.ctx_bracket}▐{r}{ctx_bar}{c.ctx_bracket}▌{r}
  Use {ctx_bar:N} instead for a FIXED N-cell bar that ignores line1 (no
  {align_pad} needed then).

  Colors default to the COLORS dict in THIS file (hex "#RRGGBB") and can be
  overridden per key from the "colors" block in statusline_config.json. Reference
  one from a template as {c.NAME} (e.g. {c.effort} or {c.ctx_bracket}).
"""
import sys
import json
import subprocess
import os
import io
import re
import unicodedata
from datetime import datetime, timezone, timedelta

# Windows fix: force LF line endings instead of CRLF
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, newline='\n', encoding='utf-8')

# ============================================
# COLORS (HEX format) — DEFAULTS
# Format: "#RRGGBB"
# These are defaults. Any key can be overridden from the "colors" block in
# statusline_config.json; keys not present there keep the value below.
# ============================================
COLORS = {
    "time":         "#E5C07B",
    "version":      "#808080",
    "model":        "#E06C75",
    "peak":         "#FF3333",
    "offpeak":      "#006400",
    "offpeak_warn": "#FFA500",
    "usd":          "#2E8B57",
    "ctx_label":    "#aaaaaa",
    "ctx_value":    "#aaaaaa",
    "ctx_percent":  "#ffffff",
    "ctx_icon":     "#aaaaaa",
    "model_icon":   "#aaaaaa",

    "total_icon":   "#717171",
    "total":        "#B8FF75",
    "bracket":      "#717171",
    "input":        "#E06C75",
    "separator":    "#717171",
    "output":       "#61AFEF",
    "cached_icon":  "#A090A0",
    "cached":       "#A090A0",

    "path":         "#61AFEF",
    "git_icon":     "#717171",
    "branch":       "#E5C07B",
    "git_status":   "#E5C07B",
    "changes":      "#E5C07B",
    "weather":      "#87CEEB",
    "effort":       "#61AFEF",
    "ctx_bar":      "#98C379",   # 0-150K   light green (base fill)
    "ctx_bar_mid":  "#3E8E41",   # 150-250K darker green
    "ctx_bar_high": "#E5C07B",   # 250-300K orange (own copy of the clock gold)
    "ctx_bar_crit": "#E06C75",   # 300-500K red (own copy of the model red)
    "ctx_bar_max":  "#FF2A2A",   # 500K+    intense red
    "ctx_bar_track":"#3a3a3a",
    "ctx_bracket":  "#ffffff",
}

# ============================================
# EXTERNAL CONFIG (templates + weather location)
# ============================================
# Templates and the weather city name / coordinates live in an external
# JSON file (statusline_config.json next to this script) so they can be
# edited without touching the Python code. The values below are fallback
# defaults used only when the config file is missing or invalid.
#
# Available template placeholders:
#   {c.COLORNAME} - apply color
#   {r} - reset (restore default color)
#   {time}, {version}, {model}, {effort}, {ctx}, {ctx_percent}
#   {ctx_bar} / {ctx_bar:N} - context-usage gauge (eighth-block precision);
#       bare {ctx_bar} auto-sizes to align with line1, {ctx_bar:N} is fixed
#   {align_pad} - spaces that align line2's bar under the line1 segment
#   {total}, {input}, {output}, {cached}
#   {path}, {branch}, {added}, {removed}, {weather}, {sun}, {peak_label}
# ============================================
# Config path: next to this script, unless STATUSLINE_CONFIG overrides it (used
# by the TUI editor to preview an unsaved config without touching the real file).
CONFIG_PATH = os.environ.get("STATUSLINE_CONFIG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "statusline_config.json")

DEFAULT_WEATHER = {
    "name": "Newyork", "latitude": 56.25, "longitude": -5.2833,
    # Which pieces of the weather string are shown. Toggle from the config (or
    # the TUI editor). All on by default = the classic "Name ☀️22°C 💧 45% 🍃 10km/h".
    "show_name": True, "show_icon": True, "show_temp": True,
    "show_humidity": True, "show_wind": True,
}
# Booleans inside the "weather" block (everything except name/latitude/longitude).
WEATHER_FLAGS = ("show_name", "show_icon", "show_temp", "show_humidity", "show_wind")
DEFAULT_EMOJI_WIDTH = 2   # terminal columns per emoji (1 or 2); used for bar alignment

DEFAULT_TEMPLATES = {
    "line1": "{sun} {c.time}[{time}]{r} {c.version}{version}{r} {c.model}{model}{r}{c.effort}{effort}{r} Ctx:{ctx} {c.ctx_percent}({ctx_percent}%){r} {ctx_micro}",
    "line2": "{c.weather}{weather}{r}  {usage_micro}",
    "line3": "{c.path}{path}{r} {c.git_icon}(git)/{r}{c.branch}{branch}{r} ({c.output}+{added}{r},{c.input}-{removed}{r}) ",
}

def load_config():
    """Load templates + weather location from statusline_config.json.

    Falls back to built-in defaults for any missing/invalid piece, so the
    status line keeps working even if the config file is absent or broken.
    Keys starting with '_' (e.g. '_comment') are ignored.

    Returns (weather, templates, emoji_width).
    """
    weather = dict(DEFAULT_WEATHER)
    templates = dict(DEFAULT_TEMPLATES)
    emoji_width = DEFAULT_EMOJI_WIDTH
    colors = {}
    bar_empty = BAR_EMPTY
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        w = cfg.get("weather", {})
        if isinstance(w, dict):
            for k in ("name", "latitude", "longitude"):
                if k in w:
                    weather[k] = w[k]
            for k in WEATHER_FLAGS:
                if k in w:
                    weather[k] = bool(w[k])
        t = cfg.get("templates", {})
        if isinstance(t, dict):
            file_lines = {k: v for k, v in t.items()
                          if k.startswith("line") and isinstance(v, str)}
            if file_lines:
                templates = file_lines
        # Emoji column width (1 or 2); anything else falls back to the default.
        emoji_width = 1 if cfg.get("emoji_width") == 1 else DEFAULT_EMOJI_WIDTH
        # Color overrides: keep only valid "#RRGGBB" values; the rest keep the
        # COLORS defaults defined in this script.
        cols = cfg.get("colors", {})
        if isinstance(cols, dict):
            colors = {k: v for k, v in cols.items()
                      if isinstance(v, str) and HEX_RE.match(v)}
        # Empty-cell glyph for the context bar (any non-empty string).
        be = cfg.get("ctx_bar_empty")
        if isinstance(be, str) and be != "":
            bar_empty = be
    except Exception:
        pass
    return weather, templates, emoji_width, colors, bar_empty

# ============================================
# HELPER FUNCTIONS
# ============================================
def hex_to_ansi(hex_color):
    """Convert hex color (#RRGGBB) to ANSI escape code"""
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"\033[38;2;{r};{g};{b}m"

RESET = "\033[0m"
BOLD = "\033[1m"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")   # validates "#RRGGBB" color values

def strip_ansi(s):
    """Remove ANSI SGR color escape codes from a string."""
    return ANSI_RE.sub("", s)

def display_width(s, emoji_width=2):
    """Approximate the terminal column width of a string.

    ANSI color codes are stripped first. Variation selectors (incl. VS16),
    zero-width joiners and combining marks count as 0. True East Asian wide
    characters (CJK) always count as 2. Emoji (VS16-presentation sequences
    and pictographic-plane code points) count as `emoji_width` columns -
    set this to match how your terminal renders emoji (2 in most modern
    terminals such as Windows Terminal / iTerm2, 1 in some others). Used to
    line up the context bar on line2 with the segment on line1.
    """
    s = strip_ansi(s)
    width = 0
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        o = ord(ch)
        nxt = s[i + 1] if i + 1 < n else ""
        # Zero-width: ZWJ, variation selectors, combining marks
        if o == 0x200D or 0xFE00 <= o <= 0xFE0F or unicodedata.combining(ch):
            i += 1
            continue
        if nxt and ord(nxt) == 0xFE0F:            # emoji-presentation (VS16)
            width += emoji_width
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 2                            # CJK fullwidth (always 2)
        elif o >= 0x1F000:                        # emoji / pictographic planes
            width += emoji_width
        else:
            width += 1
        i += 1
    return width

class ColorAccessor:
    """Enables {c.color_name} syntax in templates"""
    def __getattr__(self, name):
        hex_val = COLORS.get(name, "#ffffff")
        return hex_to_ansi(hex_val)

# ============================================
# PROGRESS BAR (e.g. context-usage gauge)
# Width is template-driven via the format spec: {ctx_bar:30} -> 30 cells.
# The trailing partial cell uses Unicode eighth-blocks for sub-cell precision.
# ============================================
BAR_FULL = "█"   # █ full block (8/8)
BAR_EMPTY = "░"       # ░ light-shade "track" shown for the unfilled cells
# Index = eighths filled in the trailing partial cell (0 = none, 1..7 = 1/8..7/8).
BAR_EIGHTHS = ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉")
#               0   ▏1/8     ▎2/8     ▍3/8     ▌4/8     ▋5/8     ▊6/8     ▉7/8
DEFAULT_BAR_WIDTH = 20
# Color bands for the context bar, keyed by ABSOLUTE token usage:
# (lower_token_bound, COLORS key). Below the first bound the bar fills with the
# base "ctx_bar" color; at/above each bound it switches to that band's color, so
# the filled run can show several colors as usage grows. Bounds are turned into
# bar fractions via the live context-window size, so they track the token count
# regardless of window size (200K, 1M, ...).
CTX_BAR_BANDS = [
    (150000, "ctx_bar_mid"),    # 150-250K  darker green
    (250000, "ctx_bar_high"),   # 250-300K  orange
    (300000, "ctx_bar_crit"),   # 300-500K  red
    (500000, "ctx_bar_max"),    # 500K+     intense red
]

# Color bands for the USAGE bars (session / weekly rate-limit gauges), keyed by
# FRACTION of the limit (0..1). Up to 40% the fill stays the base "ctx_bar"
# (light green); from 40% it walks the other bar colors as the limit fills up.
USAGE_BAR_BANDS = [
    (0.400, "ctx_bar_mid"),     # 40.0-62.5%  darker green
    (0.625, "ctx_bar_high"),    # 62.5-75.0%  orange
    (0.750, "ctx_bar_crit"),    # 75.0-87.5%  red
    (0.875, "ctx_bar_max"),     # 87.5-100%   intense red
]

class ProgressBar:
    """A fractional progress bar rendered with Unicode block characters.

    The width (in cells) comes from the template format spec, so the bar
    length is controlled entirely from the template:
        {ctx_bar}     -> DEFAULT_BAR_WIDTH cells
        {ctx_bar:30}  -> 30 cells
    The trailing partial cell uses eighth-blocks (▏▎▍▌▋▊▉) so the fill is
    accurate to 1/8 of a cell rather than to whole cells only.

    The bar colors itself: filled blocks use fill_color and the empty track
    (empty_char, default ░) uses track_color (both ANSI strings; pass "" to
    leave a part uncolored). empty_char is configurable so the unfilled cells
    can be any glyph (e.g. ░, ·, -, or a space).

    Width resolution: an explicit format spec ({ctx_bar:30}) wins; otherwise
    the per-instance `width` (set dynamically for line1 alignment) is used;
    otherwise DEFAULT_BAR_WIDTH.
    """
    def __init__(self, fraction, fill_color="", track_color="", width=None,
                 empty_char=None, bands=None):
        try:
            f = float(fraction)
        except (TypeError, ValueError):
            f = 0.0
        # Clamp to [0, 1]
        self.fraction = 0.0 if f < 0 else (1.0 if f > 1 else f)
        self.fill_color = fill_color or ""
        self.track_color = track_color or ""
        self.width = width
        self.empty_char = empty_char or BAR_EMPTY
        # Optional color bands for the filled run: a list of (lower_fraction,
        # ansi_color) sorted ascending. A filled cell at fractional position p
        # (its left edge) takes the color of the LAST band whose lower_fraction
        # <= p; below the first band it uses fill_color. None/empty -> the whole
        # fill is a single color (fill_color), i.e. the original behavior.
        self.bands = list(bands) if bands else None

    def __format__(self, spec):
        # The format spec is the desired width in cells (e.g. {ctx_bar:30}).
        default = self.width if self.width else DEFAULT_BAR_WIDTH
        try:
            width = int(spec) if spec else default
        except ValueError:
            width = default
        if width < 1:
            width = 1
        # Total fill measured in eighths of a cell, rounded to nearest eighth.
        eighths = int(round(self.fraction * width * 8))
        full, rem = divmod(eighths, 8)
        if full >= width:  # fully filled (or rounding overflow)
            full, rem = width, 0

        # Build the filled run. With color bands set, color each filled cell by
        # its position: the cell at fractional position i/width takes the color of
        # the last band whose lower_fraction <= it (fill_color below the first
        # band). Runs of the same color are coalesced so we emit as few escape
        # codes as possible. Without bands it stays a single-color run.
        if self.bands:
            def cell_color(i):
                p = (i + 0.5) / width   # cell midpoint: the cell holding a band
                col = self.fill_color   # boundary flips to that higher band
                for lo, bc in self.bands:
                    if p >= lo:
                        col = bc
                    else:
                        break
                return col or self.fill_color

            n_filled = full + (1 if rem else 0)
            filled = ""
            prev = None
            for i in range(n_filled):
                glyph = BAR_FULL if i < full else BAR_EIGHTHS[rem]
                col = cell_color(i)
                if col != prev:
                    filled += col
                    prev = col
                filled += glyph
        else:
            filled = self.fill_color + BAR_FULL * full + BAR_EIGHTHS[rem]

        empty = self.empty_char * (width - full - (1 if rem else 0))
        end = RESET if (self.fill_color or self.track_color or self.bands) else ""
        return f"{filled}{self.track_color}{empty}{end}"

def format_k(value):
    """Format number to k/M notation (e.g., 22800 -> 22.8k, 1400000 -> 1.4M)"""
    try:
        val = float(value)
        if val >= 1000000:
            return f"{val/1000000:.1f}M"
        if val >= 1000:
            return f"{val/1000:.1f}k"
        return str(int(val))
    except:
        return str(value)

def format_path(path, max_len=25):
    """Shorten path: ~ for home, and ... if too long"""
    # Replace home directory with ~
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]

    # Platform-specific path separator normalization
    if os.name == 'nt':  # Windows
        path = path.replace("/", "\\")

    # If too long, truncate from the beginning
    if len(path) > max_len:
        path = "…" + path[-(max_len-1):]

    return path

WEATHER_CACHE = os.path.join(os.path.expanduser("~"), ".claude", "weather_cache.json")
WEATHER_TTL = 600  # 10 minutes

WMO_ICONS = {
    0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️",
    45: "🌫️", 48: "🌫️",
    51: "🌦️", 53: "🌦️", 55: "🌧️",
    56: "🌧️", 57: "🌧️",
    61: "🌧️", 63: "🌧️", 65: "🌧️",
    66: "🧊", 67: "🧊",
    71: "🌨️", 73: "🌨️", 75: "❄️",
    77: "❄️",
    80: "🌦️", 81: "🌧️", 82: "⛈️",
    85: "🌨️", 86: "❄️",
    95: "⛈️", 96: "⛈️", 99: "⛈️",
}

def fetch_weather_raw(lat=56.25, lon=-5.2833):
    """Fetch the raw weather components from Open-Meteo, cached 10 minutes.

    Returns a dict {code, temp, hum, wind, sunrise, sunset} (or {} when no data
    is available at all). The RAW pieces are cached - not a pre-formatted string -
    so toggling the show_* flags (or the city name) takes effect immediately
    without waiting for the cache to expire. The cache is keyed by coordinates,
    so changing the location skips a stale hit.
    """
    keys = ("code", "temp", "hum", "wind", "sunrise", "sunset")

    def from_cache(require_fresh):
        try:
            with open(WEATHER_CACHE, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            return None
        if "code" not in cache:                       # old/foreign cache shape
            return None
        if require_fresh:
            age = datetime.now().timestamp() - cache.get("ts", 0)
            if age >= WEATHER_TTL:
                return None
            if cache.get("lat") != lat or cache.get("lon") != lon:
                return None
        return {k: cache.get(k) for k in keys}

    fresh = from_cache(require_fresh=True)
    if fresh is not None:
        return fresh

    # Fetch fresh data
    try:
        import urllib.request
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code"
            f"&daily=sunrise,sunset"
            f"&timezone=Europe/Budapest"
        )
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        cur = data.get("current", {})
        daily = data.get("daily", {})
        raw = {
            "code": cur.get("weather_code", 0),
            "temp": cur.get("temperature_2m", "?"),
            "hum": cur.get("relative_humidity_2m", "?"),
            "wind": cur.get("wind_speed_10m", "?"),
            "sunrise": (daily.get("sunrise") or [""])[0],  # "2026-03-16T06:05"
            "sunset": (daily.get("sunset") or [""])[0],
        }
        try:
            with open(WEATHER_CACHE, "w", encoding="utf-8") as f:
                json.dump({"ts": datetime.now().timestamp(), "lat": lat, "lon": lon, **raw}, f)
        except Exception:
            pass
        return raw
    except Exception:
        # Fetch failed: fall back to a stale cache (any age, any location), else {}.
        stale = from_cache(require_fresh=False)
        return stale if stale is not None else {}


def format_weather(raw, name="", flags=None):
    """Build the weather display string from raw components + show_* flags.

    flags is the weather config dict (or any mapping); each show_* defaults to
    True when absent. Returns "" when there is no data (so the line collapses).
    """
    if not raw:
        return ""
    flags = flags or {}

    def on(key):
        return bool(flags.get(key, True))

    icon = WMO_ICONS.get(raw.get("code", 0), "🌡️")
    parts = []
    if on("show_name") and name:
        parts.append(str(name))
    core = ""
    if on("show_icon"):
        core += icon
    if on("show_temp"):
        core += f"{raw.get('temp', '?')}°C"
    if core:
        parts.append(core)
    if on("show_humidity"):
        parts.append(f"💧 {raw.get('hum', '?')}%")
    if on("show_wind"):
        parts.append(f"🍃 {raw.get('wind', '?')}km/h")
    return " ".join(parts)

def get_sun_icon(sunrise_str, sunset_str):
    """Return sun state icon based on current time vs sunrise/sunset"""
    now = datetime.now()
    try:
        sunrise = datetime.fromisoformat(sunrise_str)
        sunset = datetime.fromisoformat(sunset_str)
    except:
        return "🌡️"
    margin = 30 * 60  # 30 minutes in seconds
    diff_rise = (now - sunrise).total_seconds()
    diff_set = (now - sunset).total_seconds()
    if abs(diff_rise) < margin:
        return "🌅"
    if abs(diff_set) < margin:
        return "🌇"
    if now < sunrise or now > sunset:
        return "🌙"
    return "☀️"

# ============================================
# USAGE / RATE-LIMIT GAUGES (session + weekly)
# Data comes from claude_usage.fetch_usage() (the same /api/oauth/usage endpoint
# the `/usage` panel uses). Cached briefly so the status line stays fast and we
# don't hit the network on every render.
# ============================================
USAGE_CACHE = os.path.join(os.path.expanduser("~"), ".claude", "usage_cache.json")
USAGE_TTL = 60  # seconds; the limits move slowly, so a short cache is plenty

def get_usage():
    """Return the rate-limit usage dict (cached), or None when unavailable.

    Tries the on-disk cache first (USAGE_TTL seconds), then claude_usage.fetch_usage()
    next to this script, then falls back to a stale cache. Any failure -> None so
    the usage lines simply render empty rather than breaking the status line.
    """
    # Fresh cache?
    try:
        if os.path.exists(USAGE_CACHE):
            with open(USAGE_CACHE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if datetime.now().timestamp() - cache.get("ts", 0) < USAGE_TTL:
                return cache.get("data")
    except Exception:
        pass
    # Fetch fresh (import the sibling claude_usage.py module)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import claude_usage
        data = claude_usage.fetch_usage(timeout=5)
        try:
            with open(USAGE_CACHE, "w", encoding="utf-8") as f:
                json.dump({"ts": datetime.now().timestamp(), "data": data}, f)
        except Exception:
            pass
        return data
    except Exception:
        # Stale cache as last resort
        try:
            with open(USAGE_CACHE, "r", encoding="utf-8") as f:
                return json.load(f).get("data")
        except Exception:
            return None

def fmt_reset(iso_str):
    """Format an ISO reset timestamp (UTC) as local 'MM.DD. HH:MM'."""
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.astimezone().strftime("%m.%d. %H:%M")
    except Exception:
        return ""

def build_usage_lines(usage, total_width, c, bar_empty):
    """Build (bars_line, labels_line) for the session + weekly usage gauges.

    Two bracket-less horizontal bars (same block design as the context bar:
    full blocks + an eighth-block trailing cell for sub-cell precision) sit side
    by side and together span `total_width` columns (= line1 width). Filled cells
    are colored via USAGE_BAR_BANDS (green to 40%, then the other colors). Under
    each bar: a left-aligned label "<pct>% <name>" (the pct white+bold, the name
    bold gray) and a white, right-aligned reset date.
    Returns ("", "") when usage data is missing.
    """
    if not usage:
        return "", ""
    five = usage.get("five_hour") or {}
    week = usage.get("seven_day") or {}
    sess_pct = five.get("utilization") or 0
    week_pct = week.get("utilization") or 0

    W = total_width if total_width and total_width > 0 else 60
    gap = 2                          # spaces between the two bars
    inner = (W - gap) // 2           # bracket-less: each bar is `inner` wide
    if inner < 1:
        inner = 1
    extra = W - (2 * inner + gap)    # leftover column when W is odd
    gap_str = " " * (gap + max(0, extra))

    bands = [(frac, getattr(c, key)) for frac, key in USAGE_BAR_BANDS]
    sess_bar = ProgressBar(sess_pct / 100.0, c.ctx_bar, c.ctx_bar_track,
                           width=inner, empty_char=bar_empty, bands=bands)
    week_bar = ProgressBar(week_pct / 100.0, c.ctx_bar, c.ctx_bar_track,
                           width=inner, empty_char=bar_empty, bands=bands)
    bars = f"{sess_bar}{gap_str}{week_bar}"

    # Label row: "<pct>% <name>" flush-left (pct white+bold, name bold gray),
    # white reset date flush-right, within each bar's width.
    gray = hex_to_ansi(COLORS["ctx_label"])
    white = hex_to_ansi("#ffffff")
    def label_row(pct, name, iso):
        pct_str = f"{int(round(pct))}%"
        date = fmt_reset(iso)
        pad = inner - (len(pct_str) + 1 + len(name)) - len(date)
        if pad < 1:
            pad = 1
        return (f"{BOLD}{white}{pct_str}{RESET}{BOLD}{gray} {name}{RESET}"
                f"{' ' * pad}{white}{date}{RESET}")
    labels = (label_row(sess_pct, "session", five.get("resets_at", "")) + gap_str
              + label_row(week_pct, "week", week.get("resets_at", "")))
    return bars, labels

# Single-cell height blocks for the micro gauges (▁ low .. █ full).
MICRO_HEIGHTS = "▁▂▃▄▅▆▇█"

def fmt_remaining(iso_str):
    """Rounded time-until-reset: '(Xm)' under 1h, '(~Xh)' under 24h, '(~Xd)' beyond."""
    try:
        dt = datetime.fromisoformat(iso_str)
    except Exception:
        return ""
    sec = (dt - datetime.now(timezone.utc)).total_seconds()
    if sec < 0:
        sec = 0
    if sec < 3600:                                   # under an hour -> minutes
        m = int(round(sec / 60))
        m = min(59, max(1, m)) if sec > 0 else 0
        return f"({m}m)"
    if sec < 24 * 3600:                              # under a day -> hours
        return f"(~{max(1, int(round(sec / 3600)))}h)"
    return f"(~{max(1, int(round(sec / 86400)))}d)"   # else -> days

def micro_char(frac, base_color, bands):
    """One colored micro-bar cell: ░ (track gray) at 0%, else ▁..█ by fraction,
    colored by `bands` [(lower_fraction, ansi_color), ...] over `base_color`."""
    try:
        frac = float(frac)
    except (TypeError, ValueError):
        frac = 0.0
    frac = 0.0 if frac < 0 else (1.0 if frac > 1 else frac)
    if frac <= 0:
        return f"{hex_to_ansi(COLORS['ctx_bar_track'])}░{RESET}"
    level = max(1, min(len(MICRO_HEIGHTS), int(round(frac * len(MICRO_HEIGHTS)))))
    glyph = MICRO_HEIGHTS[level - 1]
    col = base_color
    for lo, bc in (bands or []):
        if frac >= lo:
            col = bc
        else:
            break
    return f"{col}{glyph}{RESET}"

def micro_bar(frac, base_color, bands, edge_bold=True):
    """A bracketed micro gauge: '▕<cell>▏' with the ▕ ▏ edges in the ctx
    (bracket) color (bold by default)."""
    brk = hex_to_ansi(COLORS["ctx_bracket"])
    b = BOLD if edge_bold else ""
    return f"{b}{brk}▕{RESET}{micro_char(frac, base_color, bands)}{b}{brk}▏{RESET}"

def build_usage_micro(usage, c, compact=False):
    """Micro-bar line, one segment per gauge: '<label> <pct>%▕<cell>▏(~time)'.

    With compact=True the label is a single letter glued to the pct ('S41%',
    'W46%'); otherwise it's the full word + space ('session 41%'). Label gray+bold,
    pct white+bold, ▕ ▏ edges in the ctx (bracket) color + bold, the reset
    countdown plain white. Returns "" when usage data is missing.
    """
    if not usage:
        return ""
    five = usage.get("five_hour") or {}
    week = usage.get("seven_day") or {}
    gray = hex_to_ansi(COLORS["ctx_label"])
    white = hex_to_ansi("#ffffff")
    bands = [(frac, getattr(c, key)) for frac, key in USAGE_BAR_BANDS]
    def seg(label, short, pct, iso):
        frac = (pct or 0) / 100.0
        pct_str = f"{int(round(pct or 0))}%"
        lbl = short if compact else label
        head = f"{BOLD}{gray}{lbl}{RESET} {BOLD}{white}{pct_str}{RESET}"
        return (f"{head}{micro_bar(frac, c.ctx_bar, bands)}"
                f"{white}{fmt_remaining(iso)}{RESET}")
    return (seg("session", "s", five.get("utilization") or 0, five.get("resets_at", ""))
            + "  "
            + seg("week", "w", week.get("utilization") or 0, week.get("resets_at", "")))


def get_peak_label(c):
    """
    Determine if current Budapest time is peak or off-peak.

    Peak hours: weekdays (Mon-Fri), 14:00-20:00 Budapest time.
    This corresponds to 5:00 AM - 11:00 AM Pacific Time (PT), regardless of
    whether PT is on PDT (UTC-7) or PST (UTC-8), because Budapest's own DST
    offset compensates: the window is always 14:00-20:00 local Budapest time.

    Returns a colored status string.
    """
    # Determine current Budapest offset: CEST (UTC+2) Mar last Sun -> Oct last Sun,
    # otherwise CET (UTC+1).  We approximate with a fixed-offset check:
    # if month is in [4..9] it is definitely summer; for Mar/Oct we check the
    # last Sunday transition.
    now_utc = datetime.now(timezone.utc)
    year = now_utc.year
    month = now_utc.month

    def last_sunday(y, m):
        """Return the last Sunday of the given month as a UTC-aware datetime at 01:00 UTC (transition moment)."""
        import calendar
        last_day = calendar.monthrange(y, m)[1]
        d = datetime(y, m, last_day, 1, 0, 0, tzinfo=timezone.utc)
        # Walk backwards to Sunday (weekday 6)
        while d.weekday() != 6:
            d = d.replace(day=d.day - 1)
        return d

    # CEST starts last Sunday of March at 01:00 UTC, ends last Sunday of October at 01:00 UTC
    cest_start = last_sunday(year, 3)
    cest_end   = last_sunday(year, 10)

    if cest_start <= now_utc < cest_end:
        budapest_offset = timedelta(hours=2)  # CEST
    else:
        budapest_offset = timedelta(hours=1)  # CET

    now_budapest = now_utc + budapest_offset
    weekday = now_budapest.weekday()   # 0=Monday, 6=Sunday
    hour    = now_budapest.hour
    minute  = now_budapest.minute

    is_weekday = weekday < 5  # Mon-Fri
    is_peak_hour = 14 <= hour < 20

    def fmt_remaining(total_minutes):
        """Format remaining time as ~Xh or ~Xm."""
        if total_minutes >= 60:
            return f"~{total_minutes // 60}h"
        return f"~{max(1, total_minutes)}m"

    if is_weekday and is_peak_hour:
        # Peak ends at 20:00 Budapest time
        remaining = (20 - hour - 1) * 60 + (60 - minute)
        c_peak = hex_to_ansi(COLORS['model'])   # use the model's red here
        c_usd = hex_to_ansi(COLORS['usd'])
        return f"{c_usd}{BOLD}${RESET}{c_peak}² ({fmt_remaining(remaining)}){RESET}"
    else:
        # Off-peak: calculate minutes until next peak (14:00 on next weekday)
        if is_weekday and hour < 14:
            # Same day, before peak
            remaining = (14 - hour - 1) * 60 + (60 - minute)
        elif is_weekday:
            # After 20:00 on weekday
            days_ahead = 1 if weekday < 4 else (7 - weekday)  # skip to Monday if Fri
            remaining = ((days_ahead - 1) * 24 + (24 - hour) + 14 - 1) * 60 + (60 - minute)
        else:
            # Weekend: days until Monday
            days_ahead = 7 - weekday  # Sat=5->2, Sun=6->1
            remaining = ((days_ahead - 1) * 24 + (24 - hour) + 14 - 1) * 60 + (60 - minute)

        c_usd = hex_to_ansi(COLORS['usd'])
        if remaining < 60:
            c_warn = hex_to_ansi(COLORS['offpeak_warn'])
            return f"{c_usd}{BOLD}${RESET}{c_warn}¹ ({fmt_remaining(remaining)}){RESET}"
        else:
            c_off = hex_to_ansi(COLORS['offpeak'])
            return f"{c_usd}{BOLD}${RESET}{c_off}¹ ({fmt_remaining(remaining)}){RESET}"


def get_effort(data):
    """Return the live reasoning-effort label (e.g. 'high', 'xhigh', 'max').

    Source is the statusline stdin field effort.level, which reflects the
    current session value including mid-session /effort changes. Returns ''
    when the model does not support the effort parameter (field absent).
    """
    effort = data.get("effort", {})
    if isinstance(effort, dict):
        level = effort.get("level")
        if level:
            return str(level)
    return ""

def get_data():
    """Read JSON data from stdin"""
    try:
        input_data = sys.stdin.read()
        return json.loads(input_data)
    except:
        return {}

def get_git_info(cwd):
    """Get git information"""
    try:
        os.chdir(cwd)
        # Branch name
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            stderr=subprocess.DEVNULL
        ).decode().strip()

        # Changes (added/removed)
        diff_stat = subprocess.check_output(
            ["git", "diff", "--shortstat", "HEAD"],
            stderr=subprocess.DEVNULL
        ).decode().strip()

        added = 0
        removed = 0
        if diff_stat:
            import re
            add_match = re.search(r'(\d+) insertion', diff_stat)
            del_match = re.search(r'(\d+) deletion', diff_stat)
            if add_match:
                added = int(add_match.group(1))
            if del_match:
                removed = int(del_match.group(1))

        return branch, added, removed
    except:
        return "", 0, 0

# ============================================
# MAIN LOGIC
# ============================================
def main():
    data = get_data()
    c = ColorAccessor()

    # External config: weather location, templates, emoji width, colors, bar glyph
    weather_cfg, templates, emoji_width, color_overrides, bar_empty = load_config()
    # Apply color overrides over the COLORS defaults (missing keys keep defaults).
    COLORS.update(color_overrides)

    # Extract values from Claude Code JSON structure
    time_str = datetime.now().strftime("%H:%M")
    version = f"v{data.get('version', '0.0.0')}"

    # Model
    model_info = data.get("model", {})
    model = model_info.get("display_name", "Unknown") if isinstance(model_info, dict) else str(model_info)

    # Context window
    ctx_window = data.get("context_window", {})
    ctx_size = ctx_window.get("context_window_size", 200000)
    ctx_percent = ctx_window.get("used_percentage")
    # If used_percentage is None/null, default to 0
    if ctx_percent is None:
        ctx_percent = 0

    # Current usage
    current_usage = ctx_window.get("current_usage", {}) or {}
    tokens_in = current_usage.get("input_tokens", 0)
    tokens_out = current_usage.get("output_tokens", 0)
    tokens_cached = current_usage.get("cache_read_input_tokens", 0) + current_usage.get("cache_creation_input_tokens", 0)

    # Totals
    total_in = ctx_window.get("total_input_tokens", 0)
    total_out = ctx_window.get("total_output_tokens", 0)
    tokens_total = total_in + total_out

    # Context used - use current_usage which reflects actual context window
    # NOT the cumulative totals (total_input_tokens/total_output_tokens)
    if current_usage:
        ctx_used = (
            current_usage.get("input_tokens", 0) +
            current_usage.get("cache_creation_input_tokens", 0) +
            current_usage.get("cache_read_input_tokens", 0)
        )
    else:
        ctx_used = 0

    # Path (full, untruncated): keep the ~ and separator normalization but never
    # cut it off, so the whole path is shown.
    cwd_raw = data.get("cwd", data.get("workspace", {}).get("current_dir", "~"))
    cwd = format_path(cwd_raw, max_len=10**9)

    # Git info (using raw path)
    branch, added, removed = get_git_info(cwd_raw)

    # Weather + sun state (location + show_* flags from config). Only hit the
    # network when an ACTIVE template line actually uses {weather} or {sun};
    # if neither is referenced, the fetch is skipped entirely so unused weather
    # costs nothing (no network, no latency).
    tpl_text = "".join(v for k, v in templates.items()
                       if k.startswith("line") and isinstance(v, str))
    need_weather = "{weather}" in tpl_text
    need_sun = "{sun}" in tpl_text
    weather, sun_icon = "", ""
    if need_weather or need_sun:
        raw = fetch_weather_raw(
            weather_cfg.get("latitude", 56.25),
            weather_cfg.get("longitude", -5.2833),
        )
        if need_weather:
            weather = format_weather(raw, weather_cfg.get("name", "Newyork"), weather_cfg)
        if need_sun:
            sun_icon = get_sun_icon(raw.get("sunrise", ""), raw.get("sunset", ""))

    # Reasoning effort (live session value, e.g. high/xhigh/max).
    # Wrapped in parens here so the template stays empty when unsupported.
    effort_level = get_effort(data)
    effort = f"({effort_level})" if effort_level else ""

    # Peak / off-peak label
    peak_label = get_peak_label(c)

    # Use the pre-calculated percentage from Claude Code (most accurate)
    # If not available, calculate from token counts
    if ctx_percent is not None and ctx_percent > 0:
        ctx_percent_precise = ctx_percent
    elif ctx_size > 0 and ctx_used > 0:
        ctx_percent_precise = (ctx_used / ctx_size) * 100
    else:
        ctx_percent_precise = 0

    # Convert the absolute-token CTX_BAR_BANDS into (lower_fraction, ansi_color)
    # stops for the bar. getattr(c, key) resolves the COLORS entry (already
    # merged with any config overrides), so the bands honor color overrides too.
    ctx_bands = ([(thr / ctx_size, getattr(c, key)) for thr, key in CTX_BAR_BANDS]
                 if ctx_size else None)

    # Template variables
    values = {
        "c": c,
        "r": RESET,
        "time": time_str,
        "version": version,
        "model": model,
        "effort": effort,
        "ctx": format_k(ctx_used),
        "ctx_percent": f"{ctx_percent_precise:.0f}",
        "ctx_bar": ProgressBar(
            ctx_percent_precise / 100.0, c.ctx_bar, c.ctx_bar_track,
            bands=ctx_bands,
        ),
        "total": format_k(tokens_total),
        "input": format_k(tokens_in),
        "output": format_k(tokens_out),
        "cached": format_k(tokens_cached),
        "path": cwd,
        "branch": branch,
        "added": added,
        "removed": removed,
        "weather": weather,
        "sun": sun_icon,
        "peak_label": peak_label,
    }

    # Usage data: fetched once here, reused for the big bars + micro lines below.
    # The compact micro must be in `values` before line1 is measured (it can sit
    # on line1 after Ctx), so compute it now.
    usage = get_usage()
    values["compact_usage_micro"] = build_usage_micro(usage, c, compact=True)
    # Micro version of the context gauge: just '▕<cell>▏', same color bands as
    # the big ctx bar.
    values["ctx_micro"] = micro_bar(ctx_percent_precise / 100.0, c.ctx_bar, ctx_bands)

    # --- Dynamic alignment of the context bar with line1 -------------------
    # Make line2's bar occupy the same columns as the line1 segment that
    # starts at {effort} (e.g. "(xhigh) [..] Ctx:.. (16%)") and runs to the
    # end of line1. Both the bar's start column and its length therefore
    # follow line1's live content (model name, effort, time, % all vary).
    #   {align_pad} -> spaces that push the bar to the segment's start column
    #   {ctx_bar}   -> auto-sized so [bar] spans start_col .. end of line1
    line1_tpl = templates.get("line1", "")
    start_col = 0
    total_width = 0
    try:
        if line1_tpl:
            total_width = display_width(line1_tpl.format(**values), emoji_width)
            if "{effort}" in line1_tpl:
                prefix = line1_tpl.split("{effort}", 1)[0].format(**values)
                start_col = display_width(prefix, emoji_width)
    except Exception:
        start_col, total_width = 0, 0

    # Place the bar so its RIGHT edge (▌) lands on line1's last column, and its
    # LEFT edge (▐) under the segment start when there's room. If the weather
    # text is wider than the segment start, the bar can't reach that far left,
    # so it starts just after the weather and shrinks (right edge stays aligned).
    if total_width <= 0:                       # no line1 to align to -> default
        pad = 1
        bar_inner = DEFAULT_BAR_WIDTH
    else:
        weather_w = display_width(weather, emoji_width)
        actual_start = start_col if start_col >= weather_w + 1 else weather_w + 1
        pad = actual_start - weather_w          # >= 1
        bar_inner = total_width - actual_start - 2
        if bar_inner < 1:
            bar_inner = 1
    values["align_pad"] = " " * pad
    values["ctx_bar"] = ProgressBar(
        ctx_percent_precise / 100.0, c.ctx_bar, c.ctx_bar_track,
        width=bar_inner, empty_char=bar_empty,
        bands=ctx_bands,
    )

    # --- Usage gauges (session + weekly), sized to line1's width --------------
    usage_bars, usage_resets = build_usage_lines(usage, total_width, c, bar_empty)
    values["usage_bars"] = usage_bars
    values["usage_resets"] = usage_resets
    values["usage_micro"] = build_usage_micro(usage, c)

    # Output - dynamically, only existing template lines
    # Iterate through possible lines in order (line1, line2, line3, ...)
    line_num = 1
    while True:
        line_key = f"line{line_num}"
        if line_key not in templates:
            # If no more lines, exit
            if line_num > 10:  # Max 10 lines supported
                break
            line_num += 1
            continue

        template = templates[line_key]

        # Special handling: if template contains git info but no repo exists, show path only
        if "{branch}" in template and not branch:
            print(f"{hex_to_ansi(COLORS['path'])}{cwd}{RESET}")
        else:
            rendered = template.format(**values)
            # Skip lines that render empty (e.g. usage lines when data is
            # unavailable) so we don't print blank rows.
            if strip_ansi(rendered).strip() != "":
                print(rendered)

        line_num += 1

if __name__ == "__main__":
    main()
