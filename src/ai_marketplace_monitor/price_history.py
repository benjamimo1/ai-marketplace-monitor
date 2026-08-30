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

from .classify import UNKNOWN, classify, is_product
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
    -- which product the listing is actually selling; see classify.py
    model       TEXT,
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

-- Who has already been approached, so an offer is never repeated and a seller
-- with several listings is only contacted once. Keyed by listing, but sellers
-- are matched by name too: one person often lists the same model twice.
CREATE TABLE IF NOT EXISTS contact (
    marketplace  TEXT NOT NULL,
    listing_id   TEXT NOT NULL,
    seller       TEXT,
    offer        REAL,
    message      TEXT,
    contacted_at TEXT NOT NULL,
    PRIMARY KEY (marketplace, listing_id)
);

-- What the user has ACTUALLY sold, and for how much. These are achieved
-- prices, not asking prices, and are the only trustworthy basis for deciding
-- what to pay: a market can ask far more than it gets.
CREATE TABLE IF NOT EXISTS sale (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace TEXT NOT NULL,
    title       TEXT NOT NULL,
    model       TEXT,
    price       REAL NOT NULL,
    currency    TEXT,
    sold_on     TEXT,
    buyer       TEXT,
    note        TEXT,
    recorded_at TEXT NOT NULL,
    UNIQUE (marketplace, title, price)
);

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
    for column in ("model", "description", "seller", "condition"):
        if column not in existing:
            conn.execute(f"ALTER TABLE listing ADD COLUMN {column} TEXT")
    # Classify anything not yet classified, so history collected before the
    # column existed is not silently excluded from every model-aware query.
    rows = conn.execute(
        "SELECT marketplace, listing_id, title, description FROM listing WHERE model IS NULL"
    ).fetchall()
    if rows:
        conn.executemany(
            "UPDATE listing SET model = ? WHERE marketplace = ? AND listing_id = ?",
            [
                (classify(r["title"], r["description"]), r["marketplace"], r["listing_id"])
                for r in rows
            ],
        )


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
            " condition = COALESCE(NULLIF(?, ''), condition),"
            " model = ?"
            " WHERE marketplace = ? AND listing_id = ?",
            (
                getattr(listing, "description", "") or "",
                getattr(listing, "seller", "") or "",
                getattr(listing, "condition", "") or "",
                classify(
                    getattr(listing, "title", ""), getattr(listing, "description", "")
                ),
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
                "INSERT INTO listing (marketplace, listing_id, post_url, title, model,"
                " first_seen, last_seen) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT (marketplace, listing_id) DO UPDATE SET last_seen=excluded.last_seen",
                (
                    marketplace,
                    listing_id,
                    getattr(listing, "post_url", ""),
                    getattr(listing, "title", ""),
                    classify(getattr(listing, "title", "")),
                    stamp,
                    stamp,
                ),
            )
    return inserted


@dataclass
class Stats:
    item_name: str
    model: str | None
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
    model: str | None = None,
    by_model: bool = False,
    products_only: bool = True,
    db_path: Path | None = None,
) -> List[Stats]:
    """Summarize prices per item, currency and -- optionally -- product model.

    A search for one model returns many others, so a single average over an item
    mixes products that are not comparable. `by_model` splits them; `model`
    restricts to one. `products_only` drops accessories, parts and unclassified
    listings, which would otherwise drag the average down.
    """
    query = (
        "SELECT o.item_name, o.currency, o.listing_id, o.price, o.observed_at,"
        " l.model FROM observation o LEFT JOIN listing l"
        " ON l.marketplace = o.marketplace AND l.listing_id = o.listing_id"
        " WHERE o.price IS NOT NULL"
    )
    params: List[object] = []
    if not include_unmatched:
        query += " AND o.matched = 1"
    if item_name:
        query += " AND o.item_name = ?"
        params.append(item_name)
    if model:
        query += " AND l.model = ?"
        params.append(model)
    if days:
        query += " AND o.observed_at >= ?"
        params.append((datetime.now() - timedelta(days=days)).isoformat(timespec="seconds"))

    grouped: Dict[Tuple[str, str | None, str | None], List[sqlite3.Row]] = {}
    with _connect(db_path) as conn, closing(conn.execute(query, params)) as cur:
        for row in cur:
            if products_only and not is_product(row["model"] or UNKNOWN):
                continue
            key = (row["item_name"], row["currency"], row["model"] if by_model else None)
            grouped.setdefault(key, []).append(row)

    results = []
    for (name, currency, model_label), rows in sorted(
        grouped.items(), key=lambda kv: (kv[0][0], kv[0][1] or "", kv[0][2] or "")
    ):
        prices = sorted(row["price"] for row in rows)
        times = [row["observed_at"] for row in rows]
        results.append(
            Stats(
                item_name=name,
                model=model_label,
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
    model: str | None = None,
    latest_only: bool = False,
    db_path: Path | None = None,
) -> List[sqlite3.Row]:
    query = (
        "SELECT o.observed_at, o.item_name, o.search_phrase, o.title, o.price_raw,"
        " o.listing_id,"
        " o.price, o.currency, o.location, o.matched, l.post_url, l.description,"
        " l.seller, l.condition, l.model"
        " FROM observation o LEFT JOIN listing l"
        " ON l.marketplace = o.marketplace AND l.listing_id = o.listing_id WHERE 1=1"
    )
    params: List[object] = []
    if not include_unmatched:
        query += " AND o.matched = 1"
    if item_name:
        query += " AND o.item_name = ?"
        params.append(item_name)
    if model:
        query += " AND l.model = ?"
        params.append(model)
    if days:
        query += " AND o.observed_at >= ?"
        params.append((datetime.now() - timedelta(days=days)).isoformat(timespec="seconds"))
    if latest_only:
        # one row per listing: its most recent price, which is what you would pay
        query += (
            " AND o.observed_at = (SELECT MAX(o2.observed_at) FROM observation o2"
            " WHERE o2.listing_id = o.listing_id AND o2.marketplace = o.marketplace)"
        )
    query += " ORDER BY o.observed_at DESC, o.price IS NULL, o.price LIMIT ?"
    params.append(limit)
    with _connect(db_path) as conn, closing(conn.execute(query, params)) as cur:
        return cur.fetchall()


def record_sale(
    title: str,
    price: float,
    model: str | None = None,
    sold_on: str | None = None,
    buyer: str | None = None,
    note: str | None = None,
    currency: str = "$",
    marketplace: str = "facebook",
    db_path: Path | None = None,
) -> None:
    """Record something the user actually sold, and for how much.

    Classification is derived from the title when no model is given, so a sale
    lines up with the listings of the same model automatically.
    """
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sale (marketplace, title, model, price, currency,"
            " sold_on, buyer, note, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                marketplace,
                title,
                model or classify(title),
                price,
                currency,
                sold_on,
                buyer,
                note,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def sales(model: str | None = None, db_path: Path | None = None) -> List[sqlite3.Row]:
    """Achieved sale prices, most recent first."""
    query = "SELECT * FROM sale"
    params: List[object] = []
    if model:
        query += " WHERE model = ?"
        params.append(model)
    query += " ORDER BY COALESCE(sold_on, recorded_at) DESC"
    with _connect(db_path) as conn, closing(conn.execute(query, params)) as cur:
        return cur.fetchall()


def achieved_price(model: str, db_path: Path | None = None) -> Optional[float]:
    """What the user actually got for this model, if they have ever sold one."""
    rows = sales(model=model, db_path=db_path)
    return statistics.median([r["price"] for r in rows]) if rows else None


def record_contact(
    listing_id: str,
    seller: str | None = None,
    offer: float | None = None,
    message: str | None = None,
    marketplace: str = "facebook",
    db_path: Path | None = None,
) -> None:
    """Note that an offer was sent, so it is not sent again tomorrow."""
    with _connect(db_path) as conn:
        if seller is None:
            row = conn.execute(
                "SELECT seller FROM listing WHERE marketplace = ? AND listing_id = ?",
                (marketplace, listing_id),
            ).fetchone()
            seller = row["seller"] if row else None
        conn.execute(
            "INSERT INTO contact (marketplace, listing_id, seller, offer, message,"
            " contacted_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT (marketplace, listing_id) DO UPDATE SET"
            " offer=excluded.offer, message=excluded.message,"
            " contacted_at=excluded.contacted_at",
            (
                marketplace,
                listing_id,
                seller,
                offer,
                message,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def contacts(db_path: Path | None = None) -> List[sqlite3.Row]:
    """Every offer already sent, most recent first."""
    with _connect(db_path) as conn, closing(
        conn.execute(
            "SELECT c.listing_id, c.seller, c.offer, c.message, c.contacted_at,"
            " l.title FROM contact c LEFT JOIN listing l"
            " ON l.marketplace = c.marketplace AND l.listing_id = c.listing_id"
            " ORDER BY c.contacted_at DESC"
        )
    ) as cur:
        return cur.fetchall()


def contacted(db_path: Path | None = None) -> Tuple[set, set]:
    """(listing ids, seller names) already approached."""
    with _connect(db_path) as conn, closing(
        conn.execute("SELECT listing_id, seller FROM contact")
    ) as cur:
        rows = cur.fetchall()
    return (
        {r["listing_id"] for r in rows},
        {r["seller"].strip().lower() for r in rows if r["seller"]},
    )


def item_names(db_path: Path | None = None) -> List[str]:
    with _connect(db_path) as conn, closing(
        conn.execute("SELECT DISTINCT item_name FROM observation ORDER BY item_name")
    ) as cur:
        return [row["item_name"] for row in cur]
