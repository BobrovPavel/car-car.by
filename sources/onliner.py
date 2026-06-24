"""
onliner.py
Onliner.by (ab.onliner.by) used-car listing collector for car-car.by.

Pulls every advert from Onliner's automarket via its public JSON listing
endpoint and writes it into a SQLite database that uses the SAME schema as the
kufar / av.by collectors (db.py), so all sources can be merged later. Rows are
tagged source='onliner'.

Why this approach
-----------------
Onliner's automarket is an SPA backed by a clean JSON "sdapi" host. The single
ad page (/vehicles/{id}) is blocked for automated clients, but the *listing*
endpoint is open and already carries almost everything we need:

    GET https://ab.onliner.by/sdapi/ab.api/search/vehicles?page=N&extended=true&limit=50

Response shape (top level):
    {
      "adverts": [ {advert}, ... ],
      "total": <int>,
      "page": {"current": N, "last": L, "limit": 50, "items": <int>}
    }

Each advert already contains: manufacturer/model/generation (name + slug),
specs (year, body_type, color, engine{type,capacity,power}, transmission,
drivetrain, odometer, has_vin), an `equipment` tree grouped by category,
seller (type, name, unp), price in BYN/USD/EUR, deal_terms (exchange, customs),
location (region/city), an `images` array, `created_at`, and an `html_url`.

Unlike kufar there is no separate card fetch for the basics. The two fields the
listing does NOT expose are the free-text description and the full (unmasked)
VIN — both are card-only, same tradeoff as av.by. We leave them NULL; a later
per-card pass could backfill them if ever needed.

Crawl strategy
--------------
The feed is a single flat list sorted by recency, so the crawl is just a page
walk 1..last (no brand-by-brand segmentation like av.by needs). Progress is the
last fully-saved page, checkpointed after each page, so Ctrl+C is safe and a
re-run resumes where it stopped. A re-run from page 1 (the default once a full
pass is done) re-UPSERTs the freshest ads and appends price-history points.

Usage
-----
    pip install curl_cffi
    python onliner.py                 # full crawl -> cars_onliner.db
    python onliner.py --test          # only the first 2 pages, smoke test
    python onliner.py --max 500       # stop after saving 500 adverts
    python onliner.py --restart       # ignore saved progress, start at page 1
    python onliner.py --db other.db   # custom output path
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# curl_cffi is imported lazily — only when we actually open an HTTP session.
# This keeps the module (and its pure mapping helpers map_advert/save_row)
# importable without the dependency, e.g. under pytest or a stripped venv.
cffi = None


def _load_cffi():
    """Import curl_cffi on first network use; fail loudly only then."""
    global cffi
    if cffi is None:
        try:
            from curl_cffi import requests as _cffi
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "curl_cffi is required for crawling:  pip install curl_cffi"
            ) from exc
        cffi = _cffi
    return cffi


# db.py lives next to this script under sources/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources.db import init_db  # noqa: E402

SEARCH_URL = "https://ab.onliner.by/sdapi/ab.api/search/vehicles"
VEHICLE_URL = "https://ab.onliner.by/sdapi/ab.api/vehicles"  # + /{id} -> one card
WARM_URL = "https://ab.onliner.by/"
PAGE_LIMIT = 50  # adverts per page (matches the SPA's own request)

DEFAULT_DB = Path(__file__).resolve().parent / "cars_onliner.db"

# Optional global cap on how many adverts to save (for quick tests). None = no cap.
MAX_ADS: int | None = None

_SAVED = [0]  # mutable counter shared across the crawl


class _ReachedMax(Exception):
    """Raised internally once MAX_ADS adverts have been saved."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
#  HTTP layer  (mirrors avby.py: adaptive delay, cookie warm-up, 429 backoff)
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Adaptive delay with jitter; backs off hard and recovers slowly on 429."""

    def __init__(self, base: float = 1.0):
        self.base = base
        self.delay = base

    def wait(self) -> None:
        time.sleep(self.delay + random.uniform(0.0, 0.4))

    def ok(self) -> None:
        if self.delay > self.base:
            self.delay = max(self.base, self.delay * 0.97)

    def throttled(self) -> None:
        self.delay = min(max(self.delay * 2, 5.0), 60.0)


class OnlinerClient:
    def __init__(self, limiter: RateLimiter):
        self.rl = limiter
        self._cffi = _load_cffi()  # imports curl_cffi here, not at module import
        self.headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://ab.onliner.by",
            "Referer": "https://ab.onliner.by/",
            "X-Requested-With": "XMLHttpRequest",
        }
        self.req_count = 0
        self._consecutive_429 = 0
        self._warm()

    def _warm(self) -> None:
        """Fresh cffi session + warm onliner cookies."""
        self.s = self._cffi.Session(impersonate="chrome")
        try:
            self.s.get(WARM_URL, timeout=20)
        except Exception:
            pass

    def _rewarm(self, reason: str) -> None:
        print(f"  ~ refreshing session ({reason})", file=sys.stderr)
        self._warm()
        time.sleep(2.0)

    def _get(self, url: str, params: dict) -> dict:
        backoff = 30.0
        for attempt in range(12):
            self.rl.wait()
            try:
                r = self.s.get(url, params=params, headers=self.headers, timeout=30)
            except Exception as exc:  # network hiccup -> wait and retry
                print(f"  ! network error ({exc}); retry", file=sys.stderr)
                self.rl.throttled()
                time.sleep(backoff)
                backoff = min(backoff * 1.6, 300.0)
                continue
            if r.status_code == 404:
                print(f"  ! HTTP 404: {url} не найдено. Пропускаем.", file=sys.stderr)
                raise FileNotFoundError(f"404 Not Found: {url}")
            if r.status_code == 429:
                self._consecutive_429 += 1
                retry_after = r.headers.get("Retry-After", "")
                sleep_s = float(retry_after) if retry_after.isdigit() else backoff
                print(f"  ! 429; waiting out the limit ({sleep_s:.0f}s)", file=sys.stderr)
                self.rl.throttled()
                time.sleep(sleep_s)
                backoff = min(backoff * 1.6, 300.0)
                # the limit may be session-bound: refresh cookies after a couple hits
                if self._consecutive_429 == 2:
                    self._rewarm("sustained 429")
                continue
            if r.status_code != 200:
                print(f"  ! HTTP {r.status_code} on {url}", file=sys.stderr)
                self.rl.throttled()
                time.sleep(backoff)
                backoff = min(backoff * 1.6, 300.0)
                continue
            # success
            self._consecutive_429 = 0
            self.rl.ok()
            self.req_count += 1
            # periodic cookie refresh keeps long crawls under the radar
            if self.req_count % 200 == 0:
                self._rewarm("periodic")
            return r.json()
        raise RuntimeError(f"giving up on GET {url}")

    def _get_soft(self, url: str, params: dict) -> dict | None:
        """One polite attempt; returns None on any non-200/exception.
        For endpoints where failure is normal (e.g. /vin may 403/404)."""
        self.rl.wait()
        try:
            r = self.s.get(url, params=params, headers=self.headers, timeout=30)
        except Exception:
            return None
        if r.status_code != 200:
            return None
        self.rl.ok()
        try:
            return r.json()
        except Exception:
            return None

    def search(self, page: int, limit: int = PAGE_LIMIT) -> dict:
        return self._get(SEARCH_URL, {"page": page, "extended": "true", "limit": limit})

    def vehicle(self, ad_id: int) -> dict:
        """Fetch one advert's full card (adds the free-text description; the VIN
        in the card is masked — use full_vin() for the unmasked value)."""
        return self._get(f"{VEHICLE_URL}/{ad_id}", {})

    def full_vin(self, ad_id: int) -> str | None:
        """Unmasked VIN via the dedicated endpoint /vehicles/{id}/vin
        -> {"vin": "WA15AAFY0N2006960"}. None if unavailable/forbidden."""
        data = self._get_soft(f"{VEHICLE_URL}/{ad_id}/vin", {})
        if isinstance(data, dict):
            v = data.get("vin")
            if v and "*" not in str(v):
                return str(v)
        return None


# --------------------------------------------------------------------------- #
#  Advert -> row mapping
# --------------------------------------------------------------------------- #
def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _money(v):
    """Onliner amounts are decimal strings like '39060.00'; we store whole units."""
    if v is None:
        return None
    try:
        return int(round(float(str(v).replace(" ", "").replace(",", "."))))
    except (TypeError, ValueError):
        return None


def _truthy(v) -> bool:
    if v is True:
        return True
    if isinstance(v, str):
        return v.strip().lower() not in ("", "no", "none", "false", "0")
    return False


# Equipment item-id -> has_* flag. Synonyms are grouped because Onliner's ids
# vary by option (e.g. front/rear heating). This map is best-effort: it is built
# from the ids observed in the listing plus likely variants, so an item whose id
# is not listed here is NOT lost — it still lands in features_json below. Flags
# that never match stay NULL (= "not stated"), consistent with the kufar/av rule.
# TODO: tighten once the full equipment vocabulary is captured from a few cards.
EQUIP_FLAG_IDS: dict[str, set[str]] = {
    "has_navigation":   {"navigation_system", "navigation"},
    "has_aux":          {"aux"},
    "has_climate":      {"conditioner", "climate_control", "climate", "air_conditioner"},
    "has_seatwarmer":   {"front_seat_heating", "rear_seat_heating", "seat_heating",
                         "seats_heating", "steering_wheel_heating"},
    "has_abs":          {"abs"},
    "has_alloy_wheels": {"light_alloy_wheels", "alloy_wheels"},
    "has_parktronic":   {"parktronic", "front_parktronic", "rear_parktronic",
                         "parking_sensors", "parking_assistant"},
    "has_sunroof":      {"sunroof", "hatch", "panoramic_roof", "panorama"},
    "has_alarm":        {"alarm", "signaling"},
    "has_cruise":       {"cruise_control", "adaptive_cruise_control"},
}

# Equipment ids that already have dedicated columns -> not duplicated in features_json.
_STRUCTURAL_EQUIP_IDS = {"number_of_seats", "compartment_decoration", "compartment_color"}


def _parse_equipment(equipment: list) -> tuple[dict, list[str], dict]:
    """
    Walk the grouped equipment tree once. Returns:
      flags    — {has_*: 1} for matched, truthy options (others left out -> NULL)
      features — human-readable feature list for features_json (modal grouping)
      extras   — {seats, interior_material, interior_color} pulled from interior
    """
    flags: dict[str, int] = {}
    features: list[str] = []
    extras: dict[str, str | None] = {
        "seats": None, "interior_material": None, "interior_color": None,
    }
    # invert EQUIP_FLAG_IDS to id -> flag for O(1) lookup
    id_to_flag = {iid: flag for flag, ids in EQUIP_FLAG_IDS.items() for iid in ids}

    for group in equipment or []:
        for item in group.get("items", []):
            iid = item.get("id")
            name = item.get("name")
            value = item.get("value")

            if iid == "number_of_seats":
                extras["seats"] = str(value) if value not in (None, "") else None
                continue
            if iid == "compartment_decoration":
                extras["interior_material"] = value or None
                continue
            if iid == "compartment_color":
                extras["interior_color"] = value or None
                continue

            flag = id_to_flag.get(iid)
            if flag and _truthy(value):
                flags[flag] = 1

            # feed the modal's feature list: booleans as the option name,
            # descriptive string values as "name: value".
            if value is True:
                if name:
                    features.append(name)
            elif isinstance(value, str) and value.strip():
                features.append(f"{name}: {value}" if name else value)

    return flags, features, extras


def map_advert(ad: dict) -> dict:
    """Flatten one Onliner advert into the cars-table column set."""
    import json as _json

    manufacturer = ad.get("manufacturer") or {}
    model = ad.get("model") or {}
    generation = ad.get("generation") or {}
    specs = ad.get("specs") or {}
    engine = specs.get("engine") or {}
    odo = specs.get("odometer") or {}
    seller = ad.get("seller") or {}
    author = ad.get("author") or {}
    price = ad.get("price") or {}
    converted = price.get("converted") or {}
    deal = ad.get("deal_terms") or {}
    loc = ad.get("location") or {}

    brand = manufacturer.get("name")
    model_name = model.get("name")
    gen_name = (generation.get("name") or "").strip() or None
    title = ad.get("title") or " ".join(
        x for x in [brand, model_name, gen_name] if x
    ) or None

    capacity = engine.get("capacity")
    try:
        capacity_l = f"{float(capacity):.1f} л"   # 2 -> "2.0 л", 1.8 -> "1.8 л"
    except (TypeError, ValueError):
        capacity_l = None                          # electric / unknown

    power = (engine.get("power") or {}).get("value") if isinstance(engine.get("power"), dict) else engine.get("power")

    flags, features, extras = _parse_equipment(ad.get("equipment", []))

    unp = seller.get("unp")
    seller_type = seller.get("type")  # individual | dealer | autohaus | ...
    is_company = 1 if (unp or seller_type in ("dealer", "autohaus", "company", "salon")) else 0

    row = {
        "id": ad.get("id"),
        "url": ad.get("html_url"),
        "title": title,
        "subject": title,
        "brand": brand,
        "model": model_name,
        "generation": gen_name,
        "year": _to_int(specs.get("year")),
        "mileage_km": _to_int(odo.get("value")),
        "engine_type": engine.get("type"),       # gasoline/diesel/electric/hybrid
        "capacity_l": capacity_l,
        "power_hp": _to_int(power),
        "gearbox": specs.get("transmission"),     # automatic/mechanical/...
        "auto_gearbox": None,
        "body_type": specs.get("body_type"),
        "drive": specs.get("drivetrain"),         # front/rear/full
        "seats": extras["seats"],
        "condition": specs.get("state"),          # owned/new
        "repair_needed": None,
        "color": specs.get("color"),
        "interior_color": extras["interior_color"],
        "interior_material": extras["interior_material"],
        # boolean option flags (only the matched/truthy ones; rest stay NULL)
        "has_climate": flags.get("has_climate"),
        "has_seatwarmer": flags.get("has_seatwarmer"),
        "has_abs": flags.get("has_abs"),
        "has_navigation": flags.get("has_navigation"),
        "has_alloy_wheels": flags.get("has_alloy_wheels"),
        "has_parktronic": flags.get("has_parktronic"),
        "has_sunroof": flags.get("has_sunroof"),
        "has_alarm": flags.get("has_alarm"),
        "has_cruise": flags.get("has_cruise"),
        "has_aux": flags.get("has_aux"),
        "lights_json": None,
        "features_json": _json.dumps(features, ensure_ascii=False) if features else None,
        "price_byn": _money((converted.get("BYN") or {}).get("amount") or
                            (price.get("amount") if price.get("currency") == "BYN" else None)),
        "price_usd": _money((converted.get("USD") or {}).get("amount") or
                            (price.get("amount") if price.get("currency") == "USD" else None)),
        "auction": None,
        "exchange": 1 if deal.get("exchange") else 0,
        "region": (loc.get("region") or {}).get("name"),
        "city": (loc.get("city") or {}).get("name"),
        "district": None,
        "account_id": str(author.get("id")) if author.get("id") else None,
        "seller": seller.get("name") or author.get("name"),
        "is_company": is_company,
        "vin": specs.get("vin"),                  # masked on onliner (e.g. WA15AAFY0********)
        "vin_verified": 1 if (specs.get("has_vin") or specs.get("vin")) else 0,
        "description": None,                      # description is card-only on onliner
        "video": None,
        "published_at": ad.get("created_at"),
        "_photos": [img["original"] for img in ad.get("images", []) if img.get("original")],
    }
    return row


def _clean_text(v):
    """Decode/clean a free-text field. r.json() already turns \\uXXXX into real
    characters; this just strips and, defensively, fixes a value that arrived as
    a literal escaped string (double-encoded)."""
    if not isinstance(v, str):
        return None
    if "\\u" in v and not any("\u0400" <= ch <= "\u04FF" for ch in v):
        # looks like an unparsed \uXXXX string -> decode once
        try:
            v = v.encode("utf-8").decode("unicode_escape")
        except Exception:
            pass
    return v.strip() or None


def _find_vin(card: dict):
    """Full VIN can sit in a few places on the card; the listing only had has_vin."""
    specs = card.get("specs") or {}
    meta = card.get("metadata") or {}
    for v in (card.get("vin"), specs.get("vin"), meta.get("vin"),
              (specs.get("vin_info") or {}).get("vin") if isinstance(specs.get("vin_info"), dict) else None):
        if v:
            return str(v)
    return None


def map_card(card: dict) -> dict:
    """
    Flatten a full card the same way as a listing item, then overlay the two
    fields the listing lacks: the free-text description and the (masked) VIN.

    Caveat for callers: a card's `equipment` array is EMPTY — the grouped
    options live only in the listing. So map_card's equipment-derived fields
    (features_json, has_*, seats, interior_*) come back empty. The crawler's
    --cards backfill therefore does a targeted UPDATE of description/VIN only and
    must NOT overwrite a row with this dict. map_card stays useful for ad-hoc
    inspection via get_card().

    Encoding: Onliner sends description as JSON \\uXXXX escapes on the wire;
    curl_cffi's r.json() decodes them to real Cyrillic, so what we store is
    already UTF-8. FastAPI serialises with ensure_ascii=False, so the site shows
    Russian, not codes.
    """
    row = map_advert(card)
    if "description" in card:
        row["description"] = _clean_text(card.get("description")) or ""
    vin = _find_vin(card)
    if vin:
        row["vin"] = vin
    return row


# --------------------------------------------------------------------------- #
#  Persistence
# --------------------------------------------------------------------------- #
INSERT_COLUMNS = [
    "id", "url", "title", "subject", "brand", "model", "generation", "year",
    "mileage_km", "engine_type", "capacity_l", "power_hp", "gearbox",
    "auto_gearbox", "body_type", "drive", "seats", "condition", "repair_needed",
    "color", "interior_color", "interior_material",
    "has_climate", "has_seatwarmer", "has_abs", "has_navigation",
    "has_alloy_wheels", "has_parktronic", "has_sunroof", "has_alarm",
    "has_cruise", "has_aux", "lights_json", "features_json",
    "price_byn", "price_usd", "auction", "exchange",
    "region", "city", "district", "account_id", "seller", "is_company",
    "vin", "vin_verified", "description", "video", "published_at",
]

SOURCE = "onliner"


def ensure_source_column(con) -> None:
    cols = {r[1] for r in con.execute("PRAGMA table_info(cars)")}
    if "source" not in cols:
        con.execute("ALTER TABLE cars ADD COLUMN source TEXT DEFAULT 'kufar'")
        con.commit()


# Card-only fields: a listing re-crawl maps these to NULL, so it must NOT
# overwrite them — otherwise every full listing pass would wipe the descriptions
# and VINs that the card backfill (--cards) collected. The card pass DOES write them.
PRESERVE_ON_LISTING = ("description", "vin")


def save_row(con, row: dict, card: bool = False) -> None:
    """
    UPSERT a car, append a price-history point on change, replace its images.

    card=False (listing): does not overwrite the card-only fields on conflict,
    so a re-crawl never nulls a previously backfilled description/VIN.
    card=True (--cards):  writes everything, including description/VIN.
    """
    car_id = row["id"]
    if car_id is None:
        return
    ts = now_iso()

    prev = con.execute(
        "SELECT price_byn, price_usd FROM cars WHERE id=?", (car_id,)
    ).fetchone()

    cols = INSERT_COLUMNS + [
        "first_seen_at", "last_seen_at", "last_parsed_at", "is_active", "source"
    ]
    placeholders = ", ".join("?" for _ in cols)
    values = [row.get(c) for c in INSERT_COLUMNS] + [ts, ts, ts, 1, SOURCE]

    # On conflict keep first_seen_at; refresh mutable fields + timestamps.
    skip = set() if card else set(PRESERVE_ON_LISTING)
    update_cols = [c for c in INSERT_COLUMNS if c != "id" and c not in skip] + [
        "last_seen_at", "last_parsed_at", "is_active", "source"
    ]
    update_set = ", ".join(f"{c}=excluded.{c}" for c in update_cols)

    con.execute(
        f"INSERT INTO cars ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {update_set}",
        values,
    )

    # price history: new ad, or price changed
    changed = prev is None or prev[0] != row["price_byn"] or prev[1] != row["price_usd"]
    if changed:
        con.execute(
            "INSERT INTO car_prices (car_id, checked_at, price_byn, price_usd, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (car_id, ts, row["price_byn"], row["price_usd"]),
        )

    # images: replace wholesale (order preserved). Don't blow away existing
    # photos if this payload carried none (e.g. a sparse card response).
    if row.get("_photos"):
        con.execute("DELETE FROM car_images WHERE car_id=?", (car_id,))
        con.executemany(
            "INSERT INTO car_images (car_id, position, url) VALUES (?, ?, ?)",
            [(car_id, i, url) for i, url in enumerate(row["_photos"])],
        )


# --------------------------------------------------------------------------- #
#  Progress  (page-based; key onliner_page_done)
# --------------------------------------------------------------------------- #
_PROGRESS_KEY = "onliner_page_done"


def load_last_page(con) -> int:
    row = con.execute(
        "SELECT value FROM _progress WHERE key=?", (_PROGRESS_KEY,)
    ).fetchone()
    return int(row[0]) if row and str(row[0]).isdigit() else 0


def mark_page_done(con, page: int) -> None:
    con.execute(
        "INSERT INTO _progress (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_PROGRESS_KEY, str(page)),
    )


# --------------------------------------------------------------------------- #
#  Crawl
# --------------------------------------------------------------------------- #
def run(db_path: Path, test: bool = False, restart: bool = False,
        single_page: int | None = None, delay: float = 1.0) -> None:
    con = init_db(db_path)
    ensure_source_column(con)
    client = OnlinerClient(RateLimiter(base=delay))

    # --page and --test are smoke runs: they must NOT advance the resume pointer,
    # otherwise a later full crawl would skip the pages they touched.
    persist_progress = single_page is None and not test

    if single_page is not None:
        start_page = single_page
    elif restart or test:
        start_page = 1
    else:
        start_page = load_last_page(con) + 1
    if start_page > 1 and single_page is None:
        print(f"resuming from page {start_page}")

    # probe the start page to learn how many pages exist
    first = client.search(start_page)
    page_meta = first.get("page") or {}
    last_page = page_meta.get("last")
    total = first.get("total")
    if single_page is not None:
        last_page = start_page                           # exactly this one page
    elif test:
        last_page = min(last_page or 2, start_page + 1)  # first two pages only
    print(f"onliner -> {db_path}  "
          f"(total ads: {total if total is not None else '?'}, "
          f"pages: {last_page if last_page is not None else '?'}"
          f"{', single page ' + str(single_page) if single_page is not None else ''})")

    saved = 0
    started = time.time()
    page = start_page
    data = first
    try:
        while True:
            adverts = data.get("adverts", [])
            if not adverts:
                break
            for ad in adverts:
                save_row(con, map_advert(ad))
                saved += 1
                _SAVED[0] += 1
                if MAX_ADS is not None and _SAVED[0] >= MAX_ADS:
                    raise _ReachedMax
            if persist_progress:
                mark_page_done(con, page)
            con.commit()  # checkpoint after every page

            if page % 10 == 0:
                elapsed = time.time() - started
                rate = saved / elapsed if elapsed else 0
                print(f"  -- page {page}"
                      f"{'/' + str(last_page) if last_page else ''}, "
                      f"{saved} ads, {elapsed:.0f}s ({rate:.1f}/s)")

            if last_page is not None and page >= last_page:
                break
            page += 1
            data = client.search(page)
    except _ReachedMax:
        con.commit()
        print(f"  reached --max ({MAX_ADS}); stopping at page {page}")
    except KeyboardInterrupt:
        con.commit()
        print(f"\ninterrupted at page {page}; progress saved", file=sys.stderr)
        con.close()
        return

    con.commit()
    print(f"\nDone. {saved} ads in {time.time() - started:.0f}s -> {db_path}")

    if test:
        sample = con.execute(
            "SELECT id, brand, model, year, mileage_km, price_usd, city, url "
            "FROM cars WHERE source='onliner' ORDER BY id DESC LIMIT 5"
        ).fetchall()
        print("\nsample rows:")
        for r in sample:
            print("  ", r)
        n_img = con.execute("SELECT COUNT(*) FROM car_images").fetchone()[0]
        print(f"images: {n_img}")
    con.close()


def _unwrap_card(card: dict) -> dict:
    """The card endpoint may return the advert directly or wrapped under a key."""
    if not isinstance(card, dict):
        return {}
    if "manufacturer" in card or "specs" in card or "id" in card:
        return card
    for key in ("vehicle", "advert", "data", "result"):
        inner = card.get(key)
        if isinstance(inner, dict):
            return inner
    return card


def get_card(ad_id: int, client: "OnlinerClient | None" = None,
             with_vin: bool = True) -> dict:
    """Fetch and flatten one onliner card by id (ad-hoc use / debugging).
    with_vin=True also pulls the unmasked VIN from /vehicles/{id}/vin."""
    if client is None:
        client = OnlinerClient(RateLimiter(base=1.0))
    row = map_card(_unwrap_card(client.vehicle(ad_id)))
    if with_vin:
        full = client.full_vin(ad_id)
        if full:
            row["vin"] = full
            row["vin_verified"] = 1
    return row


def _apply_card(con, ad_id: int, card: dict, full_vin: str | None = None) -> None:
    """
    Write back ONLY what a card adds over the listing: description and the VIN.
    full_vin (from the dedicated /vin endpoint) wins over the masked VIN in the
    card body. The card's `equipment` is empty, so we must not re-derive and
    overwrite the listing's options/features. Images are refreshed only if the
    card carries more than we already stored (a safe upgrade, never a shrink).
    """
    desc = _clean_text(card.get("description")) or ""   # "" marks "parsed, no text"
    vin = full_vin or _find_vin(card)                   # unmasked preferred
    con.execute(
        "UPDATE cars SET "
        "  description = ?, "
        "  vin = COALESCE(?, vin), "
        "  vin_verified = CASE WHEN ? IS NOT NULL THEN 1 ELSE vin_verified END, "
        "  last_parsed_at = ? "
        "WHERE id = ? AND source = 'onliner'",
        (desc, vin, vin, now_iso(), ad_id),
    )

    card_photos = [img["original"] for img in card.get("images", []) if img.get("original")]
    have = con.execute(
        "SELECT COUNT(*) FROM car_images WHERE car_id=?", (ad_id,)
    ).fetchone()[0]
    if len(card_photos) > have:
        con.execute("DELETE FROM car_images WHERE car_id=?", (ad_id,))
        con.executemany(
            "INSERT INTO car_images (car_id, position, url) VALUES (?, ?, ?)",
            [(ad_id, i, u) for i, u in enumerate(card_photos)],
        )


def run_cards(db_path: Path, limit: int | None = None, force: bool = False,
              fetch_vin: bool = True, delay: float = 1.0) -> None:
    """
    Card backfill: for stored onliner rows, fetch /vehicles/{id} and fill the two
    fields the listing lacks — free-text description and the VIN. Only those
    fields (plus an image upgrade) are written; the listing-derived
    equipment/options are left untouched because the card returns no equipment.

    fetch_vin=True makes an extra request per ad to /vehicles/{id}/vin for the
    UNMASKED VIN (the card body only carries the masked one). That doubles the
    request count for this pass; pass fetch_vin=False to keep the masked VIN.

    Resume is automatic and needs no progress key: by default we only pick rows
    whose description IS NULL (= never card-parsed). Ads with no description text
    are stored as "" so they are not refetched on the next run.
    """
    con = init_db(db_path)
    ensure_source_column(con)
    client = OnlinerClient(RateLimiter(base=delay))

    where = "source='onliner'"
    if not force:
        where += " AND description IS NULL"   # NULL = not yet card-parsed
    ids = [r[0] for r in con.execute(
        f"SELECT id FROM cars WHERE {where} ORDER BY id DESC")]
    if limit:
        ids = ids[:limit]

    total = len(ids)
    print(f"onliner cards -> {db_path}  ({total} to fetch"
          f"{', force' if force else ''}{', +vin' if fetch_vin else ', no-vin'})")
    if not total:
        print("  nothing to do — every onliner row already has a description "
              "(use --force to refetch)")
        con.close()
        return

    done = 0
    started = time.time()
    try:
        for ad_id in ids:
            try:
                advert = _unwrap_card(client.vehicle(ad_id))
            except FileNotFoundError:
                # Если 404, помечаем в БД пустой строкой, чтобы больше не возвращаться
                con.execute(
                    "UPDATE cars SET description = '', last_parsed_at = ? WHERE id = ? AND source = 'onliner'",
                    (now_iso(), ad_id)
                )
                con.commit()
                continue
            except Exception as exc:
                print(f"  ! card {ad_id} failed: {exc}", file=sys.stderr)
                continue
            full = client.full_vin(ad_id) if fetch_vin else None
            _apply_card(con, ad_id, advert, full_vin=full)
            done += 1
            if done % 25 == 0:
                con.commit()  # checkpoint
                elapsed = time.time() - started
                rate = done / elapsed if elapsed else 0
                print(f"  -- {done}/{total} cards, {elapsed:.0f}s ({rate:.1f}/s)")
    except KeyboardInterrupt:
        con.commit()
        print(f"\ninterrupted; {done}/{total} cards done (re-run resumes)",
              file=sys.stderr)
        con.close()
        return

    con.commit()
    print(f"\nDone. {done}/{total} cards in {time.time() - started:.0f}s")
    sample = con.execute(
        "SELECT id, brand, model, substr(description, 1, 60) || '…', vin "
        "FROM cars WHERE source='onliner' AND description IS NOT NULL "
        "AND description <> '' ORDER BY last_parsed_at DESC LIMIT 3"
    ).fetchall()
    if sample:
        print("sample:")
        for r in sample:
            print("  ", r)
    con.close()


def main() -> None:
    global MAX_ADS
    ap = argparse.ArgumentParser(description="Onliner.by used-car collector")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="output SQLite path")
    ap.add_argument("--test", action="store_true", help="first 2 pages, smoke test")
    ap.add_argument("--page", type=int, default=None,
                    help="fetch exactly one listing page and stop (smoke test; "
                         "does not move the resume pointer)")
    ap.add_argument("--max", type=int, default=None,
                    help="stop after saving this many adverts (quick test)")
    ap.add_argument("--restart", action="store_true",
                    help="ignore saved progress and start from page 1")
    ap.add_argument("--cards", action="store_true",
                    help="card backfill: fetch /vehicles/{id} for stored onliner "
                         "rows to fill description + full VIN")
    ap.add_argument("--cards-limit", type=int, default=None,
                    help="with --cards: cap how many cards to fetch this run")
    ap.add_argument("--force", action="store_true",
                    help="with --cards: refetch rows that already have a description")
    ap.add_argument("--no-vin", action="store_true",
                    help="with --cards: skip the extra /vin request (keep masked VIN)")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="base seconds between requests (raise if you hit 429)")
    args = ap.parse_args()
    MAX_ADS = args.max

    if args.cards:
        run_cards(Path(args.db), limit=args.cards_limit, force=args.force,
                  fetch_vin=not args.no_vin, delay=args.delay)
    else:
        run(Path(args.db), test=args.test, restart=args.restart,
            single_page=args.page, delay=args.delay)


if __name__ == "__main__":
    main()