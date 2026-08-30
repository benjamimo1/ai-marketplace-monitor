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


def test_unmatched_listings_are_stored_but_excluded_from_stats(db: Path) -> None:
    batch = [make_listing("1", "MX$8,000", "iPad Air 5"), make_listing("2", "MX$355,000", "Ford f550")]
    price_history.record(
        batch, "ipad", "ipad air 5", matched=lambda x: "iPad" in x.title, db_path=db
    )
    # the truck is on record ...
    assert len(price_history.observations(include_unmatched=True, db_path=db)) == 2
    # ... but must not drag the average up
    result = price_history.stats(db_path=db, products_only=False)[0]
    assert result.n_observations == 1
    assert result.mean == 8000.0
    assert (
        price_history.stats(include_unmatched=True, products_only=False, db_path=db)[
            0
        ].n_observations
        == 2
    )


def test_a_broken_filter_never_discards_a_listing(db: Path) -> None:
    def explode(listing: object) -> bool:
        raise RuntimeError("filter is broken")

    price_history.record([make_listing("1", "MX$8,000")], "ipad", "q", matched=explode, db_path=db)
    assert price_history.stats(db_path=db)[0].n_observations == 1


def test_migration_adds_matched_column_to_an_existing_database(db: Path) -> None:
    import sqlite3

    # a database created before the column existed
    conn = sqlite3.connect(db)
    conn.executescript(
        price_history._SCHEMA.replace(
            "matched       INTEGER NOT NULL DEFAULT 1,", ""
        ).replace(", COALESCE(price, -1)", ", price")
    )
    conn.execute(
        "INSERT INTO observation (marketplace, listing_id, item_name, search_phrase,"
        " price, currency, observed_at, observed_date)"
        " VALUES ('facebook','1','ipad','q',8000.0,'MX$','2026-01-01T00:00:00','2026-01-01')"
    )
    conn.commit()
    conn.close()

    # opening it through the module migrates it, and pre-existing rows count as matched
    assert price_history.stats(db_path=db, products_only=False)[0].n_observations == 1


def test_update_details_backfills_description(db: Path) -> None:
    listing = make_listing("1", "MX$8,000", "iPad Air")
    price_history.record([listing], "ipad", "ipad air 5", db_path=db)
    assert price_history.observations(db_path=db)[0]["description"] is None

    listing.description = "iPad Air 5ta generacion, chip M1, 64GB, impecable"
    listing.seller = "Juan P"
    listing.condition = "Used - like new"
    price_history.update_details(listing, db_path=db)

    row = price_history.observations(db_path=db)[0]
    assert "M1" in row["description"]
    assert row["seller"] == "Juan P"
    assert row["condition"] == "Used - like new"


def test_update_details_never_overwrites_with_blanks(db: Path) -> None:
    listing = make_listing("1", "MX$8,000")
    price_history.record([listing], "ipad", "q", db_path=db)
    listing.description = "chip M1, 256GB"
    price_history.update_details(listing, db_path=db)
    # a later scrape that failed to extract the description must not erase it
    listing.description = ""
    price_history.update_details(listing, db_path=db)
    assert price_history.observations(db_path=db)[0]["description"] == "chip M1, 256GB"


def test_update_details_on_unknown_listing_is_harmless(db: Path) -> None:
    listing = make_listing("999", "MX$1,000")
    listing.description = "orphan"
    price_history.update_details(listing, db_path=db)  # must not raise
    assert price_history.observations(db_path=db) == []


def test_stats_split_by_model(db: Path) -> None:
    price_history.record(
        [
            make_listing("1", "$400.000", "iPad Air 5ta generación"),
            make_listing("2", "$440.000", "iPad Air 5 M1 256GB"),
            make_listing("3", "$180.000", "iPad Air 4ta generación"),
            make_listing("4", "$10.000", "Lápiz iPad"),
        ],
        "ipad",
        "ipad air 5",
        db_path=db,
    )
    by_model = {row.model: row for row in price_history.stats(by_model=True, db_path=db)}
    # the accessory is excluded, and the two generations do not pool
    assert set(by_model) == {"iPad Air 5 (M1)", "iPad Air 4"}
    assert by_model["iPad Air 5 (M1)"].mean == 420000.0
    assert by_model["iPad Air 4"].mean == 180000.0
    # and a single model can be requested directly
    assert price_history.stats(model="iPad Air 4", db_path=db)[0].n_listings == 1


def test_model_is_reclassified_when_a_description_arrives(db: Path) -> None:
    listing = make_listing("1", "$400.000", "iPad Air")
    price_history.record([listing], "ipad", "ipad air 5", db_path=db)
    assert price_history.stats(by_model=True, db_path=db) == []  # unknown, so excluded

    listing.description = "iPad Air 5ta generacion chip M1"
    price_history.update_details(listing, db_path=db)
    assert price_history.stats(by_model=True, db_path=db)[0].model == "iPad Air 5 (M1)"
