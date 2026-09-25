"""Configuration loading. Settings come from a YAML file; secrets come from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class DiscordConfig:
    token: str = field(default="", repr=False)
    channel_ids: list[int] = field(default_factory=list)
    # Match channels by name instead of ID, e.g. "options-with-demon". Case-insensitive substring,
    # so emoji and separators around the name don't matter.
    channel_names: list[str] = field(default_factory=list)
    author_ids: list[int] = field(default_factory=list)
    author_names: list[str] = field(default_factory=list)
    read_embeds: bool = True


@dataclass
class BrokerConfig:
    name: str = "alpaca"  # "alpaca" or "robinhood"
    paper: bool = True
    account_number: str = ""  # Robinhood only: pick one account if you have several
    api_key: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    username: str = ""
    password: str = field(default="", repr=False)
    mfa_secret: str = field(default="", repr=False)


@dataclass
class SizingConfig:
    mode: str = "dollars"  # "dollars" or "fixed"
    dollars_per_trade: float = 500.0
    fixed_shares: int = 1
    fixed_contracts: int = 1


@dataclass
class RiskConfig:
    allow_stocks: bool = True
    allow_options: bool = True
    max_trades_per_day: int = 10
    max_open_positions: int = 5
    max_dollars_per_trade: float = 1000.0
    allowed_tickers: list[str] = field(default_factory=list)
    blocked_tickers: list[str] = field(default_factory=list)
    add_to_existing_positions: bool = False
    require_price_for_entries: bool = False


@dataclass
class ParsingConfig:
    # Extra words or phrases a trader uses, on top of the built-in BTO/STC/trim vocabulary.
    extra_buy_words: list[str] = field(default_factory=list)
    extra_sell_words: list[str] = field(default_factory=list)
    extra_trim_words: list[str] = field(default_factory=list)
    # Option entries on these tickers that give no expiration date are treated as expiring today.
    assume_0dte_tickers: list[str] = field(default_factory=lambda: ["SPX", "SPXW", "XSP"])

    def extra_words(self) -> dict[str, list[str]]:
        return {"buy": self.extra_buy_words, "sell": self.extra_sell_words, "trim": self.extra_trim_words}


@dataclass
class ExecutionConfig:
    entry_order_type: str = "limit"  # "limit" or "market"
    entry_slippage_pct: float = 5.0
    exit_order_type: str = "market"  # "limit" or "market"
    exit_slippage_pct: float = 5.0
    trim_fraction: float = 0.5
    # Broker option root per ticker. SPX weeklies and 0DTE (PM-settled) trade under SPXW.
    option_roots: dict[str, str] = field(default_factory=lambda: {"SPX": "SPXW"})
    # Sell options that expire today at this time (market timezone), so a missed exit call-out can't
    # leave a 0DTE contract to expire. Set to null to turn off.
    auto_close_expiring_at: str | int | None = "15:50"
    market_timezone: str = "America/New_York"


@dataclass
class Config:
    dry_run: bool = True
    state_file: str = "state.json"
    log_file: str = "copytrader.log"
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    parsing: ParsingConfig = field(default_factory=ParsingConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)


def _build(cls, data: dict | None):
    data = data or {}
    known = cls.__dataclass_fields__
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"unknown {cls.__name__} setting(s): {', '.join(sorted(unknown))}")
    return cls(**data)


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    sections = {
        "discord": DiscordConfig,
        "broker": BrokerConfig,
        "sizing": SizingConfig,
        "risk": RiskConfig,
        "parsing": ParsingConfig,
        "execution": ExecutionConfig,
    }
    top = {k: v for k, v in raw.items() if k not in sections}
    cfg = _build(Config, top)
    for key, cls in sections.items():
        setattr(cfg, key, _build(cls, raw.get(key)))

    # Secrets are never read from the YAML file so it can be shared or committed safely.
    cfg.discord.token = os.environ.get("DISCORD_TOKEN", "")
    cfg.broker.api_key = os.environ.get("ALPACA_API_KEY", "")
    cfg.broker.secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    cfg.broker.username = os.environ.get("ROBINHOOD_USERNAME", "")
    cfg.broker.password = os.environ.get("ROBINHOOD_PASSWORD", "")
    cfg.broker.mfa_secret = os.environ.get("ROBINHOOD_MFA_SECRET", "")
    cfg.broker.account_number = str(cfg.broker.account_number or "")
    if cfg.broker.name not in ("alpaca", "robinhood"):
        raise ValueError("broker.name must be 'alpaca' or 'robinhood'")

    cfg.risk.allowed_tickers = [t.upper() for t in cfg.risk.allowed_tickers]
    cfg.risk.blocked_tickers = [t.upper() for t in cfg.risk.blocked_tickers]
    cfg.discord.channel_ids = [int(x) for x in cfg.discord.channel_ids]
    cfg.discord.author_ids = [int(x) for x in cfg.discord.author_ids]
    cfg.parsing.assume_0dte_tickers = [t.upper() for t in cfg.parsing.assume_0dte_tickers]
    cfg.execution.option_roots = {k.upper(): v.upper() for k, v in cfg.execution.option_roots.items()}
    if cfg.execution.auto_close_expiring_at:
        parse_hhmm(cfg.execution.auto_close_expiring_at)
    if cfg.sizing.mode not in ("dollars", "fixed"):
        raise ValueError("sizing.mode must be 'dollars' or 'fixed'")
    for name in ("entry_order_type", "exit_order_type"):
        if getattr(cfg.execution, name) not in ("limit", "market"):
            raise ValueError(f"execution.{name} must be 'limit' or 'market'")
    if not 0 < cfg.execution.trim_fraction <= 1:
        raise ValueError("execution.trim_fraction must be between 0 and 1")
    return cfg


def parse_hhmm(value: str):
    from datetime import time

    try:
        if isinstance(value, int):  # YAML reads an unquoted 15:50 as base-60 (950 minutes)
            return time(value // 60, value % 60)
        hour, minute = (int(x) for x in str(value).split(":"))
        return time(hour, minute)
    except ValueError as exc:
        raise ValueError(f"expected a time like 15:50, got {value!r}") from exc
