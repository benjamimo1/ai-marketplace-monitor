"""Console script for querying the price history database."""

import csv
import sys
from typing import Annotated, Optional

import rich
import typer
from rich.table import Table

from . import price_history

app = typer.Typer(help="Inspect the price history recorded by the monitor.")

ItemOption = Annotated[
    Optional[str], typer.Option("--item", "-i", help="Restrict to one [item.NAME] from the config.")
]
DaysOption = Annotated[
    Optional[int], typer.Option("--days", "-d", help="Only consider the last N days.")
]


def _money(value: float, currency: Optional[str]) -> str:
    return f"{currency or ''}{value:,.0f}" if value >= 100 else f"{currency or ''}{value:,.2f}"


@app.command()
def summary(
    item: ItemOption = None,
    days: DaysOption = None,
    full: Annotated[
        bool, typer.Option("--full", help="Also show quartiles and the observation count.")
    ] = False,
) -> None:
    """Average, median and spread of prices per item."""
    rows = price_history.stats(item_name=item, days=days)
    if not rows:
        rich.print("[yellow]No price observations recorded yet.[/yellow]")
        raise typer.Exit(1)

    table = Table(title=f"Price history{f' (last {days} days)' if days else ''}")
    # the numeric columns must never be truncated, so keep the default view narrow
    # enough for an 80-column terminal and hide the quartiles behind --full
    table.add_column("Item", justify="left", overflow="ellipsis", min_width=10)
    columns = ["Listings", "Mean", "Median", "Min", "Max"]
    if full:
        columns = ["Listings", "Obs", "Mean", "Median", "P25", "P75", "Min", "Max"]
    for column in columns:
        table.add_column(column, justify="right", no_wrap=True)

    for row in rows:
        values = [
            f"{row.item_name}{f' [{row.currency}]' if row.currency else ''}",
            str(row.n_listings),
        ]
        if full:
            values.append(str(row.n_observations))
        values += [_money(row.mean, row.currency), _money(row.median, row.currency)]
        if full:
            values += [_money(row.p25, row.currency), _money(row.p75, row.currency)]
        values += [_money(row.minimum, row.currency), _money(row.maximum, row.currency)]
        table.add_row(*values)
    rich.print(table)


@app.command()
def trend(item: ItemOption = None, days: DaysOption = 30) -> None:
    """Per-day mean price, to see whether an item is getting cheaper."""
    rows = price_history.daily_series(item_name=item, days=days)
    if not rows:
        rich.print("[yellow]No price observations recorded yet.[/yellow]")
        raise typer.Exit(1)

    means = [row["mean"] for row in rows]
    low, high = min(means), max(means)
    span = (high - low) or 1.0

    table = Table(title=f"Daily average price (last {days} days)")
    for column, justify in (("Day", "left"), ("N", "right"), ("Mean", "right"),
                            ("Range", "right")):
        table.add_column(column, justify=justify, no_wrap=True)
    table.add_column("Item", justify="left", overflow="ellipsis", min_width=8)
    table.add_column("", justify="left", no_wrap=True)
    for row in rows:
        currency = row["currency"]
        bar = "█" * (1 + int(20 * (row["mean"] - low) / span))
        table.add_row(
            row["day"],
            str(row["n"]),
            _money(row["mean"], currency),
            f"{_money(row['minimum'], currency)}–{_money(row['maximum'], currency)}",
            row["item_name"],
            bar,
        )
    rich.print(table)


@app.command("list")
def list_observations(
    item: ItemOption = None,
    days: DaysOption = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Maximum rows.")] = 40,
) -> None:
    """The most recent individual listings, cheapest first within a run."""
    rows = price_history.observations(item_name=item, days=days, limit=limit)
    if not rows:
        rich.print("[yellow]No price observations recorded yet.[/yellow]")
        raise typer.Exit(1)

    table = Table(title="Recent observations")
    for column in ("Seen", "Price"):
        table.add_column(column, no_wrap=True)
    for column in ("Item", "Phrase", "Title", "Location"):
        table.add_column(column, overflow="ellipsis")
    for row in rows:
        table.add_row(
            row["observed_at"].replace("T", " ")[5:16],
            row["price_raw"] or "-",
            row["item_name"],
            row["search_phrase"],
            (row["title"] or "")[:50],
            (row["location"] or "")[:20],
        )
    rich.print(table)


@app.command()
def export(
    item: ItemOption = None,
    days: DaysOption = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 100000,
) -> None:
    """Write every observation to stdout as CSV."""
    rows = price_history.observations(item_name=item, days=days, limit=limit)
    if not rows:
        raise typer.Exit(1)
    writer = csv.writer(sys.stdout)
    writer.writerow(rows[0].keys())
    writer.writerows(tuple(row) for row in rows)


@app.command()
def items() -> None:
    """List the item names that have recorded observations."""
    names = price_history.item_names()
    if not names:
        rich.print("[yellow]No price observations recorded yet.[/yellow]")
        raise typer.Exit(1)
    for name in names:
        rich.print(name)


@app.command()
def where() -> None:
    """Print the path to the price history database."""
    rich.print(str(price_history.DB_PATH))
