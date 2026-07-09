import asyncio
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

from playwright.async_api import Page, async_playwright

from .config import Config, _parse_hhmm

log = logging.getLogger("sesame")

APP_URL = "https://app.sesametime.com/"
LOGIN_SSO_URL = "https://app.sesametime.com/login-sso"
ABSENCES_URL = "https://app.sesametime.com/employee/absences?year={year}"
SIGNINGS_URL = "https://app.sesametime.com/employee/signings/all"

# Back-office API the SPA talks to. Known endpoints:
#   .../api/v3/employees/<uuid>/holidays/2026?page=1            -> public holidays
#   .../api/v3/employees/<uuid>/day-off-requests?...&page=1     -> requested leave
#   .../api/v3/employees/<uuid>/checks?from=..&to=..            -> clock in/out records
#   .../api/v3/employees/<uuid>/daily-computed-hour-stats?...   -> worked vs planned
# We sniff the page's own responses (already authenticated) and replay further
# requests through context.request, which shares the session cookies.
SESAME_API_RE = re.compile(r"https://back-[^/]+\.sesametime\.com/api/v3/")
# per-employee API prefix, e.g. https://back-eu5.sesametime.com/api/v3/employees/<uuid>
EMPLOYEE_API_BASE_RE = re.compile(
    r"https://back-[^/]+\.sesametime\.com/api/v3/employees/[0-9a-f-]{36}"
)
CHECKS_ENDPOINT = "{base}/checks?from={day}&to={day}&includeOut=true"
DAY_STATS_ENDPOINT = "{base}/daily-computed-hour-stats?from={day}&to={day}"
TIMEOFF_URL_RE = re.compile(
    r"absen|holiday|vacation|(time|day)-?off|leave|festiv", re.I
)
MAX_API_PAGES = 25
# the SPA authenticates its API calls with these custom headers (no cookies,
# no bearer token), so replayed requests must carry them
API_AUTH_HEADERS = ("authorization", "csid", "esid", "rsrc")

# --- selectors, grouped here so UI changes only need edits in one place ---
# Sesame dashboard (Spanish UI). The inactive clock button stays in the DOM
# with display:none, so state/click selectors must require visibility.
# Salir has a stable id (#button-click-sign-out); Entrar only has its text.
BTN_CLOCK_IN = 'button:has-text("Entrar"):visible'
BTN_CLOCK_OUT = '#button-click-sign-out:visible, button:has-text("Salir"):visible'
# once clocked in, a separate "Pausa" button appears next to Salir — it starts
# a break (opens the Descanso/Lunch dropdown below), it does NOT clock out.
BTN_PAUSE = 'button:has-text("Pausa"):visible'
# dashboard detection must also count the hidden one
ANY_CLOCK_BTN = (
    '#button-click-sign-out, button:has-text("Entrar"), button:has-text("Salir")'
)
# dropdown after Entrar offers "Normal" (in-office) and "Remote" (labels are
# English even though the rest of the UI is Spanish). Company policy is
# in-office on Thursdays, remote the rest of the week — adjust
# REMOTE_WORK_WEEKDAYS below if that changes. Selecting the wrong one for the
# day appears to leave the dropdown open and the dashboard never shows Salir.
# "Normal" is additionally IP-restricted: from outside the office network
# Sesame rejects it with "IPs Bloqueadas — Su IP no es válida", so on office
# days the scheduler never auto-clocks (it sends Telegram reminders instead,
# see ClockScheduler._plan_office_day); a manual /in on such a day will fail
# the same way unless the bot happens to run on the office network.
REMOTE_WORK_WEEKDAYS = {0, 1, 2, 4}  # date.weekday(): Mon=0 ... Sun=6
# dropdown after Pausa offers "Descanso" (short rest) and "Lunch" — always
# take the lunch break here.
PAUSE_OPTION = "Lunch"


def work_type_for_day(day: date) -> str:
    return "Remote" if day.weekday() in REMOTE_WORK_WEEKDAYS else "Normal"


def _dropdown_option(label: str) -> str:
    # scoped to the popper container (class="dropdown ..." in both the Entrar
    # and Pausa dumps) so matching text elsewhere on the page can't be clicked
    return f'.dropdown button:has-text("{label}"):visible'


# /login-sso page: one email input + one "Accede con SSO" button (no type attr)
SESAME_EMAIL_INPUT = 'input[type="email"], input[name="email"]'
SESAME_SSO_SUBMIT = (
    'button:has-text("Accede con SSO"), button:has-text("SSO"), '
    'button[type="submit"]'
)

# Microsoft login pages (name-based selectors survive their UI refreshes best)
# :not guards: on the password view AAD keeps loginfmt in the DOM but moved
# off-screen, and off-screen still counts as visible for Playwright
MS_EMAIL = 'input[name="loginfmt"]:not([aria-hidden="true"]):not(.moveOffScreen)'
MS_PASSWORD = 'input[type="password"], input[name="passwd"]'
MS_PASSWORD_VISIBLE = 'input[type="password"]:visible, input[name="passwd"]:visible'
MS_OTC = 'input[name="otc"], #idTxtBx_SAOTCC_OTC'
# "Compruebe su identidad" proof chooser: prefer SMS ("Enviar un mensaje de
# texto al +XX..."), the data-value attr is language-independent
MS_PROOF_LIST = "#idDiv_SAOTCS_Proofs"
MS_PROOF_SMS = (
    '[data-value="OneWaySMS"], [data-value="TwoWaySMS"], '
    ':text-matches("Enviar un mensaje de texto", "i")'
)
MS_PROOF_CODE = ':text-matches("código de verificación|verification code", "i")'
MS_NUMBER_MATCH = "#idRichContext_DisplaySign"
MS_SUBMIT = 'input[type="submit"]:visible, button[type="submit"]:visible'
MS_KMSI_VIEW = "#KmsiCheckboxField, #KmsiDescription"
MS_KMSI_CHECKBOX = "#KmsiCheckboxField"
MS_HOSTS = ("login.microsoftonline.com", "login.live.com", "login.microsoft.com")

LOGIN_STEP_WAIT_MS = 3000
LOGIN_MAX_STEPS = 100  # x3s = up to ~5 min, enough to approve MFA on the phone


class SesameError(Exception):
    pass


class Sesame:
    """Playwright automation over app.sesametime.com with a persistent profile."""

    def __init__(self, cfg: Config, bot):
        self.cfg = cfg
        self.bot = bot
        self._pw = None
        self._ctx = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()  # one browser interaction at a time
        # auth headers seen on the SPA's own API calls (see API_AUTH_HEADERS);
        # replayed page-2+ requests must send them again
        self._api_auth: dict[str, str] = {}
        # per-employee API prefix learned from sniffed URLs (host + employee id)
        self._api_base: str | None = None

    # --- browser lifecycle ---------------------------------------------
    async def _page_or_launch(self) -> Page:
        if self._page and not self._page.is_closed():
            return self._page
        if self._ctx:
            await self._close()
        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.cfg.data_dir / "profile"),
            headless=self.cfg.headless,
            args=["--disable-dev-shm-usage"],
            viewport={"width": 1366, "height": 900},
            locale="es-ES",
            timezone_id=str(self.cfg.tz),
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        return self._page

    async def _close(self) -> None:
        try:
            if self._ctx:
                await self._ctx.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            log.exception("error closing browser")
        self._ctx = self._pw = self._page = None

    async def close(self) -> None:
        async with self._lock:
            await self._close()

    async def screenshot(self, name: str = "page") -> Path:
        page = await self._page_or_launch()
        path = self.cfg.data_dir / "screenshots" / (
            f"{datetime.now():%Y%m%d-%H%M%S}-{name}.png"
        )
        await page.screenshot(path=str(path), full_page=True)
        try:
            path.with_suffix(".html").write_text(await page.content())
        except Exception:
            log.exception("could not dump page html")
        return path

    async def _fail(self, msg: str) -> SesameError:
        try:
            shot = await self.screenshot("error")
            await self.bot.send_photo(shot, caption=f"❌ {msg}")
        except Exception:
            log.exception("could not send failure screenshot")
        return SesameError(msg)

    # --- login -----------------------------------------------------------
    async def _on_dashboard(self, page: Page) -> bool:
        if "sesametime.com" not in page.url or "login" in page.url:
            return False
        return await page.locator(ANY_CLOCK_BTN).count() > 0

    async def _ensure_logged_in(self, page: Page) -> None:
        await page.goto(APP_URL, wait_until="domcontentloaded")
        number_notified = False
        last_url = ""
        for _ in range(LOGIN_MAX_STEPS):
            await page.wait_for_timeout(LOGIN_STEP_WAIT_MS)
            if page.url != last_url:
                log.info("login step, now at: %s", page.url.split("?")[0])
                last_url = page.url
            if await self._on_dashboard(page):
                return
            try:
                if any(h in page.url for h in MS_HOSTS):
                    number_notified = await self._microsoft_step(page, number_notified)
                    continue
                if "sesametime.com" in page.url and "login" in page.url:
                    await self._sesame_login_step(page)
                # otherwise: mid-redirect or the SPA is still loading — wait
            except Exception:
                # one flaky click must not abort the whole login attempt
                log.exception("login step failed, retrying")
        raise await self._fail("Login did not complete (see screenshot).")

    async def _sesame_login_step(self, page: Page) -> None:
        """On Sesame's own login: use /login-sso, fill the email, submit.

        Submitting there redirects to the Microsoft SAML endpoint.
        """
        if "/login-sso" not in page.url:
            await page.goto(LOGIN_SSO_URL, wait_until="domcontentloaded")
            return
        email_in = page.locator(SESAME_EMAIL_INPUT)
        if await email_in.count() and await email_in.first.is_visible():
            await email_in.first.fill(self.cfg.email)
            await page.locator(SESAME_SSO_SUBMIT).first.click()

    async def _microsoft_step(self, page: Page, number_notified: bool) -> bool:
        """Advance one step of the Microsoft SSO flow. Returns number_notified."""
        email = page.locator(MS_EMAIL)
        if await email.count() and await email.first.is_visible():
            await email.first.fill(self.cfg.email)
            await page.locator(MS_SUBMIT).first.click()
            return number_notified

        pwd = page.locator(MS_PASSWORD_VISIBLE)
        if await pwd.count():
            password = self.cfg.password or await self.bot.ask(
                "🔐 Microsoft asks for your password and SESAME_PASSWORD is not set. "
                "Reply with it here (or set the env var and restart)."
            )
            await pwd.first.fill(password)
            await page.locator(MS_SUBMIT).first.click()
            return number_notified

        otc = page.locator(MS_OTC)
        if await otc.count() and await otc.first.is_visible():
            code = await self.bot.ask("🔐 MFA: reply with the code you received by SMS.")
            await otc.first.fill(re.sub(r"\D", "", code))
            await page.locator(MS_SUBMIT).first.click()
            return number_notified

        # "Compruebe su identidad" method chooser: prefer the SMS option
        sms = page.locator(MS_PROOF_SMS)
        if await sms.count() and await sms.first.is_visible():
            await sms.first.click()
            await self.bot.send("📲 Requested the verification code by SMS.")
            return number_notified
        proof = page.locator(MS_PROOF_CODE)
        if await proof.count() and await proof.last.is_visible():
            await proof.last.click()
            return number_notified

        sign = page.locator(MS_NUMBER_MATCH)
        if await sign.count() and await sign.first.is_visible():
            if not number_notified:
                number = (await sign.first.inner_text()).strip()
                await self.bot.send(
                    f"🔐 MFA: open Microsoft Authenticator and approve with number: {number}"
                )
            return True  # keep looping while the user approves on the phone

        # "¿Quiere mantener la sesión iniciada?" → check the box, click "Sí"
        kmsi = page.locator(MS_KMSI_VIEW)
        if await kmsi.count() and await kmsi.first.is_visible():
            box = page.locator(MS_KMSI_CHECKBOX)
            if await box.count() and await box.first.is_visible():
                await box.first.check()  # "Don't show this again" → longer session
            await page.locator(MS_SUBMIT).first.click()
            return number_notified

        # a known view whose input exists in the DOM but is not visible yet is
        # still animating in — clicking the submit now would send an empty form
        # (that is how the "Escriba su contraseña" error happened)
        for selector in (MS_PASSWORD, MS_OTC, MS_PROOF_LIST):
            if await page.locator(selector).count():
                return number_notified

        # generic "Next"/"Yes"/consent page with a single submit button
        submit = page.locator(MS_SUBMIT)
        if await submit.count() and await submit.first.is_visible():
            await submit.first.click()
        return number_notified

    # --- clocking ----------------------------------------------------------
    async def get_clock_state(self) -> str:
        async with self._lock:
            page = await self._page_or_launch()
            await self._ensure_logged_in(page)
            return await self._read_clock_state(page)

    async def _read_clock_state(self, page: Page) -> str:
        # the selectors already require visibility, so count() is the state
        if await page.locator(BTN_CLOCK_OUT).count():
            return "in"
        if await page.locator(BTN_CLOCK_IN).count():
            return "out"
        raise await self._fail("Neither Entrar nor Salir button found on the dashboard.")

    async def _verified_state(self, page: Page) -> str:
        """Authoritative clock state: "in", "pause" or "out" (lock must be held).

        The DOM alone cannot represent "pause": during the lunch pause the
        dashboard shows Salir next to Entrar, which _read_clock_state reports
        as "in" — that is how break_end once claimed "was already in that
        state" without actually resuming. Trust the checks API when reachable.
        """
        dom = await self._read_clock_state(page)
        day = await self._day_stats(date.today())
        if day is None:
            log.warning("checks API unreachable — falling back to DOM state %r", dom)
            return dom
        if day["state"] != dom:
            log.info("DOM shows %r but the API says %r — trusting the API",
                     dom, day["state"])
        if not await self._on_dashboard(page):
            # _day_stats may have navigated away to sniff the signings page
            await self._ensure_logged_in(page)
        return day["state"]

    async def _confirm_api_state(self, expected: str, action: str) -> None:
        """After a click the dashboard can look right for the wrong reason
        (e.g. Salir was already visible during the pause), so wait until the
        checks API reports `expected`, allowing a few seconds of lag."""
        day = None
        for _ in range(5):
            await asyncio.sleep(2)
            day = await self._day_stats(date.today())
            if day is None or day["state"] == expected:
                break
        if day is None:
            log.warning("%s clicked but the result could not be verified "
                        "through the API", action)
        elif day["state"] != expected:
            raise await self._fail(
                f"Clicked {action} and the dashboard looked right, but the API "
                f"still reports {day['state']!r} (expected {expected!r}) — "
                "check/fix it manually in Sesame."
            )

    async def clock_in(self) -> str:
        async with self._lock:
            page = await self._page_or_launch()
            await self._ensure_logged_in(page)
            if await self._verified_state(page) == "in":
                return "already"
            # state is "out" or "pause"; during a pause Entrar means "resume",
            # so the same click both starts the day and ends the lunch pause
            await page.locator(BTN_CLOCK_IN).first.click()
            work_type = work_type_for_day(date.today())
            option = page.locator(_dropdown_option(work_type))
            try:
                await option.first.wait_for(state="visible", timeout=8000)
                await option.first.click()
            except Exception:
                # some setups clock in directly without the work-type dropdown
                log.info("no %r option appeared, assuming direct clock-in", work_type)
            try:
                await page.locator(BTN_CLOCK_OUT).first.wait_for(
                    state="visible", timeout=15000
                )
            except Exception:
                raise await self._fail(
                    f"Clicked Entrar but Salir never appeared (selected {work_type})."
                )
            await self._confirm_api_state("in", "Entrar")
            return "ok"

    async def clock_out(self) -> str:
        async with self._lock:
            page = await self._page_or_launch()
            await self._ensure_logged_in(page)
            if await self._verified_state(page) == "out":
                return "already"
            await page.locator(BTN_CLOCK_OUT).first.click()
            try:
                await page.locator(BTN_CLOCK_IN).first.wait_for(
                    state="visible", timeout=15000
                )
            except Exception:
                raise await self._fail("Clicked Salir but Entrar never appeared.")
            await self._confirm_api_state("out", "Salir")
            return "ok"

    async def start_break(self) -> str:
        """Lunch pause: Pausa -> Lunch.

        Distinct from clock_out (Salir), which ends the whole day rather than
        pausing it — the two buttons appear side by side once clocked in.
        """
        async with self._lock:
            page = await self._page_or_launch()
            await self._ensure_logged_in(page)
            state = await self._verified_state(page)
            if state == "pause":
                return "already"
            if state == "out":
                raise await self._fail(
                    "Cannot start the pause: currently clocked OUT, not IN."
                )
            await page.locator(BTN_PAUSE).first.click()
            option = page.locator(_dropdown_option(PAUSE_OPTION))
            try:
                await option.first.wait_for(state="visible", timeout=8000)
                await option.first.click()
            except Exception:
                log.info("no %r option appeared, assuming direct pause", PAUSE_OPTION)
            try:
                await page.locator(BTN_CLOCK_IN).first.wait_for(
                    state="visible", timeout=15000
                )
            except Exception:
                raise await self._fail("Clicked Pausa but Entrar never reappeared.")
            # Entrar reappearing is also what a (mis)clicked Salir looks like —
            # confirm through the API that a pause is open, not the day closed
            await self._confirm_api_state("pause", "Pausa")
            return "ok"

    # --- absences & holidays -------------------------------------------------
    async def get_time_off(self, year: int) -> tuple[list[dict], list[str]]:
        """All time-off entries for the year, plus the API URLs they came from.

        Each entry is {"from": date, "to": date, "label": str, "kind": str,
        "url": str}; partial-day absences also carry "start"/"end" ("HH:MM").
        An empty entry list with non-empty URLs means "no absences this year";
        empty URLs means the capture itself failed.
        """
        async with self._lock:
            payloads = await self._capture_api_payloads(
                ABSENCES_URL.format(year=year), "absences"
            )
            timeoff = [(u, p) for u, p in payloads if TIMEOFF_URL_RE.search(u)]
            # the SPA only loads page=1 of each paginated list; fetch the rest
            for url, payload in list(timeoff):
                for next_url in _more_page_urls(url, payload):
                    data = await self._api_get_json(next_url)
                    if data is None:
                        break
                    timeoff.append((next_url, data))

        entries: list[dict] = []
        seen: set[tuple] = set()
        for url, payload in timeoff:
            for entry in _parse_timeoff_payload(url, payload):
                entry["kind"] = _kind_of_url(url)
                key = (entry["from"], entry["to"], entry["label"])
                if key not in seen:
                    seen.add(key)
                    entries.append(entry)
        entries.sort(key=lambda e: (e["from"], e["to"]))
        return _coalesce(entries), [u for u, _ in timeoff]

    async def today_time_off(self) -> tuple[list[dict] | None, str]:
        """Today's time-off entries ([] = normal workday, None = check failed)."""
        today = date.today()
        entries, sources = await self.get_time_off(today.year)
        if not sources:
            return None, "No time-off API responses captured (page or API changed?)."
        hits = [e for e in entries if e["from"] <= today <= e["to"]]
        if hits:
            return hits, "\n".join(format_entry(e) for e in hits)
        return [], f"{len(entries)} time-off entries this year, none covers today."

    # --- signings (clock records) --------------------------------------------
    async def get_day_stats(self, day: date) -> dict | None:
        """One day's clock records + hour stats from the back-office API.

        Returns {"state": "in"|"pause"|"out", "checks": [...], "worked_seconds",
        "to_work_seconds": int|None} or None when the API could not be reached.
        Each check is {"type": "work"|"pause", "in": datetime, "out": datetime|None}.
        """
        async with self._lock:
            return await self._day_stats(day)

    async def _day_stats(self, day: date) -> dict | None:
        """get_day_stats body; caller must hold self._lock (it is not reentrant)."""
        checks = stats = None
        # fast path: replay the API directly once base+auth are known
        if self._api_base and self._ctx:
            checks = await self._api_get_json(
                CHECKS_ENDPOINT.format(base=self._api_base, day=day)
            )
            stats = await self._api_get_json(
                DAY_STATS_ENDPOINT.format(base=self._api_base, day=day)
            )
        if checks is None:
            # sniff the signings page; it fetches ~6 weeks around today,
            # which learns _api_base/_api_auth and covers `day` if current
            for url, payload in await self._capture_api_payloads(
                SIGNINGS_URL, "signings"
            ):
                if "/checks?" in url and checks is None:
                    checks = payload
                elif "daily-computed-hour-stats" in url and stats is None:
                    stats = payload
        if checks is None:
            return None

        entries = _parse_checks_payload(checks, day)
        now = datetime.now(self.cfg.tz)
        worked = sum(
            ((e["out"] or now) - e["in"]).total_seconds()
            for e in entries if e["type"] != "pause"
        )
        state = "out"
        for e in entries:
            if e["out"] is None:
                state = "pause" if e["type"] == "pause" else "in"
        return {
            "state": state,
            "checks": entries,
            "worked_seconds": int(worked),
            "to_work_seconds": _day_target_seconds(stats, day),
        }

    async def _capture_api_payloads(
        self, page_url: str, dump_name: str
    ) -> list[tuple[str, object]]:
        """Load a SPA page and collect the back-* API JSON it fetches."""
        payloads: list[tuple[str, object]] = []

        async def capture(resp):
            if not SESAME_API_RE.match(resp.url):
                return
            try:
                payloads.append((resp.url, await resp.json()))
            except Exception:
                return
            if not self._api_base:
                m = EMPLOYEE_API_BASE_RE.match(resp.url)
                if m:
                    self._api_base = m.group(0)
            if not self._api_auth:
                try:
                    headers = await resp.request.all_headers()
                    self._api_auth = {
                        k: v for k, v in headers.items()
                        if k.lower() in API_AUTH_HEADERS
                    }
                except Exception:
                    pass

        page = await self._page_or_launch()
        await self._ensure_logged_in(page)
        page.on("response", capture)
        try:
            await page.goto(page_url)
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)
        finally:
            page.remove_listener("response", capture)
        self._dump_payloads(payloads, dump_name)
        return payloads

    async def _api_get_json(self, url: str):
        if not self._ctx:
            return None
        try:
            resp = await self._ctx.request.get(url, headers=self._api_auth or None)
            if not resp.ok:
                log.warning("direct API GET %s -> HTTP %d", url, resp.status)
                return None
            return await resp.json()
        except Exception:
            log.exception("direct API GET failed: %s", url)
            return None

    def _dump_payloads(self, payloads: list[tuple[str, object]], name: str) -> None:
        """Raw dumps allow refining the parser offline against real data."""
        try:
            path = self.cfg.data_dir / "api-dumps" / (
                f"{datetime.now():%Y%m%d-%H%M%S}-{name}.json"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                [{"url": u, "payload": p} for u, p in payloads],
                indent=2, default=str,
            ))
            log.info("captured %d API payloads -> %s", len(payloads), path)
        except Exception:
            log.exception("could not dump API payloads")


# --- checks payload parsing ------------------------------------------------
def _parse_iso_dt(value) -> datetime | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _parse_checks_payload(payload, day: date) -> list[dict]:
    """checks: data[] items each hold one work/pause interval with checkIn/Out.

    An item without checkOut.date is still open (currently clocked in / on a
    pause). The payload may span several weeks — keep only `day`'s items.
    """
    entries = []
    for c in _data_list(payload):
        if not isinstance(c, dict) or c.get("date") != day.isoformat():
            continue
        start = _parse_iso_dt((c.get("checkIn") or {}).get("date"))
        if not start:
            continue
        entries.append({
            "type": c.get("checkType") or "work",
            "in": start,
            "out": _parse_iso_dt((c.get("checkOut") or {}).get("date")),
        })
    entries.sort(key=lambda e: e["in"])
    return entries


def _day_target_seconds(payload, day: date) -> int | None:
    """daily-computed-hour-stats: data[] has one item per day with the planned
    seconds to work (already reflects the company schedule, e.g. summer hours)."""
    for item in _data_list(payload):
        if isinstance(item, dict) and item.get("date") == day.isoformat():
            try:
                return int(item.get("secondsToWork"))
            except (TypeError, ValueError):
                return None
    return None


def format_hm(seconds: float) -> str:
    minutes = int(abs(seconds)) // 60
    return f"{minutes // 60}h{minutes % 60:02d}m"


# --- time-off payload parsing --------------------------------------------------
def _parse_timeoff_payload(url: str, payload) -> list[dict]:
    if "/holidays/" in url:
        return _parse_holidays(payload, url)
    if re.search(r"day-?off-requests", url, re.I):
        return _parse_day_off_requests(payload, url)
    # unknown time-off endpoint: fall back to generic key heuristics
    return _extract_entries(payload, url)


def _data_list(payload) -> list:
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, list) else []


def _parse_holidays(payload, url: str) -> list[dict]:
    """holidays/{year}: data[] has one {date, name} item per public holiday."""
    entries = []
    for item in _data_list(payload):
        if not isinstance(item, dict):
            continue
        d = _extract_date(item.get("date"))
        if d:
            entries.append({"from": d, "to": d,
                            "label": item.get("name") or "", "url": url})
    return entries


def _parse_day_off_requests(payload, url: str) -> list[dict]:
    """day-off-requests: data[] items are requests with their own daysOff[].

    Only the request's top-level daysOff are the actual absence days — the
    embedded calendar object carries the whole calendar history, ignore it.
    """
    entries = []
    for req in _data_list(payload):
        if not isinstance(req, dict):
            continue
        status = req.get("status")
        if status in ("rejected", "cancelled", "deleted"):
            continue
        if req.get("type") == "delete":  # a request to REMOVE an absence
            continue
        calendar_type = ((req.get("calendar") or {}).get("calendarType") or {})
        for day in req.get("daysOff") or []:
            if not isinstance(day, dict):
                continue
            d = _extract_date(day.get("date"))
            if not d:
                continue
            label = day.get("name") or calendar_type.get("name") or "absence"
            start_t, end_t = day.get("startTime"), day.get("endTime")
            partial = day.get("dayOffTimeType") == "partial_day" and start_t
            if partial:
                label = f"{label} {start_t}-{end_t or '?'}"
            if status and status != "accepted":
                label = f"{label} [{status}]"
            entry = {"from": d, "to": d, "label": label, "url": url}
            if partial:
                entry["start"], entry["end"] = start_t, end_t
            entries.append(entry)
    return entries


def _coalesce(entries: list[dict]) -> list[dict]:
    """Merge consecutive same-label days back into ranges (input is sorted)."""
    out: list[dict] = []
    for e in entries:
        last = out[-1] if out else None
        if (last and last["label"] == e["label"] and last["kind"] == e["kind"]
                and 0 <= (e["from"] - last["to"]).days <= 1):
            last["to"] = max(last["to"], e["to"])
        else:
            out.append(dict(e))
    return out


_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_DATEISH_KEY = re.compile(r"date|day|from|to|start|end|begin|until", re.I)
_IGNORED_KEY = re.compile(r"creat|updat|modif|delet|timestamp", re.I)
_START_KEY = re.compile(r"from|start|begin|initial", re.I)
# "to" alone is too common a substring; accept it only as prefix ("toDayOff")
# or suffix ("dateTo") of the key
_END_KEY = re.compile(r"^to|to$|end|until|final", re.I)
_LABEL_KEY = re.compile(r"name|title|type|reason|description|comment", re.I)


def _extract_date(value) -> date | None:
    if isinstance(value, str):
        m = _DATE_RE.search(value)
        if m:
            try:
                return date.fromisoformat(m.group(1))
            except ValueError:
                return None
    return None


def _extract_entries(obj, url: str, out: list[dict] | None = None) -> list[dict]:
    """Recursively turn date ranges / single dates in a payload into entries."""
    if out is None:
        out = []
    if isinstance(obj, list):
        for item in obj:
            _extract_entries(item, url, out)
        return out
    if not isinstance(obj, dict):
        return out

    dates = {}
    for key, value in obj.items():
        if _DATEISH_KEY.search(key) and not _IGNORED_KEY.search(key):
            d = _extract_date(value)
            if d:
                dates[key] = d
    starts = [d for k, d in dates.items() if _START_KEY.search(k)]
    ends = [d for k, d in dates.items() if _END_KEY.search(k)]
    if starts and ends:
        out.append({"from": min(starts), "to": max(ends),
                    "label": _label_of(obj), "url": url})
    else:
        for key, d in dates.items():
            out.append({"from": d, "to": d, "label": _label_of(obj), "url": url})

    for value in obj.values():
        if isinstance(value, (dict, list)):
            _extract_entries(value, url, out)
    return out


def _label_of(obj: dict) -> str:
    for key, value in obj.items():
        if not _LABEL_KEY.search(key):
            continue
        if isinstance(value, str) and value.strip() and not _DATE_RE.search(value):
            return value.strip()
        if isinstance(value, dict):  # e.g. {"absenceType": {"name": "Vacaciones"}}
            inner = _label_of(value)
            if inner:
                return inner
    return ""


def _kind_of_url(url: str) -> str:
    if re.search(r"holiday|festiv", url, re.I):
        return "public holiday"
    if re.search(r"day-?off|absen|vacation|leave", url, re.I):
        return "absence"
    return "time off"


def entry_emoji(entry: dict, work_minutes: int) -> str:
    """Line bullet: 🔴 public holiday, 🔹 half-day absence, 🔵 full absence."""
    if entry.get("kind") == "public holiday":
        return "🔴"
    start, end = entry.get("start"), entry.get("end")
    if start and end:
        try:
            s, e = _parse_hhmm(start), _parse_hhmm(end)
        except ValueError:
            return "🔵"
        minutes = (e.hour - s.hour) * 60 + e.minute - s.minute
        if 0 < minutes <= work_minutes // 2:
            return "🔹"
    return "🔵"


def format_entry(entry: dict) -> str:
    span = (entry["from"].isoformat() if entry["from"] == entry["to"]
            else f"{entry['from']} → {entry['to']}")
    kind = entry.get("kind", "time off")
    label = entry["label"] or kind
    if entry["label"] and kind != "time off":
        label = f"{entry['label']} ({kind})"
    return f"{span}  {label}"


def _more_page_urls(url: str, payload) -> list[str]:
    """URLs for the remaining pages of a paginated Sesame v3 response."""
    if not isinstance(payload, dict):
        return []
    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        return []
    try:
        current = int(meta.get("currentPage") or meta.get("current_page") or 0)
        last = int(meta.get("lastPage") or meta.get("last_page") or 0)
    except (TypeError, ValueError):
        return []
    if current < 1 or last <= current:
        return []
    last = min(last, current + MAX_API_PAGES)
    if "page=" in url:
        return [re.sub(r"page=\d+", f"page={p}", url) for p in range(current + 1, last + 1)]
    sep = "&" if "?" in url else "?"
    return [f"{url}{sep}page={p}" for p in range(current + 1, last + 1)]
