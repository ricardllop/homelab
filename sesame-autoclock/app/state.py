import json
import os
from datetime import date
from pathlib import Path

EVENT_ORDER = ["clock_in", "break_start", "break_end", "clock_out"]

# office days: the bot cannot clock (Normal is IP-restricted to the office
# network), so the plan holds Telegram reminders instead of clock events
OFFICE_EVENT_ORDER = [
    "remind_clock_in", "remind_break_start", "remind_break_end",
    "remind_clock_out",
]

EVENT_LABELS = {
    "clock_in": "Clock in (Entrar)",
    "break_start": "Pause start (Pausa → Lunch)",
    "break_end": "Pause end (Entrar)",
    "clock_out": "Clock out (Salir)",
    "remind_clock_in": "Remind: clock in (Entrar → Normal)",
    "remind_break_start": "Remind: lunch pause (Pausa → Lunch)",
    "remind_break_end": "Remind: clock back in (Entrar)",
    "remind_clock_out": "Remind: clock out (Salir) once hours are complete",
}


class State:
    """Tiny JSON persistence: skipped dates, office days forced to remote
    (/doremote) and the current day's plan."""

    def __init__(self, path: Path):
        self.path = path
        self._data = {"skips": [], "remote_days": [], "plans": {}}
        if path.exists():
            self._data.update(json.loads(path.read_text()))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # write + rename: a crash mid-write must not leave a corrupt file
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        os.replace(tmp, self.path)

    # --- skips -------------------------------------------------------
    def is_skipped(self, day: date) -> bool:
        return day.isoformat() in self._data["skips"]

    def add_skip(self, day: date) -> None:
        if not self.is_skipped(day):
            self._data["skips"].append(day.isoformat())
            self._save()

    def remove_skip(self, day: date) -> None:
        if self.is_skipped(day):
            self._data["skips"].remove(day.isoformat())
            self._save()

    def skips(self) -> list[str]:
        return sorted(self._data["skips"])

    # --- office days worked remotely (/doremote) ------------------------
    def is_remote_day(self, day: date) -> bool:
        return day.isoformat() in self._data["remote_days"]

    def add_remote_day(self, day: date) -> None:
        if not self.is_remote_day(day):
            self._data["remote_days"].append(day.isoformat())
            self._save()

    def remove_remote_day(self, day: date) -> None:
        if self.is_remote_day(day):
            self._data["remote_days"].remove(day.isoformat())
            self._save()

    def remote_days(self) -> list[str]:
        return sorted(self._data["remote_days"])

    # --- daily plan ----------------------------------------------------
    def save_plan(self, day: date, events: dict[str, str]) -> None:
        """events: name -> ISO datetime of the planned run."""
        self._data["plans"] = {  # keep only the current day
            day.isoformat(): {
                name: {"time": iso, "done": False} for name, iso in events.items()
            }
        }
        self._save()

    def get_plan(self, day: date) -> dict | None:
        return self._data["plans"].get(day.isoformat())

    def mark_done(self, day: date, event: str) -> None:
        plan = self.get_plan(day)
        if plan and event in plan:
            plan[event]["done"] = True
            self._save()

    def set_time(self, day: date, event: str, iso: str) -> None:
        """Keep the plan in sync when a job reschedules itself (reminder
        repeats, clock-out watcher) so restart recovery resumes correctly."""
        plan = self.get_plan(day)
        if plan and event in plan:
            plan[event]["time"] = iso
            self._save()

    def set_nag_until(self, day: date, event: str, iso: str) -> None:
        """Let a reminder keep nagging past its repeats, up to this time,
        while the user still has not clocked (see ClockScheduler)."""
        plan = self.get_plan(day)
        if plan and event in plan:
            plan[event]["nag_until"] = iso
            self._save()
