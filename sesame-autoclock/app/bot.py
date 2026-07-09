import asyncio
import logging
from datetime import date, datetime
from pathlib import Path

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Config
from .sesame import entry_emoji, format_entry, format_hm
from .state import EVENT_LABELS, EVENT_ORDER, OFFICE_EVENT_ORDER, State

log = logging.getLogger("bot")

# single source for /help and Telegram's "/" autocomplete menu
COMMANDS = [
    ("status", "clock state + today's signings + plan"),
    ("skip", "don't clock on a day: /skip [YYYY-MM-DD] (default today)"),
    ("unskip", "undo a skip: /unskip [YYYY-MM-DD]"),
    ("in", "clock in now (Entrar → Remote/Normal by weekday)"),
    ("out", "clock out now (Salir)"),
    ("absences", "fetch this year's absences & holidays and check today"),
    ("screenshot", "screenshot of the current browser page"),
    ("help", "show the available commands"),
]

HELP = "Commands:\n" + "\n".join(f"/{cmd} - {desc}" for cmd, desc in COMMANDS) + \
    "\nPlain text replies answer a pending question (e.g. an MFA code)."


class TgBot:
    """Telegram interface: notifications, MFA prompts and manual control."""

    def __init__(self, cfg: Config, state: State):
        self.cfg = cfg
        self.state = state
        self.sesame = None      # set via attach()
        self.scheduler = None   # set via attach()
        self._pending: asyncio.Future | None = None
        # concurrent updates: a command handler blocked on the browser lock must
        # not stall the queue — MFA code replies have to get through mid-login
        self.app = (
            Application.builder().token(cfg.bot_token).concurrent_updates(True).build()
        )

        for cmd, fn in [
            ("start", self._cmd_help), ("help", self._cmd_help),
            ("status", self._cmd_status), ("skip", self._cmd_skip),
            ("unskip", self._cmd_unskip), ("in", self._cmd_in),
            ("out", self._cmd_out), ("absences", self._cmd_absences),
            ("screenshot", self._cmd_screenshot),
        ]:
            self.app.add_handler(CommandHandler(cmd, self._guard(fn)))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._guard(self._on_text))
        )

    def attach(self, sesame, scheduler) -> None:
        self.sesame = sesame
        self.scheduler = scheduler

    async def start(self) -> None:
        await self.app.initialize()
        try:
            # populates Telegram's "/" autocomplete menu
            await self.app.bot.set_my_commands(
                [BotCommand(cmd, desc) for cmd, desc in COMMANDS]
            )
        except Exception:
            log.exception("could not register the bot command menu")
        await self.app.start()
        await self.app.updater.start_polling()
        log.info("telegram bot polling started")

    async def stop(self) -> None:
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    # --- outbound ------------------------------------------------------
    async def send(self, text: str) -> None:
        if self.cfg.chat_id:
            await self.app.bot.send_message(self.cfg.chat_id, text)

    async def send_photo(self, path: Path, caption: str = "") -> None:
        if self.cfg.chat_id:
            with open(path, "rb") as fh:
                await self.app.bot.send_photo(
                    self.cfg.chat_id, fh, caption=caption,
                    read_timeout=60, write_timeout=60,
                )

    async def ask(self, text: str, timeout: int = 300) -> str:
        """Send a question and wait for the next plain-text reply."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending = fut
        await self.send(text)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending = None

    # --- inbound -------------------------------------------------------
    def _guard(self, fn):
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
            chat = update.effective_chat
            if self.cfg.chat_id is None:
                await context.bot.send_message(
                    chat.id,
                    f"Your chat id is {chat.id}. Set TELEGRAM_CHAT_ID={chat.id} "
                    "in .env and restart the container.",
                )
                return
            if chat.id != self.cfg.chat_id:
                log.warning("ignoring message from unknown chat %s", chat.id)
                return
            try:
                await fn(update, context)
            except Exception:
                log.exception("handler %s failed", fn.__name__)
                await self.send(f"⚠️ {fn.__name__} failed, check container logs.")
        return wrapped

    async def _on_text(self, update: Update, _):
        if self._pending and not self._pending.done():
            self._pending.set_result(update.message.text.strip())
            await update.message.reply_text("Got it 👍")
        else:
            await update.message.reply_text(HELP)

    async def _cmd_help(self, update: Update, _):
        await update.message.reply_text(HELP)

    async def _cmd_status(self, update: Update, _):
        today = date.today()
        lines = self._day_stats_lines(await self._safe_day_stats(today))
        if lines is None:  # signings API unavailable — dashboard buttons instead
            try:
                clock = await self.sesame.get_clock_state()
                lines = [f"Clock state: "
                         f"{'🟢 clocked IN' if clock == 'in' else '⚪ clocked OUT'}"]
            except Exception as exc:
                lines = [f"Clock state: unknown ({exc})"]
        plan = self.state.get_plan(today)
        if plan:
            lines.append("Today's plan:")
            for name in EVENT_ORDER + OFFICE_EVENT_ORDER:
                if name in plan:
                    t = datetime.fromisoformat(plan[name]["time"]).strftime("%H:%M")
                    mark = "✅" if plan[name]["done"] else "•"
                    lines.append(f"  {mark} {t} {EVENT_LABELS[name]}")
        else:
            lines.append("No plan for today.")
        if self.state.is_skipped(today):
            lines.append("Today is SKIPPED.")
        if self.state.skips():
            lines.append(f"Skip list: {', '.join(self.state.skips())}")
        await update.message.reply_text("\n".join(lines))

    async def _safe_day_stats(self, day: date) -> dict | None:
        try:
            return await self.sesame.get_day_stats(day)
        except Exception:
            log.exception("day stats fetch failed")
            return None

    def _day_stats_lines(self, day: dict | None) -> list[str] | None:
        if day is None:
            return None
        state = {"in": "🟢 clocked IN", "pause": "☕ on a pause",
                 "out": "⚪ clocked OUT"}[day["state"]]
        lines = [f"Clock state: {state}"]
        if day["checks"]:
            lines.append("Today's signings:")
            for c in day["checks"]:
                end = f"{c['out']:%H:%M}" if c["out"] else "now"
                dur = ((c["out"] or datetime.now(c["in"].tzinfo)) - c["in"])
                icon = "☕" if c["type"] == "pause" else "🕒"
                lines.append(
                    f"  {icon} {c['in']:%H:%M}–{end}  {c['type']}"
                    f" ({format_hm(dur.total_seconds())})"
                )
        else:
            lines.append("No signings today yet.")
        worked, target = day["worked_seconds"], day["to_work_seconds"]
        if target:
            diff = worked - target
            lines.append(f"Worked {format_hm(worked)} of {format_hm(target)}"
                         f" ({'+' if diff >= 0 else '-'}{format_hm(diff)})")
        elif day["checks"]:
            lines.append(f"Worked {format_hm(worked)}")
        return lines

    def _arg_date(self, context) -> date:
        if context.args:
            return date.fromisoformat(context.args[0])
        return date.today()

    async def _cmd_skip(self, update: Update, context):
        day = self._arg_date(context)
        self.state.add_skip(day)
        if day == date.today():
            self.scheduler.cancel_today()
        await update.message.reply_text(f"Skipping {day.isoformat()} — no clocking that day.")

    async def _cmd_unskip(self, update: Update, context):
        day = self._arg_date(context)
        self.state.remove_skip(day)
        await update.message.reply_text(f"{day.isoformat()} removed from the skip list.")

    async def _cmd_in(self, update: Update, _):
        await update.message.reply_text("Clocking in…")
        result = await self.sesame.clock_in()
        await update.message.reply_text(
            "🟢 Clocked in." if result != "already" else "Already clocked in."
        )

    async def _cmd_out(self, update: Update, _):
        await update.message.reply_text("Clocking out…")
        result = await self.sesame.clock_out()
        await update.message.reply_text(
            "⚪ Clocked out." if result != "already" else "Already clocked out."
        )

    async def _cmd_absences(self, update: Update, _):
        await update.message.reply_text("Fetching absences & holidays…")
        today = date.today()
        entries, sources = await self.sesame.get_time_off(today.year)
        if not sources:
            await update.message.reply_text(
                "❓ Could not determine — no time-off API responses captured."
            )
            return
        current = [e for e in entries if e["from"] <= today <= e["to"]]
        lines = ["🏖 Today IS an absence/holiday:" if current
                 else "💼 Today looks like a normal workday."]
        lines += [f"  {entry_emoji(e, self.cfg.work_minutes)} {format_entry(e)}"
                  for e in current]
        upcoming = [e for e in entries if e["from"] > today]
        if upcoming:
            lines.append(f"\nUpcoming in {today.year}:")
            lines += [f"  {entry_emoji(e, self.cfg.work_minutes)} {format_entry(e)}"
                      for e in upcoming[:20]]
            if len(upcoming) > 20:
                lines.append(f"  … and {len(upcoming) - 20} more")
        elif not current:
            lines.append(f"No time off left in {today.year}.")
        await update.message.reply_text("\n".join(lines)[:4000])

    async def _cmd_screenshot(self, update: Update, _):
        path = await self.sesame.screenshot()
        await self.send_photo(path, caption="Current browser page")
