#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render the statusline against fixed sample data (terminal + optional SVG).

This is a read-only previewer: it imports statusline.py as a module, replaces
its network/git helpers with believable sample values, feeds the canonical
sample session JSON on stdin, runs the renderer, and captures the ANSI bar.

It NEVER writes statusline_config.json. With --config it only points the
renderer at that config (via STATUSLINE_CONFIG / statusline.CONFIG_PATH) for the
render; the user's live file is untouched.

Standard library only.

Examples
--------
  # preview the current/live config in the terminal
  python render_demo.py

  # preview a specific config
  python render_demo.py --config F:/ai/statusline/examples/hero.json

  # also export an SVG screenshot
  python render_demo.py --config F:/ai/statusline/examples/hero.json \\
                        --svg F:/ai/statusline/examples/hero.svg
"""
import sys
import os
import io
import json
import argparse
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
# This skill lives at <repo>/.claude/skills/statusline-preview/, so the repo
# root (where statusline.py lives) is three directories up:
#   statusline-preview -> skills -> .claude -> <repo root>
DEFAULT_STATUSLINE_DIR = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
DEFAULT_SAMPLE = os.path.join(HERE, "assets", "sample_input.json")


# --- Sample data the renderer's helpers are replaced with --------------------
def _sample_weather(lat=0.0, lon=0.0):
    """Believable partly-cloudy weather; sunrise/sunset for TODAY so {sun} works.

    Returns the same dict shape statusline.fetch_weather_raw produces:
    {code, temp, hum, wind, sunrise, sunset}. sunrise/sunset are ISO local
    timestamps for today (06:05 / 20:49) so get_sun_icon resolves the current
    state correctly whenever the preview is run.
    """
    today = datetime.now()
    sunrise = today.replace(hour=6, minute=5, second=0, microsecond=0)
    sunset = today.replace(hour=20, minute=49, second=0, microsecond=0)
    return {
        "code": 1,          # 1 -> partly cloudy / mainly clear
        "temp": 22.4,
        "hum": 48,
        "wind": 11.5,
        "sunrise": sunrise.isoformat(timespec="minutes"),
        "sunset": sunset.isoformat(timespec="minutes"),
    }


def _sample_usage():
    """Sample rate-limit usage: session 41%, week 63%.

    resets_at are computed at runtime (UTC) so the countdowns look real: the
    session resets ~3h out and the week ~2d out. Matches the dict shape
    statusline.get_usage returns (consumed by build_usage_lines /
    build_usage_micro).
    """
    now = datetime.now(timezone.utc)
    return {
        "five_hour": {
            "utilization": 41,
            "resets_at": (now + timedelta(hours=3)).isoformat(),
        },
        "seven_day": {
            "utilization": 63,
            "resets_at": (now + timedelta(days=2)).isoformat(),
        },
    }


def _sample_git(cwd):
    """Sample git info: branch 'main', +128 / -34 (matches statusline's tuple)."""
    return ("main", 128, 34)


def load_statusline(statusline_dir, config_path):
    """Import statusline.py as a module with the desired config wired in.

    If config_path is given it is exported as STATUSLINE_CONFIG *before* import
    (statusline computes CONFIG_PATH at import time) and also assigned to
    statusline.CONFIG_PATH afterwards for good measure. Returns the module.
    """
    statusline_dir = os.path.abspath(statusline_dir)
    script = os.path.join(statusline_dir, "statusline.py")
    if not os.path.isfile(script):
        sys.exit("error: statusline.py not found at %s "
                 "(pass --statusline-dir)" % script)

    if config_path:
        os.environ["STATUSLINE_CONFIG"] = os.path.abspath(config_path)

    if statusline_dir not in sys.path:
        sys.path.insert(0, statusline_dir)

    # Drop a stale import so a repeated run picks up env/config changes.
    sys.modules.pop("statusline", None)
    import statusline  # noqa: E402  (import after sys.path tweak, by design)

    if config_path:
        statusline.CONFIG_PATH = os.path.abspath(config_path)
    return statusline


def render(statusline, sample_json_text):
    """Run statusline.main() with sample data, returning the captured ANSI."""
    # Replace the helpers that would otherwise touch the network / git / disk.
    statusline.fetch_weather_raw = _sample_weather
    statusline.get_usage = _sample_usage
    statusline.get_git_info = _sample_git
    # Feed the sample session JSON instead of reading real stdin.
    statusline.get_data = lambda: json.loads(sample_json_text)

    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        statusline.main()
    finally:
        sys.stdout = old_stdout
    return buf.getvalue()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Preview the statusline against sample data "
                    "(terminal + optional SVG). Read-only; never writes config.")
    parser.add_argument("--config", default=None,
                        help="config file to render (default: the renderer's "
                             "normal resolution / current live config)")
    parser.add_argument("--svg", default=None,
                        help="also write an SVG screenshot to this path")
    parser.add_argument("--input", default=DEFAULT_SAMPLE,
                        help="sample session JSON to feed on stdin "
                             "(default: bundled assets/sample_input.json)")
    parser.add_argument("--statusline-dir", default=DEFAULT_STATUSLINE_DIR,
                        help="folder containing statusline.py "
                             "(default: inferred repo root)")
    parser.add_argument("--emoji-cells", type=int, default=1, choices=(1, 2),
                        help="cells an emoji occupies for SVG width math "
                             "(default 1, matching single-cell emoji)")
    args = parser.parse_args(argv)

    try:
        with open(args.input, encoding="utf-8") as f:
            sample_json_text = f.read()
    except OSError as e:
        sys.exit("error: cannot read sample input %s: %s" % (args.input, e))

    statusline = load_statusline(args.statusline_dir, args.config)
    ansi = render(statusline, sample_json_text)

    # Terminal preview: write the raw ANSI so the terminal renders the colors.
    # Use the byte stream to avoid any platform newline translation surprises.
    sys.stdout.buffer.write(ansi.encode("utf-8"))
    sys.stdout.buffer.flush()

    if args.svg:
        # Import the sibling converter (same directory).
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import ansi2svg
        svg = ansi2svg.ansi_to_svg(ansi, emoji_cells=args.emoji_cells)
        out_path = os.path.abspath(args.svg)
        out_dir = os.path.dirname(out_path)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(svg)
        # Note on stderr so it doesn't pollute the captured ANSI on stdout.
        sys.stderr.write("\nSVG written to %s\n" % out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
