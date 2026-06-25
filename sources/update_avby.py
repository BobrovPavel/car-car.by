"""
update_avby.py
Refresh pass for av.by rows in cars_avby.db: keep active ads current and retire
the ones that vanished from the listing. Counterpart to avby.py (the collector).

How it works
------------
av.by exposes no cheap "is this id still up?" probe — the brand `apply` listing
is the only reachable feed, and it already carries full fresh data per offer.
So a refresh is simply a full re-crawl: re-run every brand through avby.crawl_brand,
which UPSERTs each offer via save_row. save_row updates the price, appends a
car_prices point when the price changed, bumps last_seen_at, and flips is_active
back to 1 for any ad that had been retired and reappeared.

After a complete crawl, any active row NOT touched this run (last_seen_at < the
run's start) is no longer offered, so it is marked is_active=0 with a closing
car_prices point (is_active=0). The API filters is_active=1, so retired ads drop
out of search while their price history is kept.

Because this re-walks the whole av.by catalogue it costs the same as a full
collect (brand-by-brand, can take a while); schedule it accordingly.

Safety
------
The sweep only runs after a *complete* crawl of *all* brands. A single-brand run
(--brand), an interruption, or any brand that failed mid-run leaves coverage
incomplete and disables the sweep — otherwise untouched-but-still-active ads
from the un-crawled brands would be wrongly retired. As an extra guard, a sweep
larger than SWEEP_MAX_FRACTION of the active set is treated as a truncated crawl
and skipped unless --force-sweep is given.

Usage
-----
    python -m sources.update_avby                  # refresh all brands + sweep
    python -m sources.update_avby --no-sweep       # refresh only, never retire
    python -m sources.update_avby --brand 6        # one brand (disables sweep)
    python -m sources.update_avby --force-sweep    # apply sweep past the guard
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources.db import init_db  # noqa: E402
from sources.avby import (  # noqa: E402
    BRANDS,
    AvClient,
    RateLimiter,
    crawl_brand,
    ensure_source_column,
    now_iso,
)

DEFAULT_DB = Path(__file__).resolve().parent / "cars_avby.db"
SOURCE = "av"

# A sweep retiring more than this fraction of the active set is treated as a
# truncated crawl rather than a real mass de-listing (override --force-sweep).
SWEEP_MAX_FRACTION = 0.20


def _sweep(con, cutoff: str, force: bool) -> int:
    """Mark is_active=0 for av rows not refreshed this run and append a closing
    car_prices point. Returns how many were retired."""
    missing = [
        r[0] for r in con.execute(
            "SELECT id FROM cars WHERE source=? AND is_active=1 AND last_seen_at < ?",
            (SOURCE, cutoff),
        )
    ]
    if not missing:
        print("[sweep] nothing to retire")
        return 0

    active_total = con.execute(
        "SELECT COUNT(*) FROM cars WHERE source=? AND is_active=1", (SOURCE,)
    ).fetchone()[0] or 1
    frac = len(missing) / active_total
    if frac > SWEEP_MAX_FRACTION and not force:
        print(
            f"[sweep] ABORTED: {len(missing)} of {active_total} active "
            f"({frac:.0%}) would be retired — looks like a truncated crawl, not "
            f"a real mass de-listing. Re-run, or pass --force-sweep.",
            file=sys.stderr,
        )
        return 0

    ts = now_iso()
    print(f"[sweep] {len(missing)} ads gone from av.by -> is_active=0")
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


def run(db_path: Path, only_brand: int | None, delay: float,
        do_sweep: bool, force: bool) -> None:
    con = init_db(db_path)
    ensure_source_column(con)
    client = AvClient(RateLimiter(base=delay))

    if only_brand is not None:
        brands = {only_brand: BRANDS.get(only_brand, str(only_brand))}
    else:
        brands = BRANDS

    cutoff = now_iso()  # before the crawl; refreshed rows get a later last_seen
    total = 0
    failed: list[tuple[int, str]] = []
    interrupted = False
    started = time.time()
    print(f"av.by refresh -> {db_path}  ({len(brands)} brands)")

    try:
        for i, (bid, label) in enumerate(brands.items(), 1):
            try:
                total += crawl_brand(client, con, bid, label)
            except Exception as exc:
                print(f"  ! {label} failed: {exc}", file=sys.stderr)
                failed.append((bid, label))
            con.commit()  # checkpoint after every brand
            if i % 10 == 0:
                print(f"  -- {i}/{len(brands)} brands, {total} ads, "
                      f"{time.time() - started:.0f}s")
    except KeyboardInterrupt:
        interrupted = True
        print("\n[interrupted]", file=sys.stderr)
    finally:
        con.commit()

    # Sweep needs full, clean coverage of every brand.
    complete = (only_brand is None) and not interrupted and not failed
    swept = 0
    if do_sweep and complete:
        swept = _sweep(con, cutoff, force)
    elif do_sweep:
        reason = ("single brand" if only_brand is not None
                  else "interrupted" if interrupted
                  else f"{len(failed)} brand(s) failed")
        print(f"[sweep] skipped ({reason}): incomplete coverage")
    con.close()

    print(f"\nDone. refreshed {total} ads in {time.time() - started:.0f}s, "
          f"retired {swept}")
    if failed:
        names = ", ".join(label for _, label in failed)
        print(f"{len(failed)} brand(s) did not finish: {names}  "
              f"(re-run before sweeping)")


def main() -> None:
    ap = argparse.ArgumentParser(description="av.by refresh + inactive sweep")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="output SQLite path")
    ap.add_argument("--brand", type=int, default=None,
                    help="single brand id (disables sweep)")
    ap.add_argument("--no-sweep", action="store_true",
                    help="refresh only, never retire ads")
    ap.add_argument("--force-sweep", action="store_true",
                    help="apply the sweep even past the safety guard")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="base seconds between requests (raise if you hit 429)")
    args = ap.parse_args()

    do_sweep = not args.no_sweep and args.brand is None
    run(Path(args.db), args.brand, args.delay, do_sweep, args.force_sweep)


if __name__ == "__main__":
    main()