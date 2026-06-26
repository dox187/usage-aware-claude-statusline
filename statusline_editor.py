#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Statusline TUI Config Editor
============================

A Textual-based editor for ~/.claude/statusline_config.json (which drives
statusline.py). Lines are edited as rows of ELEMENT chips: each chip is a
placeholder (or literal text) and carries its own color — the raw template
plumbing ({c.NAME}...{r}) is hidden. Everything is keyboard-driven:

  Tab / Shift+Tab   switch the top tabs (Lines / Colors / Settings). The tab
                    strip is never focused itself - the active tab's content is
                    always the focused "bottom block".
  ←/→/↑/↓        navigate between elements / lines
  Alt+←/→        move the selected element within its line
  Alt+↑/↓        move the selected element to the line above / below
  Shift+↑/↓      move the whole line up / down
  Shift+A        add an element (after the selected one, else at line end)
  Shift+D        delete the selected element (asks first)
  E / Enter      edit the element (color, ctx_bar width, or literal text)
  Shift+N        new line · Shift+X delete line (asks) · Shift+T on/off
  Ctrl+Z         undo (every change) · Ctrl+S save · Ctrl+Q quit · F5 preview

The SETTINGS tab is a keyboard-driven list: ↑/↓ move, Enter edits a value /
toggles a boolean / runs an action, on a numeric row you can just start TYPING a
number (a ',' is accepted and turned into '.'), and Shift+↑/↓ nudges a number by
±1. The weather section has its own show_* toggles and a "Look up coordinates"
action that geocodes the city name into latitude/longitude.

A context-sensitive key legend (only what's available now) sits under the lines,
and a LIVE preview is pinned at the bottom.

Requires: pip install textual   (the statusline itself stays stdlib-only)
Run:      python ~/.claude/statusline_editor.py   (or: py ...)

Nothing is written to statusline_config.json until you press Ctrl+S.
"""
import colorsys
import copy
import json
import os
import re
import subprocess
import sys
import tempfile

from rich.text import Text

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Footer, Header, Input, Label, OptionList, Static,
    TabbedContent, TabPane, Tabs,
)
from textual.widgets.option_list import Option

HERE = os.path.dirname(os.path.abspath(__file__))
STATUSLINE_PY = os.path.join(HERE, "statusline.py")
CONFIG_PATH = os.path.join(HERE, "statusline_config.json")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
TOKEN_RE = re.compile(r"\{[^}]*\}")

DEFAULT_SAMPLE = {
    "version": "2.0.0",
    "model": {"display_name": "Opus 4.8"},
    "effort": {"level": "high"},
    "context_window": {
        "context_window_size": 1000000,
        "used_percentage": 18,
        "current_usage": {
            "input_tokens": 5000,
            "cache_read_input_tokens": 170000,
            "output_tokens": 2000,
        },
    },
    "cwd": os.path.join(os.path.expanduser("~"), "workspace", "claude_chat"),
}

DATA_PLACEHOLDERS = [
    ("{time}", "clock HH:MM"),
    ("{version}", "claude version"),
    ("{model}", "model display name"),
    ("{effort}", "reasoning effort, e.g. (high)"),
    ("{peak_label}", "peak/off-peak $ rate label"),
    ("{ctx}", "context tokens (k/M)"),
    ("{ctx_percent}", "context usage % (number only)"),
    ("{ctx_bar}", "context gauge (param: fixed width N)"),
    ("{ctx_micro}", "compact context gauge"),
    ("{align_pad}", "spaces aligning a line-2 bar under line 1"),
    ("{usage_micro}", "session+week micro gauges (full labels)"),
    ("{compact_usage_micro}", "session+week micro gauges (s/w)"),
    ("{usage_bars}", "big session+week bars"),
    ("{usage_resets}", "labels + reset dates under big bars"),
    ("{total}", "total tokens"),
    ("{input}", "input tokens"),
    ("{output}", "output tokens"),
    ("{cached}", "cached tokens"),
    ("{path}", "current directory"),
    ("{branch}", "git branch"),
    ("{added}", "git +lines"),
    ("{removed}", "git -lines"),
    ("{weather}", "weather text"),
    ("{sun}", "sun/moon icon"),
]
PARAM_BASES = {"ctx_bar"}

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"


def geocode(name, count=8, timeout=6):
    """Look up a place name via the Open-Meteo geocoding API (stdlib only).

    Returns a list of result dicts (possibly empty) with at least the keys
    name/latitude/longitude/country/admin1 (plus population/timezone when the
    API supplies them). Returns [] for empty input, zero matches, or ANY
    network/HTTP/timeout/parse error - it never raises, so the editor stays up.
    """
    if not name or not name.strip():
        return []
    import urllib.error
    import urllib.parse
    import urllib.request
    params = urllib.parse.urlencode({
        "name": name,            # urlencode handles non-ASCII -> UTF-8 %-encoding
        "count": count,
        "language": "en",
        "format": "json",
    })
    url = "%s?%s" % (GEOCODE_URL, params)
    req = urllib.request.Request(url, headers={"User-Agent": "statusline-editor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ValueError, OSError):
        return []
    out = []
    for r in (data.get("results") or []):   # 'results' is absent on 0 matches
        out.append({
            "name": r.get("name"),
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
            "country": r.get("country"),
            "admin1": r.get("admin1"),
            "population": r.get("population"),
            "timezone": r.get("timezone"),
        })
    return out


# --------------------------------------------------------------------------- #
# Settings tab schema
# --------------------------------------------------------------------------- #
# Each row is one setting. "section" rows are non-selectable headers. The rest
# carry a kind (bool/int/float/text/action) and a key path into the config dict
# (or the special ("__preview__", ...) path for fields that only tweak the live
# preview sample, never the saved config).
SETTINGS_SPEC = [
    {"kind": "section", "label": "WEATHER"},
    {"kind": "text",   "key": ("weather", "name"),       "label": "City name",          "default": "Newyork"},
    {"kind": "action", "action": "geocode",              "label": "↳ Look up coordinates from the city name"},
    {"kind": "float",  "key": ("weather", "latitude"),   "label": "Latitude",           "default": 56.25},
    {"kind": "float",  "key": ("weather", "longitude"),  "label": "Longitude",          "default": -5.2833},
    {"kind": "bool",   "key": ("weather", "show_name"),  "label": "Show city name",     "default": True},
    {"kind": "bool",   "key": ("weather", "show_icon"),  "label": "Show weather icon",  "default": True},
    {"kind": "bool",   "key": ("weather", "show_temp"),  "label": "Show temperature",   "default": True},
    {"kind": "bool",   "key": ("weather", "show_humidity"), "label": "Show humidity",   "default": True},
    {"kind": "bool",   "key": ("weather", "show_wind"),  "label": "Show wind",          "default": True},
    {"kind": "section", "label": "GENERAL"},
    {"kind": "int",    "key": ("emoji_width",),          "label": "Emoji width (1 or 2)", "default": 2, "min": 1, "max": 2},
    {"kind": "text",   "key": ("ctx_bar_empty",),        "label": "Context-bar empty glyph", "default": "░", "maxlen": 1, "nonempty": True},
    {"kind": "section", "label": "PREVIEW SAMPLE  (affects the live preview only, not the saved config)"},
    {"kind": "int",    "key": ("__preview__", "ctx_percent"), "label": "Context used %", "default": 18, "min": 0, "max": 100},
    {"kind": "text",   "key": ("__preview__", "model"),  "label": "Model display name", "default": "Opus 4.8"},
]


# --------------------------------------------------------------------------- #
# template <-> elements
# --------------------------------------------------------------------------- #
def parse_template(t: str) -> list[dict]:
    elements: list[dict] = []
    cur_color = None
    pending = ""
    pos = 0

    def flush():
        nonlocal pending
        if pending:
            elements.append({"type": "text", "value": pending, "color": cur_color})
            pending = ""

    for m in TOKEN_RE.finditer(t):
        if m.start() > pos:
            pending += t[pos:m.start()]
        inner = m.group(0)[1:-1]
        if inner.startswith("c."):
            flush()
            cur_color = inner[2:]
        elif inner == "r":
            flush()
            cur_color = None
        else:
            flush()
            elements.append({"type": "ph", "value": m.group(0), "color": cur_color})
        pos = m.end()
    if pos < len(t):
        pending += t[pos:]
    flush()
    return elements


def serialize_elements(elements: list[dict]) -> str:
    out = ""
    i, n = 0, len(elements)
    while i < n:
        color = elements[i].get("color")
        buf = ""
        while i < n and elements[i].get("color") == color:
            buf += elements[i]["value"]
            i += 1
        out += f"{{c.{color}}}{buf}{{r}}" if color else buf
    return out


def chip_label(e: dict) -> str:
    if e["type"] == "text":
        return e["value"].replace(" ", "·") or "·"
    return e["value"].strip("{}")


def elem_base(e: dict) -> str:
    return e["value"].strip("{}").split(":")[0]


# --------------------------------------------------------------------------- #
# modals
# --------------------------------------------------------------------------- #
class ParamScreen(ModalScreen[str]):
    def __init__(self, base: str) -> None:
        super().__init__()
        self.base = base

    def compose(self) -> ComposeResult:
        with Vertical(id="param-box"):
            yield Label(f"Fixed width for {{{self.base}}} — empty = auto")
            yield Input(placeholder="e.g. 30, or empty", id="param-input")
            with Horizontal(id="param-buttons"):
                yield Button("OK", variant="primary", id="param-ok")
                yield Button("Cancel", id="param-cancel")

    def on_mount(self) -> None:
        self.query_one("#param-input", Input).focus()

    @on(Button.Pressed, "#param-cancel")
    def _cancel(self) -> None:
        self.dismiss("{%s}" % self.base)

    @on(Button.Pressed, "#param-ok")
    @on(Input.Submitted, "#param-input")
    def _ok(self) -> None:
        v = self.query_one("#param-input", Input).value.strip()
        self.dismiss("{%s:%s}" % (self.base, v) if v.isdigit() else "{%s}" % self.base)


class PromptScreen(ModalScreen[str]):
    def __init__(self, label: str, value: str = "") -> None:
        super().__init__()
        self.label = label
        self.value = value

    def compose(self) -> ComposeResult:
        with Vertical(id="param-box"):
            yield Label(self.label)
            yield Input(value=self.value, id="prompt-input")
            with Horizontal(id="param-buttons"):
                yield Button("OK", variant="primary", id="prompt-ok")
                yield Button("Cancel", id="prompt-cancel")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    @on(Button.Pressed, "#prompt-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#prompt-ok")
    @on(Input.Submitted, "#prompt-input")
    def _ok(self) -> None:
        self.dismiss(self.query_one("#prompt-input", Input).value)


class NumberEditScreen(ModalScreen[str]):
    """Numeric entry. Accepts digits, a sign and a decimal separator; a typed
    comma is converted to a dot live (so 56,25 -> 56.25). Returns the cleaned
    string (still '.'-decimal); the caller does the final parse/clamp."""
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, label: str, value: str = "", is_float: bool = True) -> None:
        super().__init__()
        self.label = label
        self.init = value
        self.is_float = is_float
        self._sync = False

    def compose(self) -> ComposeResult:
        with Vertical(id="param-box"):
            yield Label(self.label)
            yield Input(value=self.init, id="num-input")
            hint = "Enter = save · Esc = cancel · ',' becomes '.'" if self.is_float \
                else "Enter = save · Esc = cancel"
            yield Label(hint, classes="hint")
            with Horizontal(id="param-buttons"):
                yield Button("OK", variant="primary", id="num-ok")
                yield Button("Cancel", id="num-cancel")

    def on_mount(self) -> None:
        inp = self.query_one("#num-input", Input)
        inp.focus()
        inp.cursor_position = len(inp.value)

    @on(Input.Changed, "#num-input")
    def _live(self, event: Input.Changed) -> None:
        if self._sync or "," not in event.value:
            return
        inp = self.query_one("#num-input", Input)
        pos = inp.cursor_position
        self._sync = True
        inp.value = event.value.replace(",", ".")
        inp.cursor_position = pos
        self._sync = False

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#num-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#num-ok")
    @on(Input.Submitted, "#num-input")
    def _ok(self) -> None:
        self.dismiss(self.query_one("#num-input", Input).value.replace(",", ".").strip())


class GeocodeScreen(ModalScreen[int]):
    """Pick one of the geocoding results for a city name; returns its index."""
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, query: str, results: list[dict]) -> None:
        super().__init__()
        self.query_str = query
        self.results = results

    def compose(self) -> ComposeResult:
        with Vertical(id="line-box"):
            yield Label(f"Results for “{self.query_str}” — ↑/↓ then Enter:")
            opts = []
            for i, r in enumerate(self.results):
                bits = [str(r.get("name") or "?")]
                if r.get("admin1"):
                    bits.append(str(r["admin1"]))
                if r.get("country"):
                    bits.append(str(r["country"]))
                coord = f"({r.get('latitude')}, {r.get('longitude')})"
                opts.append(Option(f"{', '.join(bits)}   {coord}", id=str(i)))
            yield OptionList(*opts, id="geo-list")

    def on_mount(self) -> None:
        self.query_one("#geo-list", OptionList).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(OptionList.OptionSelected, "#geo-list")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(int(event.option.id))


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "no", "No"), Binding("y", "yes", "Yes")]

    def __init__(self, msg: str) -> None:
        super().__init__()
        self.msg = msg

    def compose(self) -> ComposeResult:
        with Vertical(id="param-box"):
            yield Label(self.msg)
            with Horizontal(id="param-buttons"):
                yield Button("Delete", variant="error", id="confirm-yes")
                yield Button("Cancel", variant="primary", id="confirm-no")

    def on_mount(self) -> None:
        self.query_one("#confirm-no", Button).focus()

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm-yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def _no(self) -> None:
        self.dismiss(False)


class AddElementScreen(ModalScreen[dict]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="line-box"):
            yield Label("Add element — pick a placeholder or literal text:")
            opts = [Option("✎  Literal text…", id="__text__")]
            opts += [Option(f"{n}   — {h}", id=n) for n, h in DATA_PLACEHOLDERS]
            yield OptionList(*opts, id="add-list")

    def on_mount(self) -> None:
        self.query_one("#add-list", OptionList).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(OptionList.OptionSelected, "#add-list")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        # Return only the chosen id; the app drives any follow-up prompt so we
        # never dismiss one modal from inside another's callback (re-entrancy).
        self.dismiss(event.option.id)


class ElementEditorScreen(ModalScreen[dict]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, element: dict, color_rows: list[list]) -> None:
        super().__init__()
        self.element = dict(element)
        self.color_rows = color_rows           # [[key, hex], ...]
        self.color_keys = [k for k, _ in color_rows]

    def _color_option(self, key: str, hx: str) -> Option:
        prompt = Text()
        if HEX_RE.match(hx or ""):
            prompt.append("██ ", style=hx)
        else:
            prompt.append("?? ")
        prompt.append(key)
        return Option(prompt, id=key)

    def compose(self) -> ComposeResult:
        e = self.element
        with Vertical(id="line-box"):
            yield Label(f"Edit element:  {chip_label(e)}")
            if e["type"] == "text":
                yield Label("Literal text:")
                yield Input(value=e["value"], id="ee-text")
                yield Label("Color (↑/↓ to choose, shown in its own color):")
                opts = [Option("   (none)", id="__none__")]
                opts += [self._color_option(k, v) for k, v in self.color_rows]
                yield OptionList(*opts, id="ee-color")
            elif elem_base(e) in PARAM_BASES:
                cur = e["value"].strip("{}")
                yield Label("Fixed width (empty = auto):")
                yield Input(value=cur.split(":")[1] if ":" in cur else "", id="ee-width")
            else:
                yield Label("This placeholder has no editable options.", classes="hint")
            with Horizontal(id="line-buttons"):
                yield Button("Save", variant="primary", id="ee-ok")
                yield Button("Cancel", id="ee-cancel")

    def on_mount(self) -> None:
        if self.element["type"] == "text":
            ol = self.query_one("#ee-color", OptionList)
            cur = self.element.get("color")
            ol.highlighted = (self.color_keys.index(cur) + 1) if cur in self.color_keys else 0
            self.query_one("#ee-text", Input).focus()
        elif elem_base(self.element) in PARAM_BASES:
            self.query_one("#ee-width", Input).focus()
        else:
            self.query_one("#ee-cancel", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#ee-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#ee-ok")
    def _ok(self) -> None:
        e = dict(self.element)
        if e["type"] == "text":
            e["value"] = self.query_one("#ee-text", Input).value
            idx = self.query_one("#ee-color", OptionList).highlighted or 0
            e["color"] = None if idx == 0 else self.color_keys[idx - 1]
        elif elem_base(e) in PARAM_BASES:
            w = self.query_one("#ee-width", Input).value.strip()
            base = elem_base(e)
            e["value"] = "{%s:%s}" % (base, w) if w.isdigit() else "{%s}" % base
        self.dismiss(e)


class SpectrumPicker(Static):
    """2D rainbow picker: hue across, lightness down. Mouse-clickable and
    cursor-navigable; reports the picked RGB to the ColorPickerScreen."""
    can_focus = True
    COLS = 24
    ROWS = 9
    BINDINGS = [
        Binding("left", "mv(-1, 0)", "◀", show=False),
        Binding("right", "mv(1, 0)", "▶", show=False),
        Binding("up", "mv(0, -1)", "▲", show=False),
        Binding("down", "mv(0, 1)", "▼", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(id="cp-spectrum")
        self.cx = self.COLS // 2
        self.cy = self.ROWS // 2

    def cell_rgb(self, col: int, row: int):
        h = col / (self.COLS - 1)
        l = 1.0 - row / (self.ROWS - 1)
        r, g, b = colorsys.hls_to_rgb(h, l, 1.0)
        return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))

    def _grid(self) -> Text:
        t = Text()
        for row in range(self.ROWS):
            for col in range(self.COLS):
                r, g, b = self.cell_rgb(col, row)
                bg = "#%02x%02x%02x" % (r, g, b)
                if col == self.cx and row == self.cy:
                    lum = 0.299 * r + 0.587 * g + 0.114 * b
                    fg = "#000000" if lum > 140 else "#ffffff"
                    t.append("[]", style=f"{fg} on {bg}")
                else:
                    t.append("  ", style=f"on {bg}")
            t.append("\n")
        return t

    def on_mount(self) -> None:
        self.update(self._grid())

    def _apply(self) -> None:
        self.update(self._grid())
        scr = self.screen
        if hasattr(scr, "set_from_picker"):
            scr.set_from_picker(*self.cell_rgb(self.cx, self.cy))

    def action_mv(self, dx: int, dy: int) -> None:
        self.cx = max(0, min(self.COLS - 1, self.cx + dx))
        self.cy = max(0, min(self.ROWS - 1, self.cy + dy))
        self._apply()

    def on_click(self, event) -> None:
        col = (event.screen_x - self.region.x) // 2
        row = event.screen_y - self.region.y
        if 0 <= col < self.COLS and 0 <= row < self.ROWS:
            self.cx, self.cy = col, row
            self.focus()
            self._apply()


class ColorPickerScreen(ModalScreen[str]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "bump(1)", "+1", show=False),
        Binding("down", "bump(-1)", "-1", show=False),
        Binding("pageup", "bump(10)", "+10", show=False),
        Binding("pagedown", "bump(-10)", "-10", show=False),
    ]

    def __init__(self, key: str, hex_value: str) -> None:
        super().__init__()
        self.key = key
        if not HEX_RE.match(hex_value or ""):
            hex_value = "#ffffff"
        self.r = int(hex_value[1:3], 16)
        self.g = int(hex_value[3:5], 16)
        self.b = int(hex_value[5:7], 16)
        self._syncing = False

    def compose(self) -> ComposeResult:
        with Vertical(id="cp-box"):
            yield Label(f"Color: {self.key}", id="cp-title")
            yield Label("Click or use ←→↑↓ on the spectrum; fine-tune with R/G/B:",
                        classes="hint")
            yield SpectrumPicker()
            with Horizontal(id="cp-row"):
                yield Input(value=str(self.r), id="cp-r")
                yield Input(value=str(self.g), id="cp-g")
                yield Input(value=str(self.b), id="cp-b")
                yield Input(value=self._hex(), id="cp-hex")
            yield Static("", id="cp-swatch")
            yield Static("", id="cp-sample")
            yield Label("↑/↓ ±1 · PgUp/PgDn ±10 on the focused R/G/B box", id="cp-hint")
            with Horizontal(id="cp-buttons"):
                yield Button("Apply", variant="primary", id="cp-ok")
                yield Button("Cancel", id="cp-cancel")

    def on_mount(self) -> None:
        self.query_one(SpectrumPicker).focus()
        self._refresh()

    def set_from_picker(self, r: int, g: int, b: int) -> None:
        self.r, self.g, self.b = r, g, b
        self._syncing = True
        self.query_one("#cp-r", Input).value = str(r)
        self.query_one("#cp-g", Input).value = str(g)
        self.query_one("#cp-b", Input).value = str(b)
        self._syncing = False
        self._refresh()

    def _hex(self) -> str:
        return "#%02x%02x%02x" % (self.r, self.g, self.b)

    def _refresh(self) -> None:
        hx = self._hex()
        try:
            col = Color(self.r, self.g, self.b)
        except Exception:
            return
        sw = self.query_one("#cp-swatch", Static)
        sw.update("")
        sw.styles.background = col
        self.query_one("#cp-sample", Static).update(
            Text(f"  sample: the quick brown fox  {hx}  ", style=hx))
        self._syncing = True
        self.query_one("#cp-hex", Input).value = hx
        self._syncing = False

    @on(Input.Changed, "#cp-r")
    @on(Input.Changed, "#cp-g")
    @on(Input.Changed, "#cp-b")
    def _rgb_changed(self, event: Input.Changed) -> None:
        if self._syncing:
            return
        try:
            v = max(0, min(255, int(event.value or 0)))
        except ValueError:
            return
        setattr(self, event.input.id[-1], v)
        self._refresh()

    @on(Input.Changed, "#cp-hex")
    def _hex_changed(self, event: Input.Changed) -> None:
        if self._syncing:
            return
        hx = event.value.strip()
        if not HEX_RE.match(hx):
            return
        self.r, self.g, self.b = int(hx[1:3], 16), int(hx[3:5], 16), int(hx[5:7], 16)
        self._syncing = True
        self.query_one("#cp-r", Input).value = str(self.r)
        self.query_one("#cp-g", Input).value = str(self.g)
        self.query_one("#cp-b", Input).value = str(self.b)
        self._syncing = False
        self._refresh()

    def action_bump(self, delta: int) -> None:
        f = self.focused
        if not isinstance(f, Input) or f.id not in ("cp-r", "cp-g", "cp-b"):
            return
        ch = f.id[-1]
        setattr(self, ch, max(0, min(255, getattr(self, ch) + delta)))
        self._syncing = True
        f.value = str(getattr(self, ch))
        self._syncing = False
        self._refresh()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cp-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cp-ok")
    def _ok(self) -> None:
        self.dismiss(self._hex())


# --------------------------------------------------------------------------- #
# widgets
# --------------------------------------------------------------------------- #
class ColorsTable(DataTable):
    BINDINGS = [
        Binding("a", "add", "Add key"),
        Binding("d", "delete", "Delete key"),
        Binding("tab", "app.next_tab", "next tab", show=False),
        Binding("shift+tab", "app.prev_tab", "prev tab", show=False),
    ]

    def action_add(self) -> None: self.app.colors_add()
    def action_delete(self) -> None: self.app.colors_delete()


class LinesView(Static):
    """Focusable, keyboard-driven view of all lines as element chips."""
    can_focus = True
    BINDINGS = [
        Binding("left", "nav(-1)", "prev"),
        Binding("right", "nav(1)", "next"),
        Binding("up", "navline(-1)", "up"),
        Binding("down", "navline(1)", "down"),
        Binding("alt+left", "move(-1)", "move ◀"),
        Binding("alt+right", "move(1)", "move ▶"),
        Binding("alt+up", "moverow(-1)", "to line ↑"),
        Binding("alt+down", "moverow(1)", "to line ↓"),
        Binding("shift+up", "lineup", "line ↑"),
        Binding("shift+down", "linedown", "line ↓"),
        Binding("A", "addelem", "add element"),
        Binding("D", "delelem", "delete element"),
        Binding("e", "editelem", "edit"),
        Binding("enter", "editelem", "edit", show=False),
        Binding("N", "newline", "new line"),
        Binding("X", "delline", "delete line"),
        Binding("T", "toggleline", "on/off"),
        Binding("tab", "app.next_tab", "next tab", show=False),
        Binding("shift+tab", "app.prev_tab", "prev tab", show=False),
    ]

    def action_nav(self, d): self.app.nav(d)
    def action_navline(self, d): self.app.nav_line(d)
    def action_move(self, d): self.app.move_elem(d)
    def action_moverow(self, d): self.app.move_elem_row(d)
    def action_lineup(self): self.app.line_move(-1)
    def action_linedown(self): self.app.line_move(1)
    def action_addelem(self): self.app.add_elem()
    def action_delelem(self): self.app.del_elem()
    def action_editelem(self): self.app.edit_elem()
    def action_newline(self): self.app.new_line()
    def action_delline(self): self.app.del_line()
    def action_toggleline(self): self.app.toggle_line()


class SettingsView(Static):
    """Focusable, keyboard-driven settings list (no raw input boxes).

    ↑/↓ move the selection · Enter edits the value or toggles a bool or runs an
    action · Space toggles a bool · on a numeric row just start TYPING a number
    (the editor pops up pre-seeded) · Shift+↑/↓ nudges a numeric value by ±1 ·
    Tab/Shift+Tab switch the top tabs.
    """
    can_focus = True
    BINDINGS = [
        Binding("up", "nav(-1)", "up"),
        Binding("down", "nav(1)", "down"),
        Binding("enter", "activate", "edit / toggle"),
        Binding("space", "activate", "edit / toggle", show=False),
        Binding("shift+up", "bump(1)", "+1"),
        Binding("shift+down", "bump(-1)", "-1"),
        Binding("tab", "app.next_tab", "next tab", show=False),
        Binding("shift+tab", "app.prev_tab", "prev tab", show=False),
    ]

    def action_nav(self, d): self.app.settings_nav(d)
    def action_activate(self): self.app.settings_activate()
    def action_bump(self, d): self.app.settings_bump(d)

    def on_key(self, event) -> None:
        # Unbound keys land here (bindings are checked first by the App). On a
        # numeric row, a typed digit/sign/decimal opens the editor pre-seeded.
        self.app.settings_typed(event)


# --------------------------------------------------------------------------- #
# main app
# --------------------------------------------------------------------------- #
class StatuslineEditor(App):
    CSS = """
    TabbedContent { height: 1fr; }
    #lines-view { height: 1fr; padding: 0 1; }
    .chip-sel { }
    #help { height: auto; color: $text-muted; padding: 0 1; border-top: solid $panel; }
    #colors-table { height: 1fr; }
    #settings-pane { height: 1fr; }
    #settings-view { height: auto; padding: 0 1; }
    #line-box, #cp-box, #param-box {
        width: 84%; height: auto; max-height: 92%;
        border: thick $accent; background: $surface; padding: 1 2; }
    #param-box { width: 64; }
    #add-list, #ee-color, #geo-list { height: 14; }
    #cp-box { overflow-y: auto; }
    #cp-spectrum { height: auto; width: auto; }
    #cp-row Input { width: 12; }
    #cp-swatch { height: 1; border: round $foreground; }
    #preview-box { height: auto; max-height: 10; border: round $accent; padding: 0 1; }
    #preview-title { color: $accent; text-style: bold; }
    #preview { height: auto; }
    .hint { color: $text-muted; }
    """
    TAB_IDS = ["tab-lines", "tab-colors", "tab-settings"]
    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+z", "undo", "Undo"),
        Binding("ctrl+q", "quit_check", "Quit"),
        Binding("f5", "refresh_preview", "Preview"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg: dict = {}
        self.tpl_comment = None
        self.colors_comment = None
        self.lines: list[dict] = []
        self.color_rows: list[list] = []
        self.cur_line = 0
        self.cur_elem = None
        self.cur_setting = 0
        self._settings_sel_y = 0
        self.undo: list = []
        self.dirty = False

    # ---- model ----------------------------------------------------------- #
    def load(self) -> None:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            self.cfg = json.load(f)
        tpl = self.cfg.get("templates", {})
        self.tpl_comment = tpl.get("_comment")
        self.lines = []
        for k, v in tpl.items():
            if k == "_comment":
                continue
            self.lines.append({"name": k, "enabled": k.startswith("line"),
                               "elements": parse_template(v)})
        cols = self.cfg.get("colors", {})
        self.colors_comment = cols.get("_comment")
        self.color_rows = [[k, v] for k, v in cols.items() if k != "_comment"]
        self.cur_line, self.cur_elem = 0, (0 if self.lines and self.lines[0]["elements"] else None)

    def build_config_dict(self) -> dict:
        cfg = dict(self.cfg)
        tpl = {}
        if self.tpl_comment is not None:
            tpl["_comment"] = self.tpl_comment
        n, disabled = 1, 0
        for e in self.lines:
            s = serialize_elements(e["elements"])
            if e["enabled"]:
                tpl[f"line{n}"] = s
                n += 1
            else:
                disabled += 1
                name = e["name"] if e["name"].startswith("_") else f"_disabled_{disabled}"
                tpl[name] = s
        cfg["templates"] = tpl
        cols = {}
        if self.colors_comment is not None:
            cols["_comment"] = self.colors_comment
        for k, v in self.color_rows:
            cols[k] = v
        cfg["colors"] = cols
        return cfg

    def color_keys(self) -> list[str]:
        return [k for k, _ in self.color_rows]

    def color_hex(self, key: str):
        for k, v in self.color_rows:
            if k == key:
                return v
        return None

    # ---- compose --------------------------------------------------------- #
    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="tab-lines"):
            with TabPane("Lines", id="tab-lines"):
                yield LinesView(id="lines-view")
                yield Static(id="help")
            with TabPane("Colors", id="tab-colors"):
                yield Static("Enter: edit color · a: add key · d: delete key", classes="hint")
                yield ColorsTable(id="colors-table", cursor_type="row")
            with TabPane("Settings", id="tab-settings"):
                yield Static(
                    "↑/↓ move · Enter edit/toggle · type a number to edit a value · "
                    "Shift+↑/↓ ±1 · Tab/Shift+Tab switch tabs", classes="hint")
                with VerticalScroll(id="settings-pane"):
                    yield SettingsView(id="settings-view")
        with Vertical(id="preview-box"):
            yield Static("Live preview", id="preview-title")
            yield Static("", id="preview")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Statusline Config Editor"
        self.sub_title = CONFIG_PATH
        self.load()
        # The top tab strip must never grab keyboard focus: the active tab's
        # content is always the focused "bottom block", and Tab/Shift+Tab switch
        # tabs (handled by the content widgets' bindings). Removing the tab bar
        # from the focus chain is what makes that reliable.
        for tabs in self.query(Tabs):
            tabs.can_focus = False
        # The Settings pane is a VerticalScroll, which is focusable by default;
        # take it out of the focus chain so focus always lands on SettingsView,
        # never the scroll wrapper (which would otherwise swallow Tab and every
        # settings key when a click in its padding/scrollbar focused it).
        try:
            self.query_one("#settings-pane", VerticalScroll).can_focus = False
        except Exception:  # noqa: BLE001
            pass
        self.rebuild_lines()
        self._fill_colors_table()
        self.rebuild_settings()
        self.query_one(LinesView).focus()
        self.update_preview()

    # ---- tab switching (Tab / Shift+Tab; the tab bar itself is never focused) #
    def action_next_tab(self) -> None:
        self._cycle_tab(1)

    def action_prev_tab(self) -> None:
        self._cycle_tab(-1)

    def _cycle_tab(self, d: int) -> None:
        tc = self.query_one(TabbedContent)
        cur = tc.active
        i = self.TAB_IDS.index(cur) if cur in self.TAB_IDS else 0
        tc.active = self.TAB_IDS[(i + d) % len(self.TAB_IDS)]

    @on(TabbedContent.TabActivated)
    def _tab_activated(self, event: TabbedContent.TabActivated) -> None:
        # Keep focus on the active tab's content (the "bottom block"), never the
        # tab strip. call_after_refresh so the pane has been switched in first.
        pane_id = event.pane.id
        self.call_after_refresh(lambda: self._focus_tab_content(pane_id))

    def _focus_tab_content(self, pane_id: str) -> None:
        target = {
            "tab-lines": LinesView,
            "tab-colors": ColorsTable,
            "tab-settings": SettingsView,
        }.get(pane_id)
        if target is not None:
            try:
                self.query_one(target).focus()
            except Exception:  # noqa: BLE001
                pass

    # ---- lines rendering ------------------------------------------------- #
    def _cur(self):
        return self.lines[self.cur_line] if 0 <= self.cur_line < len(self.lines) else None

    def rebuild_lines(self) -> None:
        self.query_one(LinesView).update(self._render_lines())
        self.query_one("#help", Static).update(self._help_text())

    def _render_lines(self) -> Text:
        t = Text()
        n = 1
        for li, line in enumerate(self.lines):
            enabled = line["enabled"]
            tag = f"L{n}" if enabled else "off"
            if enabled:
                n += 1
            sel_line = (li == self.cur_line)
            tag_style = "reverse bold" if sel_line and self.cur_elem is None else (
                "bold" if enabled else "dim")
            t.append(f" {tag} ", style=tag_style)
            t.append("  ")
            if line["elements"]:
                for ei, el in enumerate(line["elements"]):
                    hx = self.color_hex(el["color"]) if el.get("color") else None
                    style = hx if (hx and HEX_RE.match(hx)) else ""
                    if el["type"] == "text":
                        style = (style + " italic").strip()
                    if sel_line and ei == self.cur_elem:
                        style = (style + " reverse bold").strip()
                    t.append(chip_label(el), style=style or None)
                    t.append("  ")
            else:
                t.append("(empty)", style="dim italic")
            t.append("\n")
        return t

    def _help_text(self) -> str:
        ln = self._cur()
        has_elem = ln is not None and self.cur_elem is not None and bool(ln["elements"])
        items = ["←→↑↓ navigate"]
        if has_elem:
            items += ["Alt+←→ move", "Alt+↑↓ to line", "E edit", "Shift+D delete"]
        items += ["Shift+A add element"]
        if ln is not None:
            items += ["Shift+↑↓ move line", "Shift+T on/off", "Shift+X del line"]
        items += ["Shift+N new line", "Ctrl+Z undo", "Ctrl+S save", "Ctrl+Q quit"]
        return "  ·  ".join(items)

    # ---- undo ------------------------------------------------------------ #
    def push_undo(self) -> None:
        self.undo.append((copy.deepcopy(self.lines), copy.deepcopy(self.color_rows),
                          copy.deepcopy(self.cfg), self.cur_line, self.cur_elem))
        if len(self.undo) > 100:
            self.undo.pop(0)

    def action_undo(self) -> None:
        if not self.undo:
            self.notify("Nothing to undo")
            return
        self.lines, self.color_rows, self.cfg, self.cur_line, self.cur_elem = self.undo.pop()
        self.dirty = True
        self.rebuild_lines()
        self._fill_colors_table()
        self.rebuild_settings()
        self.update_preview()

    def _changed(self) -> None:
        self._mark_dirty()
        self.rebuild_lines()
        self.update_preview()

    # ---- navigation (no undo) ------------------------------------------- #
    def _positions(self):
        pos = []
        for li, line in enumerate(self.lines):
            if line["elements"]:
                pos += [(li, ei) for ei in range(len(line["elements"]))]
            else:
                pos.append((li, None))
        return pos

    def nav(self, d: int) -> None:
        pos = self._positions()
        if not pos:
            return
        cur = (self.cur_line, self.cur_elem)
        i = pos.index(cur) if cur in pos else 0
        i = max(0, min(len(pos) - 1, i + d))
        self.cur_line, self.cur_elem = pos[i]
        self.rebuild_lines()

    def nav_line(self, d: int) -> None:
        j = self.cur_line + d
        if 0 <= j < len(self.lines):
            self.cur_line = j
            els = self.lines[j]["elements"]
            self.cur_elem = min(self.cur_elem if self.cur_elem is not None else 0,
                                len(els) - 1) if els else None
            self.rebuild_lines()

    # ---- element ops ----------------------------------------------------- #
    def move_elem(self, d: int) -> None:
        ln = self._cur()
        if ln is None or self.cur_elem is None:
            return
        i, j = self.cur_elem, self.cur_elem + d
        if 0 <= j < len(ln["elements"]):
            self.push_undo()
            ln["elements"][i], ln["elements"][j] = ln["elements"][j], ln["elements"][i]
            self.cur_elem = j
            self._changed()

    def move_elem_row(self, d: int) -> None:
        ln = self._cur()
        tgt = self.cur_line + d
        if ln is None or self.cur_elem is None or not (0 <= tgt < len(self.lines)):
            return
        self.push_undo()
        el = ln["elements"].pop(self.cur_elem)
        dest = self.lines[tgt]["elements"]
        pos = min(self.cur_elem, len(dest))
        dest.insert(pos, el)
        self.cur_line, self.cur_elem = tgt, pos
        self._changed()

    def add_elem(self) -> None:
        ln = self._cur()
        if ln is None:
            return

        def insert(el):
            if not el:
                return
            self.push_undo()
            pos = (self.cur_elem + 1) if self.cur_elem is not None else len(ln["elements"])
            ln["elements"].insert(pos, el)
            self.cur_elem = pos
            self._changed()

        def picked(oid):
            if not oid:
                return
            if oid == "__text__":
                self.push_screen(PromptScreen("Literal text (use spaces as needed):"),
                                 lambda v: insert({"type": "text", "value": v, "color": None}
                                                  if v else None))
            elif oid.strip("{}") in PARAM_BASES:
                self.push_screen(ParamScreen(oid.strip("{}")),
                                 lambda tok: insert({"type": "ph", "value": tok, "color": None}))
            else:
                insert({"type": "ph", "value": oid, "color": None})
        self.push_screen(AddElementScreen(), picked)

    def edit_elem(self) -> None:
        ln = self._cur()
        if ln is None or self.cur_elem is None or not ln["elements"]:
            return
        el = ln["elements"][self.cur_elem]

        def done(new):
            if new:
                self.push_undo()
                ln["elements"][self.cur_elem] = new
                self._changed()
        self.push_screen(ElementEditorScreen(el, self.color_rows), done)

    def del_elem(self) -> None:
        ln = self._cur()
        if ln is None or self.cur_elem is None or not ln["elements"]:
            return
        label = chip_label(ln["elements"][self.cur_elem])

        def cb(ok):
            if ok:
                self.push_undo()
                del ln["elements"][self.cur_elem]
                if not ln["elements"]:
                    self.cur_elem = None
                else:
                    self.cur_elem = min(self.cur_elem, len(ln["elements"]) - 1)
                self._changed()
        self.push_screen(ConfirmScreen(f"Really delete element “{label}”?"), cb)

    # ---- line ops -------------------------------------------------------- #
    def new_line(self) -> None:
        self.push_undo()
        idx = self.cur_line + 1 if self.lines else 0
        self.lines.insert(idx, {"name": "line_new", "enabled": True, "elements": []})
        self.cur_line, self.cur_elem = idx, None
        self._changed()

    def line_move(self, d: int) -> None:
        i, j = self.cur_line, self.cur_line + d
        if 0 <= j < len(self.lines):
            self.push_undo()
            self.lines[i], self.lines[j] = self.lines[j], self.lines[i]
            self.cur_line = j
            self._changed()

    def toggle_line(self) -> None:
        ln = self._cur()
        if ln:
            self.push_undo()
            ln["enabled"] = not ln["enabled"]
            self._changed()

    def del_line(self) -> None:
        if self._cur() is None:
            return
        n = sum(1 for e in self.lines[:self.cur_line + 1] if e["enabled"])
        label = f"L{n}" if self.lines[self.cur_line]["enabled"] else "this disabled line"

        def cb(ok):
            if ok:
                self.push_undo()
                del self.lines[self.cur_line]
                self.cur_line = max(0, min(self.cur_line, len(self.lines) - 1))
                els = self.lines[self.cur_line]["elements"] if self.lines else []
                self.cur_elem = 0 if els else None
                self._changed()
        self.push_screen(ConfirmScreen(f"Really delete {label}?"), cb)

    # ---- colors ---------------------------------------------------------- #
    def _fill_colors_table(self) -> None:
        t = self.query_one("#colors-table", ColorsTable)
        keep = t.cursor_row
        t.clear(columns=True)
        t.add_columns("Key", "Hex", "Swatch")
        for key, hx in self.color_rows:
            try:
                sw = Text("        ", style=f"on {hx}") if HEX_RE.match(hx) else Text("??")
            except Exception:
                sw = Text("??")
            t.add_row(key, hx, sw)
        if self.color_rows:
            t.move_cursor(row=min(keep, len(self.color_rows) - 1))

    @on(DataTable.RowSelected, "#colors-table")
    def _edit_color(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if idx is None or idx >= len(self.color_rows):
            return

        def done(value):
            if value is not None:
                self.push_undo()
                self.color_rows[idx][1] = value
                self._mark_dirty()
                self._fill_colors_table()
                self.rebuild_lines()
                self.update_preview()
        self.push_screen(ColorPickerScreen(self.color_rows[idx][0], self.color_rows[idx][1]), done)

    def colors_add(self) -> None:
        def done(name):
            if name:
                self.push_undo()
                self.color_rows.append([name, "#ffffff"])
                self._mark_dirty()
                self._fill_colors_table()
        self.push_screen(PromptScreen("New color key name:"), done)

    def colors_delete(self) -> None:
        t = self.query_one("#colors-table", ColorsTable)
        idx = t.cursor_row
        if 0 <= idx < len(self.color_rows):
            self.push_undo()
            del self.color_rows[idx]
            self._mark_dirty()
            self._fill_colors_table()
            self.update_preview()

    # ---- settings (navigable list; no raw input boxes) ------------------- #
    def _selectable_specs(self) -> list:
        return [s for s in SETTINGS_SPEC if s["kind"] != "section"]

    def _cur_setting_spec(self):
        specs = self._selectable_specs()
        return specs[self.cur_setting] if 0 <= self.cur_setting < len(specs) else None

    @staticmethod
    def _is_preview(spec) -> bool:
        return spec.get("key", (None,))[0] == "__preview__"

    def _setting_get(self, spec):
        key = spec["key"]
        if key[0] == "__preview__":
            if key[1] == "ctx_percent":
                return DEFAULT_SAMPLE["context_window"]["used_percentage"]
            if key[1] == "model":
                return DEFAULT_SAMPLE["model"]["display_name"]
            return None
        if len(key) == 1:
            return self.cfg.get(key[0], spec.get("default"))
        d = self.cfg.get(key[0], {})
        if not isinstance(d, dict):
            d = {}
        return d.get(key[1], spec.get("default"))

    def _setting_set(self, spec, val) -> None:
        key = spec["key"]
        if key[0] == "__preview__":
            if key[1] == "ctx_percent":
                DEFAULT_SAMPLE["context_window"]["used_percentage"] = val
            elif key[1] == "model":
                DEFAULT_SAMPLE["model"]["display_name"] = val
            return
        if len(key) == 1:
            self.cfg[key[0]] = val
        else:
            if not isinstance(self.cfg.get(key[0]), dict):
                self.cfg[key[0]] = {}
            self.cfg[key[0]][key[1]] = val

    @staticmethod
    def _fmt_value(spec, val) -> str:
        if spec["kind"] == "bool":
            return "[x] on" if val else "[ ] off"
        if spec["kind"] == "float":
            try:
                return f"{float(val):g}"
            except (TypeError, ValueError):
                return str(val)
        return str(val)

    def rebuild_settings(self) -> None:
        self.query_one("#settings-view", SettingsView).update(self._render_settings())
        try:
            pane = self.query_one("#settings-pane", VerticalScroll)
            pane.scroll_to(y=max(0, self._settings_sel_y - 3), animate=False)
        except Exception:  # noqa: BLE001
            pass

    def _render_settings(self) -> Text:
        t = Text()
        sel = 0
        line = 0
        for spec in SETTINGS_SPEC:
            if spec["kind"] == "section":
                if line:
                    t.append("\n")
                    line += 1
                t.append(f" {spec['label']}\n", style="bold #61AFEF")
                line += 1
                continue
            is_sel = (sel == self.cur_setting)
            if is_sel:
                self._settings_sel_y = line
            cursor = "▶ " if is_sel else "  "
            if spec["kind"] == "action":
                row = f"{cursor}{spec['label']}"
                style = "reverse bold" if is_sel else "italic #87CEEB"
            else:
                val = self._fmt_value(spec, self._setting_get(spec))
                row = f"{cursor}{spec['label']:<26}  {val}"
                style = "reverse bold" if is_sel else None
            t.append(row + "\n", style=style)
            line += 1
            sel += 1
        return t

    def settings_nav(self, d: int) -> None:
        specs = self._selectable_specs()
        if not specs:
            return
        self.cur_setting = max(0, min(len(specs) - 1, self.cur_setting + d))
        self.rebuild_settings()

    def settings_activate(self) -> None:
        spec = self._cur_setting_spec()
        if spec is None:
            return
        kind = spec["kind"]
        if kind == "bool":
            self._commit_setting(spec, not bool(self._setting_get(spec)))
        elif kind == "action":
            if spec.get("action") == "geocode":
                self.settings_geocode()
        elif kind in ("int", "float"):
            self._open_number_editor(spec)
        else:  # text
            cur = "" if self._setting_get(spec) is None else str(self._setting_get(spec))

            def done(v):
                if v is None:
                    return
                if spec.get("maxlen"):
                    v = v[:spec["maxlen"]]
                if spec.get("nonempty") and not v:
                    self.notify("Can't be empty — use a space for an invisible glyph",
                                severity="warning")
                    return
                self._commit_setting(spec, v)
            self.push_screen(PromptScreen(spec["label"] + ":", cur), done)

    def settings_typed(self, event) -> None:
        """A bare keypress on the settings list: on a numeric row, a typed
        digit / sign / decimal opens the number editor already seeded with it."""
        spec = self._cur_setting_spec()
        if spec is None or spec["kind"] not in ("int", "float"):
            return
        ch = getattr(event, "character", None)
        if not ch:
            return
        if ch.isdigit() or ch in "+-.,":
            seed = "." if ch == "," else ch
            event.stop()
            event.prevent_default()
            self._open_number_editor(spec, seed)

    def settings_bump(self, d: int) -> None:
        spec = self._cur_setting_spec()
        if spec is None or spec["kind"] not in ("int", "float"):
            return
        try:
            num = float(self._setting_get(spec))
        except (TypeError, ValueError):
            num = 0.0
        self._apply_number_value(spec, num + d)

    def _open_number_editor(self, spec, seed=None) -> None:
        is_float = spec["kind"] == "float"
        if seed is not None:
            init = seed
        else:
            cur = self._setting_get(spec)
            init = self._fmt_value(spec, cur) if cur is not None else ""
        kind_hint = "decimal" if is_float else "whole number"
        rng = ""
        if spec.get("min") is not None or spec.get("max") is not None:
            rng = f"  [{spec.get('min', '')}..{spec.get('max', '')}]"
        label = f"{spec['label']} — {kind_hint}{rng}"

        def done(raw):
            # Cancelled, or just a lone sign/decimal the user never completed:
            # treat as no change rather than nagging with a warning.
            if raw is None or raw in ("", "+", "-", ".", "+.", "-."):
                return
            try:
                num = float(raw)
            except (TypeError, ValueError):
                self.notify("Not a number", severity="warning")
                return
            self._apply_number_value(spec, num)
        self.push_screen(NumberEditScreen(label, init, is_float), done)

    def _apply_number_value(self, spec, num) -> None:
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None:
            num = max(lo, num)
        if hi is not None:
            num = min(hi, num)
        if spec["kind"] == "int":
            num = int(round(num))
        self._commit_setting(spec, num)

    def _commit_setting(self, spec, val) -> None:
        """Write a setting and refresh. Config settings are undoable + mark the
        file dirty; preview-only settings (DEFAULT_SAMPLE) do neither."""
        if self._is_preview(spec):
            self._setting_set(spec, val)
            self.rebuild_settings()
            self.update_preview()
            return
        self.push_undo()
        self._setting_set(spec, val)
        self._mark_dirty()
        self.rebuild_settings()
        self.update_preview()

    # ---- geocoding (city name -> coordinates) ---------------------------- #
    def settings_geocode(self) -> None:
        name = ((self.cfg.get("weather") or {}).get("name") or "").strip()
        if not name:
            self.notify("Set a city name first", severity="warning")
            return
        self.notify(f"Looking up “{name}”…")
        self._geocode_worker(name)

    @work(exclusive=True, thread=True, group="geocode")
    def _geocode_worker(self, name: str) -> None:
        results = geocode(name)
        self.call_from_thread(self._geocode_done, name, results)

    def _geocode_done(self, name: str, results: list) -> None:
        if not results:
            self.notify(f"No coordinates found for “{name}”", severity="warning")
            return
        if len(results) == 1:
            self._apply_geocode(results[0])
            return

        def done(idx):
            if idx is not None and 0 <= idx < len(results):
                self._apply_geocode(results[idx])
        self.push_screen(GeocodeScreen(name, results), done)

    def _apply_geocode(self, res: dict) -> None:
        try:
            lat = round(float(res["latitude"]), 4)
            lon = round(float(res["longitude"]), 4)
        except (KeyError, TypeError, ValueError):
            self.notify("Bad geocode result", severity="error")
            return
        self.push_undo()
        w = self.cfg.setdefault("weather", {})
        if not isinstance(w, dict):
            w = {}
            self.cfg["weather"] = w
        if res.get("name"):
            w["name"] = res["name"]
        w["latitude"] = lat
        w["longitude"] = lon
        self._mark_dirty()
        self.rebuild_settings()
        self.update_preview()
        self.notify(f"Set to {w.get('name', '?')}  ({lat}, {lon})", severity="information")

    # ---- preview --------------------------------------------------------- #
    @work(exclusive=True, thread=True, group="preview")
    def update_preview(self) -> None:
        text = self._render_preview()
        self.call_from_thread(self._apply_preview, text)

    def _render_preview(self) -> Text:
        try:
            cfg = self.build_config_dict()
            fd, tmp = tempfile.mkstemp(suffix=".json", prefix="sl_preview_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False)
                env = dict(os.environ, STATUSLINE_CONFIG=tmp)
                res = subprocess.run(
                    [sys.executable, STATUSLINE_PY], input=json.dumps(DEFAULT_SAMPLE),
                    capture_output=True, text=True, encoding="utf-8", env=env, timeout=15)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            out = res.stdout or ""
            if res.returncode != 0 and res.stderr:
                out += "\n[render error]\n" + res.stderr
            return Text.from_ansi(out.rstrip("\n"))
        except Exception as exc:  # noqa: BLE001
            return Text(f"preview failed: {exc}", style="red")

    def _apply_preview(self, text: Text) -> None:
        self.query_one("#preview", Static).update(text)

    def action_refresh_preview(self) -> None:
        self.update_preview()

    # ---- save / quit ----------------------------------------------------- #
    def _mark_dirty(self) -> None:
        self.dirty = True
        self.sub_title = CONFIG_PATH + "  *unsaved*"

    def action_save(self) -> None:
        bad = [k for k, v in self.color_rows if not HEX_RE.match(v or "")]
        if bad:
            self.notify("Invalid colors (need #RRGGBB): " + ", ".join(bad),
                        severity="error", timeout=6)
            return
        cfg = self.build_config_dict()
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
                f.write("\n")
        except OSError as exc:
            self.notify(f"Save failed: {exc}", severity="error")
            return
        self.dirty = False
        self.sub_title = CONFIG_PATH
        cl, ce = self.cur_line, self.cur_elem
        self.load()
        self.cur_line = min(cl, max(0, len(self.lines) - 1))
        self.cur_elem = ce
        self.rebuild_lines()
        self._fill_colors_table()
        self.notify("Saved ✓", severity="information")
        self.update_preview()

    def action_quit_check(self) -> None:
        if not self.dirty:
            self.exit()
            return

        def done(answer):
            if answer is not None and answer.lower().startswith("y"):
                self.exit()
        self.push_screen(PromptScreen("Unsaved changes! Type 'y' to quit anyway:"), done)


if __name__ == "__main__":
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"Config not found: {CONFIG_PATH}")
    StatuslineEditor().run()
