# sesame-autoclock

Automates the daily Sesame Time clock in/out (Entrar / Salir) through a
headless Chromium driven by Playwright, controlled and supervised via a Telegram bot.

Daily flow on remote days (all times jittered ±`JITTER_MINUTES`):

1. `PLAN_TIME` — checks the skip list and the Sesame absences page
   (`/employee/absences?year=...`). If today is a vacation/public holiday it does
   nothing; otherwise it announces the day's plan on Telegram (reply `/skip` to cancel),
   including today's work type (see below).
   The day's length comes from Sesame's own schedule when available (so e.g. summer
   hours are honoured automatically); `WORK_MINUTES` is the fallback.
2. `CLOCK_IN` — Entrar, then selects the work-type dropdown option for the day
3. `BREAK_START` — Pausa → Lunch (a pause, not a full clock-out)
4. `BREAK_START + BREAK_MINUTES` — Entrar, same work-type selection as step 2
5. Clock out (Salir) when total worked time reaches `WORK_MINUTES` (pause excluded)

Every "Entrar" click opens a dropdown to pick "Normal" (in-office) or "Remote".
Which one applies is hardcoded by weekday in `work_type_for_day()` at the top
of `app/sesame.py` (`REMOTE_WORK_WEEKDAYS`) — currently Remote on Mon/Tue/Wed/Fri,
Normal on Thursday. Picking the wrong option for the day leaves the dropdown open
and the dashboard never shows the Salir button, which is what a mismatch looks like
in the logs/screenshot. Edit `REMOTE_WORK_WEEKDAYS` there if the policy changes.

On office days (Thursdays) the bot never clicks anything: "Normal" clock-ins are
IP-restricted and Sesame rejects them from outside the office network ("IPs
Bloqueadas — Su IP no es válida"). Instead, `PLAN_TIME` plans a reminder-only day
(no jitter — these are nudges, not clock events):

1. `OFFICE_REMIND_CLOCK_IN` (07:30) — "clock in" reminder, repeated twice every
   `OFFICE_REMIND_REPEAT_MINUTES` (07:45, 08:00) while the signings API shows you
   haven't clocked in yet
2. `OFFICE_REMIND_BREAK_START` (12:50) — single "start the lunch pause" reminder
   (skipped if you already paused or clocked out)
3. `OFFICE_REMIND_BREAK_END` (14:00) — "clock back in" reminder, repeated twice
   (14:15, 14:30) while you're still on the pause
4. A clock-out watcher polls the signings API from just after the back-from-lunch
   reminder and messages you the moment your actual worked time (from your real
   clock-ins, pauses excluded) reaches the day's planned hours. It gives up at 22:00.

Reminders check the real clock state through the API first, so once you've clocked
they go quiet on their own.

Once clocked in, Sesame shows two separate buttons side by side: "Pausa" (opens a
Descanso/Lunch dropdown, starts a break) and "Salir" (`#button-click-sign-out`,
ends the day). The scheduler only ever clicks "Salir" for the final `CLOCK_OUT`
event — the lunch break always goes through "Pausa" → "Lunch" so it doesn't
accidentally end the whole day early.

The Microsoft SSO session lives in a persistent browser profile under `./data/profile`,
so MFA is only needed occasionally. When it is, the bot relays it over Telegram:
number-matching prompts send you the number to approve in Authenticator; code prompts
ask you to reply with the code.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather), copy the token.
2. `cp example.env .env` and fill in `TELEGRAM_BOT_TOKEN` and `SESAME_EMAIL`
   (leave `TELEGRAM_CHAT_ID` empty for now).
3. `docker compose up -d --build`
4. Message your bot — it replies with your chat id. Put it in `.env` as
   `TELEGRAM_CHAT_ID` and `docker compose up -d` again.
5. On startup the container performs a login check: follow the MFA prompts the bot
   sends you. When Microsoft shows "Stay signed in?" the script answers Yes, so this
   should be rare afterwards.

## Telegram commands

| Command | Effect |
|---|---|
| `/status` | Clock state, today's signings (from the Sesame API), worked vs planned hours, and today's plan |
| `/skip [YYYY-MM-DD]` | Don't clock that day (default today; cancels pending events) |
| `/unskip [YYYY-MM-DD]` | Undo a skip |
| `/in` / `/out` | Manual clock in / out |
| `/absences` | Run the absence check for today and show the evidence |
| `/screenshot` | Screenshot of the current browser page |

Any failure sends a screenshot to Telegram; screenshots are also kept in
`./data/screenshots`.

## How the absence check works

Instead of parsing the calendar DOM, the app captures the JSON responses the absences
page fetches (URLs containing `absen|holiday|vacation|time-off|leave|festiv`) and looks
for today's date, either as a single date field or inside a from/to range. Run
`/absences` on a day you know is a holiday and on a normal day to confirm it matches
your tenant's payloads — if it doesn't, the URL filter and key patterns live at the
bottom of `app/sesame.py`.

## How the signings data works

`/status` and the daily planner use the same back-office API the signings page
(`/employee/signings/all`) talks to: `.../employees/<id>/checks` (each work/pause
interval; a missing checkOut means currently clocked in) and
`.../daily-computed-hour-stats` (worked vs planned seconds per day). The first call
sniffs the signings page to learn the API host and auth headers; later calls hit the
API directly, which is much faster. Every capture is dumped under `./data/api-dumps`
for offline parser debugging.

## Selectors may need a first-run tweak

Button texts/selectors are grouped at the top of `app/sesame.py` (`BTN_CLOCK_IN`,
`OPT_WORK_TYPE`, `SSO_BUTTON`, ...). If the first `/in` fails, the bot sends a
screenshot — adjust the selectors to what you see and rebuild.

## Notes

- This creates official time records. It only formalizes hours you actually work —
  `/skip` any sick day or ad-hoc absence the Sesame calendar doesn't know about.
- State (skip list, today's plan) lives in `./data/state.json`; the browser profile in
  `./data/profile` contains live session tokens — both are gitignored, don't commit them.
- If login gets flaky under headless Chromium, set `HEADLESS=false` and run under
  xvfb (`xvfb-run python -m app.main`) as a fallback.
