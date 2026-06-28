#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Color utilities for the statusline-theme skill (standard library ONLY).

Provides:
  * hex_to_rgb / rgb_to_hex          - "#RRGGBB" <-> (r, g, b) ints 0..255
  * srgb_to_xyz / xyz_to_lab         - sRGB (D65) -> CIE XYZ -> CIE L*a*b*
  * hex_to_lab                       - convenience: "#RRGGBB" -> Lab
  * ciede2000(lab1, lab2)            - perceptual color difference (delta-E 2000)
  * delta_e_hex(hex1, hex2)          - CIEDE2000 between two hex colors
  * nearest_color(target, candidates)- closest candidate hex by CIEDE2000
  * swatch_line(items)               - one truecolor ANSI line of labeled swatches
  * print_swatches(items)            - print swatch_line to stdout

Run directly (python colorutil.py) for a self-test that prints a known
CIEDE2000 reference value (~2.0425) so the math can be verified.

This file is self-contained: the theme skill may import it or shell out to it.
"""
import math
import sys

__all__ = [
    "hex_to_rgb", "rgb_to_hex", "srgb_to_xyz", "xyz_to_lab",
    "hex_to_lab", "ciede2000", "delta_e_hex", "nearest_color",
    "swatch_line", "print_swatches",
]


# ---------------------------------------------------------------------------
# Hex <-> RGB
# ---------------------------------------------------------------------------
def hex_to_rgb(hex_color):
    """'#RRGGBB' (or 'RRGGBB') -> (r, g, b) as ints 0..255."""
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:                      # allow shorthand #abc -> #aabbcc
        h = "".join(ch * 2 for ch in h)
    if len(h) != 6:
        raise ValueError("expected a #RRGGBB hex color, got: %r" % (hex_color,))
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def rgb_to_hex(rgb):
    """(r, g, b) ints (each clamped to 0..255) -> '#rrggbb' lowercase."""
    def clamp(v):
        return 0 if v < 0 else (255 if v > 255 else int(round(v)))
    r, g, b = rgb
    return "#%02x%02x%02x" % (clamp(r), clamp(g), clamp(b))


# ---------------------------------------------------------------------------
# sRGB -> XYZ -> CIE L*a*b*   (D65 reference white, 2 degree observer)
# ---------------------------------------------------------------------------
# D65 reference white tristimulus values (scaled so Y = 100).
_D65 = (95.047, 100.000, 108.883)


def _srgb_inv_companding(c):
    """Linearize a single 0..1 sRGB channel."""
    return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92


def srgb_to_xyz(rgb):
    """(r, g, b) ints 0..255 -> CIE XYZ (D65), with Y on a 0..100 scale."""
    r, g, b = (_srgb_inv_companding(v / 255.0) for v in rgb)
    # sRGB (D65) linear-RGB -> XYZ matrix, scaled to 0..100.
    x = (r * 0.4124564 + g * 0.3575761 + b * 0.1804375) * 100.0
    y = (r * 0.2126729 + g * 0.7151522 + b * 0.0721750) * 100.0
    z = (r * 0.0193339 + g * 0.1191920 + b * 0.9503041) * 100.0
    return (x, y, z)


def xyz_to_lab(xyz):
    """CIE XYZ (Y on 0..100, D65) -> CIE L*a*b*."""
    eps = 216.0 / 24389.0       # (6/29)^3
    kappa = 24389.0 / 27.0      # (29/3)^3

    def f(t):
        return t ** (1.0 / 3.0) if t > eps else (kappa * t + 16.0) / 116.0

    fx = f(xyz[0] / _D65[0])
    fy = f(xyz[1] / _D65[1])
    fz = f(xyz[2] / _D65[2])
    L = 116.0 * fy - 16.0
    a = 500.0 * (fx - fy)
    b = 200.0 * (fy - fz)
    return (L, a, b)


def hex_to_lab(hex_color):
    """'#RRGGBB' -> CIE L*a*b* tuple."""
    return xyz_to_lab(srgb_to_xyz(hex_to_rgb(hex_color)))


# ---------------------------------------------------------------------------
# CIEDE2000 perceptual color difference
# Reference: Sharma, Wu & Dalal (2005), "The CIEDE2000 Color-Difference
# Formula: Implementation Notes, Supplementary Test Data, and Mathematical
# Observations." Variable names follow that paper.
# ---------------------------------------------------------------------------
def ciede2000(lab1, lab2, kL=1.0, kC=1.0, kH=1.0):
    """CIEDE2000 delta-E between two CIE L*a*b* triples."""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2

    C1 = math.hypot(a1, b1)
    C2 = math.hypot(a2, b2)
    C_bar = (C1 + C2) / 2.0

    C_bar7 = C_bar ** 7
    G = 0.5 * (1.0 - math.sqrt(C_bar7 / (C_bar7 + 25.0 ** 7)))

    a1p = (1.0 + G) * a1
    a2p = (1.0 + G) * a2
    C1p = math.hypot(a1p, b1)
    C2p = math.hypot(a2p, b2)

    def hp(b, ap):
        if b == 0 and ap == 0:
            return 0.0
        h = math.degrees(math.atan2(b, ap))
        return h + 360.0 if h < 0 else h

    h1p = hp(b1, a1p)
    h2p = hp(b2, a2p)

    dLp = L2 - L1
    dCp = C2p - C1p

    if C1p * C2p == 0:
        dhp = 0.0
    else:
        diff = h2p - h1p
        if diff > 180.0:
            diff -= 360.0
        elif diff < -180.0:
            diff += 360.0
        dhp = diff
    dHp = 2.0 * math.sqrt(C1p * C2p) * math.sin(math.radians(dhp / 2.0))

    Lp_bar = (L1 + L2) / 2.0
    Cp_bar = (C1p + C2p) / 2.0

    if C1p * C2p == 0:
        hp_bar = h1p + h2p
    else:
        absdiff = abs(h1p - h2p)
        if absdiff <= 180.0:
            hp_bar = (h1p + h2p) / 2.0
        elif (h1p + h2p) < 360.0:
            hp_bar = (h1p + h2p + 360.0) / 2.0
        else:
            hp_bar = (h1p + h2p - 360.0) / 2.0

    T = (1.0
         - 0.17 * math.cos(math.radians(hp_bar - 30.0))
         + 0.24 * math.cos(math.radians(2.0 * hp_bar))
         + 0.32 * math.cos(math.radians(3.0 * hp_bar + 6.0))
         - 0.20 * math.cos(math.radians(4.0 * hp_bar - 63.0)))

    d_theta = 30.0 * math.exp(-(((hp_bar - 275.0) / 25.0) ** 2))
    Cp_bar7 = Cp_bar ** 7
    Rc = 2.0 * math.sqrt(Cp_bar7 / (Cp_bar7 + 25.0 ** 7))
    Sl = 1.0 + (0.015 * (Lp_bar - 50.0) ** 2) / math.sqrt(20.0 + (Lp_bar - 50.0) ** 2)
    Sc = 1.0 + 0.045 * Cp_bar
    Sh = 1.0 + 0.015 * Cp_bar * T
    Rt = -math.sin(math.radians(2.0 * d_theta)) * Rc

    term_L = dLp / (kL * Sl)
    term_C = dCp / (kC * Sc)
    term_H = dHp / (kH * Sh)
    return math.sqrt(term_L ** 2 + term_C ** 2 + term_H ** 2
                     + Rt * term_C * term_H)


def delta_e_hex(hex1, hex2):
    """CIEDE2000 difference between two '#RRGGBB' colors."""
    return ciede2000(hex_to_lab(hex1), hex_to_lab(hex2))


def nearest_color(target_hex, candidate_hexes):
    """Return (best_hex, delta_e) for the candidate closest to target_hex.

    candidate_hexes: an iterable of '#RRGGBB' strings. Distance is CIEDE2000
    on the perceptually-uniform CIE Lab space (D65). Returns (None, inf) when
    candidate_hexes is empty.
    """
    target_lab = hex_to_lab(target_hex)
    best_hex = None
    best_de = float("inf")
    for cand in candidate_hexes:
        de = ciede2000(target_lab, hex_to_lab(cand))
        if de < best_de:
            best_de = de
            best_hex = cand
    return best_hex, best_de


# ---------------------------------------------------------------------------
# Truecolor ANSI swatches
# ---------------------------------------------------------------------------
_RESET = "\033[0m"


def _fg(hex_color):
    r, g, b = hex_to_rgb(hex_color)
    return "\033[38;2;%d;%d;%dm" % (r, g, b)


def _bg(hex_color):
    r, g, b = hex_to_rgb(hex_color)
    return "\033[48;2;%d;%d;%dm" % (r, g, b)


def swatch_line(items, block="  ", show_hex=True):
    """Build a single truecolor line of labeled swatches.

    items: iterable of (label, hex). Each entry renders as a colored block
    (background = the color) followed by the label and, when show_hex, the hex
    in that color. Segments are separated by two spaces. Returns the line as a
    string (no trailing newline) so callers can print it as-is.
    """
    parts = []
    for label, hex_color in items:
        sw = "%s%s%s" % (_bg(hex_color), block, _RESET)
        tail = " %s" % hex_color if show_hex else ""
        parts.append("%s %s%s%s%s" % (sw, _fg(hex_color), label, tail, _RESET))
    return "  ".join(parts)


def print_swatches(items, block="  ", show_hex=True):
    """Print swatch_line(items) to stdout."""
    print(swatch_line(items, block=block, show_hex=show_hex))


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def _selftest():
    # Documented CIEDE2000 reference pair (Sharma et al. test data):
    #   Lab(50, 2.6772, -79.7751) vs Lab(50, 0, -82.7485) -> dE00 = 2.0425
    lab1 = (50.0, 2.6772, -79.7751)
    lab2 = (50.0, 0.0, -82.7485)
    de = ciede2000(lab1, lab2)
    expected = 2.0425
    ok = abs(de - expected) < 1e-3
    print("CIEDE2000 reference pair:")
    print("  lab1 = %s" % (lab1,))
    print("  lab2 = %s" % (lab2,))
    print("  computed dE00 = %.4f   expected ~= %.4f   %s"
          % (de, expected, "OK" if ok else "FAIL"))

    # Round-trip + Lab sanity: pure white should be L*=100, a*=b*=0.
    wl = hex_to_lab("#ffffff")
    print("  Lab(#ffffff) = (%.2f, %.2f, %.2f)  [expect ~ (100, 0, 0)]"
          % wl)

    # nearest_color demo.
    target = "#f38ba8"   # Catppuccin Mocha red
    cands = ["#a6e3a1", "#89b4fa", "#fab387", "#f38ba8", "#cdd6f4"]
    best, bde = nearest_color(target, cands)
    print("  nearest to %s among %d -> %s (dE00=%.3f)  [expect exact match 0.000]"
          % (target, len(cands), best, bde))

    # Visible swatches so a human can eyeball the truecolor output.
    print("Swatch demo:")
    print_swatches([("mocha.red", "#f38ba8"), ("mocha.green", "#a6e3a1"),
                    ("mocha.blue", "#89b4fa"), ("mocha.text", "#cdd6f4")])

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_selftest())
