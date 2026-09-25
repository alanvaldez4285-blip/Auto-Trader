from types import SimpleNamespace

import discord

from copytrader.config import DiscordConfig
from copytrader.discord_listener import SignalListener, message_text


def msg(content="", channel=1, author_id=10, name="trader", embeds=()):
    author = SimpleNamespace(id=author_id, name=name, display_name=name)
    return SimpleNamespace(content=content, embeds=list(embeds), channel=SimpleNamespace(id=channel), author=author)


def listener(**kw):
    return SignalListener(DiscordConfig(token="x", **kw), trader=None)


def test_embed_text_is_read():
    embed = discord.Embed(title="New alert", description="BTO SPY 450c 10/20 @ 1.20")
    assert "BTO SPY" in message_text(msg(embeds=[embed]))


def test_channel_filter():
    lst = listener(channel_ids=[1])
    assert lst._is_wanted(msg(channel=1))
    assert not lst._is_wanted(msg(channel=2))


def test_author_filters():
    lst = listener(author_ids=[10], author_names=["BigTrader"])
    assert lst._is_wanted(msg(author_id=10))
    assert lst._is_wanted(msg(author_id=99, name="bigtrader"))
    assert not lst._is_wanted(msg(author_id=99, name="someone"))


def named_msg(channel_name, channel_id=5):
    m = msg()
    m.channel = SimpleNamespace(id=channel_id, name=channel_name)
    return m


def test_channel_name_filter():
    lst = listener(channel_names=["options-with-demon"])
    assert lst._is_wanted(named_msg("😈｜options-with-demon😈"))
    assert not lst._is_wanted(named_msg("💬｜main-chat"))


def test_thread_inside_watched_channel():
    lst = listener(channel_names=["phil-trades"])
    m = msg()
    parent = SimpleNamespace(id=7, name="⛳｜phil-trades⛳")
    m.channel = SimpleNamespace(id=99, name="QCOM 205C", parent=parent)
    assert lst._is_wanted(m)


def test_reply_context_uses_resolved_parent():
    import asyncio

    lst = listener()
    parent = discord.Message.__new__(discord.Message)
    parent.content, parent.embeds = "QCOM 205C at 1.00 - lotto", []
    m = msg("Sold")
    m.id = 2
    m.reference = SimpleNamespace(message_id=1, resolved=parent, type=discord.MessageReferenceType.reply)
    assert asyncio.run(lst._reply_context(m)) == "QCOM 205C at 1.00 - lotto"
    m.reference = None
    assert asyncio.run(lst._reply_context(m)) is None
