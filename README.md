# Discord Copy Trader

Watches a Discord trade-alert channel and copies the call-outs into your brokerage account.
It reads messages like `BTO SPY 450c 10/20 @ 1.20`, sizes the trade to your budget, and places the order
on [Alpaca](https://alpaca.markets). Stocks and options are both supported.

> **Risk warning.** This places real orders from messages written by someone else. Signals can be late,
> wrong, or misread. Start in dry-run mode, then paper trading, and only go live with money you can lose.
> Nothing here is financial advice.

## How it works

1. A Discord bot listens to the channel(s) you choose, optionally only to specific people.
2. Each message is parsed into a signal: buy, sell, or trim; ticker; option strike, side, and expiry; price.
3. Risk rules decide whether to act: daily trade limit, max open positions, dollar caps, ticker lists.
4. The order goes to Alpaca. Entries use a limit order a little above the called price, exits use market orders.
5. Every position the bot opens is saved in `state.json`. Exit signals only sell what the bot bought,
   so anything you hold yourself in the same account is left alone.

### Messages it understands

| Message | What the bot does |
| --- | --- |
| `BTO SPY 450c 10/20 @ 1.20` | Buys SPY 450 calls expiring Oct 20 |
| `BTO $TSLA 12/15 250P 1.50 SL 1.00` | Buys TSLA puts at about 1.50; the stop-loss number is ignored |
| `bto qqq 0dte 480p .85` | Buys QQQ puts expiring today |
| `Buy NVDA @ 118.50` / `Entry $SOFI 7.85` | Buys shares |
| `Trim SPY 450c` / `Sell half TSLA` | Sells half of the bot's position (see `trim_fraction`) |
| `STC SPY 450c @ 1.80` / `Sold all AAPL` / `All out SPY` | Sells the bot's whole position in that ticker or contract |
| `In SPX 5800C 3.20` / `SPX 5800C @ 3.20 BTO` | Buys the SPXW 5800 call expiring today (no date on SPX means 0DTE) |
| `Out SPX` / `Getting out SPX 5800C` | Sells the bot's SPX position |

"In" and "Out" only count when they start a line, so everyday chat like "SPX looking good in here" is ignored.
If the trader uses other words, add them under `parsing` in `config.yaml` instead of editing code.

Test how your server's messages are read before trading:

```bash
python -m copytrader parse "BTO SPY 450c 10/20 @ 1.20" "Trim SPY 450c" "Sold all AAPL"
```

If a format your server uses is not recognised, open an issue with a few example messages or adjust
the patterns in `copytrader/parser.py`.

## Copying the options-with-demon channel (SPX 0DTE)

A ready-made config for this channel is in `examples/options-with-demon.yaml`:

```bash
cp examples/options-with-demon.yaml config.yaml
```

What it sets up:

- **Channel by name.** It watches any channel whose name contains `options-with-demon`, so you don't need the ID.
- **SPX routing.** SPX alerts are placed as SPXW contracts, the PM-settled weeklies and 0DTEs that
  Alpaca supports for paper and live trading. An SPX alert with no date is treated as expiring today.
- **SPX price ticks.** Limit prices round to $0.05 under $3 and $0.10 at $3 and above, as Cboe requires.
- **One contract per alert,** capped at $1,500, at most 5 entries a day. Only alerts with an entry price are copied.
- **End-of-day safety net.** At 3:50pm ET the bot sells any contract that expires today, in case the
  trader's exit alert was missed or never posted. Change or turn off `auto_close_expiring_at` in the config.

Before trading, paste a handful of the trader's real alerts into the parse command and confirm each one
reads correctly. Include entries, trims, and exits:

```bash
python -m copytrader parse "paste an entry alert here" "paste a trim alert here" "paste an exit alert here"
```

**Access.** The lock icon on that channel means it is limited to members with a certain role. A bot can
only read it if a server admin invites the bot and gives it that role. It is a regular text channel, not
an Announcement channel, so the Follow workaround below does not apply to it.

SPXW contracts carry a regulatory fee of about $0.50 to $0.59 per contract on Alpaca, on top of the usual costs.

## Setup

### 1. Install

Requires Python 3.10 or newer.

```bash
git clone https://github.com/alanvaldez4285-blip/Auto-Trader.git
cd Auto-Trader
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env
```

### 2. Create a Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Open **Bot**, click **Reset Token**, and put the token in `.env` as `DISCORD_TOKEN`.
3. On the same page, turn on **Message Content Intent**. Without it the bot sees empty messages.
4. Open **OAuth2 > URL Generator**, tick the `bot` scope, then the **View Channels** and
   **Read Message History** permissions. Open the generated link to invite the bot.

### 3. Get the bot into the signal channel

A bot can only read servers it has been invited to. Because this is a server you are a member of,
not one you run, pick one of these:

- **Ask a server admin to invite your bot.** It only needs read access. Many signal servers allow this.
- **Follow the channel into your own server.** If the alerts channel is an Announcement channel,
  it shows a **Follow** button. Create a free server of your own, follow the alerts channel into it,
  invite your bot to your server, and point `channel_ids` at the followed copy. Followed posts show up
  under the source server's name, so leave `author_ids` empty or use `author_names` with that name.
- Logging in with your personal account token (a "self-bot") breaks Discord's Terms of Service and can
  get your account banned. This project does not support it.

Then turn on **Developer Mode** in Discord (Settings > Advanced), right-click the channel and choose
**Copy Channel ID**, and put it in `config.yaml` under `discord.channel_ids`. Do the same with
**Copy User ID** on the person whose trades you want to follow and put it under `author_ids`.
Filtering by author stops the bot from acting on chatter from other members.

### 4. Connect Alpaca

1. Sign up at [alpaca.markets](https://alpaca.markets). Paper trading is free and needs no deposit.
2. In the paper trading dashboard, generate API keys and put them in `.env` as `ALPACA_API_KEY` and
   `ALPACA_SECRET_KEY`.
3. For options, make sure options trading is enabled on the account (level 2 or higher to buy calls and puts).

### 5. Run it

```bash
python -m copytrader run            # add -v to log every message the bot sees
```

Recommended progression:

1. `dry_run: true`. Orders are simulated and logged only. Watch `copytrader.log` for a few days and
   check that every call-out was read correctly.
2. `dry_run: false` with `broker.paper: true`. Real orders on Alpaca's paper account with fake money.
3. `broker.paper: false`, with live API keys in `.env`. Real money. Keep the budgets small at first.

The bot has to stay running to catch alerts. A cheap always-on VPS, a Raspberry Pi, or a spare PC works.
Keep `state.json` between restarts, since that is how the bot remembers what it bought.

## Using Robinhood instead of Alpaca

The bot can place orders on Robinhood, but read this first:

- **It uses an unofficial API.** Robinhood has no public API for stocks or options. The bot logs in with
  your username and password through the [robin_stocks](https://pypi.org/project/robin-stocks/) library
  and calls the same private endpoints the app uses. That is against Robinhood's terms of service and
  can get your account restricted. It can also stop working whenever Robinhood changes its app.
- **There is no paper trading.** Every Robinhood order is real money. Test with `dry_run: true` until the
  log shows every alert handled correctly. The bot refuses to start on Robinhood unless you set
  `broker.paper: false`, as a deliberate confirmation.
- **SPX is untested on Robinhood.** The bot looks up SPX contracts under both SPXW and SPX. Watch your
  first few trades in the Robinhood app to confirm the right contract is picked.
- **Options always go in as limit orders.** Exits labelled "market" are sent as limits at the bid, which fill right away.

Setup:

```bash
pip install -r requirements-robinhood.txt
```

1. Put `ROBINHOOD_USERNAME` and `ROBINHOOD_PASSWORD` in `.env`.
2. Recommended: in the Robinhood app, turn on two-factor authentication with an **authenticator app**.
   When it shows the setup key, copy it into `.env` as `ROBINHOOD_MFA_SECRET`, then finish setup in your
   authenticator app as usual. The bot then generates login codes itself.
   Without it, the first login asks you to approve the device in the app or type a texted code.
3. In `config.yaml`, set `broker.name: robinhood`, `broker.paper: false`, and `dry_run: false`.

The login is saved in `~/.tokens/robinhood.pickle`, so restarts don't ask again. Keep that file private.

## Configuration

Everything is in `config.yaml`; each setting is explained in `config.example.yaml`. The main ones:

| Setting | Default | Meaning |
| --- | --- | --- |
| `dry_run` | `true` | Simulate orders instead of sending them |
| `sizing.dollars_per_trade` | `500` | Budget per entry. Options buy `floor(500 / (price x 100))` contracts |
| `risk.max_dollars_per_trade` | `1000` | Hard cap per entry in any sizing mode |
| `risk.max_trades_per_day` | `10` | Max new entries per day. Exits are never blocked |
| `risk.max_open_positions` | `5` | Max positions held by the bot at once |
| `risk.allowed_tickers` | empty | Only trade these tickers, for example `[SPY, QQQ]` |
| `execution.entry_slippage_pct` | `5` | Entry limit price is the called price plus this percentage |
| `execution.exit_order_type` | `market` | `market` exits fast; `limit` sells at the called price minus `exit_slippage_pct` |
| `execution.trim_fraction` | `0.5` | Share of the position sold on a trim |

## Things to know

- **Pattern day trader rule.** US margin accounts under $25,000 are limited to 3 day trades in 5 business
  days. Same-day scalps and 0DTE options count. Alpaca will reject orders that break the rule.
- **Entries can miss.** If price runs past your limit, the order does not fill and expires at the close.
  A later exit call cancels the unfilled order instead of selling.
- **Option call-outs need an expiry.** `BTO SPY 450c` with no date is skipped rather than guessed.
  Exits can leave details out: `STC SPY 450c` or `STC SPY` sell whatever matching position the bot holds.
- **Contracts too expensive for your budget are skipped.** Raise `dollars_per_trade` if that happens a lot.
- **Edited or deleted Discord messages are ignored.** Only new messages trigger trades.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Code layout:

- `copytrader/parser.py` turns message text into a signal.
- `copytrader/trader.py` applies risk rules, sizes orders, and keeps the position ledger.
- `copytrader/broker.py` holds the Alpaca adapter and the dry-run simulator. Add other brokers here.
- `copytrader/discord_listener.py` connects to Discord and filters channels and authors.
