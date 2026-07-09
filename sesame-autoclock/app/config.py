import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo


def _parse_hhmm(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm))


@dataclass(frozen=True)
class Config:
    bot_token: str
    chat_id: int | None
    email: str
    password: str | None

    plan_time: time      # when the daily plan is computed and announced
    clock_in: time       # target morning clock-in
    break_start: time    # target start of the lunch pause
    break_minutes: int   # minimum pause length
    work_minutes: int    # total worked time (pause excluded)
    jitter_minutes: int  # +/- randomization applied to each event

    # office days ("Normal" clock-ins are IP-blocked outside the office
    # network, so the bot only reminds — see ClockScheduler._plan_office_day)
    office_remind_clock_in: time     # first "clock in manually" reminder
    office_remind_break_start: time  # single "start the lunch pause" reminder
    office_remind_break_end: time    # first "clock back in" reminder
    office_remind_repeat_minutes: int  # spacing of the two repeat reminders

    tz: ZoneInfo
    headless: bool
    data_dir: Path

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is required")
        chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        email = os.environ.get("SESAME_EMAIL", "")
        if not email:
            raise SystemExit("SESAME_EMAIL is required")

        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
        (data_dir / "screenshots").mkdir(parents=True, exist_ok=True)

        return cls(
            bot_token=token,
            chat_id=int(chat_id_raw) if chat_id_raw else None,
            email=email,
            password=os.environ.get("SESAME_PASSWORD") or None,
            plan_time=_parse_hhmm(os.environ.get("PLAN_TIME", "08:15")),
            clock_in=_parse_hhmm(os.environ.get("CLOCK_IN", "09:00")),
            break_start=_parse_hhmm(os.environ.get("BREAK_START", "13:30")),
            break_minutes=int(os.environ.get("BREAK_MINUTES", "45")),
            work_minutes=int(os.environ.get("WORK_MINUTES", "480")),
            jitter_minutes=int(os.environ.get("JITTER_MINUTES", "5")),
            office_remind_clock_in=_parse_hhmm(
                os.environ.get("OFFICE_REMIND_CLOCK_IN", "07:30")),
            office_remind_break_start=_parse_hhmm(
                os.environ.get("OFFICE_REMIND_BREAK_START", "12:50")),
            office_remind_break_end=_parse_hhmm(
                os.environ.get("OFFICE_REMIND_BREAK_END", "14:00")),
            office_remind_repeat_minutes=int(
                os.environ.get("OFFICE_REMIND_REPEAT_MINUTES", "15")),
            tz=ZoneInfo(os.environ.get("TZ", "Europe/Madrid")),
            headless=os.environ.get("HEADLESS", "true").lower() != "false",
            data_dir=data_dir,
        )
