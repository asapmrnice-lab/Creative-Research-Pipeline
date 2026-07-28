# pavluk.md — how to run this on your machine

Instructions for a human setting this up for the first time, and for Claude (or any
AI agent) picking up the project cold. Read the **Mandatory** section before anything
else; skipping it is the only way this fails.

---

## What this actually is

Two separate things live in this repository. Don't confuse them.

| | What it is | Status |
|---|---|---|
| **The trial app** — `trial/telegram-archive/` | Third-party open-source tool ([GeiserX/Telegram-Archive](https://github.com/GeiserX/Telegram-Archive), GPL-3.0). Archives Telegram channels you have joined into a local SQLite database, downloads media, serves a web viewer. | Installed and verified working. This is what you run. |
| **The custom pipeline** — `*.md` design docs | Our own planned system: structured fact extraction on top of the archive. | Design only. **No code written yet.** |

The trial exists to answer one question before we write code: *does an existing tool
already cover "subscribe to channels and see the data"?* If yes, we build only the
extraction layer on top instead of rebuilding everything.

---

## Mandatory — read this first

**1. Nothing in `trial/` reaches you through git.** `trial/` is in `.gitignore`
(deliberately — it holds credentials and a session file). Cloning this repo gives you
the design docs and this file, and nothing else. You must run the setup below to
create the app locally. This is the single most common reason "it doesn't work."

**2. Python 3.14 or newer is required.** Not 3.12, not 3.13. Upstream declares
`requires-python = ">=3.14"`. Check with `python3 --version`.

**3. The virtual environment is machine-specific and must not be copied.** The one on
Anastasiia's Mac is pinned to `/opt/homebrew/opt/python@3.14/bin`. Copying the folder
between machines produces a venv that silently points at a Python that isn't there.
Always create your own with the commands below.

**4. The Telegram session file is full access to your account.** After you
authenticate, `trial/telegram-archive/data/session/` can read your messages without
your password and without a login code. Never commit it, never send it to anyone,
never put it in a shared folder. If it leaks, revoke it immediately in Telegram:
**Settings → Devices → terminate the session**.

**5. Use your own Telegram account and your own credentials.** Don't reuse someone
else's `api_id`/`api_hash` — they're tied to a person, and Telegram rate-limits and
bans on abuse.

---

## Credentials required

Exactly three values. Nothing else is needed to start.

| Variable | What it is | Where to get it |
|---|---|---|
| `TELEGRAM_API_ID` | Numeric app ID, e.g. `12345678` | [my.telegram.org/apps](https://my.telegram.org/apps) |
| `TELEGRAM_API_HASH` | 32-character hex string | same page |
| `TELEGRAM_PHONE` | Your number in international format, e.g. `+380671234567` | your own account |

To get the first two: open [my.telegram.org](https://my.telegram.org), log in with your
phone number (Telegram sends a code to the app, not by SMS), choose **API development
tools**, and create an application. Any name and platform will do. The page then shows
`App api_id` and `App api_hash`.

There is **no Anthropic/Claude API key needed** for the trial. The trial does no AI
work — that only becomes relevant if we build the extraction layer later.

---

## Setup

Run these from the repository root. They work on macOS and Linux. On Windows use WSL.

### 1. Prerequisites

```bash
python3 --version          # must be 3.14 or newer
git --version
```

If Python is too old: macOS `brew install python@3.14` · Ubuntu/Debian
`sudo apt install python3.14` (or use [pyenv](https://github.com/pyenv/pyenv) if your
distro doesn't package it yet).

Install `uv`, the package manager used here:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Get the app

```bash
mkdir -p trial
git clone --depth 1 https://github.com/GeiserX/Telegram-Archive.git trial/telegram-archive
cd trial/telegram-archive
rm -rf .git                # keeps it out of our repo's history
```

### 3. Create the environment

```bash
uv venv --python 3.14
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-viewer.txt
```

Verify it worked:

```bash
.venv/bin/python -c "import telethon, sqlalchemy; print('ok')"
```

### 4. Configure

```bash
cp .env.example .env
```

Then edit `.env`. Set the three credentials, and **override these four paths** — the
upstream defaults are Docker container paths (`/data/...`) and fail when running
natively with a read-only filesystem error:

```ini
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_PHONE=

# Native run — must be relative, not the upstream /data/... defaults
DB_PATH=./data/backups/telegram_backup.db
BACKUP_PATH=./data/backups
SESSION_DIR=./data/session

# Only archive channels. Excludes your private chats and groups entirely.
CHAT_TYPES=channels

# Your timezone, IANA name. Blank = UTC.
VIEWER_TIMEZONE=

# Local single-user trial: read-only viewer, no login.
ALLOW_ANONYMOUS_VIEWER=true
```

Create the directories:

```bash
mkdir -p data/backups data/session
```

### 5. Authenticate (interactive — needs your phone)

```bash
.venv/bin/python -m src auth
```

Telegram sends a code **to your Telegram app**, not by SMS. Enter it when prompted. If
you have two-factor auth enabled, it asks for that password too. This is a one-time
step; the session persists afterwards.

### 6. Run

```bash
.venv/bin/python -m src backup       # one-time sync
.venv/bin/python -m src stats        # how much was captured
.venv/bin/python -m src list-chats   # which channels were found
```

The first run on a busy account takes a while and may pause on Telegram rate limits
(`FloodWait`) — that's normal, it resumes on its own. To keep the first run small,
set `CHAT_IDS` in `.env` to two or three channel IDs before running.

For continuous operation instead of one-off: `.venv/bin/python -m src schedule`.

---

## Verify it worked

```bash
sqlite3 data/backups/telegram_backup.db \
  "SELECT COUNT(*) AS messages FROM messages; SELECT COUNT(*) AS chats FROM chats;"
```

Non-zero counts mean it's working. A `messages` count of 0 after a successful backup
usually means `CHAT_TYPES` excluded everything, or the account has not joined any
channels.

---

## Notes for Claude

Context you need that isn't obvious from the file tree:

- **Do not hardcode anything.** This is the project owner's explicit, emphasised
  constraint. Credentials, paths, thresholds, channel IDs, model names, and the
  extraction whitelist all live in config files or `.env` — never in source. There is a
  committed `.env.example`; secrets go only in `.env`, which is gitignored.
- **The `trial/` directory is a third-party GPL-3.0 codebase.** Don't modify it and
  don't copy code out of it into our own project. Our extraction layer must be a
  **separate process reading the database** — that avoids GPL derivative-work
  obligations, which forking or vendoring the code would not.
- **Our design docs contradict current requirements in three places** — storage
  (they lock SQLite/local-only; we now use Supabase), the absence of a status field
  (breaks once the workflow is unattended), and a Core Principle that forbids AI
  judgment outright (unresolved, and it blocks writing any LLM code). Treat the direct
  instructions of the project owner as authoritative over the locked docs.
- **`answers.md` and `research-pipeline-architecture.md` are duplicates** (identical
  content, different formatting). `answer.md` has a copy-paste corruption at lines
  37–48. Don't treat the duplication as meaningful.
- **The schema already covers most of our domain model.** `chats` ≈ Source,
  `messages` ≈ Research Item, `media` ≈ Media Asset, `sync_status` ≈ processing state.
  Missing: `structured_field` and `note` tables, and dedup columns
  (`content_hash`/`simhash`). Those are the parts worth building.
- **Never run `auth` on the owner's behalf** or ask for credentials in chat. It needs a
  live code from their phone; it is theirs to run.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Read-only file system: '/data'` | `.env` still has the Docker paths. Set `DB_PATH`, `BACKUP_PATH`, `SESSION_DIR` to the `./data/...` values in step 4. |
| `requires-python >=3.14` on install | Python too old. Check `python3 --version` and rebuild the venv with `uv venv --python 3.14`. |
| Venv errors after copying the folder | Expected — venvs aren't portable. Delete `.venv` and redo step 3. |
| `A wait of N seconds is required` | Telegram rate limit. Not an error; it waits and continues. Reduce scope via `CHAT_IDS` if it's excessive. |
| Backup succeeds but 0 messages | `CHAT_TYPES=channels` only captures **channels you have joined**. Join them in Telegram first. |
| Phone code never arrives | It goes to the Telegram app, not SMS. Check your other logged-in devices. |
