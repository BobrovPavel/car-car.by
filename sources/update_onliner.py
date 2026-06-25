"""
update_onliner.py
Refresh pass for onliner rows in cars_onliner.db: keep active ads current and
retire the ones that vanished from the listing. Counterpart to onliner.py.

How it works
------------
Onliner's feed is one flat, recency-sorted list, so a refresh is a full page
walk 1..last. Each advert is UPSERTed via onliner.save_row, which updates the
price, appends a car_prices point when the price changed, bumps last_seen_at,
and flips is_active back to 1 for an ad that had been retired and reappeared.
Listing rows do not carry description/VIN, and save_row(card=False) is careful
NOT to null those out on conflict, so a refresh never wipes data the --cards
backfill collected.

After a complete walk, any active row NOT touched this run (last_seen_at < the
run's start) is no longer listed, so it is marked is_active=0 with a closing
car_prices point (is_active=0). The API filters is_active=1, so retired ads drop
out of search while their price history is kept.

This pass does not move the collector's resume pointer (the onliner_page_done
progress key); it always re-walks from page 1.

Safety
------
The sweep only runs after a *complete* walk to the last page. An interruption or
a --max-capped run leaves coverage partial and disables the sweep. As an extra
guard, a sweep larger than SWEEP_MAX_FRACTION of the active set is treated as a
truncated walk and skipped unless --force-sweep is given.

Usage
-----
    python -m sources.update_onliner               # refresh + sweep
    python -m sources.update_onliner --no-sweep    # refresh only, never retire
    python -m sources.update_onliner --max 500     # smoke test (disables sweep)
    python -m sources.update_onliner --force-sweep # apply sweep past the guard
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources.db import init_db  # noqa: E402
from sources.onliner import (  # noqa: E402
    OnlinerClient,
    RateLimiter,
    ensure_source_column,
    map_advert,
    now_iso,
    save_row,
)

DEFAULT_DB = Path(__file__).resolve().parent / "cars_onliner.db"
SOURCE = "onliner"

# A sweep retiring more than this fraction of the active set is treated as a
# truncated walk rather than a real mass de-listing (override --force-sweep).
SWEEP_MAX_FRACTION = 0.20


def _sweep(con, cutoff: str, force: bool) -> int:
    """Mark is_active=0 for onliner rows not refreshed this run and append a
    closing car_prices point. Returns how many were retired."""
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
            f"({frac:.0%}) would be retired — looks like a truncated walk, not a "
            f"real mass de-listing. Re-run, or pass --force-sweep.",
            file=sys.stderr,
        )
        return 0

    ts = now_iso()
    print(f"[sweep] {len(missing)} ads gone from onliner -> is_active=0")
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


def run(db_path: Path, max_ads: int | None, delay: float,
        do_sweep: bool, force: bool) -> None:
    con = init_db(db_path)
    ensure_source_column(con)
    client = OnlinerClient(RateLimiter(base=delay))

    cutoff = now_iso()  # before the walk; refreshed rows get a later last_seen
    first = client.search(1)
    last_page = (first.get("page") or {}).get("last")
    total = first.get("total")
    print(f"onliner refresh -> {db_path}  "
          f"(total ads: {total if total is not None else '?'}, "
          f"pages: {last_page if last_page is not None else '?'})")

    saved = 0
    page = 1
    data = first
    complete = False
    started = time.time()
    try:
        while True:
            adverts = data.get("adverts", [])
            if not adverts:
                complete = True  # ran off the end of the feed
                break
            for ad in adverts:
                save_row(con, map_advert(ad))
                saved += 1
                if max_ads is not None and saved >= max_ads:
                    con.commit()
                    print(f"  reached --max ({max_ads}) at page {page}")
                    raise StopIteration
            con.commit()  # checkpoint after every page
            if page % 10 == 0:
                elapsed = time.time() - started
                rate = saved / elapsed if elapsed else 0
                print(f"  -- page {page}"
                      f"{'/' + str(last_page) if last_page else ''}, "
                      f"{saved} ads, {elapsed:.0f}s ({rate:.1f}/s)")
            if last_page is not None and page >= last_page:
                complete = True
                break
            page += 1
            data = client.search(page)
    except StopIteration:
        pass  # --max hit: partial pass, complete stays False
    except KeyboardInterrupt:
        print(f"\n[interrupted] at page {page}", file=sys.stderr)
    finally:
        con.commit()

    swept = 0
    if do_sweep and complete:
        swept = _sweep(con, cutoff, force)
    elif do_sweep:
        print("[sweep] skipped: listing walk incomplete")
    con.close()

    print(f"\nDone. refreshed {saved} ads in {time.time() - started:.0f}s, "
          f"retired {swept}")


def main() -> None:
    ap = argparse.ArgumentParser(description="onliner refresh + inactive sweep")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="output SQLite path")
    ap.add_argument("--max", type=int, default=None,
                    help="stop after refreshing this many ads (disables sweep)")
    ap.add_argument("--no-sweep", action="store_true",
                    help="refresh only, never retire ads")
    ap.add_argument("--force-sweep", action="store_true",
                    help="apply the sweep even past the safety guard")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="base seconds between requests (raise if you hit 429)")
    args = ap.parse_args()

    do_sweep = not args.no_sweep and args.max is None
    run(Path(args.db), args.max, args.delay, do_sweep, args.force_sweep)


if __name__ == "__main__":
    main()