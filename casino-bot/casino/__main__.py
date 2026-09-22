"""Entry point: ``python -m casino``."""

from __future__ import annotations

import asyncio
import logging
import sys

import discord

from casino.config import settings
from casino.core.bot import CasinoBot


def configure_logging() -> None:
    discord.utils.setup_logging(level=getattr(logging, settings.log_level.upper(), logging.INFO))


async def run() -> None:
    configure_logging()
    bot = CasinoBot(settings)
    async with bot:
        await bot.start(settings.require_token())


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("shutting down", file=sys.stderr)


if __name__ == "__main__":
    main()
