# car-car.by — прогресс проекта
 
> Файл для контекста между сессиями. Держится в корне репозитория
> `BobrovPavel/car-car.by` (сейчас **не закоммичен** — живёт локально / в проекте).
> Обновляется по мере прохождения этапов.
 
---
 
## Что за проект
 
Агрегатор объявлений о продаже авто в Беларуси. Собирает объявления с площадок
(kufar.by, av.by, abw.by, onliner.by) в одну базу, делает удобный поиск.
Главная фича: все объявления в одном месте. Вторичная: история цены.
 
**Стек:**
- Python 3.12 (venv в `.venv/`)
- SQLite — локально и на проде. **Одна база на площадку**: `cars.db` (kufar),
  `cars_avby.db` (av.by); abw.by приедет своей базой
- FastAPI + uvicorn — бэкенд, объединяет базы на лету (см. «Архитектура»)
- `requests` — клиент kufar; `curl_cffi` — клиент av.by (обход WAF)
- Vanilla HTML/JS + Chart.js — фронтенд (один файл)
- Oracle Cloud Always Free (ARM, eu-stockholm-1) — VPS под прод
**Инфраструктура:**
- Домен: `car-car.by` (куплен на domain.by)
- GitHub: `BobrovPavel/car-car.by`
- Лендинг "скоро запуск" живёт на Cloudflare Workers (переедет на VPS)
---
 
## Где мы сейчас
 
**Этапы 1-7 закрыты: kufar, av.by и onliner собираются, мультиисточник в UI работает,
плюс готова система обновления — рефреш активных объявлений и снятие неактивных.
Сейчас — этап 8: парсер abw.by (ловим реальный API площадки).
Деплой (этап 9) ждёт capacity на Oracle для VPS. Параллельно полируем сайт локально.**
 
---
 
## Архитектура мультиисточника (важно)
 
Источники НЕ сливаются в одну таблицу с колонкой `source`. Вместо этого —
**отдельный SQLite-файл на площадку, общая схема (`db.py`):**
 
- `cars.db` — kufar (собирает `sources/kufar.py`)
- `cars_avby.db` — av.by (собирает `sources/avby.py`)
- `cars_onliner.db` — onliner (собирает `sources/onliner.py`)
- `cars_abw.db` — abw.by (планируется, этап 8)
`web/api.py` на каждый запрос подключает базы площадок через `ATTACH` и строит
временное представление:
 
```
cars_all = main.cars      ('kufar'   AS source)
    UNION ALL av.cars      ('av'      AS source)
    UNION ALL onliner.cars ('onliner' AS source)
    [UNION ALL abw.cars    ('abw'     AS source)]   ← добавится на этапе 8
```
 
Все листинговые / агрегатные эндпоинты ходят в `cars_all`. Если базы площадки
ещё нет на диске — API мягко деградирует и работает по доступным источникам
(сейчас минимально — только kufar).
 
**Коллизии id между источниками** (у kufar и av свои числовые id, могут
пересечься) разрешаются параметром `?source=` у `GET /api/cars/{id}` — он
говорит, из какой базы тянуть карточку.
 
Колонка `source` в `db.py` не заложена; `avby.py` / `onliner.py` добавляют её в свою
базу рантайм-миграцией (`ensure_source_column`, DEFAULT 'kufar'), но фактический
источник в API определяется по тому, из какой базы пришла строка (литерал в UNION).
 
---
 
## Что сделано
 
### Этап 1. Лендинг ✅
Лендинг "скоро запуск" на `car-car.by`. Тёмная тема (`#0D0E12`) + лимонный акцент
(`#E8FF47`), шрифты Manrope/Inter. Сейчас в репозитории два лендинг-файла:
корневой `index.html` (актуальный, упоминает av.by/abw.by/kufar.by) и
`web/static/landing.html` (ранний сохранённый вариант). Стоит со временем
прибраться и оставить один.
 
### Этап 2. Парсер карточки kufar ✅
 
**Файл:** `kufar_detail_parser.py`
 
`get_ad(ad_id)` → плоский dict с ~50 полями.
 
**Ключевые открытия:**
- У kufar **нет публичного JSON-API для одной карточки**. Данные в HTML страницы
  в теге `<script id="__NEXT_DATA__">` по пути `props.initialState.adView.data`.
- Марка (`cars_brand_v2`) **отсутствует в листинг-API**, только в карточке.
- `adParams[*].vl` — основные параметры, `accountParams[*].v` — реквизиты дилера
  (разные поля!).
- `userName` для юрлиц = название компании; `contactPerson` = имя менеджера
  (НЕ название). Эти два поля долго путали — теперь разведены.
- Фото kufar хранит как `adim1/<uuid>.jpg`, полный URL —
  `https://rms.kufar.by/v1/gallery/<path>`. Собираются рекурсивно.
### Этап 3. SQLite + сборщик kufar ✅
 
**Файлы:** `db.py`, `collector.py`, `repair.py`
 
**Схема (`db.py`, общая для всех источников):**
- `cars` — основная карточка, PK = ad_id. Помимо базовых полей содержит:
  `auto_gearbox`, `repair_needed`, цвет/салон (`color`, `interior_color`,
  `interior_material`), **булевы опции** `has_climate / has_seatwarmer / has_abs /
  has_navigation / has_alloy_wheels / has_parktronic / has_sunroof / has_alarm /
  has_cruise / has_aux` (0/1/NULL), списочные опции `lights_json` / `features_json`,
  `auction`, `exchange`, `district`, `vin_verified`, метки времени
  (`first_seen_at` / `last_seen_at` / `last_parsed_at`), `is_active`
- `car_images` — фото 1:N, PK (car_id, position), индекс по car_id
- `car_prices` — история цены (новая запись при изменении), `is_active=0`
  отмечает «снято с продажи»
- `dealers` — справочник по `account_id` (только kufar): `company`,
  `company_legal`, `company_unp`, `company_address`, `contact_person`,
  `egr_number`, `egr_date`
- `_progress` — служебная (чекпоинты сборщиков)
**Миграции (`db._migrate()`, идемпотентные):**
- 2026-06-20: `contact_person` в `dealers` (отдельно от company)
- 2026-06-21: `district` в `cars` + бэкофилл (для Минска район жил в `city`)
**Сборщик kufar (`collector.py`):**
- `RateLimiter` — адаптивная задержка, при 429 пауза и подъём базовой задержки,
  медленное восстановление
- `Progress` — ETA по ходу прогона
- page-token пагинация листинга, `sweep_inactive()` при `--full` помечает
  `is_active=0` для пропавших из листинга
- Чекпоинт после каждого объявления, Ctrl+C безопасен
- `--max N`, `--full` для полного прогона ~43к (~6ч при 0.5с)
- `need_reparse()` / `REPARSE_DAYS` — карточку повторно не дёргаем чаще порога,
  поэтому возобновление прогона «бесплатное»
**Утилита `repair.py`:** перепарсивает битые строки
(`--no-images` / `--no-brand` / `--bad-price` / `--id` / `--all` / `--dry-run`).
При перепарсинге `skip_price_history=True`.
 
### Этап 4. Web UI (мультиисточник) ✅
 
**Файлы:** `web/api.py`, `web/static/index.html`, `web/requirements.txt`
 
**FastAPI (`web/api.py`):** объединяет базы источников в `cars_all` (см.
«Архитектура»).
- `GET /api/stats` — счётчики, включая разбивку `by_source`
- `GET /api/brands` — марки с count
- `GET /api/models?brand=X` — модели марки
- `GET /api/facets` — значения для дропдаунов (вкл. `source`)
- `GET /api/cars` — листинг с фильтрами (brand/model/year/price/mileage/region/
  body_type/engine_type/**source**), пагинация, сортировка. Превью-фото тянутся
  из базы соответствующего источника и ключуются по `(source, id)`
- `GET /api/cars/{id}?source=` — детально: фото + price_history + dealer JOIN
  (справочник дилеров есть только у kufar; для av показываем `seller` как есть)
**Фронт (`web/static/index.html`):**
- Тёмная тема, Manrope/Inter + JetBrains Mono для всех чисел
- Sticky header, левый сайдбар 280px с каскадом марка→модель и фильтрами
- Сетка карточек: фото 4:3, **бейдж источника** в углу (Kufar / av.by),
  год·пробег·кузов, цена лаймом моноширинно
- Модалка с галереей (← →, Esc), сеткой характеристик, **комплектацией,
  сгруппированной по категориям** (Комфорт / Безопасность / Мультимедиа /
  Экстерьер) — объединяет булевы `has_*` флаги и строки из `features_json`
  через эвристику `featureCategory()`, графиком истории цены (Chart.js),
  карточкой дилера
- **Ограниченная отдача контента оригинала:** показываем максимум 5 фото
  (6-е — размытый CTA-слайд «ещё N фото на <источник>») и обрезаем описание
  (`DESC_LIMIT`, дальше ссылка «читать полностью на <источник>»). Это снижает
  нагрузку на площадки-источники и аккуратнее по части прав на контент
- Пагинация ‹ 1 … 4 5 6 … 23 ›
Запуск:
```powershell
.\.venv\Scripts\python.exe -m uvicorn web.api:app --reload
```
Открыть http://127.0.0.1:8000
 
### Этап 5. Парсер av.by ✅
 
**Файл:** `av_listing.py` (зависимость: `curl_cffi`)
 
Собирает **весь листинг** легковых av.by через JSON-API и пишет в `cars_avby.db`
(та же схема `db.py`), строки тегируются `source='av'`. Карточку отдельно дёргать
не нужно — листинг av.by уже отдаёт марку/модель/поколение, полное описание,
цены, фото, продавца и маскированный VIN.
 
**Почему так:** фронт `cars.av.by` за WAF (отдаёт кастомный 468 даже
браузер-имперсонирующим клиентам), а JSON-хост `web-api.av.by` доступен через
`curl_cffi`. Эндпойнт листинга — `apply`-фильтр, пойманный из SPA:
 
```
POST https://web-api.av.by/offer-types/cars/filters/main/apply
body: {"page": N,
       "properties": [
         {"name":"brands","property":6,
          "value":[[{"name":"brand","value":<brandId>},
                    {"name":"model","value":<modelId>}]]},
         {"name":"price_currency","value":2}],
       "sorting": 1}
→ {count, pageCount, advertsPerPage:25, adverts:[...]}
```
 
**Стратегия краулинга:** идём по брендам (числовые id зашиты в `BRANDS`).
Сначала бренд целиком по всем страницам; если av обрезает пагинацию
(`pageCount*perPage < count`) — сегментируем по моделям (id моделей из
`.../filters/main/init`). Обычно до моделей не доходит. `RateLimiter` + прогрев
cookie-сессии + периодический ре-варм держат длинные прогоны под лимитами.
 
Запуск:
```
pip install curl_cffi
python avby.py                # полный прогон -> cars_avby.db
python avby.py --test         # только Acura (smoke test)
python avby.py --brand 6      # один бренд (напр. Audi)
```
Прогресс по брендам в `_progress` (`av_brand_done:*`), Ctrl+C безопасен,
повторный запуск идемпотентен и добивает пропущенные бренды.
 
---
 
## Этап 6. Парсер onliner.by ✅

**Файл:** `sources/onliner.py` (зависимость: `curl_cffi`)

Собирает весь automarket onliner через публичный JSON «sdapi» в `cars_onliner.db`
(та же схема `db.py`), строки тегируются `source='onliner'`. Лента — один плоский
список, отсортированный по свежести, поэтому краулинг это простой проход страниц
1..last (без сегментации по брендам, как у av.by).

**Эндпойнты:**
- Листинг: `GET https://ab.onliner.by/sdapi/ab.api/search/vehicles?page=N&extended=true&limit=50`
  → `{adverts:[...], total, page:{current,last,limit,items}}`
- Карточка: `GET .../sdapi/ab.api/vehicles/{id}` — добавляет описание (свободный текст)
- VIN: `GET .../sdapi/ab.api/vehicles/{id}/vin` — немаскированный VIN

**Что листинг отдаёт сразу:** manufacturer/model/generation, specs (год, кузов, цвет,
двигатель, КПП, привод, пробег, has_vin), дерево `equipment` по категориям, продавца
(тип, имя, УНП), цены BYN/USD/EUR, deal_terms, локацию, фото, created_at, html_url.
**Только в карточке:** свободное описание и полный VIN (в листинге он маскирован) — тот
же компромисс, что у av.by. Бэкофилл этих двух полей — режим `--cards`.

**`equipment` → опции:** `EQUIP_FLAG_IDS` маппит id опций на булевы `has_*`, всё прочее
уходит в `features_json`; сиденья/материал/цвет салона вытягиваются оттуда же.

Запуск:
```
pip install curl_cffi
python -m sources.onliner                # полный прогон -> cars_onliner.db
python -m sources.onliner --test         # первые 2 страницы (smoke test)
python -m sources.onliner --max 500      # стоп после N объявлений
python -m sources.onliner --cards        # бэкофилл описания + полного VIN по карточкам
```
Прогресс по последней странице в `_progress` (`onliner_page_done`), Ctrl+C безопасен,
повторный запуск возобновляется. Карточный бэкофилл (`--cards`) резюмируется сам: берёт
строки с `description IS NULL`, а 404 помечает пустой строкой, чтобы не возвращаться.

---

## Этап 7. Обновление объявлений (рефреш + снятие неактивных) ✅

**Файлы:** `sources/update_kufar.py`, `sources/update_avby.py`, `sources/update_onliner.py`
— по одному на источник, рядом со сборщиками.

Задача: держать активные объявления актуальными (свежая цена, запись в историю при
изменении) и снимать пропавшие. **Снятие — это `is_active=0`, а не удаление строки:** API
фильтрует `is_active=1`, поэтому снятые исчезают из поиска, а в `car_prices` пишется
закрывающая запись «снято с продажи» — история цены сохраняется. Если объявление вернулось
в листинг, `is_active` поднимается обратно в 1.

**Общий механизм (одинаков во всех трёх):** перед проходом фиксируем `cutoff = now()`;
рефреш поднимает `last_seen_at` у живых; всё, что осталось с `last_seen_at < cutoff`,
помечается `is_active=0` + закрывающая запись в `car_prices`.

**Различие по источникам** (каждый апдейтер переиспользует машинерию своего сборщика):
- **kufar** — лёгкий: листинг уже отдаёт цену, поэтому цена и `last_seen_at` обновляются
  прямо из листинга, **без захода в ~43к карточек** (минуты вместо часов). Цена в листинге
  в сотых (`"2533668"` → 25337) — делим на 100, чтобы совпало с тем, что пишет карточный
  парсер. Новые id (которых ещё нет в базе) пропускаются — их находит `kufar.py`.
- **av.by** и **onliner** — рефреш = повторный проход листинга через существующие
  `crawl_brand` / `save_row`. Они и так пишут историю цены при изменении и двигают
  `last_seen_at`. У onliner `save_row(card=False)` не затирает description/VIN из `--cards`.

**Защита от ложного массового снятия** (оборванный проход листинга, а не реальная
распродажа): sweep идёт только после **полного** прохода (прерывание / `--max` / `--brand` /
упавший бренд у av.by его отключают) и дополнительно блокируется, если под снятие попадает
>20% активных — тогда нужен `--force-sweep`.

Флаги: `--no-sweep` (только рефреш), `--force-sweep`, `--max`/`--brand` (smoke-тесты),
`--delay` (для av/onliner). Связка на проде: сборщик добавляет новое → апдейтер держит
актуальность и снимает ушедшее.

Запуск:
```
python -m sources.update_kufar
python -m sources.update_avby
python -m sources.update_onliner
```

---

## Этап 8. Парсер abw.by 🔧 (в работе)

Цель: по образцу av.by/onliner сделать `sources/abw.py` → своя база `cars_abw.db`
(схема `db.py`), `source='abw'`, и расширить UNION в `web/api.py`.

**На шаге разведки API.** abw.by — SPA за анти-ботом: прямой fetch и сам SSR-сайт
блокируются bot-detection, готового разбора в поиске/на GitHub нет. По образцу av.by нужно
поймать реальные сетевые запросы из SPA (DevTools → Network → Fetch/XHR) и от их формы
строить парсер: запрос листинга, пагинацию, при необходимости карточку и фильтр марки.

## Этап 9. Деплой на Oracle Cloud ⏳ (ждёт capacity)
 
**Что сделано:**
- Аккаунт Oracle Cloud, регион `eu-stockholm-1`
- VCN (`car-car-vcn`), public/private subnets, Internet Gateway
- API key, OCI CLI настроен (`~/.oci/config`), все OCIDs известны
**Что осталось:**
- Получить инстанс (`VM.Standard.A1.Flex`, 4 OCPU, 24 GB, Ubuntu 24.04 ARM).
  Capacity занят, `try_create_instance.py` каждые 5 мин пробует создать; при
  успехе пишет `SUCCESS.txt` с публичным IP и пикает.
- FastAPI как systemd unit
- nginx reverse proxy 80/443 → localhost:8000
- Cloudflare DNS `car-car.by` → IP, SSL через Let's Encrypt
- cron на сборщики: раз в сутки
- Полные прогоны kufar (~43к) и av.by на сервере
---
 
## Параметры Oracle (для скрипта-ловушки и деплоя)
 
```
user OCID:        ocid1.user.oc1..aaaaaaaan4aptvsjv5duuf4szhxad3y4syugtpgme544ftq74ippus2jweva
tenancy OCID:     ocid1.tenancy.oc1..aaaaaaaaqtkwrdxebshq26zazdqpu3zzxr6bdlewdvibghksej4tsfunx24a
compartment OCID: совпадает с tenancy (root compartment)
region:           eu-stockholm-1
fingerprint:      0c:fe:06:72:be:0c:37:06:a4:64:9f:ac:cc:97:37:8e
 
subnet OCID:      ocid1.subnet.oc1.eu-stockholm-1.aaaaaaaaoi4psfwvacu4bo36pjfj3uifgnom4fspwubx4b3chekhd5hkjo4q
AD name:          SAUQ:EU-STOCKHOLM-1-AD-1
image OCID:       ocid1.image.oc1.eu-stockholm-1.aaaaaaaazir62xrvbzdlkuxaocszd5vearz3g5lvepuu3wer6jcderozo65q
                  (Canonical-Ubuntu-24.04-aarch64-2026.04.30-1)
 
API private key:  C:\Users\raind\.oci\oci_api_key.pem
SSH private key:  C:\Users\raind\.ssh\car-car.key
SSH public key:   C:\Users\raind\.ssh\car-car.key.pub
```
 
---
 
## Структура проекта
 
```
car-car/
  data/
    cars_avby.db                  # SQLite база av.by (gitignored)
  sources/
    cars.db                       # SQLite база kufar (gitignored)
    cars_avby.db                  # SQLite база av.by (gitignored) — см. заметку о пути ниже
    cars_onliner.db               # SQLite база onliner (gitignored)
    kufar.py                      # сборщик kufar (карточки)
    kufar_detail_parser.py        # парсер карточки kufar
    avby.py                       # сборщик av.by (листинг, curl_cffi)
    onliner.py                    # сборщик onliner.by (листинг + --cards)
    update_kufar.py               # рефреш цен + снятие неактивных (kufar)
    update_avby.py                # рефреш + снятие неактивных (av.by)
    update_onliner.py             # рефреш + снятие неактивных (onliner)
    repair.py                     # ремонт битых строк kufar
    db.py                         # общая схема БД + миграции
  web/
    api.py                      # FastAPI, объединяет базы в cars_all
    static/
      index.html                # поиск (приложение, отдаётся FastAPI)
      landing.html              # ранний лендинг (сохранён)
  progress.md                   # этот файл (пока не в git)
  try_create_instance.py        # capacity-hunter для Oracle
  requirements.txt

```
 
Заметка: в git закоммичены и `__pycache__/*.pyc` — стоит добавить в `.gitignore`.

Заметка о пути базы av.by: `web/api.py` читает её из `data/cars_avby.db`, а `avby.py`
по умолчанию пишет в `sources/cars_avby.db`. Апдейтеры наследуют дефолт сборщиков
(`sources/...`), поэтому при работе через `data/` запускать с `--db data/cars_avby.db`.
Стоит свести путь к одному месту.
 
---
 
## Что НЕ сделано из плана-MVP
 
- Парсер abw.by — в работе (этап 8)
- Объединить все базы в одну
- Перечитывание ОТРЕДАКТИРОВАННЫХ карточек kufar (описание/опции при правке
  объявления) — пока только через `REPARSE_DAYS` в `kufar.py`; лёгкий
  `update_kufar.py` обновляет лишь цену/статус. У av.by/onliner правки приходят
  с рефрешем листинга
- Поиск по тексту в API/фронте (пока только фильтры)
- Сравнение объявлений (две карточки рядом)
- Аналитика «средняя цена по марке/модели»
- Уведомления о новых объявлениях
- Топ-3 ключевых опции прямо на карточке списка (в модалке комплектация уже
  сгруппирована — см. этап 4)
- Полные прогоны kufar и av.by на проде — сборщики готовы, ждём VPS
---
 
## Технические заметки на будущее
 
### kufar
 
**Листинг (43к):**
`GET https://cre-api.kufar.by/ads-search/v1/engine/v1/search/rendered-paginated?cat=2010&size=100&lang=ru`
→ `{ads, pagination:{pages:[{label:"next", token}]}, total}`
 
**Карточка:** `GET https://auto.kufar.by/vi/{id}` → HTML →
`<script id="__NEXT_DATA__">` → JSON.
 
**В листинге уже есть:** ad_id, price_byn, price_usd, currency, list_time,
ad_link, превью-фото, account_id, company_ad, ad_parameters (но **без
марки/модели!**).
**Только в карточке:** brand, model, generation, vin, описание, реквизиты
дилера, полноразмерные фото.
 
**Пагинация — page-token (pit-снимок):** токены живут ограниченное время. Для
возобновления — по сохранённым id / `list_time`, не по самому токену.
 
**Цены через `money()`** — было два бага: «копейки» из калькулятора (×100) и
потерянная точка в `"286.74 $ *"` → `28674`. Сейчас оба случая обрабатываются.
 
**Дилер `company`:** `trademark` → `userName` → `accountParams.name`.
`contactPerson` НЕ участвует (имя менеджера, отдельно в `contact_person`).
 
### av.by
 
**Хост:** фронт `cars.av.by` за WAF (468), JSON-хост `web-api.av.by` доступен
через `curl_cffi` (`impersonate="chrome"` + прогретые cookie).
 
**Листинг:** `POST https://web-api.av.by/offer-types/cars/filters/main/apply`
(тело см. этап 5). `property:6` = бренды, `price_currency:2`, `sorting:1`.
Ответ: `count`, `pageCount`, `advertsPerPage` (25), `adverts[]`.
**Модели бренда:** `.../filters/main/init?brands[0][brand]=<id>`.
 
**В отличие от kufar — карточка не нужна:** один advert уже несёт точные
brand/model/generation (в `properties` и `metadata`), описание, целочисленные
цены, фото на `avcdn.av.by`, продавца/организацию, маскированный VIN и
`publicUrl`.
 
### onliner

**Хост:** одиночная страница `/vehicles/{id}` закрыта для автоматов, но JSON-хост
`ab.onliner.by/sdapi/ab.api/...` открыт через `curl_cffi`.

**Листинг:** `GET .../search/vehicles?page=N&extended=true&limit=50` — плоская лента по
свежести, `page.last` даёт число страниц. Один advert уже несёт марку/модель/поколение,
specs, дерево `equipment`, продавца с УНП, цены BYN/USD/EUR, фото, created_at.
**Только в карточке** (`.../vehicles/{id}`): свободное описание; полный VIN — отдельным
запросом `.../vehicles/{id}/vin` (в листинге и карточке он маскирован). Бэкофилл — `--cards`.

**Цены** приходят строками-десятичными (`"39060.00"`) — `_money()` округляет до целых.

### abw.by
 
TODO: заполнить после захвата API (этап 6) — хост, эндпойнт листинга, формат
пагинации, нужна ли карточка, как передаётся фильтр марки.
 
---
 
*Последнее обновление: 2026-06-25. onliner отмечен готовым (этап 6), добавлен этап 7 —
система обновления объявлений (рефреш + снятие неактивных, три `update_*.py`). abw.by
сдвинут на этап 8, деплой — на этап 9. Поправлены устаревшие имена файлов в архитектуре,
дерево и заметки.*