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
