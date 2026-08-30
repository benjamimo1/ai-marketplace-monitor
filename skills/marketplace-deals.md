---
name: marketplace-deals
description: Daily Facebook Marketplace resale check for Santiago, Chile — refresh price data, find listings worth buying below resale, and send negotiated offers. Use when the user asks to check marketplace deals, run the daily iPad/marketplace check, look for arbitrage on Marketplace listings, or follow up on offers sent to sellers.
---

# Marketplace deals — daily run

Finds listings that can be bought below what the user can resell them for, and
sends offers. The analysis is scripted; the judgment and the sending are not.

Repo: `~/Documents/ai-marketplace-monitor` (fork, branch `feat/price-history`)
Config: `~/.ai-marketplace-monitor/ipad-air-5.toml`
Data: `~/.ai-marketplace-monitor/price_history.db`
All commands run from the repo with `.venv/bin/`.

## The user's numbers

| | |
|---|---|
| Sells an **iPad Air 5 (M1)** for | **$300.000 CLP** |
| Sells an **Apple Pencil 2** for | **$60.000 CLP** |
| Can usually haggle | **up to 20%** off asking |
| Market | Santiago, Chile (`santiagocl`), prices in CLP |

Their resale sits ~25% below the median *asking* price. That gap is the whole
point: only a seller pricing well below the pack is worth approaching. Never
compute a margin by comparing one asking price to another — it invents profit
that does not exist. Ask the user for the resale figure of any model they have
not quoted rather than extrapolating.

## 1. Refresh the data

```bash
cd ~/Documents/ai-marketplace-monitor && FACEBOOK_USERNAME="benja_mimo@hotmail.com" FACEBOOK_PASSWORD="$(security find-internet-password -s facebook.com -a benja_mimo@hotmail.com -w)" .venv/bin/aimm -r ~/.ai-marketplace-monitor/ipad-air-5.toml -v
```

Run it in the background and watch `~/.ai-marketplace-monitor/ai-marketplace-monitor.log`,
not the command's own stdout. Kill stray instances first (`pkill -f "aimm -r"`) —
two monitors at once double the request rate to Facebook.

A saved session lives at `~/.ai-marketplace-monitor/facebook_session.json`, so a
login prompt should be rare. If Facebook does challenge it, the user must clear
the CAPTCHA by hand; never type their password.

Skip this step if the data is only a few hours old — the monitor also runs on
its own 2h schedule.

## 2. Find the deals

```bash
cd ~/Documents/ai-marketplace-monitor && .venv/bin/aimm-prices deals --sell 300000 --pencil 60000 --haggle 20
```

Then `--messages` for ready-to-send text. Useful variants:

- `-m "iPad Air M2 (6th)"` — a different model (ask the user for its resale price first)
- `--include-contacted` — show sellers already approached
- `summary --by-model`, `trend`, `show -n 10`, `sent`

Sellers already contacted are hidden automatically. Listings whose pencil
generation is unstated do not get the pencil credit; `--assume-pencil` overrides
that, but say so explicitly if you use it.

## 3. Check each listing before offering

The database is a snapshot. For every candidate, open it in the user's Chrome
(`mcp__claude-in-chrome__*`) and confirm:

- **Still live, still at that price.** Prices move; several were already marked down.
- **The description.** It routinely contains value the title omits — one $330.000
  listing titled "iPad Air 5ta generación 64gb" included an Apple Pencil 2 in its
  description, worth $60.000. Read it, don't trust the title.
- **Whether a thread already exists.** A button reading **"Enviar otro mensaje"**
  (rather than "Enviar mensaje") means the seller has been contacted before.

**Skip any seller the user has already spoken to.** Opening a thread cold when a
negotiation is live reads badly and weakens their position. One seller may hold
several listings — match on the seller's name, not just the listing.

## 4. Send

Confirm the list with the user before sending anything. Message template:

```
Hola! aun disponible? Aceptarias $260.000
```

Prices are CLP: a dot for thousands, no decimals (`$260.000`). Round offers to
the nearest 10.000.

Sending mechanics, learned the hard way:

1. Click the message box directly by coordinate — refs go stale and the box
   sits at roughly (1035, 923), shifting a few pixels between listings.
2. `cmd+a`, then type the message. **`form_input` silently fails** — it sets the
   value but React never sees the change.
3. Press **Return**. The "Enviar" button often does nothing.
4. Verify: reload the listing and confirm the button now reads **"Enviar otro
   mensaje"**. That flip is the only reliable proof it sent.

Record every send so it is never repeated:

```bash
cd ~/Documents/ai-marketplace-monitor && .venv/bin/aimm-prices contacted --id <listing_id> --offer <amount>
```

Also record sellers in an active conversation who were deliberately not offered
to, so they stay excluded.

## 5. Report

Give the user: new listings since yesterday, anything clearing their threshold,
replies waiting in Messenger, and what was skipped and why. Keep it short.

## Gotchas

- `search_city` must be **`santiagocl`**, not `santiago` — Facebook silently
  falls back to a US location and returns pickup trucks.
- Never set `min_price`/`max_price`: they go into the search URL and are read in
  the *marketplace's* currency, not CLP.
- `language = "es"` is required, or every listing-detail fetch fails — the parser
  looks for "Condition" on a page that says "Estado".
- `keywords` never filters at search time (the description is not loaded yet);
  `classify.py` separates models after the fact instead.
- A profit under ~$20.000 is not worth a trip across Santiago.
