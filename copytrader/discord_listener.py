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
        self._channel_names = [n.lower() for n in cfg.channel_names]

    async def setup_hook(self) -> None:
        self.loop.create_task(self._expiry_watchdog())

    async def _expiry_watchdog(self) -> None:
        """Every 30 seconds, sell same-day-expiry options once the configured cut-off time passes."""
        while not self.is_closed():
            try:
                await asyncio.to_thread(self.trader.close_expiring)
            except Exception:  # noqa: BLE001 - keep the watchdog alive
                log.exception("Expiry watchdog failed")
            await asyncio.sleep(30)

    def _channel_matches(self, channel) -> bool:
        if not (self.cfg.channel_ids or self._channel_names):
            return True
        # Messages inside a thread (or a forum post) count as part of the channel the thread belongs to.
        candidates = [channel]
        if getattr(channel, "parent", None) is not None:
            candidates.append(channel.parent)
        for c in candidates:
            if c.id in self.cfg.channel_ids:
                return True
            name = (getattr(c, "name", None) or "").lower()
            if any(wanted in name for wanted in self._channel_names):
                return True
        return False

    async def _reply_context(self, message: discord.Message) -> str | None:
        """Text of the alert this message replies to, so "Sold" can be matched to its contract."""
        ref = message.reference
        if ref is None or ref.message_id is None:
            return None
        if getattr(ref, "type", None) == getattr(discord.MessageReferenceType, "forward", object()):
            return None  # a forwarded message, not a reply
        parent = ref.resolved if isinstance(ref.resolved, discord.Message) else None
        if parent is None:
            try:
                parent = await message.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                log.warning("Could not load the message %s replies to", message.id)
                return None
        return message_text(parent, self.cfg.read_embeds) or None

    async def on_ready(self) -> None:
        log.info("Logged in to Discord as %s", self.user)
        if not (self.cfg.channel_ids or self._channel_names):
            log.warning("No channels configured: listening to EVERY channel the bot can see")
        if self._channel_names:
            found = [
                c
                for c in self.get_all_channels()
                if not isinstance(c, discord.CategoryChannel) and self._channel_matches(c)
            ]
            for channel in found:
                log.info("Watching #%s in %s (matched by name)", channel, channel.guild)
            if not found:
                log.error(
                    "No visible channel matches %s. The bot must be in the server and allowed to read it.",
                    self.cfg.channel_names,
                )
        for channel_id in self.cfg.channel_ids:
            channel = self.get_channel(channel_id)
            if channel is None:
                log.error("Channel %s not visible to the bot. Is it invited, with Read Messages?", channel_id)
            else:
                log.info("Watching #%s in %s", channel, getattr(channel, "guild", "DM"))

    def _is_wanted(self, message: discord.Message) -> bool:
        if message.author == self.user:
            return False
        if not self._channel_matches(message.channel):
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
        reply_to = await self._reply_context(message)
        log.debug("Message %s from %s: %s (reply to: %s)", message.id, message.author, text, reply_to)
        # Broker calls block on HTTP, so run them off the event loop to keep the gateway heartbeat alive.
        await asyncio.to_thread(self.trader.handle_message, message.id, text, reply_to)


def run_listener(cfg: DiscordConfig, trader: Trader) -> None:
    if not cfg.token:
        raise SystemExit("DISCORD_TOKEN is not set. Put it in your .env file.")
    SignalListener(cfg, trader).run(cfg.token, log_handler=None)
