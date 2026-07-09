# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram-controlled bot that automates daily clock in/out on Sesame Time
(`app.sesametime.com`) through a headless Chromium (Playwright) browser, for an
account that logs in via Microsoft SSO. It runs as a single long-lived asyncio
process inside one Docker container.

## Commands

```bash
# Run (only supported way — Playwright/Chromium deps are baked into the image)
docker compose up -d --build
docker compose logs -f
docker compose restart

# Local dev without Docker (needs Playwright browsers installed locally)
pip install -r requirements.txt
playwright install --with-deps chromium
python -m app.main   # reads config from process env, not .env — export vars or use dotenv manually
```

There is no test suite, linter, or CI in this repo. `.env` (gitignored) is the only config source; copy `example.env` to `.env` and fill it in — see `Config.from_env()` in `app/config.py` for every variable read and its default.

Config is env-only, validated in `app/config.py::Config.from_env()`. `TELEGRAM_BOT_TOKEN` and `SESAME_EMAIL` are required (raises `SystemExit` if missing); everything else has a default.

## Architecture

Five modules under `app/`, wired together in `main.py`:

- **`config.py`** — `Config` dataclass, loaded once from env vars at startup.
- **`state.py`** — `State`: tiny JSON persistence (`./data/state.json`) for the skip-day list and the current day's planned event times/done-flags. No DB.
- **`sesame.py`** — `Sesame`: all Playwright automation. Owns the single browser page behind an `asyncio.Lock` (one browser interaction at a time, across scheduled events, manual commands, and status checks). Two capability groups:
  - **Clocking**: `clock_in()` / `clock_out()` / `get_clock_state()`, driving the dashboard's Entrar/Salir buttons. The DOM alone cannot represent "on a pause" (during lunch, Salir stays visible next to Entrar and reads as "in"), so every clock action resolves the state through `_verified_state()` (checks API wins over DOM) before deciding it's a no-op, and `_confirm_api_state()` re-polls the API after each click to catch clicks that visually "worked" but changed nothing.
  - **Login**: `_ensure_logged_in()` drives an SSO state machine (`_sesame_login_step` → Microsoft's multi-step flow in `_microsoft_step`) purely by polling `page.url` and known selectors every 3s (`LOGIN_STEP_WAIT_MS`), for up to ~5 minutes (`LOGIN_MAX_STEPS`) to allow phone-based MFA approval. When Microsoft needs a password/code/number-match, it calls back into `bot.ask(...)` and blocks until the user replies in Telegram.
  - **Data fetching (no DOM scraping)**: `get_time_off()` and `get_day_stats()` don't parse the calendar/signings UI. They navigate to the relevant SPA page, sniff the back-office JSON responses it fetches (`_capture_api_payloads`, matched via `SESAME_API_RE`/`TIMEOFF_URL_RE`), and learn the per-employee API base URL + auth headers (`_api_base`, `_api_auth`) from the first sniff so later calls can hit `.../checks` and `.../daily-computed-hour-stats` directly via `context.request` (`_api_get_json`), skipping the UI. Every captured payload is dumped to `./data/api-dumps/*.json` for offline debugging (see the sibling memory on the offline HAR-based testing technique).
  - Persistent browser profile at `./data/profile` (Playwright `launch_persistent_context`) keeps SSO cookies/session alive across restarts so MFA is rarely required.
  - Selectors for both Sesame's dashboard and Microsoft's login pages are grouped as module-level constants at the top of `sesame.py` (`BTN_CLOCK_IN`, `MS_EMAIL`, `MS_PROOF_SMS`, ...) — this is the first place to look/edit when the UI changes and `/in`, `/out`, or login start failing.
- **`scheduler.py`** — `ClockScheduler`, built on APScheduler (`AsyncIOScheduler`). One cron job (`plan_day`, weekdays at `PLAN_TIME`) computes and schedules that day's four datetime jobs (clock-in, break-start, break-end, clock-out), each jittered by `±JITTER_MINUTES`. `plan_day` also: checks the skip list, checks Sesame's absence API (`sesame.today_time_off()`) and skips full-day absences/holidays, computes a half-day work window when a partial absence fits (`_half_day_window`), and prefers Sesame's own `to_work_seconds` (from `get_day_stats`) over the static `WORK_MINUTES` fallback (so e.g. summer hours apply automatically). `_recover()` runs once at boot to reschedule the current day's still-pending events after a container restart (state read from `State`). Each event job retries once after a 60s wait before reporting failure to Telegram.
  - **Office days are reminder-only.** On days where `work_type_for_day()` says "Normal" (Thursdays), Sesame IP-blocks clock-ins from outside the office network, so `plan_day` branches to `_plan_office_day`: instead of clock jobs it schedules Telegram reminders (`OFFICE_EVENT_ORDER` names, `OFFICE_REMIND_*` env vars). The clock-in/back-from-lunch reminders re-check the checks API and repeat twice at `OFFICE_REMIND_REPEAT_MINUTES` spacing while the user hasn't clocked; `_watch_clock_out` self-reschedules until real worked seconds reach the day's target, then sends the final "Salir" reminder (gives up at 22:00). Reminders that reschedule themselves write the new time back via `State.set_time` so `_recover` can resume them; a missed reminder is re-run immediately at recovery (safe — each one verifies the clock state before sending).
- **`bot.py`** — `TgBot`: python-telegram-bot polling app. All commands are chat-id-gated (`_guard`) to the single `TELEGRAM_CHAT_ID` in config; on first message with no chat id configured, it replies with the chat id to copy into `.env`. `concurrent_updates(True)` is required so an MFA-code text reply can reach `_on_text` (resolves a pending `asyncio.Future` set up by `bot.ask()`) while another handler is blocked awaiting the Playwright lock. Command list lives once in `COMMANDS` and drives both `/help` text and Telegram's slash-autocomplete menu.
- **`main.py`** — constructs the four objects above, wires `bot.attach(sesame, scheduler)` (breaks the circular `Sesame`/`ClockScheduler` ↔ `TgBot` dependency), starts polling + scheduler, runs a one-off startup login check (surfaces MFA immediately at boot rather than waiting for the first scheduled event), then blocks forever.

## Working on this repo

- Any change to Sesame's page structure only requires touching the selector constants at the top of `sesame.py`, not the flow logic below them. Confirm a selector fix by running `/in`/`/out`/`/absences` manually rather than guessing from source alone — the bot sends a screenshot to Telegram on failure (also saved under `./data/screenshots`).
- Anything that talks to the back-office API (`get_time_off`, `get_day_stats`) should be validated against a real captured payload before changing parsing logic — dumps accumulate under `./data/api-dumps/`. Prefer testing new parser logic offline against an existing dump over re-triggering a live Playwright run.
- `./data/` (state, browser profile with live session cookies, screenshots, API dumps) is gitignored and must stay that way — never commit it.
- `EVENT_ORDER`/`OFFICE_EVENT_ORDER`/`EVENT_LABELS` in `state.py` are the canonical event lists — auto-clock events (`clock_in`, `break_start`, `break_end`, `clock_out`) on remote days, reminder events (`remind_*`) on office days; a day's plan holds one set or the other, and `scheduler.py`/`bot.py` key off these names when reading/writing plan state.
