"""Console script for querying the price history database."""

import csv
import re
import sys
from typing import Annotated, Optional

import rich
import typer
from rich.table import Table

from . import classify, price_history

app = typer.Typer(help="Inspect the price history recorded by the monitor.")

ItemOption = Annotated[
    Optional[str], typer.Option("--item", "-i", help="Restrict to one [item.NAME] from the config.")
]
DaysOption = Annotated[
    Optional[int], typer.Option("--days", "-d", help="Only consider the last N days.")
]
AllOption = Annotated[
    bool,
    typer.Option(
        "--all",
        help="Include listings that did not match the item's filters, such as the"
        " loosely related results Facebook pads thin searches with.",
    ),
]


def _clp(value: float) -> str:
    """Chilean pesos: a dot as the thousands separator, no decimals."""
    return f"${value:,.0f}".replace(",", ".")


def _money(value: float, currency: Optional[str]) -> str:
    return f"{currency or ''}{value:,.0f}" if value >= 100 else f"{currency or ''}{value:,.2f}"


@app.command()
def summary(
    item: ItemOption = None,
    days: DaysOption = None,
    by_model: Annotated[
        bool, typer.Option("--by-model/--pooled", help="Split the average per product model.")
    ] = True,
    model: Annotated[
        Optional[str], typer.Option("--model", "-m", help="Restrict to one model label.")
    ] = None,
    products_only: Annotated[
        bool,
        typer.Option(
            "--products-only/--everything",
            help="Drop accessories, for-parts units and unclassified listings.",
        ),
    ] = True,
    unmatched: AllOption = False,
    full: Annotated[
        bool, typer.Option("--full", help="Also show quartiles and the observation count.")
    ] = False,
) -> None:
    """Average, median and spread of prices per item."""
    rows = price_history.stats(
        item_name=item,
        days=days,
        include_unmatched=unmatched,
        model=model,
        by_model=by_model,
        products_only=products_only,
    )
    if not rows:
        rich.print("[yellow]No price observations recorded yet.[/yellow]")
        raise typer.Exit(1)

    currency = rows[0].currency or ""
    title = f"{rows[0].item_name} price history{f' (last {days} days)' if days else ''}"
    table = Table(title=f"{title} — {currency}" if currency else title)
    # the numeric columns must never be truncated, so keep the default view narrow
    # enough for an 80-column terminal and hide the quartiles behind --full
    table.add_column("Model" if by_model else "Item", justify="left",
                     overflow="ellipsis", min_width=10)
    columns = ["Listings", "Mean", "Median", "Min", "Max"]
    if full:
        columns = ["Listings", "Obs", "Mean", "Median", "P25", "P75", "Min", "Max"]
    for column in columns:
        table.add_column(column, justify="right", no_wrap=True)

    for row in rows:
        label = row.model or row.item_name
        values = [label, str(row.n_listings)]
        if full:
            values.append(str(row.n_observations))
        values += [_money(row.mean, row.currency), _money(row.median, row.currency)]
        if full:
            values += [_money(row.p25, row.currency), _money(row.p75, row.currency)]
        values += [_money(row.minimum, row.currency), _money(row.maximum, row.currency)]
        table.add_row(*values)
    rich.print(table)


@app.command()
def trend(item: ItemOption = None, days: DaysOption = 30, unmatched: AllOption = False) -> None:
    """Per-day mean price, to see whether an item is getting cheaper."""
    rows = price_history.daily_series(item_name=item, days=days, include_unmatched=unmatched)
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
    unmatched: AllOption = False,
) -> None:
    """The most recent individual listings, cheapest first within a run."""
    rows = price_history.observations(
        item_name=item, days=days, limit=limit, include_unmatched=unmatched
    )
    if not rows:
        rich.print("[yellow]No price observations recorded yet.[/yellow]")
        raise typer.Exit(1)

    table = Table(title="Recent observations")
    for column in ("Seen", "Price"):
        table.add_column(column, no_wrap=True)
    for column in ("Item", "Phrase", "Title", "Location"):
        table.add_column(column, overflow="ellipsis")
    if unmatched:
        table.add_column("Match", no_wrap=True)
    for row in rows:
        table.add_row(
            row["observed_at"].replace("T", " ")[5:16],
            row["price_raw"] or "-",
            row["item_name"],
            row["search_phrase"],
            (row["title"] or "")[:50],
            (row["location"] or "")[:20],
            *(["yes" if row["matched"] else "[red]no[/red]"] if unmatched else []),
        )
    rich.print(table)


@app.command()
def show(
    item: ItemOption = None,
    days: DaysOption = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    unmatched: AllOption = False,
) -> None:
    """Full title and description per listing, to identify the actual model."""
    rows = price_history.observations(
        item_name=item, days=days, limit=limit, include_unmatched=unmatched
    )
    if not rows:
        rich.print("[yellow]No price observations recorded yet.[/yellow]")
        raise typer.Exit(1)

    for row in rows:
        flag = "" if row["matched"] else " [red](unmatched)[/red]"
        rich.print(f"\n[bold]{row['price_raw'] or '-'}[/bold]  {row['title']}{flag}")
        rich.print(f"  [dim]{row['location'] or '?'} · {row['observed_at'].replace('T', ' ')}"
                   f" · via \"{row['search_phrase']}\"[/dim]")
        description = (row["description"] or "").strip()
        if description:
            rich.print(f"  {description[:400]}")
        else:
            rich.print("  [dim]no description recorded (details were never fetched)[/dim]")
        rich.print(f"  [dim]{row['post_url']}[/dim]")


@app.command()
def deals(
    sell: Annotated[
        int, typer.Option("--sell", help="What YOU can actually sell the device for.")
    ],
    model: Annotated[
        str, typer.Option("--model", "-m", help="Model label to hunt for.")
    ] = "iPad Air 5 (M1)",
    pencil: Annotated[
        int,
        typer.Option("--pencil", help="What you can sell a bundled Apple Pencil 2 for."),
    ] = 0,
    haggle: Annotated[
        float,
        typer.Option("--haggle", help="Percent you can typically negotiate off the asking price."),
    ] = 0.0,
    assume_pencil: Annotated[
        bool,
        typer.Option(
            "--assume-pencil",
            help="Credit the pencil value even when the listing does not state its"
            " generation. Off by default: a 1st gen is worth much less.",
        ),
    ] = False,
    include_contacted: Annotated[
        bool,
        typer.Option("--include-contacted", help="Also show sellers already approached."),
    ] = False,
    messages: Annotated[
        bool, typer.Option("--messages", help="Print ready-to-send offers instead of a table.")
    ] = False,
    item: ItemOption = None,
    days: DaysOption = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 200,
) -> None:
    """Listings you could buy and resell for a profit.

    Compares each listing's asking price against what you can actually sell for,
    not against other asking prices -- a market can ask far more than it gets,
    and comparing asks to asks invents margins that do not exist.
    """
    rows = price_history.observations(
        item_name=item, days=days, limit=limit, model=model, latest_only=True
    )
    rows = [r for r in rows if r["price"] is not None]
    if not rows:
        rich.print(f"[yellow]No listings recorded for model {model!r}.[/yellow]")
        raise typer.Exit(1)

    seen_listings, seen_sellers = price_history.contacted()
    factor = 1 - (haggle / 100.0)
    found, skipped = [], 0
    for row in rows:
        generation = classify.pencil(row["title"], row["description"])
        # only a stated 2nd generation earns the quoted resale
        credit = pencil if generation == 2 or (generation == 0 and assume_pencil) else 0
        resale = sell + credit
        offer = round(min(row["price"] * factor, resale) / 10000) * 10000
        already = row["listing_id"] in seen_listings or (
            (row["seller"] or "").strip().lower() in seen_sellers and row["seller"]
        )
        if already and not include_contacted:
            skipped += 1
            continue
        found.append(
            {
                "id": row["listing_id"],
                "ask": row["price"],
                "offer": offer,
                "profit": resale - offer,
                "at_ask": resale - row["price"],
                "gen": generation,
                "seller": row["seller"] or "?",
                "title": row["title"],
                "url": (row["post_url"] or "").split("?")[0],
                "already": already,
            }
        )

    viable = sorted((f for f in found if f["profit"] > 0), key=lambda x: -x["profit"])

    if messages:
        if not viable:
            rich.print("[yellow]Nothing to offer on.[/yellow]")
            raise typer.Exit(1)
        for f in viable:
            note = {2: "Pencil 2", 0: "pencil, generation unstated", 1: "Pencil 1"}.get(f["gen"], "")
            rich.print(
                f"\n[bold]{f['seller']}[/bold] — ask {_clp(f['ask'])}, profit"
                f" [green]{_clp(f['profit'])}[/green]{f' · {note}' if note else ''}"
            )
            rich.print(f"  [dim]{f['url']}[/dim]")
            rich.print(f'  Hola! aun disponible? Aceptarias {_clp(f["offer"])}')
        rich.print(
            f"\n[dim]After sending, record each one:[/dim]\n"
            f"  aimm-prices contacted --id <listing_id> --offer <amount>"
        )
        return

    table = Table(
        title=f"{model} — sell at {sell:,}"
        + (f" + pencil {pencil:,}" if pencil else "")
        + (f", haggling up to {haggle:g}%" if haggle else "")
    )
    for column in ("Ask", "Offer", "P/L ask", "P/L hagg"):
        table.add_column(column, justify="right", no_wrap=True)
    table.add_column("Pencil", no_wrap=True)
    table.add_column("Listing", overflow="ellipsis")
    for f in (viable or sorted(found, key=lambda x: -x["profit"]))[:20]:
        style = "green" if f["profit"] > 0 else "red"
        table.add_row(
            f"{f['ask']:,.0f}",
            f"{f['offer']:,.0f}",
            f"[{'green' if f['at_ask'] > 0 else 'red'}]{f['at_ask']:+,.0f}[/]",
            f"[{style}]{f['profit']:+,.0f}[/]",
            {2: "P2", 1: "[red]P1[/red]", 0: "[yellow]p?[/yellow]"}.get(f["gen"], ""),
            f["title"][:24],
        )
    rich.print(table)
    if skipped:
        rich.print(f"[dim]{skipped} listing(s) hidden: seller already contacted."
                   " Use --include-contacted to show them.[/dim]")
    if viable:
        rich.print(f"[green]{len(viable)} worth offering on.[/green]"
                   " Run with --messages for ready-to-send text.")
    else:
        rich.print("[red]Nothing clears your resale price.[/red]")
    if any(f["gen"] == 0 for f in found) and not assume_pencil:
        rich.print("[yellow]p?[/yellow] = pencil mentioned, generation unstated; its value is"
                   " NOT counted. Use --assume-pencil to include it.")


@app.command("contacted")
def mark_contacted(
    listing_id: Annotated[str, typer.Option("--id", help="Facebook listing id.")],
    offer: Annotated[Optional[int], typer.Option("--offer", help="Amount offered.")] = None,
    message: Annotated[Optional[str], typer.Option("--message")] = None,
    seller: Annotated[Optional[str], typer.Option("--seller")] = None,
) -> None:
    """Record that an offer was sent, so it is not offered again."""
    price_history.record_contact(listing_id, seller=seller, offer=offer, message=message)
    rich.print(f"[green]Recorded contact for {listing_id}.[/green]")


@app.command("sent")
def list_contacts() -> None:
    """Offers already sent."""
    rows = price_history.contacts()
    if not rows:
        rich.print("[yellow]No offers recorded yet.[/yellow]")
        raise typer.Exit(1)
    table = Table(title="Offers sent")
    for column in ("When", "Seller", "Offer", "Listing"):
        table.add_column(column, overflow="ellipsis")
    for row in rows:
        table.add_row(
            (row["contacted_at"] or "").replace("T", " ")[5:16],
            row["seller"] or "?",
            _clp(row["offer"]) if row["offer"] else "-",
            (row["title"] or row["listing_id"])[:30],
        )
    rich.print(table)


@app.command()
def export(
    item: ItemOption = None,
    days: DaysOption = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 100000,
    unmatched: AllOption = True,
) -> None:
    """Write every observation to stdout as CSV."""
    rows = price_history.observations(
        item_name=item, days=days, limit=limit, include_unmatched=unmatched
    )
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
