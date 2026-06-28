#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Merge a statusLine block into a Claude Code settings.json (stdlib only).

Reads an existing settings.json (or starts from {} if it does not exist),
sets the "statusLine" command while preserving every other top-level key AND
carrying forward any extra keys already on a previous statusLine block (most
importantly a user's custom "padding"), then writes the file back
pretty-printed (2-space indent, UTF-8, trailing newline). The parent
directory is created if needed; the original file is backed up to "<path>.bak"
before the first write.

Usage:
    python merge_settings.py <settings.json path> <command string>

Examples:
    python merge_settings.py ~/.claude/settings.json "uv run ~/.claude/statusline.py"
    python merge_settings.py ./.claude/settings.json "python3 ./.claude/statusline.py"

Exit codes:
    0  settings written successfully
    2  wrong number of arguments
    3  existing settings.json is present but not valid JSON / not a JSON object
       at the top level (left untouched)
"""
import json
import os
import shutil
import sys

USAGE = (
    "usage: python merge_settings.py <settings.json path> <command string>\n"
    '  e.g. python merge_settings.py ~/.claude/settings.json '
    '"uv run ~/.claude/statusline.py"'
)


def main(argv):
    if len(argv) != 3:
        print(USAGE, file=sys.stderr)
        return 2

    path = os.path.expanduser(argv[1])
    command = argv[2]

    # Load existing settings (preserve everything), or start fresh.
    settings = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            settings = json.loads(text) if text.strip() else {}
        except ValueError:
            print(
                "ERROR: %s exists but is not valid JSON. Fix or remove it, "
                "then re-run. Nothing was changed." % path,
                file=sys.stderr,
            )
            return 3
        if not isinstance(settings, dict):
            print(
                "ERROR: %s does not contain a JSON object at the top level. "
                "Nothing was changed." % path,
                file=sys.stderr,
            )
            return 3

    # Start from the existing statusLine block (if it is a dict) so a user's
    # tuned extra keys -- most importantly a custom "padding" -- survive. Then
    # set only the keys we own: type and command. If there was no usable block,
    # default padding to 0.
    existing = settings.get("statusLine")
    block = dict(existing) if isinstance(existing, dict) else {}
    block["type"] = "command"
    block["command"] = command
    block.setdefault("padding", 0)
    settings["statusLine"] = block

    # Ensure the parent directory exists.
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)

    # Back up an existing file once (do not overwrite an earlier backup).
    if os.path.exists(path):
        backup = path + ".bak"
        if not os.path.exists(backup):
            shutil.copy2(path, backup)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")

    action = "Updated" if existing is not None else "Added"
    print("%s statusLine in %s" % (action, path))
    print("  command: %s" % command)
    print("  padding: %s" % block["padding"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
