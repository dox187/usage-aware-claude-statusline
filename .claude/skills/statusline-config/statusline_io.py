#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared I/O helper for the statusline skills (stdlib only).

This single file is the ONE source of truth for two cross-skill behaviors:

  1. Resolving the *active* statusline -- the copy Claude Code is really running,
     which is usually the DEPLOYED copy (e.g. ~/.claude/statusline.py), NOT the
     repo copy. Resolution order: a gitignored POINTER file, else the active
     settings.json -> statusLine.command, else nothing (caller asks the user).

  2. History-aware config writes -- never blind-overwrite the live config: before
     a write the current config is snapshotted into a ".statusline-config-history/"
     directory next to it (YYYYMMDD-hhmm.json) and a human-readable entry is
     appended to YYYYMMDD.md describing what changed and why.

It is the only copy of this helper. Every statusline skill calls THIS file as a
CLI subprocess (python <path> <subcommand> ...) -- there is no Python import-path
coupling. From a sibling skill it is reached as
    ../statusline-config/statusline_io.py

The POINTER path is derived from this file's OWN location. This file lives at
    <X>/.claude/skills/statusline-config/statusline_io.py
so the pointer is
    <X>/.claude/.statusline-active.json
i.e. os.path.join(HERE, "..", "..", ".statusline-active.json"). When the skills
live in the repo the pointer is <repo>/.claude/.statusline-active.json; when
deployed under ~/.claude/skills it is ~/.claude/.statusline-active.json --
automatically, with no configuration.

Subcommands (all stdlib; print results; never crash the bar):

  locate
      Resolve the active statusline (pointer -> settings.json -> none) and print
      a single JSON object to stdout (always exit 0, even on internal error).

  save-pointer --statusline-py P --config C [--settings S] [--launcher L]
      Write the pointer file (path computed from HERE) as pretty JSON. Print the
      pointer path.

  read-pointer
      Print the pointer JSON (or {"source": "none"} if absent).

  commit --config ACTIVE --new NEWFILE --skill NAME --summary TEXT --why TEXT
         [--diff TEXT]
      History-aware install: validate NEWFILE is JSON (else exit 4, touch
      nothing), snapshot ACTIVE into the history dir, install NEWFILE bytes to
      ACTIVE, append a changelog entry, print {snapshot, changelog, config_path}.

Standard library only.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sys

# This file's own directory. Everything that must follow the skill -- most
# importantly the pointer path -- is derived from HERE so the helper "just
# works" both in the repo and when deployed under ~/.claude/skills.
HERE = os.path.dirname(os.path.abspath(__file__))

# The pointer lives next to the .claude directory that contains this skill:
#   <X>/.claude/skills/statusline-config/statusline_io.py   (this file)
#   <X>/.claude/.statusline-active.json                     (the pointer)
# i.e. two directories up from HERE (statusline-config -> skills -> .claude).
POINTER_PATH = os.path.abspath(
    os.path.join(HERE, "..", "..", ".statusline-active.json")).replace("\\", "/")


# --------------------------------------------------------------------------- #
# Path normalization helpers
# --------------------------------------------------------------------------- #
def _expand_drive_form(path):
    """Expand a leading git-bash drive form "/c/Users/x" -> "C:/Users/x".

    Only a single-letter first segment is treated as a drive (that is the
    git-bash convention). Anything else is returned unchanged.
    """
    if not path:
        return path
    m = re.match(r"^/([A-Za-z])/(.*)$", path)
    if m:
        return "%s:/%s" % (m.group(1).upper(), m.group(2))
    # Bare "/c" with nothing after it.
    m = re.match(r"^/([A-Za-z])/?$", path)
    if m:
        return "%s:/" % m.group(1).upper()
    return path


def _normalize_path(path):
    """Normalize a path token for use/display.

    - expand a leading "~"
    - expand a leading git-bash "/c/..." drive form to "C:/..."
    - convert backslashes to forward slashes (consistent display cross-platform)
    - make absolute

    Returns an absolute, forward-slashed path string (or None for falsy input).
    """
    if not path:
        return None
    p = path.strip().strip('"').strip("'")
    if not p:
        return None
    p = os.path.expanduser(p)
    p = _expand_drive_form(p)
    p = p.replace("\\", "/")
    # abspath may re-introduce OS separators on Windows; normalize again so the
    # output is always forward-slashed for stable display/JSON.
    p = os.path.abspath(p).replace("\\", "/")
    return p


# --------------------------------------------------------------------------- #
# settings.json discovery + command parsing
# --------------------------------------------------------------------------- #
def _read_json_file(path):
    """Read + parse a JSON file. Return the object, or None on any problem."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        return json.loads(text) if text.strip() else None
    except Exception:
        return None


def _find_settings_chain():
    """Find candidate settings.json files, project first, then user.

    Project: walk upward from CWD looking for ".claude/settings.json".
    User:    ~/.claude/settings.json.

    Returns a list of (path, kind) tuples in precedence order ("project" before
    "user"), each path existing on disk and appearing at most once. Project
    takes precedence for the statusLine key.

    The user file (~/.claude/settings.json) is always labelled "user", even
    when CWD is under the home directory and the upward walk would otherwise
    reach it: it is skipped during the project walk and emitted once at the end
    as kind "user", so the human-readable notes never mislabel it.
    """
    candidates = []
    seen_paths = set()  # normcased abspaths already emitted (de-dup)

    # The user file, identified by its normcased abspath so we can recognize it
    # if the upward walk happens to pass through the home directory.
    user = os.path.abspath(
        os.path.join(os.path.expanduser("~"), ".claude", "settings.json"))
    user_key = os.path.normcase(user)

    # Project: search upward from CWD for a .claude/settings.json. Skip the
    # user file here so it is only ever emitted once, labelled "user".
    cur = os.path.abspath(os.getcwd())
    seen_dirs = set()
    while True:
        if cur in seen_dirs:  # safety against any pathological loop
            break
        seen_dirs.add(cur)
        candidate = os.path.join(cur, ".claude", "settings.json")
        if os.path.isfile(candidate):
            key = os.path.normcase(os.path.abspath(candidate))
            if key != user_key and key not in seen_paths:
                candidates.append((candidate, "project"))
                seen_paths.add(key)
        parent = os.path.dirname(cur)
        if parent == cur:  # reached filesystem root
            break
        cur = parent

    # User: ~/.claude/settings.json, always labelled "user".
    if os.path.isfile(user) and user_key not in seen_paths:
        candidates.append((user, "user"))
        seen_paths.add(user_key)

    return candidates


def _extract_config_from_command(command):
    """Pull a STATUSLINE_CONFIG value out of a command string, if present.

    Handles a "STATUSLINE_CONFIG=... " env prefix and an "--env STATUSLINE_CONFIG=..."
    / "STATUSLINE_CONFIG ..." style. Returns the raw (un-normalized) value or None.
    """
    if not command:
        return None
    # KEY=VALUE form, possibly quoted; value runs to the next unquoted space.
    m = re.search(r"STATUSLINE_CONFIG=(\"[^\"]*\"|'[^']*'|\S+)", command)
    if m:
        return m.group(1)
    # "--env STATUSLINE_CONFIG <value>" / "STATUSLINE_CONFIG <value>" form.
    m = re.search(r"STATUSLINE_CONFIG\s+(\"[^\"]*\"|'[^']*'|\S+)", command)
    if m:
        return m.group(1)
    return None


def _tokenize_command(command):
    """Tokenize a command string, honoring simple single/double quoting."""
    if not command:
        return []
    return re.findall(r"\"[^\"]*\"|'[^']*'|\S+", command)


def _parse_command(command):
    """Parse a statusLine.command string.

    Returns (statusline_py, launcher, config_path) where:
      - statusline_py: absolute, forward-slashed path to the real renderer, or
        None if no script token was found. If the command points at a .ps1
        wrapper, the real script is statusline.py in the SAME directory (used
        only if it exists).
      - launcher: the leading words before the script token (e.g. "uv run",
        "python3", "python", "powershell -NoProfile -File"), or None.
      - config_path: STATUSLINE_CONFIG value from the command (normalized) if
        present, else <dir of statusline.py>/statusline_config.json, else None.
    """
    tokens = _tokenize_command(command)
    if not tokens:
        return None, None, None

    # Find the first token that ends in .py or .ps1 (strip quotes for the test).
    script_idx = None
    for i, tok in enumerate(tokens):
        bare = tok.strip('"').strip("'")
        low = bare.lower()
        if low.endswith(".py") or low.endswith(".ps1"):
            script_idx = i
            break

    statusline_py = None
    launcher = None
    script_dir = None

    if script_idx is not None:
        raw_script = tokens[script_idx].strip('"').strip("'")
        norm_script = _normalize_path(raw_script)
        launcher_tokens = tokens[:script_idx]
        # Drop any STATUSLINE_CONFIG=... env prefix from the launcher words.
        launcher_tokens = [
            t for t in launcher_tokens
            if not t.strip('"').strip("'").startswith("STATUSLINE_CONFIG")
        ]
        launcher = " ".join(launcher_tokens).strip() or None

        if norm_script and norm_script.lower().endswith(".ps1"):
            # PowerShell wrapper: the real renderer is statusline.py next to it.
            script_dir = os.path.dirname(norm_script)
            sibling = (script_dir + "/statusline.py") if script_dir else None
            if sibling and os.path.isfile(sibling):
                statusline_py = sibling
            else:
                # No sibling renderer found; keep the wrapper as the best guess
                # and still derive the config dir from the wrapper's directory.
                statusline_py = norm_script
        else:
            statusline_py = norm_script
            script_dir = os.path.dirname(norm_script) if norm_script else None

    # config_path: explicit STATUSLINE_CONFIG in the command wins; else the
    # default next to statusline.py.
    raw_cfg = _extract_config_from_command(command)
    if raw_cfg:
        config_path = _normalize_path(raw_cfg)
    elif script_dir:
        config_path = (script_dir + "/statusline_config.json")
    else:
        config_path = None

    return statusline_py, launcher, config_path


# --------------------------------------------------------------------------- #
# Subcommand: locate
# --------------------------------------------------------------------------- #
def _result(statusline_py=None, config_path=None, settings_json=None,
            launcher=None, source="none", notes=""):
    """Build the canonical locate result dict (pointer_path always included)."""
    return {
        "statusline_py": statusline_py,
        "config_path": config_path,
        "settings_json": settings_json,
        "launcher": launcher,
        "source": source,
        "pointer_path": POINTER_PATH,
        "notes": notes,
    }


def _locate_from_pointer():
    """Try to resolve from the pointer file. Return a result dict or None."""
    if not os.path.isfile(POINTER_PATH):
        return None
    data = _read_json_file(POINTER_PATH)
    if not isinstance(data, dict):
        return None
    config_path = data.get("config_path")
    statusline_py = data.get("statusline_py")
    # Consider the pointer valid if the config file exists OR its parent dir
    # exists (the config may not have been written yet but the install is real).
    cfg_ok = False
    if config_path:
        if os.path.exists(config_path):
            cfg_ok = True
        else:
            parent = os.path.dirname(config_path)
            if parent and os.path.isdir(parent):
                cfg_ok = True
    if not cfg_ok:
        return None
    return _result(
        statusline_py=statusline_py,
        config_path=config_path,
        settings_json=data.get("settings_json"),
        launcher=data.get("launcher"),
        source="pointer",
        notes="resolved from pointer %s" % POINTER_PATH,
    )


def _locate_from_settings():
    """Try to resolve from settings.json. Return a result dict or None."""
    chain = _find_settings_chain()
    for path, kind in chain:
        data = _read_json_file(path)
        if not isinstance(data, dict):
            continue
        block = data.get("statusLine")
        if not isinstance(block, dict):
            continue
        command = block.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        statusline_py, launcher, config_path = _parse_command(command)
        if not statusline_py:
            continue
        return _result(
            statusline_py=statusline_py,
            config_path=config_path,
            settings_json=path.replace("\\", "/"),
            launcher=launcher,
            source="settings",
            notes="read statusLine.command from %s settings.json: %s"
                  % (kind, path.replace("\\", "/")),
        )
    return None


def cmd_locate(args):
    """Resolve the active statusline; always print valid JSON and exit 0."""
    try:
        result = _locate_from_pointer()
        if result is None:
            result = _locate_from_settings()
        if result is None:
            chain = _find_settings_chain()
            if chain:
                where = ", ".join(p.replace("\\", "/") for p, _ in chain)
                notes = ("no usable statusLine.command found (checked: %s); "
                         "ask the user where the statusline is" % where)
            else:
                notes = ("no pointer and no settings.json found "
                         "(searched upward from CWD and ~/.claude); "
                         "ask the user where the statusline is")
            result = _result(source="none", notes=notes)
    except Exception as e:
        # Robustness: never throw out of locate -- emit a valid 'none' result.
        result = _result(
            source="none",
            notes="locate failed unexpectedly (%s: %s)"
                  % (type(e).__name__, e),
        )
    print(json.dumps(result))
    return 0


# --------------------------------------------------------------------------- #
# Subcommand: save-pointer
# --------------------------------------------------------------------------- #
def cmd_save_pointer(args):
    """Write the pointer file (path computed from HERE)."""
    payload = {
        "statusline_py": _normalize_path(args.statusline_py),
        "config_path": _normalize_path(args.config),
        "settings_json": (_normalize_path(args.settings)
                          if args.settings else None),
        "launcher": args.launcher if args.launcher else None,
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    parent = os.path.dirname(POINTER_PATH)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(POINTER_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(POINTER_PATH)
    return 0


# --------------------------------------------------------------------------- #
# Subcommand: read-pointer
# --------------------------------------------------------------------------- #
def cmd_read_pointer(args):
    """Print the pointer JSON, or {"source": "none"} if it is absent/unreadable."""
    if not os.path.isfile(POINTER_PATH):
        print(json.dumps({"source": "none"}))
        return 0
    data = _read_json_file(POINTER_PATH)
    if not isinstance(data, dict):
        print(json.dumps({"source": "none"}))
        return 0
    print(json.dumps(data))
    return 0


# --------------------------------------------------------------------------- #
# Subcommand: commit  (history-aware install of a new config)
# --------------------------------------------------------------------------- #
def _unique_snapshot_path(history_dir, stamp):
    """Return a non-colliding snapshot path: YYYYMMDD-hhmm[.-N].json."""
    base = os.path.join(history_dir, "%s.json" % stamp)
    if not os.path.exists(base):
        return base
    n = 2
    while True:
        candidate = os.path.join(history_dir, "%s-%d.json" % (stamp, n))
        if not os.path.exists(candidate):
            return candidate
        n += 1


def cmd_commit(args):
    """History-aware install: snapshot current ACTIVE, then write NEWFILE to it."""
    active = os.path.abspath(args.config)
    newfile = os.path.abspath(args.new)

    # 1) Validate the NEW config parses as JSON. If not, touch NOTHING.
    try:
        with open(newfile, "rb") as f:
            new_bytes = f.read()
    except Exception as e:
        print("ERROR: cannot read new config %s (%s). Nothing was changed."
              % (newfile, e), file=sys.stderr)
        return 4
    try:
        json.loads(new_bytes.decode("utf-8"))
    except Exception as e:
        print("ERROR: new config %s is not valid JSON (%s). "
              "Nothing was changed." % (newfile, e), file=sys.stderr)
        return 4

    # 2) Prepare the history directory next to the ACTIVE config.
    active_dir = os.path.dirname(active)
    history_dir = os.path.join(active_dir, ".statusline-config-history")
    if not os.path.isdir(history_dir):
        os.makedirs(history_dir, exist_ok=True)

    now = datetime.datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M")

    # 3) Snapshot the CURRENT ACTIVE bytes (if any) before overwriting.
    snapshot_path = None
    if os.path.isfile(active):
        snapshot_path = _unique_snapshot_path(history_dir, stamp)
        shutil.copy2(active, snapshot_path)

    # 4) Install the new config (verbatim bytes; the skill already shaped it).
    if active_dir and not os.path.isdir(active_dir):
        os.makedirs(active_dir, exist_ok=True)
    with open(active, "wb") as f:
        f.write(new_bytes)

    # 5) Append a human-readable changelog entry for today.
    changelog_path = os.path.join(history_dir, "%s.md" % now.strftime("%Y%m%d"))
    new_log = not os.path.exists(changelog_path)
    snap_name = (os.path.basename(snapshot_path)
                 if snapshot_path else "none (new config)")
    if args.diff:
        # Indent each diff line by two spaces so it nests under "Changes:".
        diff_block = "\n".join(
            "  " + line for line in args.diff.splitlines()) or "  (empty)"
    else:
        diff_block = "  (not provided)"

    entry_lines = []
    if new_log:
        entry_lines.append(
            "# Statusline config change log — %s\n"
            % now.strftime("%Y-%m-%d"))
    entry_lines.append("## %s — %s" % (now.strftime("%H:%M"), args.skill))
    entry_lines.append("- Previous version: `%s`" % snap_name)
    entry_lines.append("- Summary: %s" % args.summary)
    entry_lines.append("- Why: %s" % args.why)
    entry_lines.append("- Changes:")
    entry_lines.append(diff_block)
    entry_lines.append("")  # trailing blank line between entries
    entry = "\n".join(entry_lines) + "\n"

    with open(changelog_path, "a", encoding="utf-8") as f:
        f.write(entry)

    print(json.dumps({
        "snapshot": (snapshot_path.replace("\\", "/")
                     if snapshot_path else None),
        "changelog": changelog_path.replace("\\", "/"),
        "config_path": active.replace("\\", "/"),
    }))
    return 0


# --------------------------------------------------------------------------- #
# CLI dispatch
# --------------------------------------------------------------------------- #
def build_parser():
    parser = argparse.ArgumentParser(
        prog="statusline_io.py",
        description="Shared I/O helper for the statusline skills: resolve the "
                    "active statusline and perform history-aware config writes.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser(
        "locate",
        help="resolve the active statusline (pointer -> settings.json -> none); "
             "prints a JSON object, always exit 0")

    sp = sub.add_parser(
        "save-pointer",
        help="write the gitignored pointer file recording the active install")
    sp.add_argument("--statusline-py", required=True,
                    help="path to the active statusline.py")
    sp.add_argument("--config", required=True,
                    help="path to the active statusline_config.json")
    sp.add_argument("--settings", default=None,
                    help="path to the settings.json that wires it (optional)")
    sp.add_argument("--launcher", default=None,
                    help='launcher words, e.g. "uv run" or "python3" (optional)')

    rp = sub.add_parser(
        "read-pointer",
        help='print the pointer JSON, or {"source": "none"} if absent')

    cm = sub.add_parser(
        "commit",
        help="history-aware install of a new config (snapshot + changelog)")
    cm.add_argument("--config", required=True,
                    help="path to the ACTIVE config to replace")
    cm.add_argument("--new", required=True,
                    help="path to the fully-formed NEW config file to install")
    cm.add_argument("--skill", required=True,
                    help="name of the skill performing the write")
    cm.add_argument("--summary", required=True,
                    help="short summary of what changed")
    cm.add_argument("--why", required=True,
                    help="the user intent behind the change")
    cm.add_argument("--diff", default=None,
                    help="optional human-readable list of key changes")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        # No subcommand: print usage and exit 2 (no destructive default action).
        parser.print_help(sys.stderr)
        return 2

    dispatch = {
        "locate": cmd_locate,
        "save-pointer": cmd_save_pointer,
        "read-pointer": cmd_read_pointer,
        "commit": cmd_commit,
    }
    handler = dispatch[args.command]
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
