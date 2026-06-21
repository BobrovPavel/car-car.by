"""
Make-segmented kufar listing collector.

Strategy: iterate over every car make and pull the listing filtered by that make
(`cbnd2=category_2010.mark_<make>`). Every ad returned is that make BY CONSTRUCTION
-- no text parsing, 100% accurate. All filterable fields and image paths already
live in the listing, so we never touch the per-ad HTML card here. The card stays a
lazy/background job for VIN, description, features and dealer legal details only.

Request budget for the full ~43k catalogue: ~150 model-taxonomy calls (one per make,
optional) + ~300 listing pages = a few hundred requests, i.e. minutes. The per-IP
rate limit is irrelevant at this volume.

Wire `save_row()` to your existing db.py and run.
"""

import re
import time
import random
import logging
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import requests

from db import init_db  # reuse the project's schema so the new DB matches cars.db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kufar")

CAT = "2010"  # passenger cars
SIZE = 200    # max page size kufar accepts
SEARCH_URL = "https://cre-api.kufar.by/ads-search/v1/engine/v1/search/rendered-paginated"
COUNT_URL = "https://cre-api.kufar.by/ads-search/v1/engine/v1/search/count"
NODES_URL = "https://api.kufar.by/catalog/v1/nodes"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# polite fixed delay with jitter -- at a few hundred requests this never trips a block
DELAY = 0.7

# listing ad_parameters[p] -> our column name. Value comes from `vl` (human label),
# except numeric fields where `v` is cleaner.
PARAM_MAP = {
    "regdate":        ("year", "v"),          # "1997"
    "mileage":        ("mileage", "v"),       # 400000 (int)
    "cars_engine":    ("engine_type", "vl"),  # "Дизель"
    "cars_capacity":  ("engine_volume", "vl"),  # "1.9 л"
    "cars_gearbox":   ("gearbox", "vl"),      # "Механика"
    "cars_type":      ("body_type", "vl"),    # "Хэтчбек"
    "cars_drive":     ("drive", "vl"),        # "Передний"
    "cars_seats":     ("seats", "vl"),        # "5"
    "condition":      ("condition", "vl"),    # "С пробегом"
    "region":         ("region", "vl"),       # "Гродненская область"
    "area":           ("area", "vl"),         # "Гродно"
    "vehicle_vin_verified_checkbox": ("vin_verified", "v"),  # bool
}


def _get(url, params):
    """GET with a small retry; returns parsed JSON."""
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=25)
            if r.status_code == 200:
                return r.json()
            # soft block / transient -> short backoff, not a 5-minute pause
            wait = int(r.headers.get("Retry-After", 0)) or (5 * (attempt + 1))
            log.warning("HTTP %s on %s -> sleep %ss", r.status_code, url, wait)
            time.sleep(wait)
        except requests.RequestException as e:
            log.warning("request error: %s -> retry", e)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"failed after retries: {url} {params}")


def get_makes():
    """Return [(make_tag, make_label, count), ...] for the cars category."""
    data = _get(NODES_URL, {"tag": f"category_{CAT}", "view": "taxonomy",
                            "with-content": "true"})
    makes = []
    for node in data:
        # make nodes look like {"value":"category_2010.mark_audi",
        #                       "labels":{"ru":"Audi"}, "params":{"node_type":"mark","count":...}}
        params = node.get("params", {}) or {}
        value = node.get("value")
        if not value or params.get("node_type") != "mark":
            continue
        label = (node.get("labels", {}) or {}).get("ru") or value
        makes.append((value, label, int(params.get("count", 0) or 0)))
    return makes


def get_models(make_tag):
    """Return a longest-first list of (label, value) models for a make, for subject matching."""
    data = _get(NODES_URL, {"tag": make_tag, "view": "taxonomy", "with-content": "true"})
    models = []
    for n in (data or []):
        lbl = (n.get("labels", {}) or {}).get("ru") or ""
        val = n.get("value")
        if lbl and val and ".model_" in val:  # model nodes only
            models.append((lbl, val))
    models.sort(key=lambda x: len(x[0]), reverse=True)  # "A4 Allroad" before "A4"
    return models


def make_model_matcher(models):
    """Best-effort exact model from subject, constrained to this make's real models.
    NOTE: matches latin/numeric labels ("A4", "Q5", "Passat"); cyrillic subjects
    ("Пассат") won't match -- those stay null and get refined from the card later,
    or via a model-segmented sweep (see notes at bottom)."""
    compiled = [(re.compile(r"\b" + re.escape(lbl) + r"\b", re.IGNORECASE), lbl, val)
                for lbl, val in models]

    def match(subject):
        for rx, lbl, val in compiled:
            if rx.search(subject):
                return lbl
        return None
    return match


def image_urls(images):
    """Build full-size image URLs straight from listing paths -- no card needed."""
    urls = []
    for img in images or []:
        path = img.get("path")
        if not path:
            continue
        if img.get("media_storage") == "rms":
            urls.append(f"https://rms.kufar.by/v1/gallery/{path}")
        else:
            # fallback for other storages; verify if you ever see a non-rms ad
            urls.append(f"https://rms.kufar.by/v1/gallery/{path}")
    return urls


def _money(raw):
    """Listing prices come as integers in MINOR units (kopecks/cents) -> divide by 100.
    e.g. price_usd "490000" -> 4900.0 ; price_byn "1367100" -> 13671.0"""
    try:
        return round(int(raw) / 100, 2)
    except (TypeError, ValueError):
        return None


def parse_ad(ad, make_label, match_model):
    """Flatten one listing ad into a row dict ready for the DB."""
    row = {
        "id": ad["ad_id"],
        "ad_link": ad.get("ad_link"),
        "subject": ad.get("subject"),
        "brand": make_label,                       # exact, from the query filter
        "model": match_model(ad.get("subject", "")),  # best-effort within the make
        "price_byn": _money(ad.get("price_byn")),
        "price_usd": _money(ad.get("price_usd")),
        "currency": ad.get("currency"),
        "published_at": ad.get("list_time"),
        "company_ad": ad.get("company_ad"),
        "account_id": ad.get("account_id"),
        "images_json": image_urls(ad.get("images")),
        "is_active": 1,
        "source": "kufar",
    }
    for p in ad.get("ad_parameters", []):
        mapped = PARAM_MAP.get(p.get("p"))
        if mapped:
            col, src = mapped
            row[col] = p.get(src)
    return row


def iter_make(make_tag):
    """Yield raw ads for one make, walking cursor pagination."""
    cursor = ""
    while True:
        params = {"cat": CAT, "cbnd2": make_tag, "size": SIZE, "lang": "ru"}
        if cursor:
            params["cursor"] = cursor
        data = _get(SEARCH_URL, params)
        for ad in data.get("ads", []):
            yield ad
        # find the "next" page token (guard against missing pagination on empty results)
        pages = (data.get("pagination", {}) or {}).get("pages", []) or []
        cursor = next((pg.get("token") for pg in pages if pg.get("label") == "next"), None)
        if not cursor:
            return
        time.sleep(DELAY + random.uniform(0, 0.3))


# ===================== persistence (SEPARATE database) =====================
# Writes to its OWN file so the running card-based collector (cars.db) is left
# untouched and the two bases can be compared afterwards. Schema is identical:
# we build it with the project's own init_db(), just pointed at a new path.

DB_FILE = Path(__file__).with_name("cars_listing.db")
COMMIT_EVERY = 200
_con = None
_pending = 0


def open_db():
    global _con
    _con = init_db(DB_FILE)        # same schema/migrations as cars.db, fresh file
    _con.row_factory = sqlite3.Row
    log.info("writing to %s", DB_FILE)
    return _con


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _maybe_commit():
    global _pending
    _pending += 1
    if _pending >= COMMIT_EVERY:
        _con.commit()
        _pending = 0


def _to_cars(row):
    """Map a listing row onto cars-table columns. Card-only fields stay NULL."""
    region, area = row.get("region"), row.get("area")
    # kufar quirk (same as your card parser): for Minsk, area is the district
    if region == "Минск":
        city, district = "Минск", area
    else:
        city, district = area, None

    def _int(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    acc = row.get("account_id")
    return {
        "id": row["id"],
        "url": row.get("ad_link"),
        "title": row.get("subject"),
        "subject": row.get("subject"),
        "brand": row.get("brand"),
        "model": row.get("model"),                # best-effort; pass 2 overwrites with exact
        "year": _int(row.get("year")),
        "mileage_km": _int(row.get("mileage")),
        "engine_type": row.get("engine_type"),
        "capacity_l": row.get("engine_volume"),
        "gearbox": row.get("gearbox"),
        "body_type": row.get("body_type"),
        "drive": row.get("drive"),
        "seats": row.get("seats"),
        "condition": row.get("condition"),
        "price_byn": _int(row.get("price_byn")),  # INTEGER schema -> drop kopecks, like the card parser
        "price_usd": _int(row.get("price_usd")),
        "region": region,
        "city": city,
        "district": district,
        # listing returns account_id 0 (real dealer id is card-only) -> NULL
        "account_id": str(acc) if acc else None,
        "is_company": 1 if row.get("company_ad") else 0,
        "vin_verified": 1 if row.get("vin_verified") else 0,
        "published_at": row.get("published_at"),
    }


_CARS_UPSERT = """
INSERT INTO cars (
    id, url, title, subject, brand, model,
    year, mileage_km, engine_type, capacity_l, gearbox, body_type, drive, seats, condition,
    price_byn, price_usd, region, city, district,
    account_id, is_company, vin_verified, published_at,
    first_seen_at, last_seen_at, last_parsed_at, is_active
) VALUES (
    :id, :url, :title, :subject, :brand, :model,
    :year, :mileage_km, :engine_type, :capacity_l, :gearbox, :body_type, :drive, :seats, :condition,
    :price_byn, :price_usd, :region, :city, :district,
    :account_id, :is_company, :vin_verified, :published_at,
    :now, :now, :now, 1
)
ON CONFLICT(id) DO UPDATE SET
    url=excluded.url, title=excluded.title, subject=excluded.subject, brand=excluded.brand,
    year=excluded.year, mileage_km=excluded.mileage_km, engine_type=excluded.engine_type,
    capacity_l=excluded.capacity_l, gearbox=excluded.gearbox, body_type=excluded.body_type,
    drive=excluded.drive, seats=excluded.seats, condition=excluded.condition,
    price_byn=excluded.price_byn, price_usd=excluded.price_usd,
    region=excluded.region, city=excluded.city, district=excluded.district,
    account_id=excluded.account_id, is_company=excluded.is_company,
    vin_verified=excluded.vin_verified, published_at=excluded.published_at,
    last_seen_at=excluded.last_seen_at, is_active=1
    -- model and first_seen_at deliberately NOT overwritten (model is owned by pass 2)
"""


def save_row(row):
    """UPSERT one listing ad into cars + car_images + car_prices (new DB)."""
    c = _to_cars(row)
    c["now"] = _now()
    _con.execute(_CARS_UPSERT, c)

    # price history: append only on change (or first sighting)
    last = _con.execute(
        "SELECT price_byn, price_usd FROM car_prices WHERE car_id=? "
        "ORDER BY rowid DESC LIMIT 1", (c["id"],)).fetchone()
    if last is None or last["price_byn"] != c["price_byn"] or last["price_usd"] != c["price_usd"]:
        _con.execute(
            "INSERT INTO car_prices (car_id, checked_at, price_byn, price_usd, is_active) "
            "VALUES (?,?,?,?,1)", (c["id"], c["now"], c["price_byn"], c["price_usd"]))

    # images: replace wholesale with the listing's full-size URLs
    _con.execute("DELETE FROM car_images WHERE car_id=?", (c["id"],))
    imgs = [(c["id"], i, u) for i, u in enumerate(row.get("images_json") or [])]
    if imgs:
        _con.executemany(
            "INSERT INTO car_images (car_id, position, url) VALUES (?,?,?)", imgs)

    _maybe_commit()


def iter_model_ids(make_tag, model_value):
    """Yield ad_ids for one (make, model) via the cmdl2 filter."""
    cursor = ""
    while True:
        params = {"cat": CAT, "cbnd2": make_tag, "cmdl2": model_value,
                  "size": SIZE, "lang": "ru"}
        if cursor:
            params["cursor"] = cursor
        data = _get(SEARCH_URL, params)
        for ad in data.get("ads", []):
            yield ad["ad_id"]
        pages = (data.get("pagination", {}) or {}).get("pages", []) or []
        cursor = next((pg.get("token") for pg in pages if pg.get("label") == "next"), None)
        if not cursor:
            return
        time.sleep(DELAY + random.uniform(0, 0.3))


def update_model(ad_id, model_label):
    """Exact model from the cmdl2 filter -> overwrite is correct."""
    _con.execute("UPDATE cars SET model=? WHERE id=?", (model_label, ad_id))
    _maybe_commit()


def fill_models(makes=None):
    """Second pass: assign EXACT model from the cmdl2 filter. Ads whose seller left
    the model unset don't appear under any model node and keep whatever the make pass
    set (best-effort from subject, or null) -- which is correct."""
    if makes is None:
        makes = get_makes()
    total = 0
    for make_tag, make_label, _ in makes:
        try:
            for label, value in get_models(make_tag):
                n = 0
                for ad_id in iter_model_ids(make_tag, value):
                    update_model(ad_id, label)
                    n += 1
                total += n
                if n:
                    log.info("%-16s %-22s %5d", make_label, label, n)
        except Exception as e:
            log.exception("model pass failed for %s: %s", make_label, e)
    log.info("MODEL PASS DONE. exact models assigned: %d", total)


def run(makes=None):
    if makes is None:
        makes = get_makes()
    log.info("makes: %d, total ads expected: %d", len(makes), sum(m[2] for m in makes))

    grand = 0
    for make_tag, make_label, expected in makes:
        if expected == 0:
            continue
        try:
            match_model = make_model_matcher(get_models(make_tag))
            got = 0
            for ad in iter_make(make_tag):
                save_row(parse_ad(ad, make_label, match_model))
                got += 1
        except Exception as e:
            log.exception("make %s (%s) failed, skipping: %s", make_label, make_tag, e)
            continue
        grand += got
        # only a real under-collection is suspicious (cursor died mid-make);
        # small over/under counts are just live drift vs the cached taxonomy count.
        flag = "  <-- UNDER" if got < expected * 0.95 else ""
        log.info("%-18s got %5d / taxonomy %5d%s", make_label, got, expected, flag)

    log.info("DONE. total collected: %d", grand)


if __name__ == "__main__":
    open_db()
    try:
        # quick test on one make first:
        #   run([("category_2010.mark_acura", "Acura", 33)])
        #   fill_models([("category_2010.mark_acura", "Acura", 33)])
        run()          # pass 1: all ads -> cars + car_images + car_prices
        fill_models()  # pass 2: exact model
    finally:
        _con.commit()
        _con.close()
    log.info("done -> %s", DB_FILE)