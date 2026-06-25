"""
update_kufar.py
Refresh pass for kufar rows in cars.db: keep active ads current and retire the
ones that vanished from the listing. This is the lightweight counterpart to
kufar.py (the collector). It is meant to run often (e.g. daily).

How it differs from kufar.py
----------------------------
kufar.py discovers NEW ads and parses their full card (~43k card fetches on a
full run). This updater never touches a card: the kufar *listing* already
carries the current price for every active ad, so a single listing walk is
enough to (a) refresh prices and (b) learn which ads are still up. That makes
this pass cheap — a few hundred listing pages, minutes instead of hours.

What it does
------------
1. Walk the whole kufar listing (reusing kufar.walk_listing).
2. For every ad already in cars.db: update price + last_seen_at, set is_active=1,
   and append a car_prices point if the price changed. Ads not yet in the DB are
   left for the collector (discovering them needs the card) — they are simply
   skipped here.
3. Sweep: any active row NOT touched this run (last_seen_at < the run's start)
   is no longer in the listing, so it is marked is_active=0 and gets a closing
   car_prices row (is_active=0). The API filters is_active=1, so swept ads drop
   out of search while their price history is preserved.

Safety
------
The sweep only runs after a *complete* listing walk. If the walk is interrupted
(Ctrl+C) or aborts, the sweep is skipped — a partial pass would wrongly retire
everything we did not reach. As an extra guard, if the number of ads about to be
retired exceeds SWEEP_MAX_FRACTION of the active set (a sign the listing was
truncated mid-walk, not that a fifth of the market sold overnight) the sweep is
skipped unless --force-sweep is given.

Usage
-----
    python -m sources.update_kufar                 # refresh + sweep
    python -m sources.update_kufar --no-sweep      # refresh only, never retire
    python -m sources.update_kufar --max 500       # smoke test (disables sweep)
    python -m sources.update_kufar --force-sweep   # apply sweep past the guard
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a script (python sources/update_kufar.py) as well as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources.db import init_db  # noqa: E402
from sources.kufar import (  # noqa: E402
    DELAY_BETWEEN_DETAILS,
    Progress,
    RateLimiter,
    now_iso,
    walk_listing,
)

# If the sweep wants to retire more than this fraction of the active set, treat
# it as a truncated listing walk rather than a real mass de-listing and bail out
# (override with --force-sweep).
SWEEP_MAX_FRACTION = 0.20


def _listing_money(v) -> int | None:
    """kufar listing prices are strings in hundredths of a unit
    (e.g. '2533668' -> 25336.68 BYN -> 25337). The card endpoint already returns
    whole units, so we divide by 100 here to match what kufar.py stores."""
    if v in (None, ""):
        return None
    try:
        return int(round(int(str(v)) / 100))
    except (TypeError, ValueError):
        return None


def _refresh_one(con, ad_id: int, rec: dict) -> str:
    """Update price + last_seen for one existing kufar row from its listing
    record. No card fetch. Returns 'repriced' | 'seen' | 'unknown'."""
    row = con.execute(
        "SELECT price_byn, price_usd FROM cars WHERE id=?", (ad_id,)
    ).fetchone()
    if row is None:
        return "unknown"  # not collected yet — discovering it is kufar.py's job

    ts = now_iso()
    price_byn = _listing_money(rec.get("price_byn"))
    price_usd = _listing_money(rec.get("price_usd"))

    con.execute(
        "UPDATE cars SET price_byn=?, price_usd=?, last_seen_at=?, is_active=1 "
        "WHERE id=?",
        (price_byn, price_usd, ts, ad_id),
    )

    if row[0] != price_byn or row[1] != price_usd:
        con.execute(
            "INSERT INTO car_prices (car_id, checked_at, price_byn, price_usd, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (ad_id, ts, price_byn, price_usd),
        )
        return "repriced"
    return "seen"


def _sweep(con, cutoff: str, force: bool) -> int:
    """Mark is_active=0 for active rows not refreshed this run (last_seen_at <
    cutoff) and append a closing car_prices point. cars.db holds only kufar rows,
    so no source filter is needed. Returns how many were retired."""
    missing = [
        r[0] for r in con.execute(
            "SELECT id FROM cars WHERE is_active=1 AND last_seen_at < ?", (cutoff,)
        )
    ]
    if not missing:
        print("[sweep] nothing to retire")
        return 0

    active_total = con.execute(
        "SELECT COUNT(*) FROM cars WHERE is_active=1"
    ).fetchone()[0] or 1
    frac = len(missing) / active_total
    if frac > SWEEP_MAX_FRACTION and not force:
        print(
            f"[sweep] ABORTED: {len(missing)} of {active_total} active "
            f"({frac:.0%}) would be retired — that looks like a truncated "
            f"listing walk, not a real mass de-listing. Re-run, or pass "
            f"--force-sweep if this is genuinely correct.",
            file=sys.stderr,
        )
        return 0

    ts = now_iso()
    print(f"[sweep] {len(missing)} ads gone from the listing -> is_active=0")
    con.executemany(
        "UPDATE cars SET is_active=0, last_seen_at=? WHERE id=?",
        [(ts, m) for m in missing],
    )
    con.executemany(
        "INSERT INTO car_prices (car_id, checked_at, price_byn, price_usd, is_active) "
        "SELECT ?, ?, price_byn, price_usd, 0 FROM cars WHERE id=?",
        [(m, ts, m) for m in missing],
    )
    con.commit()
    return len(missing)


def run(max_ids: int | None, do_sweep: bool, force: bool) -> None:
    con = init_db()  # sources/cars.db
    rl = RateLimiter(DELAY_BETWEEN_DETAILS)
    progress = Progress()
    cutoff = now_iso()  # captured BEFORE the walk; refreshed rows get a later ts

    n_repriced = n_seen = n_unknown = 0
    complete = False
    try:
        # We only hit the listing here (no per-ad card fetch), so there is no
        # need to sleep between ads — walk_listing already paces the page calls.
        for ad_id, rec in walk_listing(rl, max_ids=max_ids, progress=progress):
            progress.tick()
            status = _refresh_one(con, ad_id, rec)
            if status == "repriced":
                n_repriced += 1
            elif status == "seen":
                n_seen += 1
            else:
                n_unknown += 1
            con.commit()
        complete = True
    except KeyboardInterrupt:
        print("\n[interrupted] partial listing — sweep disabled", file=sys.stderr)
    finally:
        con.commit()

    swept = 0
    if do_sweep and complete:
        swept = _sweep(con, cutoff, force)
    elif do_sweep and not complete:
        print("[sweep] skipped: listing pass incomplete")
    con.close()

    print(
        f"\nDone. repriced {n_repriced}, unchanged {n_seen}, "
        f"new (left for collector) {n_unknown}, retired {swept}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="kufar refresh + inactive sweep")
    ap.add_argument("--max", type=int, default=None,
                    help="cap listing ids processed (smoke test; disables sweep)")
    ap.add_argument("--no-sweep", action="store_true",
                    help="refresh prices only, never retire ads")
    ap.add_argument("--force-sweep", action="store_true",
                    help="apply the sweep even past the safety guard")
    args = ap.parse_args()

    # A capped run does not see the whole listing, so its sweep would be wrong.
    do_sweep = not args.no_sweep and args.max is None
    run(max_ids=args.max, do_sweep=do_sweep, force=args.force_sweep)


if __name__ == "__main__":
    main()