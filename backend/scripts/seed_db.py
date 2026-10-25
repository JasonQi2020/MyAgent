"""Create the sample stock database (data/demo.db). Safe to re-run: it rebuilds the file.

Prices are randomly generated (fixed seed), NOT real market data.
"""

from __future__ import annotations

import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_demo.config import PROJECT_ROOT  # noqa: E402

COMPANIES = [
    # symbol, name, exchange, sector, currency, starting price
    ("AAPL", "Apple Inc.", "NASDAQ", "Technology", "USD", 230.0),
    ("MSFT", "Microsoft Corp.", "NASDAQ", "Technology", "USD", 420.0),
    ("NVDA", "NVIDIA Corp.", "NASDAQ", "Technology", "USD", 130.0),
    ("JPM", "JPMorgan Chase & Co.", "NYSE", "Financials", "USD", 210.0),
    ("KO", "Coca-Cola Co.", "NYSE", "Consumer Staples", "USD", 68.0),
    ("XOM", "Exxon Mobil Corp.", "NYSE", "Energy", "USD", 115.0),
    ("RY", "Royal Bank of Canada", "TSX", "Financials", "CAD", 165.0),
    ("TD", "Toronto-Dominion Bank", "TSX", "Financials", "CAD", 82.0),
    ("SHOP", "Shopify Inc.", "TSX", "Technology", "CAD", 150.0),
    ("ENB", "Enbridge Inc.", "TSX", "Energy", "CAD", 58.0),
    ("CNR", "Canadian National Railway", "TSX", "Industrials", "CAD", 160.0),
    ("SU", "Suncor Energy Inc.", "TSX", "Energy", "CAD", 52.0),
]

WATCHLIST = [
    ("NVDA", "AI leader, wait for pullback"),
    ("RY", "Dividend, Canadian bank exposure"),
    ("SHOP", "Growth, volatile"),
    ("KO", "Defensive"),
    ("ENB", "High dividend yield"),
    ("MSFT", "Core holding candidate"),
]

SCHEMA = """
CREATE TABLE companies (
    symbol   TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    exchange TEXT NOT NULL,          -- NYSE, NASDAQ, TSX
    sector   TEXT NOT NULL,
    currency TEXT NOT NULL           -- USD or CAD
);
CREATE TABLE daily_prices (
    symbol TEXT NOT NULL REFERENCES companies(symbol),
    date   TEXT NOT NULL,            -- YYYY-MM-DD
    open   REAL, high REAL, low REAL, close REAL,
    volume INTEGER,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE watchlist (
    symbol   TEXT PRIMARY KEY REFERENCES companies(symbol),
    added_on TEXT NOT NULL,
    note     TEXT
);
CREATE TABLE trades (
    id        INTEGER PRIMARY KEY,
    symbol    TEXT NOT NULL REFERENCES companies(symbol),
    side      TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty       INTEGER NOT NULL,
    price     REAL NOT NULL,
    traded_at TEXT NOT NULL          -- YYYY-MM-DD (paper trades)
);
"""


def trading_days(end: date, count: int) -> list[date]:
    days, d = [], end
    while len(days) < count:
        if d.weekday() < 5:  # Mon-Fri (holidays ignored in this demo)
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


def build(db_path: Path, days: int = 60, seed: int = 42) -> None:
    rng = random.Random(seed)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    conn.executemany(
        "INSERT INTO companies VALUES (?, ?, ?, ?, ?)", [c[:5] for c in COMPANIES]
    )

    dates = trading_days(date.today(), days)
    closes: dict[str, dict[str, float]] = {}
    for symbol, *_rest, start in COMPANIES:
        price = start
        closes[symbol] = {}
        for d in dates:
            open_ = price
            price = max(1.0, price * (1 + rng.gauss(0.0005, 0.018)))
            high = max(open_, price) * (1 + abs(rng.gauss(0, 0.006)))
            low = min(open_, price) * (1 - abs(rng.gauss(0, 0.006)))
            volume = int(rng.uniform(2e6, 4e7))
            conn.execute(
                "INSERT INTO daily_prices VALUES (?, ?, ?, ?, ?, ?, ?)",
                (symbol, d.isoformat(), round(open_, 2), round(high, 2), round(low, 2), round(price, 2), volume),
            )
            closes[symbol][d.isoformat()] = round(price, 2)

    conn.executemany(
        "INSERT INTO watchlist VALUES (?, ?, ?)",
        [(s, dates[rng.randrange(0, 20)].isoformat(), note) for s, note in WATCHLIST],
    )

    trade_rows = []
    for _ in range(14):
        symbol = rng.choice([w[0] for w in WATCHLIST] + ["AAPL", "TD"])
        d = rng.choice(dates[5:]).isoformat()
        side = "BUY" if rng.random() < 0.65 else "SELL"
        trade_rows.append((symbol, side, rng.choice([5, 10, 20, 25, 50]), closes[symbol][d], d))
    trade_rows.sort(key=lambda r: r[4])
    conn.executemany(
        "INSERT INTO trades (symbol, side, qty, price, traded_at) VALUES (?, ?, ?, ?, ?)", trade_rows
    )

    conn.commit()
    conn.close()


if __name__ == "__main__":
    target = PROJECT_ROOT / "data" / "demo.db"
    build(target)
    print(f"Created {target}")
