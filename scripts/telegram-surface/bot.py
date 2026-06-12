"""Superclaude Telegram factory surface v0 (DIR-001 T3, plan superclaude-v4 F3).

Read-only monitoring bot. Whitelisted commands ONLY — no free-form shell, no LLM
in the loop, no write path of any kind (see docs/telegram-surface-security.md).

Fail-closed startup: refuses to run without a token, without a non-empty user
allowlist, or with a group/world-readable .env file.

Run:    ~/.claude/.venv/bin/python ~/.claude/scripts/telegram-surface/bot.py
Unit:   telegram-surface.service (NOT enabled until HG-F3 clears)
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

sys.path.insert(0, str(Path(__file__).resolve().parent))
import surfaces  # noqa: E402

ENV_FILE = Path(__file__).resolve().parent / ".env"

HELP = (
    "Superclaude factory surface — read-only.\n"
    "/status — active sessions + latest report per agent\n"
    "/plans — active plan cards\n"
    "/report <agent> — latest full report from one agent\n"
    "/help — this message"
)

log = logging.getLogger("telegram-surface")


def load_config() -> tuple[str, frozenset[int]]:
    """Token + allowlist from env (systemd EnvironmentFile) or .env file. Fail-closed."""
    if ENV_FILE.exists():
        mode = ENV_FILE.stat().st_mode & 0o777
        if mode & 0o077:
            sys.exit(f"REFUSING to start: {ENV_FILE} is mode {oct(mode)}; chmod 600 it.")
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        sys.exit(
            "TELEGRAM_BOT_TOKEN missing — bot stays OFF until HG-F3 clears "
            f"(create {ENV_FILE} mode 600; see .env.example)."
        )
    raw_ids = os.environ.get("ALLOWED_USER_IDS", "").strip()
    try:
        allowed = frozenset(int(x) for x in raw_ids.split(",") if x.strip())
    except ValueError:
        sys.exit("ALLOWED_USER_IDS must be comma-separated integer Telegram user IDs.")
    if not allowed:
        sys.exit("ALLOWED_USER_IDS is empty — refusing to start an open bot (fail-closed).")
    return token, allowed


class AllowlistMiddleware(BaseMiddleware):
    """Outer middleware on ALL updates: silently drop anything not from the allowlist.

    Runs before any filter or handler. No reply is sent to strangers — the bot
    must not act as an oracle for its own existence.
    """

    def __init__(self, allowed: frozenset[int]):
        self.allowed = allowed

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id not in self.allowed:
            log.info("dropped update from non-allowlisted user id=%s", getattr(user, "id", None))
            return None
        return await handler(event, data)


def build_dispatcher(allowed: frozenset[int]) -> Dispatcher:
    dp = Dispatcher()
    dp.update.outer_middleware(AllowlistMiddleware(allowed))

    @dp.message(CommandStart())
    @dp.message(Command("help"))
    async def cmd_help(message: Message):
        await message.answer(HELP)

    @dp.message(Command("status"))
    async def cmd_status(message: Message):
        await message.answer(surfaces.status_text())

    @dp.message(Command("plans"))
    async def cmd_plans(message: Message):
        await message.answer(surfaces.plans_text())

    @dp.message(Command("report"))
    async def cmd_report(message: Message):
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.answer("Usage: /report <agent>   e.g. /report scaf")
            return
        await message.answer(surfaces.report_text(parts[1]))

    @dp.message()
    async def fallback(message: Message):
        # Only reachable by allowlisted users; anything outside the whitelist is refused.
        await message.answer("Unknown command — whitelisted commands only.\n\n" + HELP)

    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    token, allowed = load_config()
    log.info("starting; %d allowlisted user(s)", len(allowed))
    bot = Bot(token=token)
    dp = build_dispatcher(allowed)
    # Drop updates queued while the bot was offline — never replay stale commands.
    await bot.delete_webhook(drop_pending_updates=True)
    # Narrow the update surface to plain messages only.
    await dp.start_polling(bot, allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())
