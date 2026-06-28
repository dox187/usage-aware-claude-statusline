---
name: statusline-install
description: >-
  Deploy the usage-aware Claude Code statusline and wire it into Claude Code on
  macOS, Linux, or Windows. Invoke when the user says things like "install the
  statusline", "set up the statusline", "deploy statusline.py", "wire the
  statusline into Claude Code", "add the statusLine to my settings.json", "make
  the statusline work in every project", "the statusline isn't showing up", or
  "fix my statusline command / path on Windows". Handles runtime detection (uv /
  python), copying the files, merging the statusLine block into settings.json,
  the Windows forward-slash path rule + PowerShell wrapper fallback, OAuth token
  setup per OS, and a final verification render.
---

# statusline-install — deploy & wire the statusline into Claude Code

Goal: get `statusline.py` running and wired into Claude Code's `settings.json`
on whatever OS the user is on, without clobbering their existing config, and
verify it actually renders. Work through the steps below in order. **Never run an
installer or write `settings.json` without explicit user confirmation.**

The repo lives at the root that contains `statusline.py`, `claude_usage.py`,
`statusline_config.json`, and this skill under `.claude/skills/statusline-install/`.
Refer to that root as `REPO`. This skill bundles:

- `merge_settings.py` — stdlib helper that merges a `statusLine` block into a
  `settings.json`, preserving every other key (and carrying forward an existing
  `padding`); it backs up to `<file>.bak` first.
- `assets/settings_snippet.json` — the example `statusLine` block.
- `assets/statusline.ps1.template` — a Windows PowerShell wrapper, used **only**
  when Git Bash is absent on Windows.
- `assets/sample_input.json` — canonical session JSON for the verify render.

---

## Step 1 — Detect the OS

Determine the platform first; every later command branches on it.

- PowerShell: `$IsWindows`, `$IsMacOS`, `$IsLinux`
  (or `[System.Environment]::OSVersion.Platform`).
- bash: `uname -s` (`Darwin` = macOS, `Linux` = Linux; Windows users are usually
  in Git Bash here).

Map to one of: **win32**, **darwin**, **linux**. Tell the user which you detected.

---

## Step 2 — Detect runtimes (uv, python) and report versions

Check what is available and report versions back to the user:

- **uv**: `uv --version`
- **python**: try `python3 --version` first (macOS/Linux), then `python --version`
  (common on Windows). The floor is **Python 3.7** (the renderer uses
  `datetime.fromisoformat`). If only an older Python is found, say so.

Decision:

- If **uv** is present → the launcher will be `uv run <path>`.
- Else if **python3/python ≥ 3.7** is present → launcher `python3 <path>`
  (macOS/Linux) or `python <path>` (Windows). This is perfectly fine — the
  project is stdlib-only, so no `pip install` is ever needed.
- If neither: offer to install **uv** (do not run without confirmation):
  - macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
  - Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

  uv is OPTIONAL — installing a plain Python 3.7+ is an equally valid path. Let
  the user choose. Only run an installer after explicit confirmation.

**Remember the launcher you settled on here — call it `LAUNCHER`** (one of
`uv run`, `python3`, or `python`). The command strings in Steps 5, 6, and 8 use it.

---

## Step 3 — Ask the run location

Use AskUserQuestion. Offer:

1. **`~/.claude` (recommended)** — global, the statusline shows in every project.
   On Windows that is `C:/Users/<you>/.claude`.
2. **A specific project** — copy into that project's `.claude/` folder; the
   statusline applies only there (project `settings.json` overrides the user one).
3. **A custom path** — free-text.

Also ask (separately, yes/no): **copy the skills into `~/.claude/skills`** so the
`/statusline-*` commands work in every project, not just this repo. If yes, copy
the whole `REPO/.claude/skills/` tree to `~/.claude/skills/`.

Call the chosen directory `DEST`.

---

## Step 4 — Copy files to DEST (never clobber an existing config)

Copy from `REPO` to `DEST`:

- `statusline.py` — **required**.
- `claude_usage.py` — **required**. It MUST sit in the **same folder** as
  `statusline.py`; the renderer imports it as a sibling module. Do not split them.
- `statusline_config.json` — copy **only if `DEST/statusline_config.json` does
  NOT already exist**. If one is there, leave it untouched and tell the user you
  preserved their existing config.
- Optional extras (offer, default no): `statusline_editor.py`, `README*`,
  `examples/`.

Create `DEST` if needed (`mkdir -p` / `New-Item -ItemType Directory -Force`).

Per-OS copy examples (adjust `DEST`):

- bash (macOS/Linux/Git Bash):
  ```sh
  mkdir -p ~/.claude
  cp "$REPO/statusline.py" "$REPO/claude_usage.py" ~/.claude/
  [ -f ~/.claude/statusline_config.json ] || cp "$REPO/statusline_config.json" ~/.claude/
  ```
- PowerShell (Windows):
  ```powershell
  $dest = "$env:USERPROFILE\.claude"
  New-Item -ItemType Directory -Force $dest | Out-Null
  Copy-Item "$REPO\statusline.py","$REPO\claude_usage.py" $dest
  if (-not (Test-Path "$dest\statusline_config.json")) {
    Copy-Item "$REPO\statusline_config.json" $dest
  }
  ```

---

## Step 5 — Wire settings.json (use the bundled `merge_settings.py`)

Pick the target `settings.json`:

- Default / `~/.claude` install → `~/.claude/settings.json`
  (Windows: `C:/Users/<you>/.claude/settings.json`).
- Project install → that project's `.claude/settings.json`.

Build the **command string** from `LAUNCHER` (Step 2) and the `statusline.py`
path in `DEST`. Prefer `~` or `/c/...` over absolute backslash paths:

| OS | uv present | uv absent |
|----|-----------|-----------|
| macOS / Linux | `uv run ~/.claude/statusline.py` | `python3 ~/.claude/statusline.py` |
| Windows (Git Bash present) | `uv run ~/.claude/statusline.py` | `python ~/.claude/statusline.py` |
| Windows (no Git Bash) | see PowerShell-wrapper rule below | same |

> ### WINDOWS PATH RULE — STATE THIS LOUDLY TO THE USER
> On Windows, Claude Code runs the `statusLine.command` through **Git Bash when
> it is installed**, otherwise through **PowerShell**. Git Bash treats `\` as an
> escape character, so a path like `C:\Users\you\.claude\statusline.py` is
> **mangled and fails silently**. **Always use forward slashes or `~`**:
> `~/.claude/statusline.py` or `/c/Users/you/.claude/statusline.py` — **never**
> backslashes.
>
> **If Git Bash is NOT installed on Windows**, the bare `uv run ...` / `python ...`
> command will not run. Instead install the bundled wrapper:
> 1. Copy `assets/statusline.ps1.template` to `~/.claude/statusline.ps1`
>    (`C:/Users/<you>/.claude/statusline.ps1`).
> 2. Edit its `$Script` line to point at the deployed `statusline.py`, and pick
>    `uv` vs `python` to match what is installed (the template already prefers
>    `uv` and falls back to `python`).
> 3. Set the command to:
>    `powershell -NoProfile -File C:/Users/<you>/.claude/statusline.ps1`
>
> To check for Git Bash: `where.exe bash` / `Get-Command bash` (a path under
> `Git\...` means Git Bash is present).

Now run the merge helper. It preserves every other key in `settings.json`,
creates the file/parent dir if missing, backs up to `<file>.bak`, and — if a
previous `statusLine` block already had a custom `padding` (or other extra
keys) — **carries those forward** while updating only `type` and `command`.

**In the examples below, substitute the `LAUNCHER` you chose in Step 2**
(`uv run`, `python3`, or `python`) for `<LAUNCHER>` in the command string — do
NOT leave it pinned to `uv run` if uv is not installed, or the statusline will
render empty:

```sh
# macOS / Linux / Git Bash  (replace <LAUNCHER> with uv run / python3 / python)
python3 "$REPO/.claude/skills/statusline-install/merge_settings.py" \
  ~/.claude/settings.json "<LAUNCHER> ~/.claude/statusline.py"
```

```powershell
# Windows  (replace <LAUNCHER> with uv run / python)
python "$REPO\.claude\skills\statusline-install\merge_settings.py" `
  "$env:USERPROFILE\.claude\settings.json" "<LAUNCHER> ~/.claude/statusline.py"
```

Before running it: **show the user the exact, fully-substituted command string
and the target settings.json path, and get explicit confirmation.**
`assets/settings_snippet.json` shows the resulting block if they prefer to paste
it by hand (edit its `command` to your launcher).

---

## Step 6 — Record the deployment (save the active-statusline pointer)

Now that `settings.json` is wired and the files are in `DEST`, record **which
statusline is the active one** so the other skills (`/statusline-config`,
`/statusline-theme`, `/statusline-preview`, `/statusline-doctor`) can target this
live install instantly instead of re-parsing `settings.json` or guessing the repo
copy every time.

Use the shared helper that lives in the **statusline-config** skill
(`statusline_io.py`). From this repo it is at
`REPO/.claude/skills/statusline-config/statusline_io.py`; if you copied the skills
into `~/.claude/skills`, the deployed copy at
`~/.claude/skills/statusline-config/statusline_io.py` works the same — the helper
computes the pointer location from its **own** path, so either copy writes the
right pointer. Run `save-pointer` with the paths you just resolved:

```sh
# macOS / Linux / Git Bash  (use the same LAUNCHER + paths you wired in Step 5)
python3 "$REPO/.claude/skills/statusline-config/statusline_io.py" save-pointer \
  --statusline-py ~/.claude/statusline.py \
  --config        ~/.claude/statusline_config.json \
  --settings      ~/.claude/settings.json \
  --launcher      "<LAUNCHER>"
```

```powershell
# Windows  (forward slashes / ~ in paths, same as Step 5)
python "$REPO\.claude\skills\statusline-config\statusline_io.py" save-pointer `
  --statusline-py ~/.claude/statusline.py `
  --config        ~/.claude/statusline_config.json `
  --settings      ~/.claude/settings.json `
  --launcher      "<LAUNCHER>"
```

Substitute the **actual resolved paths** for your `DEST` (e.g. a project
`.claude/...` install) and the `LAUNCHER` from Step 2.

> **`.ps1`-wrapper case:** always pass the **real deployed `statusline.py`** (the
> renderer the wrapper launches) as `--statusline-py`, and record the wrapper +
> PowerShell launcher in `--launcher` — e.g.
> `--statusline-py ~/.claude/statusline.py --launcher "powershell -NoProfile -File ~/.claude/statusline.ps1"`.
> Do **not** put the `.ps1` path in `--statusline-py`: `save-pointer` stores that
> value verbatim, so a later pointer-based `locate` would report the wrapper as
> the renderer. Recording the real `statusline.py` keeps the pointer consistent
> with how settings-based resolution unwraps a `.ps1` to its sibling renderer.

The helper prints the pointer path it wrote — a `.statusline-active.json` written
**inside** the resolved `.claude` directory (i.e.
`<...>/.claude/.statusline-active.json`). Tell the user where it landed.

> **These are machine-specific — gitignored, do not commit.**
> `.claude/.statusline-active.json` (the pointer) and
> `.statusline-config-history/` (the history-aware config snapshots + change log
> the writer skills create next to the active config) are per-machine deployment
> state. The repo `.gitignore` already excludes them; never add them to a commit.

---

## Step 7 — OAuth token (so usage segments populate)

Usage segments (`{usage_bars}`, `{usage_micro}`, `{compact_usage_micro}`,
`{usage_resets}`) need an OAuth token. `claude_usage.py` resolves it, never
writing it to disk, in this order:

1. **`CLAUDE_CODE_OAUTH_TOKEN`** env var (any OS).
2. **macOS login Keychain** (`Claude Code-credentials`) — read automatically via
   `/usr/bin/security`, **macOS only**. Nothing to configure.
3. **`~/.claude/.credentials.json`** with `claudeAiOauth.accessToken` — Linux /
   Windows / headless. Windows path: `C:/Users/<you>/.claude/.credentials.json`.

Per OS:

- **macOS** — usually automatic via Keychain once the user has signed into
  Claude Code. Nothing to do.
- **Linux / Windows** — verify `~/.claude/.credentials.json` exists and contains
  `claudeAiOauth.accessToken`. If it is missing, suggest either:
  - run `claude setup-token`, or
  - set `CLAUDE_CODE_OAUTH_TOKEN` in the environment.

If there is **no token, that is fine** — the usage segments simply render empty
and the rest of the bar still works. Do not block the install on this.

---

## Step 8 — VERIFY (test-render with the sample input)

Render the real `statusline.py` against the bundled
`assets/sample_input.json`. Point `STATUSLINE_CONFIG` at the deployed config so
the test is faithful (or omit it to use the one beside the script). Use the same
`LAUNCHER` you wired into `settings.json`:

- PowerShell:
  ```powershell
  $env:STATUSLINE_CONFIG="$env:USERPROFILE\.claude\statusline_config.json"
  Get-Content "$REPO\.claude\skills\statusline-install\assets\sample_input.json" | uv run ~/.claude/statusline.py
  # fallback: ... | python ~/.claude/statusline.py
  ```
- bash:
  ```sh
  STATUSLINE_CONFIG=~/.claude/statusline_config.json \
    uv run ~/.claude/statusline.py < "$REPO/.claude/skills/statusline-install/assets/sample_input.json"
  # fallback: python3 instead of uv run
  ```

A **non-empty, colored multi-line** output = success. If a token is present,
confirm at least one **usage segment populated** (look for `s 43%…` / `w 67%…`,
or the full `{usage_bars}` line). If the output is empty:

- empty / nothing prints → the launcher or path is wrong (re-check the Windows
  path rule; try the fallback launcher; on Windows with no Git Bash use the
  `.ps1` wrapper).
- bar renders but usage is blank → no token; revisit Step 7.

Finally, tell the user to **restart Claude Code** (or open a new session) so it
picks up the new `settings.json`.

---

## Quick checklist

1. Detect OS.
2. Detect uv / python (≥3.7); pick `LAUNCHER`; offer to install uv only on
   confirmation.
3. Ask DEST (`~/.claude` recommended) + whether to copy skills globally.
4. Copy `statusline.py` + `claude_usage.py` (same folder); copy
   `statusline_config.json` only if absent.
5. Merge `statusLine` into `settings.json` via `merge_settings.py` using your
   chosen `LAUNCHER` (not hardcoded `uv run`) — forward slashes / `~` only on
   Windows; `.ps1` wrapper if no Git Bash. Confirm the exact command first.
6. Record the deployment: `statusline-config/statusline_io.py save-pointer`
   (--statusline-py / --config / --settings / --launcher) so the other skills
   target this live install. The pointer + `.statusline-config-history/` are
   gitignored — don't commit them.
7. Check the OAuth token per OS (optional — empty usage is OK).
8. Verify render with `assets/sample_input.json`; restart Claude Code.

English only. Never offer `{peak_label}` as a current option. Never recolor the
semantic `ctx_bar*` band keys.
