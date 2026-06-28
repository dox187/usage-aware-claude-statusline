#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Convert ANSI (24-bit SGR) status-line output into an SVG screenshot.

The output matches the bundled examples/*.svg in style:

  * a dark rounded card background (#0E1116, rx=14),
  * the Agave Nerd Font family with a generic monospace fallback,
  * font-size 26 on a fixed monospace cell grid,
  * one <tspan fill=\"#RRGGBB\"> per ANSI color run (bold runs get
    font-weight=\"700\"),
  * default / uncolored text drawn in #C9D2DD,
  * block and eighth-block gauge glyphs (full block, eighth blocks, light
    shade, half blocks) pass straight through as text — the monospace font
    draws them as cells,
  * emoji counted as a single cell for the width math.

The card width/height and the line-1 baseline reproduce the published examples
exactly (the height formula yields the same 79/111/143/174/206 heights); interior
baselines on later rows can differ from a published file by ~0.1px because the
originals accumulated per-row rounding. That sub-pixel difference is
imperceptible — the output is visually indistinguishable from examples/*.svg.

This module only understands the SGR subset the renderer emits: 24-bit
foreground color (ESC[38;2;r;g;b m), bold (ESC[1m / ESC[22m), default fg
(ESC[39m) and full reset (ESC[0m / ESC[m). Anything else is ignored gracefully.

Standard library only.

CLI:
    python ansi2svg.py < ansi.txt > out.svg
    python ansi2svg.py --emoji-cells 2 < ansi.txt > out.svg

Importable:
    from ansi2svg import ansi_to_svg
    svg = ansi_to_svg(ansi_text)            # returns the SVG string
"""
import sys
import re
import argparse
import unicodedata

# ---- Geometry / style constants (derived from examples/*.svg) ---------------
FONT_FAMILY = ("'Agave Nerd Font','Agave NF','JetBrainsMono Nerd Font',"
               "ui-monospace,'Cascadia Code',Consolas,monospace")
FONT_SIZE = 26
CELL_W = 15.6          # monospace advance per cell (0.6em at size 26)
MARGIN_L = 24          # left padding (x of the first glyph)
MARGIN_R = 24          # right padding
FIRST_BASELINE = 50.0  # y of line 1's baseline
LINE_STEP = 31.7       # baseline-to-baseline distance
BOTTOM_PAD = 29.2      # space below the last baseline
BG_COLOR = "#0E1116"   # card background
BG_RADIUS = 14         # rounded-corner radius
DEFAULT_FG = "#C9D2DD" # color used for uncolored / reset text

# ---- ANSI SGR parsing -------------------------------------------------------
# Matches a CSI ... m (Select Graphic Rendition) sequence and captures the
# numeric parameter list (possibly empty, which means reset).
SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(s):
    """Remove ANSI SGR escape codes from a string."""
    return ANSI_RE.sub("", s)


def cell_width(s, emoji_cells=1):
    """Approximate the number of monospace cells `s` occupies.

    Mirrors statusline.display_width but with a configurable emoji width that
    defaults to 1 (the SVG draws emoji in a single cell). ANSI is assumed to be
    already stripped. Zero-width joiners, variation selectors and combining
    marks count 0; CJK wide chars count 2; emoji count `emoji_cells`.
    """
    width = 0
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        o = ord(ch)
        nxt = s[i + 1] if i + 1 < n else ""
        if o == 0x200D or 0xFE00 <= o <= 0xFE0F or unicodedata.combining(ch):
            i += 1
            continue
        if nxt and ord(nxt) == 0xFE0F:            # emoji-presentation (VS16)
            width += emoji_cells
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 2
        elif o >= 0x1F000:                        # emoji / pictographic planes
            width += emoji_cells
        else:
            width += 1
        i += 1
    return width


def _xml_escape(s):
    """Escape text for safe inclusion in SVG/XML text content."""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


def parse_line(line):
    """Split one ANSI line into a list of runs: (text, hex_color, bold).

    Tracks the current foreground color and bold state across SGR sequences.
    Uncolored text uses DEFAULT_FG. Consecutive text in the same color+bold
    state is coalesced into a single run. Empty-text runs are dropped.
    """
    runs = []
    cur_color = DEFAULT_FG
    cur_bold = False
    buf = []

    def flush():
        if buf:
            runs.append(("".join(buf), cur_color, cur_bold))
            buf.clear()

    pos = 0
    for m in SGR_RE.finditer(line):
        # text before this escape keeps the current state
        buf.append(line[pos:m.start()])
        pos = m.end()
        params = m.group(1)
        # An empty param list (ESC[m) means reset, same as ESC[0m.
        codes = [p for p in params.split(";") if p != ""] or ["0"]
        # Apply the codes; a color or bold change starts a new run, so flush
        # the accumulated text first.
        i = 0
        while i < len(codes):
            try:
                code = int(codes[i])
            except ValueError:
                i += 1
                continue
            if code == 0:                      # reset all
                flush()
                cur_color = DEFAULT_FG
                cur_bold = False
            elif code == 1:                    # bold on
                flush()
                cur_bold = True
            elif code == 22:                   # bold off
                flush()
                cur_bold = False
            elif code == 39:                   # default foreground
                flush()
                cur_color = DEFAULT_FG
            elif code == 38 and i + 2 < len(codes) and codes[i + 1] == "2":
                # 24-bit truecolor: 38;2;R;G;B
                try:
                    r = int(codes[i + 2])
                    g = int(codes[i + 3])
                    b = int(codes[i + 4])
                except (ValueError, IndexError):
                    i += 1
                    continue
                flush()
                cur_color = "#%02x%02x%02x" % (r & 255, g & 255, b & 255)
                i += 4
            # other SGR codes (italics, 256-color, etc.) are ignored
            i += 1
    buf.append(line[pos:])
    flush()
    # Drop empty-text runs (can appear from back-to-back escapes).
    return [(t, c, b) for (t, c, b) in runs if t != ""]


def ansi_to_svg(ansi_text, emoji_cells=1):
    """Render captured ANSI status-line text to an SVG string.

    `ansi_text` is the multi-line output of statusline.py (each line a status
    row). `emoji_cells` controls how many cells an emoji counts as for the
    width calculation only (1 = how the SVG draws them, matching the examples).
    Blank lines (after stripping ANSI) are skipped, mirroring the renderer.
    """
    raw_lines = ansi_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # Keep only non-empty rendered lines (the renderer already skips blanks, but
    # a trailing newline yields one empty element here).
    lines = [ln for ln in raw_lines if strip_ansi(ln).strip() != ""]
    if not lines:
        lines = [""]  # always emit a valid, if empty, card

    max_cells = max(
        (cell_width(strip_ansi(ln), emoji_cells) for ln in lines),
        default=0,
    )
    width = round(MARGIN_L + max_cells * CELL_W + MARGIN_R)
    n = len(lines)
    height = round(FIRST_BASELINE + (n - 1) * LINE_STEP + BOTTOM_PAD)

    out = []
    out.append(
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'width="%d" height="%d" viewBox="0 0 %d %d" '
        'font-family="%s" font-size="%d">'
        % (width, height, width, height, FONT_FAMILY, FONT_SIZE)
    )
    out.append(
        '<rect width="%d" height="%d" rx="%d" fill="%s"/>'
        % (width, height, BG_RADIUS, BG_COLOR)
    )
    for idx, line in enumerate(lines):
        y = FIRST_BASELINE + idx * LINE_STEP
        # Render y like the examples: one decimal place (e.g. "50.0", "81.7").
        y_str = "%.1f" % y
        spans = []
        for text, color, bold in parse_line(line):
            esc = _xml_escape(text)
            if bold:
                spans.append('<tspan fill="%s" font-weight="700">%s</tspan>'
                             % (color, esc))
            else:
                spans.append('<tspan fill="%s">%s</tspan>' % (color, esc))
        out.append('<text x="%d" y="%s" xml:space="preserve">%s</text>'
                   % (MARGIN_L, y_str, "".join(spans)))
    out.append('</svg>')
    return "\n".join(out) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Convert ANSI status-line output (stdin) into an SVG "
                    "(stdout) matching examples/*.svg.")
    parser.add_argument("--emoji-cells", type=int, default=1, choices=(1, 2),
                        help="cells an emoji occupies for width math "
                             "(default 1, matching the SVG's single-cell emoji)")
    args = parser.parse_args(argv)
    data = sys.stdin.buffer.read().decode("utf-8", "replace")
    svg = ansi_to_svg(data, emoji_cells=args.emoji_cells)
    sys.stdout.buffer.write(svg.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
