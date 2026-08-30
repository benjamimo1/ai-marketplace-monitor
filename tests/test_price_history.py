from pathlib import Path

import pytest

from ai_marketplace_monitor import price_history
from ai_marketplace_monitor.listing import Listing


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "price_history.db"


def make_listing(listing_id: str, price: str, title: str = "iPad Air 5") -> Listing:
    return Listing(
        marketplace="facebook",
        name="",
        id=listing_id,
        title=title,
        image="",
        price=price,
        post_url=f"https://www.facebook.com/marketplace/item/{listing_id}",
        location="Guadalajara, JAL",
        seller="",
        condition="",
        description="",
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("$450", (450.0, "$")),
        ("MX$8,500", (8500.0, "MX$")),
        ("US$1,299.00", (1299.0, "US$")),
        ("$1.299,00", (1299.0, "$")),  # European grouping
        ("$450 | $500", (450.0, "$")),  # discounted price wins over the original
        ("$12,000", (12000.0, "$")),
        ("€899", (899.0, "€")),
        ("Free", (None, None)),
        ("", (None, None)),
        ("**unspecified**", (None, None)),
    ],
)
def test_parse_price(raw: str, expected: tuple) -> None:
    assert price_history.parse_price(raw) == expected


def test_record_is_idempotent_within_a_day(db: Path) -> None:
    batch = [make_listing("1", "MX$8,500"), make_listing("2", "Free")]
    assert price_history.record(batch, "ipad", "ipad air 5", db_path=db) == 2
    # a second run on the same day adds nothing, including the priceless listing
    assert price_history.record(batch, "ipad", "ipad air 5", db_path=db) == 0


def test_overlapping_search_phrases_do_not_double_count(db: Path) -> None:
    batch = [make_listing("1", "MX$8,500")]
    price_history.record(batch, "ipad", "ipad air 5", db_path=db)
    price_history.record(batch, "ipad", "ipad air m1", db_path=db)
    assert price_history.stats(db_path=db)[0].n_observations == 1


def test_price_change_records_a_second_observation(db: Path) -> None:
    price_history.record([make_listing("1", "MX$8,500")], "ipad", "ipad air 5", db_path=db)
    price_history.record([make_listing("1", "MX$7,000")], "ipad", "ipad air 5", db_path=db)
    result = price_history.stats(db_path=db)[0]
    assert result.n_observations == 2
    assert result.n_listings == 1
    assert result.minimum == 7000.0


def test_stats_ignore_unpriced_listings_and_split_currencies(db: Path) -> None:
    price_history.record(
        [
            make_listing("1", "MX$8,000"),
            make_listing("2", "MX$10,000"),
            make_listing("3", "Free"),
            make_listing("4", "US$500"),
        ],
        "ipad",
        "ipad air 5",
        db_path=db,
    )
    by_currency = {row.currency: row for row in price_history.stats(db_path=db)}
    assert set(by_currency) == {"MX$", "US$"}
    assert by_currency["MX$"].mean == 9000.0
    assert by_currency["MX$"].n_observations == 2
    assert by_currency["US$"].mean == 500.0


def test_stats_percentiles(db: Path) -> None:
    prices = ["MX$1,000", "MX$2,000", "MX$3,000", "MX$4,000", "MX$5,000"]
    price_history.record(
        [make_listing(str(i), p) for i, p in enumerate(prices)], "ipad", "q", db_path=db
    )
    result = price_history.stats(db_path=db)[0]
    assert result.median == 3000.0
    assert result.p25 == 2000.0
    assert result.p75 == 4000.0


def test_empty_database_returns_no_rows(db: Path) -> None:
    assert price_history.stats(db_path=db) == []
    assert price_history.item_names(db_path=db) == []
    assert price_history.record([], "ipad", "q", db_path=db) == 0


def test_daily_series_and_observations(db: Path) -> None:
    price_history.record(
        [make_listing("1", "MX$8,000"), make_listing("2", "MX$6,000")],
        "ipad",
        "ipad air 5",
        city="guadalajara",
        db_path=db,
    )
    series = price_history.daily_series(db_path=db)
    assert len(series) == 1
    assert series[0]["n"] == 2
    assert series[0]["mean"] == 7000.0

    rows = price_history.observations(db_path=db)
    assert len(rows) == 2
    assert rows[0]["post_url"].startswith("https://www.facebook.com/marketplace/item/")
    assert price_history.item_names(db_path=db) == ["ipad"]
