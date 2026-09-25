"""Discord side: watch the configured channels and feed trade call-outs to the Trader."""

from __future__ import annotations

import asyncio
import logging

import discord

from .config import DiscordConfig
from .trader import Trader

log = logging.getLogger(__name__)


def message_text(message: discord.Message, read_embeds: bool = True) -> str:
    """Collect the message body plus embed text; many signal bots post call-outs as embeds."""
    parts = [message.content or ""]
    if read_embeds:
        for embed in message.embeds:
            parts += [embed.title or "", embed.description or ""]
            for f in embed.fields:
                parts += [f.name or "", f.value or ""]
    return "\n".join(p for p in parts if p).strip()


class SignalListener(discord.Client):
    def __init__(self, cfg: DiscordConfig, trader: Trader):
        intents = discord.Intents.default()
        intents.message_content = True  # must also be enabled in the Developer Portal
        super().__init__(intents=intents)
        self.cfg = cfg
        self.trader = trader
        self._author_names = {n.lower() for n in cfg.author_names}

    async def on_ready(self) -> None:
        log.info("Logged in to Discord as %s", self.user)
        if not self.cfg.channel_ids:
            log.warning("No channel_ids configured: listening to EVERY channel the bot can see")
        for channel_id in self.cfg.channel_ids:
            channel = self.get_channel(channel_id)
            if channel is None:
                log.error("Channel %s not visible to the bot. Is it invited, with Read Messages?", channel_id)
            else:
                log.info("Watching #%s in %s", channel, getattr(channel, "guild", "DM"))

    def _is_wanted(self, message: discord.Message) -> bool:
        if message.author == self.user:
            return False
        if self.cfg.channel_ids and message.channel.id not in self.cfg.channel_ids:
            return False
        if not (self.cfg.author_ids or self._author_names):
            return True
        if message.author.id in self.cfg.author_ids:
            return True
        names = {message.author.name.lower(), getattr(message.author, "display_name", "").lower()}
        return bool(names & self._author_names)

    async def on_message(self, message: discord.Message) -> None:
        if not self._is_wanted(message):
            return
        text = message_text(message, self.cfg.read_embeds)
        if not text:
            return
        log.debug("Message %s from %s: %s", message.id, message.author, text)
        # Broker calls block on HTTP, so run them off the event loop to keep the gateway heartbeat alive.
        await asyncio.to_thread(self.trader.handle_message, message.id, text)


def run_listener(cfg: DiscordConfig, trader: Trader) -> None:
    if not cfg.token:
        raise SystemExit("DISCORD_TOKEN is not set. Put it in your .env file.")
    SignalListener(cfg, trader).run(cfg.token, log_handler=None)
