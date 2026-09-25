"""Command line: `python -m copytrader run` to trade, `python -m copytrader parse "..."` to test parsing."""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv


def _setup_logging(log_file: str | None, verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("discord").setLevel(logging.WARNING)


def cmd_parse(args) -> int:
    from pathlib import Path

    from .broker import DryRunBroker
    from .config import Config, load_config
    from .trader import Ledger, Trader

    cfg = load_config(args.config) if Path(args.config).exists() else Config()
    # A throwaway trader gives the same parsing (extra words, 0DTE defaults, SPX->SPXW) as a live run.
    trader = Trader(cfg, DryRunBroker(), ledger=Ledger("/nonexistent/never-saved.json"))
    for text in args.messages:
        signal = trader.parse(text)
        if signal is None:
            print(f"{text!r}\n  -> not a trade signal")
            continue
        if signal.action != "buy":
            note = "  (sells matching positions the bot opened)"
        elif signal.asset_type == "stock" or signal.is_complete_contract:
            note = f"  (broker symbol {signal.broker_symbol(cfg.execution.option_roots)})"
        else:
            note = "  (would be skipped: option has no expiration date)"
        print(f"{text!r}\n  -> {signal.describe()}{note}")
    return 0


def cmd_run(args) -> int:
    from .broker import make_broker
    from .config import load_config
    from .discord_listener import run_listener
    from .trader import Trader

    cfg = load_config(args.config)
    _setup_logging(cfg.log_file, args.verbose)
    log = logging.getLogger("copytrader")
    mode = "DRY RUN" if cfg.dry_run else ("PAPER" if cfg.broker.paper else "LIVE")
    log.info("Starting copy trader in %s mode", mode)
    trader = Trader(cfg, make_broker(cfg))
    run_listener(cfg.discord, trader)
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="copytrader", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="connect to Discord and copy trades")
    run.add_argument("-c", "--config", default="config.yaml")
    run.add_argument("-v", "--verbose", action="store_true", help="log every message the bot sees")
    run.set_defaults(func=cmd_run)

    parse = sub.add_parser("parse", help="show how messages would be interpreted, without trading")
    parse.add_argument("messages", nargs="+")
    parse.add_argument("-c", "--config", default="config.yaml", help="used for extra words, if it exists")
    parse.set_defaults(func=cmd_parse)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
