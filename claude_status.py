#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetches the Claude status page incident feed and returns the CURRENTLY
ACTIVE incidents (stdlib only).

This is the status-line counterpart of the heavier claude-status-bot Discord
project: that bot keeps a store and diffs the feed so it can *notify* about
every delta. A status line only needs to show the *current* state, so this
module is deliberately simple — the RSS feed always carries the latest update
of each incident (newest update first), so "what is broken right now" is just a
filter over the freshly fetched feed. No store, no diff: when an incident's
newest update flips to "Resolved" it stops being active and disappears on the
next render.

Usage
  - CLI:     python claude_status.py          -> active incidents as JSON
             python claude_status.py --all     -> every parsed incident
  - module:  from claude_status import get_incidents
             incidents = get_incidents()        # cached; active first

CACHING (mirrors statusline.py's weather/usage caches)
  get_incidents() reads ~/.claude/status_cache.json first and only hits the
  network when that cache is older than the TTL. Network refreshes use a
  conditional GET (If-None-Match with the stored ETag); the status host answers
  with a cheap HTTP 304 when nothing changed, so we reuse the cached parse. Any
  network/parsing failure falls back to the (possibly stale) cache, then to an
  empty list, so the status line never breaks because the status page is down.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

STATUS_URL = "https://status.claude.com/history.rss"
USER_AGENT = os.environ.get(
    "CLAUDE_STATUS_UA",
    "claude-statusline/1.0 (+https://github.com/dox187/statusline)",
)

DEFAULT_CACHE = os.path.join(os.path.expanduser("~"), ".claude", "status_cache.json")
DEFAULT_TTL = 120   # seconds; incidents move slowly, conditional GET keeps it cheap

# Canonical, normalized status vocabulary (lowercased, whitespace-collapsed),
# extracted from the feed's <strong> tags.
STATUSES = (
    "investigating", "identified", "monitoring", "resolved", "update",
    "scheduled", "in progress", "completed", "postmortem", "verifying",
)
# A status that means "this incident is over" -> not active, hidden.
RESOLVED_STATUSES = {"resolved", "completed", "postmortem"}
# Maintenance-flavored statuses (planned/ongoing maintenance, not an outage).
MAINTENANCE_STATUSES = {"scheduled", "in progress"}

# Emoji per normalized status; unknown -> the default key.
STATUS_EMOJI = {
    "investigating": "🔍",
    "identified": "🛠️",
    "monitoring": "👀",
    "verifying": "👀",
    "resolved": "✅",
    "completed": "✅",
    "scheduled": "🗓️",
    "in progress": "⏳",
    "update": "📣",
    "postmortem": "📝",
    "default": "⚠️",
}

_P_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.I | re.S)
_SMALL_RE = re.compile(r"<small\b[^>]*>(.*?)</small>", re.I | re.S)
_STRONG_RE = re.compile(r"<strong\b[^>]*>(.*?)</strong>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


def normalize_status(raw):
    """Map a raw <strong> label ('Monitoring', 'In Progress') onto the canonical
    lowercase vocabulary. Unrecognized/empty input -> 'update' (neutral)."""
    if not isinstance(raw, str):
        return "update"
    norm = _WS_RE.sub(" ", raw.strip().lower())
    return norm if norm in STATUSES else "update"


def decode_entities(s):
    """Decode the handful of XML/HTML entities the feed uses. ElementTree already
    decodes the description once, but a doubly-encoded value can still contain
    '&lt;'; this makes parsing robust either way. Ampersand is decoded last."""
    if not isinstance(s, str) or not s:
        return ""
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
             .replace("&#39;", "'").replace("&apos;", "'").replace("&amp;", "&"))


def strip_tags(html):
    """Drop all HTML tags and collapse whitespace to single spaces."""
    if not isinstance(html, str):
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()


def parse_updates(description):
    """Parse the per-update <p> blocks out of an incident description (newest
    first). Returns [{status, label, when, text}, ...]."""
    desc = description if isinstance(description, str) else ""
    if "&lt;" in desc or "&amp;" in desc:
        desc = decode_entities(desc)
    updates = []
    for m in _P_RE.finditer(desc):
        block = m.group(1)
        if not block or not block.strip():
            continue
        # Timestamp lives in the leading <small> (drop the stray space the feed
        # leaves before the comma after the <var>date</var>).
        when = ""
        sm = _SMALL_RE.search(block)
        if sm:
            when = re.sub(r"\s+([,.;:])", r"\1", strip_tags(sm.group(1)))
        # Status is the <strong> text; the body is everything after it.
        st = _STRONG_RE.search(block)
        label = strip_tags(st.group(1)) if st else ""
        if st:
            text = re.sub(r"^[\s\-]+", "", strip_tags(block[st.end():])).strip()
        else:
            rest = block.replace(sm.group(0), " ") if sm else block
            text = strip_tags(rest)
        if not label and not text:
            continue
        updates.append({
            "status": normalize_status(label),
            "label": label,
            "when": when,
            "text": text,
        })
    return updates


def parse_pubdate(s):
    """Parse an RFC 822 RSS <pubDate> into a UTC epoch (float), or None.

    The feed stamps each item's pubDate with the time of its newest update, so
    this doubles as the incident's "last activity" time for age filtering."""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        dt = parsedate_to_datetime(s)
        return dt.timestamp() if dt is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def classify(status):
    """'resolved' (hide) | 'maintenance' | 'incident' for a normalized status."""
    if status in RESOLVED_STATUSES:
        return "resolved"
    if status in MAINTENANCE_STATUSES:
        return "maintenance"
    return "incident"


def parse_feed(xml_text):
    """Parse the RSS body into incident dicts (feed order: newest first).

    Each incident is reduced to its CURRENT state, i.e. its newest update:
      {title, link, status, label, text, when, state, emoji, updates}
    Malformed individual items are skipped; a fatal XML error returns []."""
    if not isinstance(xml_text, str) or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall(".//item")

    incidents = []
    for item in items:
        try:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or item.findtext("guid") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            updates = parse_updates(item.findtext("description") or "")
            if not updates:
                continue
            latest = updates[0]
            status = latest["status"]
            incidents.append({
                "title": title,
                "link": link,
                "status": status,
                "label": latest["label"] or status.title(),
                "text": latest["text"],
                "when": latest["when"],
                "pub_date": pub,
                "pub_ts": parse_pubdate(pub),
                "state": classify(status),
                "emoji": STATUS_EMOJI.get(status, STATUS_EMOJI["default"]),
                "updates": len(updates),
            })
        except Exception:
            continue
    return incidents


def fetch_raw(etag=None, timeout=5):
    """Fetch the feed with a conditional GET.

    Returns (status_code, body_or_None, etag_or_None). On 304 the body is None
    and the passed-in etag is echoed back. Raises urllib errors on failure."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(STATUS_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body, resp.headers.get("ETag") or etag
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, None, etag
        raise


def _read_cache(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        if isinstance(cache, dict) and isinstance(cache.get("incidents"), list):
            return cache
    except Exception:
        pass
    return None


def get_incidents(cache_path=DEFAULT_CACHE, ttl=DEFAULT_TTL, timeout=5):
    """Return parsed incident dicts (feed order), cached for `ttl` seconds.

    Fresh cache -> returned as-is. Otherwise a conditional GET refreshes it:
    HTTP 304 reuses the cached parse (just bumps the timestamp + ETag), HTTP 200
    re-parses. Any error falls back to the stale cache, then to []."""
    cache = _read_cache(cache_path)
    if cache is not None and (time.time() - cache.get("ts", 0)) < ttl:
        return cache["incidents"]

    etag = cache.get("etag") if cache else None
    try:
        code, body, new_etag = fetch_raw(etag=etag, timeout=timeout)
        if code == 304 and cache is not None:
            incidents = cache["incidents"]
        else:
            incidents = parse_feed(body or "")
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "etag": new_etag,
                           "incidents": incidents}, f, ensure_ascii=False)
        except Exception:
            pass
        return incidents
    except Exception:
        return cache["incidents"] if cache is not None else []


def active_incidents(incidents, include_maintenance=False, max_age_hours=None):
    """Filter to the incidents that are still ongoing (feed order preserved).

    max_age_hours, when positive, drops incidents whose last update (pub_ts) is
    older than that many hours; an incident with an unparseable/missing pub_ts is
    KEPT (we don't hide a real outage just because its date couldn't be read)."""
    cutoff = None
    if isinstance(max_age_hours, (int, float)) and max_age_hours > 0:
        cutoff = time.time() - max_age_hours * 3600
    out = []
    for i in incidents:
        state = i.get("state")
        if not (state == "incident" or (include_maintenance and state == "maintenance")):
            continue
        if cutoff is not None:
            ts = i.get("pub_ts")
            if ts is not None and ts < cutoff:
                continue
        out.append(i)
    return out


def main():
    args = sys.argv[1:]
    show_all = "--all" in args
    include_maint = "--maintenance" in args or "-m" in args
    # Optional "--max-age N" (hours) to mirror the status-line age filter.
    max_age = None
    if "--max-age" in args:
        try:
            max_age = float(args[args.index("--max-age") + 1])
        except (IndexError, ValueError):
            print("--max-age needs a number of hours", file=sys.stderr)
            return 2
    try:
        # CLI bypasses the disk cache so it always reflects the live feed.
        code, body, _ = fetch_raw(timeout=10)
        incidents = parse_feed(body or "")
    except urllib.error.URLError as e:
        print(f"Network error: {getattr(e, 'reason', e)}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    result = (incidents if show_all
              else active_incidents(incidents, include_maint, max_age_hours=max_age))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
