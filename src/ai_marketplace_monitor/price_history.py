"""SQLite-backed price history for marketplace listings.

Upstream is alert-driven: it notifies about new listings and then forgets them.
This module records every listing returned by a search, on every run, so that
average / median prices for an item can be tracked over time.

Recording happens on the raw search-results page, before the local keyword,
seller and AI filters are applied, so the sample reflects the whole market for a
phrase rather than only the listings that were interesting enough to notify
about. Note that `min_price` / `max_price` are passed to Facebook in the search
URL and so still bound the sample -- which is usually wanted, since it keeps
cases and spare parts out of the average -- but the band should be set wide
enough not to clip genuine listings.
"""

import re
import sqlite3
import statistics
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from .utils import amm_home

DB_PATH = amm_home / "price_history.db"

_SCHEMA = """
-- Per-listing facts that do not change between observations. Description,
-- seller and condition only become available once the listing's own page has
-- been opened, so they are backfilled by update_details() and stay NULL for
-- listings whose details were never fetched.
CREATE TABLE IF NOT EXISTS listing (
    marketplace TEXT NOT NULL,
    listing_id  TEXT NOT NULL,
    post_url    TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT,
    seller      TEXT,
    condition   TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    PRIMARY KEY (marketplace, listing_id)
);

CREATE TABLE IF NOT EXISTS observation (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace   TEXT NOT NULL,
    listing_id    TEXT NOT NULL,
    item_name     TEXT NOT NULL,
    search_phrase TEXT NOT NULL,
    city          TEXT,
    title         TEXT,
    price         REAL,
    currency      TEXT,
    price_raw     TEXT,
    location      TEXT,
    -- 1 when the listing passed the item's own title/location filters. Facebook
    -- pads thin result sets with loosely related items, so averaging over
    -- everything the page returned would fold in unrelated products.
    matched       INTEGER NOT NULL DEFAULT 1,
    observed_at   TEXT NOT NULL,
    observed_date TEXT NOT NULL
);

-- One row per listing per item per day per price. A price change on the same
-- day therefore records a second row, which is the interesting event.
-- COALESCE because NULL never compares equal in a unique index, so listings
-- with no parseable price ("Free") would otherwise re-insert on every run.
-- search_phrase is deliberately not part of the key: overlapping phrases for
-- the same item must not weight a listing two or three times in the average.
CREATE UNIQUE INDEX IF NOT EXISTS observation_daily
    ON observation (marketplace, listing_id, item_name, observed_date, COALESCE(price, -1));

CREATE INDEX IF NOT EXISTS observation_item_time
    ON observation (item_name, observed_at);
"""

# "$450", "MX$8,500", "US$1.299,00" -> leading non-digits are the currency.
_PRICE_RE = re.compile(r"^\s*([^\d]*?)\s*([\d.,]+)\s*$")


def parse_price(raw: str) -> Tuple[Optional[float], Optional[str]]:
    """Split a displayed price into (amount, currency).

    `utils.extract_price` joins a discounted price and its original with " | ";
    the first is the current price, so that is what we keep.
    """
    if not raw or raw == "**unspecified**":
        return None, None
    current = raw.split("|")[0].strip()
    matched = _PRICE_RE.match(current)
    if not matched:
        return None, None
    currency, digits = matched.group(1).strip() or None, matched.group(2)
    # Strip thousands separators. Both "1,299.00" and "1.299,00" appear
    # depending on locale; the last separator with 1-2 trailing digits decides.
    if re.search(r"[.,]\d{1,2}$", digits):
        sep = digits[-3] if digits[-3] in ".," else digits[-2]
        digits = digits.replace("." if sep == "," else ",", "").replace(sep, ".")
    else:
        digits = digits.replace(",", "").replace(".", "")
    try:
        return float(digits), currency
    except ValueError:
        return None, None


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(observation)")}
    if "matched" not in existing:
        conn.execute("ALTER TABLE observation ADD COLUMN matched INTEGER NOT NULL DEFAULT 1")
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(listing)")}
    for column in ("description", "seller", "condition"):
        if column not in existing:
            conn.execute(f"ALTER TABLE listing ADD COLUMN {column} TEXT")


def update_details(
    listing: object,
    marketplace: str = "facebook",
    db_path: Path | None = None,
) -> None:
    """Backfill the facts that only appear on a listing's own page.

    Titles alone are often ambiguous -- "iPad Air" spans several generations at
    very different prices -- so the description is kept to identify what was
    actually being sold. Existing values are only overwritten by non-empty ones,
    since a later scrape may fail to extract a description it got before.
    """
    listing_id = getattr(listing, "id", "") or ""
    if not listing_id:
        return
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE listing SET description = COALESCE(NULLIF(?, ''), description),"
            " seller = COALESCE(NULLIF(?, ''), seller),"
            " condition = COALESCE(NULLIF(?, ''), condition)"
            " WHERE marketplace = ? AND listing_id = ?",
            (
                getattr(listing, "description", "") or "",
                getattr(listing, "seller", "") or "",
                getattr(listing, "condition", "") or "",
                marketplace,
                listing_id,
            ),
        )


def _is_matched(predicate: Optional[Callable[[object], bool]], listing: object) -> int:
    """Never let a broken filter lose data: on error the listing still counts."""
    if predicate is None:
        return 1
    try:
        return 1 if predicate(listing) else 0
    except KeyboardInterrupt:
        raise
    except Exception:
        return 1


@contextmanager
def _connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def record(
    listings: Sequence,
    item_name: str,
    search_phrase: str,
    city: str | None = None,
    marketplace: str = "facebook",
    matched: Optional[Callable[[object], bool]] = None,
    db_path: Path | None = None,
) -> int:
    """Record one observation per listing. Returns the number of new rows.

    `matched` decides whether a listing counts towards the item's statistics.
    Everything is stored either way, so a filter that turns out to be too strict
    can be reviewed later with `aimm-prices list --all`.
    """
    if not listings:
        return 0
    now = datetime.now()
    stamp, day = now.isoformat(timespec="seconds"), now.date().isoformat()
    inserted = 0
    with _connect(db_path) as conn:
        for listing in listings:
            price, currency = parse_price(getattr(listing, "price", "") or "")
            listing_id = getattr(listing, "id", "") or ""
            if not listing_id:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO observation (marketplace, listing_id, item_name,"
                " search_phrase, city, title, price, currency, price_raw, location,"
                " matched, observed_at, observed_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    marketplace,
                    listing_id,
                    item_name,
                    search_phrase,
                    city,
                    getattr(listing, "title", ""),
                    price,
                    currency,
                    getattr(listing, "price", ""),
                    getattr(listing, "location", ""),
                    _is_matched(matched, listing),
                    stamp,
                    day,
                ),
            )
            inserted += cur.rowcount
            conn.execute(
                "INSERT INTO listing (marketplace, listing_id, post_url, title,"
                " first_seen, last_seen) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT (marketplace, listing_id) DO UPDATE SET last_seen=excluded.last_seen",
                (
                    marketplace,
                    listing_id,
                    getattr(listing, "post_url", ""),
                    getattr(listing, "title", ""),
                    stamp,
                    stamp,
                ),
            )
    return inserted


@dataclass
class Stats:
    item_name: str
    currency: str | None
    n_observations: int
    n_listings: int
    mean: float
    median: float
    minimum: float
    maximum: float
    p25: float
    p75: float
    first_seen: str
    last_seen: str


def _percentile(values: List[float], fraction: float) -> float:
    if len(values) == 1:
        return values[0]
    position = fraction * (len(values) - 1)
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def stats(
    item_name: str | None = None,
    days: int | None = None,
    include_unmatched: bool = False,
    db_path: Path | None = None,
) -> List[Stats]:
    """Summarize prices per item (and per currency, so locales do not mix)."""
    query = (
        "SELECT item_name, currency, listing_id, price, observed_at FROM observation"
        " WHERE price IS NOT NULL"
    )
    params: List[object] = []
    if not include_unmatched:
        query += " AND matched = 1"
    if item_name:
        query += " AND item_name = ?"
        params.append(item_name)
    if days:
        query += " AND observed_at >= ?"
        params.append((datetime.now() - timedelta(days=days)).isoformat(timespec="seconds"))

    grouped: Dict[Tuple[str, str | None], List[sqlite3.Row]] = {}
    with _connect(db_path) as conn, closing(conn.execute(query, params)) as cur:
        for row in cur:
            grouped.setdefault((row["item_name"], row["currency"]), []).append(row)

    results = []
    for (name, currency), rows in sorted(grouped.items()):
        prices = sorted(row["price"] for row in rows)
        times = [row["observed_at"] for row in rows]
        results.append(
            Stats(
                item_name=name,
                currency=currency,
                n_observations=len(prices),
                n_listings=len({row["listing_id"] for row in rows}),
                mean=statistics.fmean(prices),
                median=statistics.median(prices),
                minimum=prices[0],
                maximum=prices[-1],
                p25=_percentile(prices, 0.25),
                p75=_percentile(prices, 0.75),
                first_seen=min(times),
                last_seen=max(times),
            )
        )
    return results


def daily_series(
    item_name: str | None = None,
    days: int | None = None,
    include_unmatched: bool = False,
    db_path: Path | None = None,
) -> List[sqlite3.Row]:
    """Per-day count / mean / median / min / max, for plotting a trend."""
    query = (
        "SELECT observed_date AS day, item_name, currency, COUNT(*) AS n,"
        " AVG(price) AS mean, MIN(price) AS minimum, MAX(price) AS maximum,"
        " GROUP_CONCAT(price) AS prices"
        " FROM observation WHERE price IS NOT NULL"
    )
    params: List[object] = []
    if not include_unmatched:
        query += " AND matched = 1"
    if item_name:
        query += " AND item_name = ?"
        params.append(item_name)
    if days:
        query += " AND observed_date >= ?"
        params.append((datetime.now() - timedelta(days=days)).date().isoformat())
    query += " GROUP BY day, item_name, currency ORDER BY day"
    with _connect(db_path) as conn, closing(conn.execute(query, params)) as cur:
        return cur.fetchall()


def observations(
    item_name: str | None = None,
    days: int | None = None,
    limit: int = 200,
    include_unmatched: bool = False,
    db_path: Path | None = None,
) -> List[sqlite3.Row]:
    query = (
        "SELECT o.observed_at, o.item_name, o.search_phrase, o.title, o.price_raw,"
        " o.price, o.currency, o.location, o.matched, l.post_url, l.description,"
        " l.seller, l.condition"
        " FROM observation o LEFT JOIN listing l"
        " ON l.marketplace = o.marketplace AND l.listing_id = o.listing_id WHERE 1=1"
    )
    params: List[object] = []
    if not include_unmatched:
        query += " AND o.matched = 1"
    if item_name:
        query += " AND o.item_name = ?"
        params.append(item_name)
    if days:
        query += " AND o.observed_at >= ?"
        params.append((datetime.now() - timedelta(days=days)).isoformat(timespec="seconds"))
    query += " ORDER BY o.observed_at DESC, o.price IS NULL, o.price LIMIT ?"
    params.append(limit)
    with _connect(db_path) as conn, closing(conn.execute(query, params)) as cur:
        return cur.fetchall()


def item_names(db_path: Path | None = None) -> List[str]:
    with _connect(db_path) as conn, closing(
        conn.execute("SELECT DISTINCT item_name FROM observation ORDER BY item_name")
    ) as cur:
        return [row["item_name"] for row in cur]
