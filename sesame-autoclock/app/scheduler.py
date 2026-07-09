import asyncio
import logging
import random
from datetime import date, datetime, time, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Config, _parse_hhmm
from .sesame import format_hm, work_type_for_day
from .state import EVENT_LABELS, EVENT_ORDER, OFFICE_EVENT_ORDER, State

log = logging.getLogger("scheduler")


class ClockScheduler:
    """Plans the four daily events (with jitter) and executes them."""

    def __init__(self, cfg: Config, state: State, sesame, bot):
        self.cfg = cfg
        self.state = state
        self.sesame = sesame
        self.bot = bot
        self.sched = AsyncIOScheduler(timezone=cfg.tz)

    def start(self) -> None:
        self.sched.add_job(
            self.plan_day,
            CronTrigger(
                hour=self.cfg.plan_time.hour,
                minute=self.cfg.plan_time.minute,
                day_of_week="mon-fri",
                timezone=self.cfg.tz,
            ),
            id="plan-day",
        )
        self.sched.start()
        asyncio.create_task(self._recover())

    def _now(self) -> datetime:
        return datetime.now(self.cfg.tz)

    # --- daily planning ---------------------------------------------------
    async def plan_day(self) -> None:
        today = date.today()
        if today.weekday() >= 5:
            return
        if self.state.is_skipped(today):
            await self.bot.send(f"⏭ {today} is on the skip list — not clocking today.")
            return

        try:
            hits, evidence = await self.sesame.today_time_off()
        except Exception:
            log.exception("absence check failed")
            hits, evidence = None, "absence check raised an error (see logs)"

        half_day = None
        if hits:
            half_day = self._half_day_window(hits)
            if half_day is None:
                self.state.add_skip(today)
                await self.bot.send(
                    f"🏖 Absence/holiday detected for today — not clocking.\n{evidence[:500]}"
                )
                return

        # Sesame's own schedule knows the real target for the day (e.g. summer
        # hours); prefer it over the static WORK_MINUTES when available.
        target = None
        try:
            stats = await self.sesame.get_day_stats(today)
            target = (stats or {}).get("to_work_seconds")
        except Exception:
            log.exception("day stats fetch failed")
        if target == 0:
            await self.bot.send(
                f"🏖 Sesame plans 0h of work for {today} — not clocking today."
            )
            return
        work_minutes = target // 60 if target else self.cfg.work_minutes

        if work_type_for_day(today) == "Normal":
            # office day: "Normal" clock-ins are IP-blocked from outside the
            # office network, so the bot must not click anything — remind only
            await self._plan_office_day(today, work_minutes, hits, half_day, evidence)
            return

        events = (self._make_half_day_times(today, *half_day) if half_day
                  else self._make_times(today, work_minutes))
        self.state.save_plan(today, {k: v.isoformat() for k, v in events.items()})
        for name, when in events.items():
            self._schedule_event(name, when)

        lines = [f"📋 Plan for {today} (reply /skip to cancel):",
                 "🏠 Work type: Remote"]
        if hits is None:
            lines.append("⚠️ Could not verify absences — assuming a workday.")
        if half_day:
            lines.append(f"🌗 Partial absence today — planning a half day.\n{evidence[:300]}")
        elif work_minutes != self.cfg.work_minutes:
            lines.append(f"🕒 Sesame plans {format_hm(work_minutes * 60)} today "
                         f"— using that instead of WORK_MINUTES.")
        lines += [f"  {when:%H:%M}  {EVENT_LABELS[name]}" for name, when in events.items()]
        await self.bot.send("\n".join(lines))

    # --- office days: reminders instead of clicks -----------------------------
    async def _plan_office_day(
        self, today: date, work_minutes: int,
        hits: list[dict] | None, half_day, evidence: str,
    ) -> None:
        """Office days are clocked manually (Entrar → Normal only works from
        the office network); the bot schedules Telegram reminders instead."""
        events = self._make_office_times(today)
        now, late = self._now(), 0
        for name, when in list(events.items()):
            if when <= now:
                # planned after the fact (late PLAN_TIME or restart) — the
                # reminders verify the clock state first, so just run them now
                late += 1
                events[name] = when = now + timedelta(seconds=30 * late)
            self._schedule_event(name, when)
        self.state.save_plan(today, {k: v.isoformat() for k, v in events.items()})

        r = self.cfg.office_remind_repeat_minutes
        lines = [f"📋 Plan for {today} (reply /skip to cancel):",
                 "🏢 Office day: clock in/out manually — I can't (the office "
                 "network blocks my IP for Normal), so I will only remind you:"]
        if hits is None:
            lines.append("⚠️ Could not verify absences — assuming a workday.")
        if half_day:
            lines.append("🌗 Partial absence today — reminder times are the "
                         f"usual office ones, adjust on your own.\n{evidence[:300]}")
        lines += [f"  {when:%H:%M}  {EVENT_LABELS[name]}" for name, when in events.items()]
        lines.append(f"↻ The clock in/back reminders repeat twice every {r} min "
                     "until you have clocked; the clock-out one fires once "
                     f"{format_hm(work_minutes * 60)} of work are complete.")
        await self.bot.send("\n".join(lines))

    def _make_office_times(self, day: date) -> dict[str, datetime]:
        def at(t):
            return datetime.combine(day, t, tzinfo=self.cfg.tz)

        return {
            "remind_clock_in": at(self.cfg.office_remind_clock_in),
            "remind_break_start": at(self.cfg.office_remind_break_start),
            "remind_break_end": at(self.cfg.office_remind_break_end),
            # the clock-out watcher starts polling right after the
            # back-from-lunch reminder and reschedules itself until the
            # planned hours are actually worked (see _watch_clock_out)
            "remind_clock_out": at(self.cfg.office_remind_break_end)
            + timedelta(minutes=5),
        }

    # --- half-day absences --------------------------------------------------
    def _half_day_window(self, hits: list[dict]) -> tuple[time, time] | None:
        """The combined absence window when today's absences fit a half day.

        None means "skip the whole day": any full-day entry (public holiday or
        absence without times), or partial absences that together exceed half
        the workday (e.g. a 09:00-16:00 leave is 7h — nothing left to work).
        """
        spans = []
        for e in hits:
            start, end = e.get("start"), e.get("end")
            if not start or not end:
                return None
            try:
                s, en = _parse_hhmm(start), _parse_hhmm(end)
            except ValueError:
                return None
            if en <= s:
                return None
            spans.append((s, en))
        total = sum((en.hour - s.hour) * 60 + en.minute - s.minute for s, en in spans)
        if total > self.cfg.work_minutes // 2:
            return None
        return min(s for s, _ in spans), max(en for _, en in spans)

    def _make_half_day_times(
        self, day: date, absence_start: time, absence_end: time
    ) -> dict[str, datetime]:
        """One block of half the workday, before the absence if it fits."""
        j = self.cfg.jitter_minutes
        block = timedelta(minutes=self.cfg.work_minutes // 2)
        tz = self.cfg.tz
        start = datetime.combine(day, self.cfg.clock_in, tzinfo=tz) + timedelta(
            minutes=random.randint(-j, j)
        )
        if start + block > datetime.combine(day, absence_start, tzinfo=tz):
            # morning does not fit -> work right after the absence ends
            start = datetime.combine(day, absence_end, tzinfo=tz) + timedelta(
                minutes=random.randint(0, j)
            )
        return {"clock_in": start, "clock_out": start + block}

    def _make_times(self, day: date, work_minutes: int) -> dict[str, datetime]:
        j = self.cfg.jitter_minutes

        def at(t, minutes_offset=0):
            return datetime.combine(day, t, tzinfo=self.cfg.tz) + timedelta(
                minutes=minutes_offset
            )

        clock_in = at(self.cfg.clock_in, random.randint(-j, j))
        break_start = at(self.cfg.break_start, random.randint(-j, j))
        # never shorten the pause below the required minimum
        pause = timedelta(minutes=self.cfg.break_minutes + random.randint(0, j))
        break_end = break_start + pause
        clock_out = clock_in + timedelta(minutes=work_minutes) + pause
        return {
            "clock_in": clock_in,
            "break_start": break_start,
            "break_end": break_end,
            "clock_out": clock_out,
        }

    def _schedule_event(self, name: str, when: datetime) -> None:
        if when <= self._now():
            log.warning("not scheduling %s: %s already passed", name, when)
            return
        fn = self._run_reminder if name in OFFICE_EVENT_ORDER else self._run_event
        self.sched.add_job(
            fn,
            "date",
            run_date=when,
            args=[name],
            id=f"event-{name}",
            replace_existing=True,
        )

    def _reschedule_reminder(
        self, name: str, when: datetime, attempt: int = 0
    ) -> None:
        """Reminders reschedule themselves (repeats, clock-out watch); the
        saved plan follows along so /status and restart recovery stay true."""
        self.sched.add_job(
            self._run_reminder, "date", run_date=when, args=[name, attempt],
            id=f"event-{name}", replace_existing=True,
        )
        self.state.set_time(date.today(), name, when.isoformat())

    def cancel_today(self) -> None:
        for name in EVENT_ORDER + OFFICE_EVENT_ORDER:
            job = self.sched.get_job(f"event-{name}")
            if job:
                job.remove()

    # --- execution ---------------------------------------------------------
    async def _run_event(self, name: str) -> None:
        today = date.today()
        if self.state.is_skipped(today):
            log.info("skipping %s: day is on the skip list", name)
            return
        action = {
            "clock_in": self.sesame.clock_in,
            "break_start": self.sesame.start_break,
            "break_end": self.sesame.clock_in,
            "clock_out": self.sesame.clock_out,
        }[name]
        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                result = await action()
                self.state.mark_done(today, name)
                note = " (was already in that state)" if result == "already" else ""
                await self.bot.send(f"✅ {EVENT_LABELS[name]} done at {self._now():%H:%M}{note}")
                return
            except Exception as exc:
                last_error = exc
                log.exception("%s attempt %d failed", name, attempt)
                if attempt == 1:
                    await asyncio.sleep(60)
        await self.bot.send(
            f"❌ {EVENT_LABELS[name]} FAILED after 2 attempts: {last_error}\n"
            "Do it manually or retry with /in or /out."
        )

    # --- office-day reminders -------------------------------------------------
    # first message + this many repeats (spaced office_remind_repeat_minutes
    # apart) while the user still has not clocked
    REMINDER_REPEATS = {"remind_clock_in": 2, "remind_break_start": 0,
                        "remind_break_end": 2}
    WATCH_GIVE_UP_HOUR = 22  # stop chasing the clock-out reminder after this

    async def _run_reminder(self, name: str, attempt: int = 0) -> None:
        today = date.today()
        if self.state.is_skipped(today):
            return
        if name == "remind_clock_out":
            await self._watch_clock_out()
            return
        try:
            stats = await self.sesame.get_day_stats(today)
        except Exception:
            log.exception("%s: day stats fetch failed", name)
            stats = None
        if stats is None:
            needed = True  # cannot verify — remind anyway
        elif name == "remind_clock_in":
            needed = stats["state"] == "out" and not stats["checks"]
        elif name == "remind_break_start":
            needed = stats["state"] == "in"
        else:  # remind_break_end: on the pause (or Salir'd out for lunch)
            needed = stats["state"] != "in"
        if not needed:
            self.state.mark_done(today, name)
            return
        msg = {
            "remind_clock_in": "🏢 Office day — remember to clock in "
                               "(Entrar → Normal)!",
            "remind_break_start": "🥪 Remember to start the lunch pause "
                                  "(Pausa → Lunch).",
            "remind_break_end": "⏰ Lunch is over — remember to clock back in "
                                "(Entrar)!",
        }[name]
        if stats is None:
            msg += " (could not verify the current clock state)"
        repeats = self.REMINDER_REPEATS[name]
        if attempt < repeats:
            nxt = self._now() + timedelta(
                minutes=self.cfg.office_remind_repeat_minutes)
            self._reschedule_reminder(name, nxt, attempt + 1)
        else:
            self.state.mark_done(today, name)
        if repeats:
            msg += f" ({attempt + 1}/{repeats + 1})"
        await self.bot.send(msg)

    async def _watch_clock_out(self) -> None:
        """Poll until today's planned hours are worked, then remind to Salir.

        Self-rescheduling: while clocked in it jumps straight to the projected
        completion time; on a pause / API failure it re-checks periodically.
        """
        name, today, now = "remind_clock_out", date.today(), self._now()
        if now.hour >= self.WATCH_GIVE_UP_HOUR:
            log.info("clock-out watch: giving up for today")
            self.state.mark_done(today, name)
            return
        try:
            stats = await self.sesame.get_day_stats(today)
        except Exception:
            log.exception("clock-out watch: day stats fetch failed")
            stats = None
        if stats is None:
            self._reschedule_reminder(name, now + timedelta(minutes=15))
            return
        target = stats["to_work_seconds"] or self.cfg.work_minutes * 60
        remaining = target - stats["worked_seconds"]
        if stats["state"] == "out":
            if stats["checks"] and remaining <= 0:
                self.state.mark_done(today, name)  # already out, day complete
                return
            # not (back) in yet — worked time is frozen, poll at a slow pace
            self._reschedule_reminder(name, now + timedelta(minutes=30))
            return
        if remaining <= 60:
            note = (" (you are still on the pause)"
                    if stats["state"] == "pause" else "")
            await self.bot.send(
                f"🏁 Planned hours complete — worked "
                f"{format_hm(stats['worked_seconds'])} of {format_hm(target)}. "
                f"Remember to clock out (Salir)!{note}"
            )
            self.state.mark_done(today, name)
            return
        if stats["state"] == "pause":  # clock frozen — re-check periodically
            self._reschedule_reminder(name, now + timedelta(minutes=15))
        else:  # clocked in — wake up when the remaining time has elapsed
            self._reschedule_reminder(
                name, now + timedelta(seconds=max(remaining, 60)))

    # --- restart recovery ----------------------------------------------------
    async def _recover(self) -> None:
        """After a container restart, resume today's pending events."""
        await asyncio.sleep(5)  # let the bot finish starting
        today = date.today()
        now = self._now()
        if today.weekday() >= 5 or self.state.is_skipped(today):
            return
        plan = self.state.get_plan(today)
        if plan:
            pending, missed = [], []
            late = 0
            for name in EVENT_ORDER + OFFICE_EVENT_ORDER:
                if name not in plan or plan[name]["done"]:
                    continue
                when = datetime.fromisoformat(plan[name]["time"])
                if when > now:
                    self._schedule_event(name, when)
                    pending.append(f"{when:%H:%M} {EVENT_LABELS[name]}")
                elif name in OFFICE_EVENT_ORDER:
                    # reminders verify the clock state before sending, so a
                    # missed one can safely run right now instead of dropping
                    late += 1
                    self._reschedule_reminder(
                        name, now + timedelta(seconds=30 * late))
                    pending.append(f"now: {EVENT_LABELS[name]}")
                else:
                    missed.append(EVENT_LABELS[name])
            if pending or missed:
                msg = "🔄 Restarted, resuming today's plan."
                if pending:
                    msg += "\nPending: " + "; ".join(pending)
                if missed:
                    msg += "\n⚠️ Missed while down: " + "; ".join(missed) + \
                           " — use /in or /out if needed."
                await self.bot.send(msg)
        elif now.time() >= self.cfg.plan_time:
            log.info("no plan for today yet, planning now")
            await self.plan_day()
