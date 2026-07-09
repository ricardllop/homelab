import asyncio
import logging

from .bot import TgBot
from .config import Config
from .scheduler import ClockScheduler
from .sesame import Sesame
from .state import State

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("main")


async def startup_check(sesame: Sesame, bot: TgBot) -> None:
    """Validate the session at boot so MFA (if any) happens right away."""
    try:
        clock = await sesame.get_clock_state()
        await bot.send(f"🚀 sesame-autoclock started. Session OK, currently clocked "
                       f"{'IN' if clock == 'in' else 'OUT'}.")
    except Exception as exc:
        log.exception("startup login check failed")
        await bot.send(f"🚀 sesame-autoclock started but the login check failed: {exc}")


async def main() -> None:
    cfg = Config.from_env()
    state = State(cfg.data_dir / "state.json")
    bot = TgBot(cfg, state)
    sesame = Sesame(cfg, bot)
    scheduler = ClockScheduler(cfg, state, sesame, bot)
    bot.attach(sesame, scheduler)

    await bot.start()
    if cfg.chat_id is None:
        log.warning("TELEGRAM_CHAT_ID not set — message the bot to discover your chat id")
    scheduler.start()
    if cfg.chat_id is not None:
        asyncio.create_task(startup_check(sesame, bot))

    try:
        await asyncio.Event().wait()
    finally:
        await sesame.close()
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
