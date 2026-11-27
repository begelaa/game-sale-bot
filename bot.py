import os
import json
import time
import logging
import re
import requests

from bs4 import BeautifulSoup
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed


# ==============================
# CONFIGURATION
# ==============================

COUNTRY = "GE"

MIN_ORIGINAL = 2500
MAX_FINAL = 700
MIN_DISCOUNT = 80

MAX_RETRIES = 3
TIMEZONE = ZoneInfo("Asia/Tbilisi")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_FILE = Path("sent_deals.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)


def create_session():
    session = requests.Session()

    session.headers.update({
        "User-Agent": "Mozilla/5.0 GameSaleBot/2.1",
        "Accept-Language": "en-US,en;q=0.9"
    })

    return session


# ==============================
# HTTP REQUEST WITH RETRIES
# ==============================

def request_api(session, method, url, platform, **kwargs):

    for attempt in range(1, MAX_RETRIES + 1):

        try:
            response = session.request(
                method,
                url,
                **kwargs
            )

            if response.status_code == 429:

                try:
                    wait = int(
                        response.headers.get("Retry-After", 60)
                    )
                except (ValueError, TypeError):
                    wait = 60

                wait = max(60, wait)

                logging.warning(
                    "%s: HTTP 429, retry %s/%s",
                    platform,
                    attempt,
                    MAX_RETRIES
                )

                if attempt == MAX_RETRIES or wait > 300:
                    response.raise_for_status()

                logging.info(
                    "%s: waiting %s seconds",
                    platform,
                    wait
                )

                time.sleep(wait)
                continue

            if response.status_code in (
                408, 500, 502, 503, 504
            ):

                logging.warning(
                    "%s: HTTP %s, attempt %s/%s",
                    platform,
                    response.status_code,
                    attempt,
                    MAX_RETRIES
                )

                if attempt == MAX_RETRIES:
                    response.raise_for_status()

                time.sleep(15 * attempt)
                continue

            if not response.ok:
                logging.error(
                    "%s: HTTP status %s",
                    platform,
                    response.status_code
                )

            response.raise_for_status()
            return response

        except requests.HTTPError:
            raise

        except (
            requests.Timeout,
            requests.ConnectionError
        ):

            logging.warning(
                "%s: connection problem, attempt %s/%s",
                platform,
                attempt,
                MAX_RETRIES
            )

            if attempt == MAX_RETRIES:
                raise

            time.sleep(10 * attempt)

    raise RuntimeError(
        platform + ": maximum retries exceeded"
    )


# ==============================
# PRICE FILTER
# ==============================

def qualifies(original, final, currency):

    if currency != "USD":
        return False

    if not isinstance(original, int):
        return False

    if not isinstance(final, int):
        return False

    if original < MIN_ORIGINAL:
        return False

    if final < 0 or final > MAX_FINAL:
        return False

    return (
        (original - final) * 100
        >= original * MIN_DISCOUNT
    )


def discount_percent(original, final):
    return (original - final) * 100 // original


# ==============================
# DATE FORMAT
# ==============================

def format_end(value):

    if not value:
        return "უცნობია"

    try:

        if isinstance(value, (int, float)):

            dt = datetime.fromtimestamp(
                value,
                TIMEZONE
            )

        else:

            dt = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )

            if dt.tzinfo is None:
                return "უცნობია"

            dt = dt.astimezone(TIMEZONE)

        return (
            dt.strftime("%d.%m.%Y %H:%M")
            + " (თბილისი)"
        )

    except (
        ValueError,
        TypeError,
        OverflowError
    ):
        return "უცნობია"


# ==============================
# STEAM PRICE PARSER
# ==============================

def steam_price(tag):

    if tag is None:
        return None

    text = tag.get_text(strip=True)

    match = re.fullmatch(
        r"(?:US\s*)?\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)",
        text
    )

    if not match:
        return None

    return int(
        Decimal(
            match.group(1).replace(",", "")
        ) * 100
    )


# ==============================
# STEAM
# ==============================

def get_steam_deals():

    deals = []
    seen = set()
    start = 0
    complete = True

    with create_session() as steam:

        try:

            while True:

                logging.info(
                    "Steam: checking offset %s",
                    start
                )

                response = request_api(
                    steam,
                    "GET",
                    "https://store.steampowered.com/search/results/",
                    "Steam",
                    params={
                        "query": "",
                        "start": start,
                        "count": 100,
                        "specials": 1,
                        "category1": 998,
                        "cc": COUNTRY.lower(),
                        "l": "english",
                        "infinite": 1
                    },
                    timeout=40
                )

                data = response.json()

                total = int(data["total_count"])

                soup = BeautifulSoup(
                    data.get("results_html", ""),
                    "html.parser"
                )

                rows = soup.select(
                    "a.search_result_row"
                )

                if not rows:

                    if start < total:
                        complete = False

                    break

                new_rows = 0

                for row in rows:

                    appid = row.get(
                        "data-ds-appid",
                        ""
                    )

                    if not appid.isdigit():
                        continue

                    if appid in seen:
                        continue

                    seen.add(appid)
                    new_rows += 1

                    discount_tag = row.select_one(
                        ".discount_pct, .search_discount"
                    )

                    if not discount_tag:
                        continue

                    match = re.search(
                        r"(\d+)%",
                        discount_tag.get_text(strip=True)
                    )

                    if not match:
                        continue

                    pct = int(match.group(1))

                    if pct < MIN_DISCOUNT:
                        continue

                    raw_final = row.get(
                        "data-price-final"
                    )

                    if raw_final is None:

                        price_tag = row.select_one(
                            "[data-price-final]"
                        )

                        if price_tag:
                            raw_final = price_tag.get(
                                "data-price-final"
                            )

                    if raw_final is not None:

                        try:

                            if int(raw_final) > MAX_FINAL:
                                continue

                        except (
                            ValueError,
                            TypeError
                        ):
                            pass

                    original = steam_price(
                        row.select_one(
                            ".discount_original_price"
                        )
                    )

                    final = steam_price(
                        row.select_one(
                            ".discount_final_price"
                        )
                    )

                    if original is not None:

                        if original < MIN_ORIGINAL:
                            continue

                    if final is not None:

                        if final > MAX_FINAL:
                            continue

                    if (
                        original is not None
                        and final is not None
                    ):

                        if not qualifies(
                            original,
                            final,
                            "USD"
                        ):
                            continue

                    time.sleep(1)

                    detail = request_api(
                        steam,
                        "GET",
                        "https://store.steampowered.com/api/appdetails",
                        "Steam details",
                        params={
                            "appids": appid,
                            "cc": COUNTRY.lower(),
                            "l": "english"
                        },
                        timeout=20
                    )

                    item = detail.json().get(
                        appid,
                        {}
                    )

                    if not item.get("success"):

                        complete = False

                        logging.warning(
                            "Steam: unavailable app %s",
                            appid
                        )

                        continue

                    game = item.get("data", {})

                    if game.get("type") != "game":
                        continue

                    price = game.get(
                        "price_overview",
                        {}
                    ) or {}

                    original = price.get("initial", 0)
                    final = price.get("final", 0)
                    currency = price.get("currency", "")

                    if not qualifies(
                        original,
                        final,
                        currency
                    ):
                        continue

                    deal = {
                        "id": "steam-" + appid,
                        "name": game.get(
                            "name",
                            "Unknown"
                        ),
                        "platform": "Steam",
                        "original": original,
                        "final": final,
                        "discount": discount_percent(
                            original,
                            final
                        ),
                        "end": price.get(
                            "discount_expiration"
                        ),
                        "url": (
                            "https://store.steampowered.com/app/"
                            + appid + "/"
                        )
                    }

                    deals.append(deal)

                    logging.info(
                        "Steam MATCH: %s | $%.2f -> $%.2f | %s%%",
                        deal["name"],
                        original / 100,
                        final / 100,
                        deal["discount"]
                    )

                start += len(rows)

                logging.info(
                    "Steam progress: %s/%s | matches: %s",
                    start,
                    total,
                    len(deals)
                )

                if start >= total:
                    break

                if new_rows == 0:
                    raise RuntimeError(
                        "Steam pagination stalled"
                    )

                time.sleep(1.5)

        except Exception as error:

            complete = False

            logging.error(
                "Steam stopped: %s - %s",
                type(error).__name__,
                str(error)
            )

    logging.info(
        "Steam finished | matches: %s | complete: %s",
        len(deals),
        complete
    )

    return deals, complete


# ==============================
# EPIC GAMES
# ==============================

EPIC_QUERY = """
query SearchStore(
    $country: String!,
    $start: Int!,
    $count: Int!
) {
    Catalog {
        searchStore(
            country: $country,
            locale: "en-US",
            start: $start,
            count: $count
        ) {
            elements {
                id
                title
                offerType
                productSlug
                urlSlug
                price(country: $country) {
                    totalPrice {
                        originalPrice
                        discountPrice
                        currencyCode
                    }
                }
                promotions {
                    promotionalOffers {
                        promotionalOffers {
                            startDate
                            endDate
                        }
                    }
                }
            }
            paging {
                total
            }
        }
    }
}
"""


def get_epic_deals():

    deals = []
    seen = set()
    start = 0
    complete = True

    with create_session() as epic:

        try:

            while True:

                logging.info(
                    "Epic: checking offset %s",
                    start
                )

                # GraphQL can return partial data together with errors.
                # Keep usable products and mark the scan incomplete rather
                # than discarding the entire page.
                price_failed_indices = set()
                promotion_failed_indices = set()
                page_ready = False
                catalog_data = {}

                for attempt in range(1, MAX_RETRIES + 1):

                    response = request_api(
                        epic,
                        "POST",
                        "https://store.epicgames.com/graphql",
                        "Epic",
                        json={
                            "query": EPIC_QUERY,
                            "variables": {
                                "country": COUNTRY,
                                "start": start,
                                "count": 40
                            }
                        },
                        timeout=40
                    )

                    try:
                        result = response.json()
                    except ValueError as error:
                        logging.warning(
                            "Epic: invalid JSON response (%s/%s): %s",
                            attempt,
                            MAX_RETRIES,
                            error
                        )

                        if attempt == MAX_RETRIES:
                            raise RuntimeError(
                                "Epic returned invalid JSON"
                            ) from error

                        time.sleep(15 * attempt)
                        continue

                    data = result.get("data") or {}
                    catalog_data = (
                        (data.get("Catalog") or {})
                        .get("searchStore") or {}
                    )

                    elements = catalog_data.get("elements")
                    paging = catalog_data.get("paging") or {}
                    errors_list = result.get("errors") or []

                    # A page is usable if the product list and total count
                    # were returned, even when individual fields failed.
                    if (
                        isinstance(elements, list)
                        and paging.get("total") is not None
                    ):

                        page_ready = True

                        if errors_list:
                            complete = False

                            for error in errors_list:

                                path = error.get("path") or []

                                field = (
                                    path[4]
                                    if len(path) > 4
                                    else None
                                )

                                index = (
                                    path[3]
                                    if len(path) > 3
                                    else None
                                )

                                if (
                                    len(path) >= 5
                                    and path[0] == "Catalog"
                                    and path[1] == "searchStore"
                                    and path[2] == "elements"
                                    and isinstance(index, int)
                                ):

                                    if field == "price":
                                        price_failed_indices.add(index)

                                    elif field == "promotions":
                                        promotion_failed_indices.add(index)

                                logging.warning(
                                    "Epic partial GraphQL error: %s | path: %s",
                                    error.get("message", "Unknown GraphQL error"),
                                    path
                                )

                        break

                    logging.error(
                        "Epic GraphQL request failed (%s/%s): %s",
                        attempt,
                        MAX_RETRIES,
                        errors_list or "Missing catalog data"
                    )

                    if attempt == MAX_RETRIES:
                        raise RuntimeError(
                            "Epic GraphQL query failed: no usable catalog data"
                        )

                    time.sleep(15 * attempt)

                if not page_ready:
                    raise RuntimeError(
                        "Epic: no usable catalog data"
                    )

                elements = catalog_data.get("elements", [])
                total = int(catalog_data["paging"]["total"])

                if not elements:

                    if start < total:
                        complete = False

                    break

                new_rows = 0

                for element_index, game in enumerate(elements):

                    if not isinstance(game, dict):
                        complete = False
                        logging.warning(
                            "Epic: empty/invalid product at index %s",
                            element_index
                        )
                        continue

                    game_id = game.get("id")

                    if not game_id:
                        continue

                    if game_id in seen:
                        continue

                    seen.add(game_id)
                    new_rows += 1

                    if game.get("offerType") != "BASE_GAME":
                        continue

                    # A GraphQL error on this product's price means we
                    # cannot safely determine whether it qualifies.
                    if element_index in price_failed_indices:

                        logging.warning(
                            "Epic: skipping game with unavailable price: %s",
                            game_id
                        )

                        continue

                    price_data = game.get("price")

                    if not isinstance(price_data, dict):
                        complete = False
                        logging.warning(
                            "Epic: missing price data for %s",
                            game_id
                        )
                        continue

                    price = price_data.get("totalPrice") or {}

                    original = price.get("originalPrice", 0)
                    final = price.get("discountPrice", 0)
                    currency = price.get("currencyCode", "")

                    if not qualifies(
                        original,
                        final,
                        currency
                    ):
                        continue

                    end = None

                    if element_index in promotion_failed_indices:

                        logging.warning(
                            "Epic: promotion data unavailable: %s",
                            game_id
                        )

                    else:

                        promotions = game.get("promotions") or {}

                        for group in (
                            promotions.get("promotionalOffers") or []
                        ):

                            for promo in (
                                group.get("promotionalOffers") or []
                            ):

                                try:

                                    now = datetime.now(TIMEZONE)

                                    start_date = promo.get("startDate")
                                    end_date = promo.get("endDate")

                                    if (
                                        not isinstance(start_date, str)
                                        or not isinstance(end_date, str)
                                    ):
                                        continue

                                    promo_start = datetime.fromisoformat(
                                        start_date.replace("Z", "+00:00")
                                    )

                                    promo_end = datetime.fromisoformat(
                                        end_date.replace("Z", "+00:00")
                                    )

                                    if (
                                        promo_start.tzinfo is None
                                        or promo_end.tzinfo is None
                                    ):
                                        continue

                                    if promo_start <= now < promo_end:
                                        end = end_date
                                        break

                                except (
                                    ValueError,
                                    TypeError,
                                    AttributeError
                                ):
                                    continue

                            if end:
                                break

                    slug = (
                        game.get("productSlug")
                        or game.get("urlSlug")
                    )

                    if not isinstance(slug, str) or not slug.strip("/"):
                        logging.warning(
                            "Epic: missing product link for %s",
                            game_id
                        )
                        continue

                    slug = slug.removesuffix("/home").strip("/")

                    deal = {
                        "id": "epic-" + game_id,
                        "name": game.get("title", "Unknown"),
                        "platform": "Epic Games",
                        "original": original,
                        "final": final,
                        "discount": discount_percent(
                            original,
                            final
                        ),
                        "end": end,
                        "url": (
                            "https://store.epicgames.com/en-US/p/"
                            + slug
                        )
                    }

                    deals.append(deal)

                    logging.info(
                        "Epic MATCH: %s | $%.2f -> $%.2f | %s%%",
                        deal["name"],
                        original / 100,
                        final / 100,
                        deal["discount"]
                    )

                start += len(elements)

                logging.info(
                    "Epic progress: %s/%s | matches: %s",
                    start,
                    total,
                    len(deals)
                )

                if start >= total:
                    break

                if new_rows == 0:
                    raise RuntimeError(
                        "Epic pagination stalled"
                    )

                time.sleep(1.5)

        except Exception as error:

            complete = False

            logging.exception(
                "Epic stopped: %s - %s",
                type(error).__name__,
                str(error)
            )

    logging.info(
        "Epic finished | matches: %s | complete: %s",
        len(deals),
        complete
    )

    return deals, complete


# ==============================
# TELEGRAM
# ==============================

def send_telegram(deal):

    message = (
        "🔥 ახალი ფასდაკლება!\n\n"
        f"🎮 თამაში: {deal['name']}\n"
        f"🛒 პლატფორმა: {deal['platform']}\n\n"
        f"💵 საწყისი ფასი: ${deal['original'] / 100:.2f}\n"
        f"💰 ახალი ფასი: ${deal['final'] / 100:.2f}\n"
        f"🏷️ ფასდაკლება: {deal['discount']}%\n\n"
        f"📅 დასრულება: {format_end(deal['end'])}\n"
        "🌍 რეგიონი: საქართველო\n\n"
        f"🔗 {deal['url']}"
    )

    try:

        response = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message,
                "link_preview_options": {
                    "is_disabled": True
                }
            },
            timeout=30
        )

        if not response.ok:

            logging.error(
                "Telegram HTTP status: %s",
                response.status_code
            )

            raise RuntimeError(
                "Telegram rejected message"
            )

        if not response.json().get("ok"):

            raise RuntimeError(
                "Telegram delivery failed"
            )

    except (
        requests.RequestException,
        ValueError
    ):

        raise RuntimeError(
            "Telegram request failed"
        ) from None

    logging.info(
        "Telegram SENT: %s",
        deal["name"]
    )

    time.sleep(1)


# ==============================
# SAVE SENT DEALS
# ==============================

def load_state():

    if STATE_FILE.exists():

        return json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

    return {}


def save_state(state):

    temp_file = STATE_FILE.with_suffix(
        ".tmp"
    )

    temp_file.write_text(
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    temp_file.replace(STATE_FILE)


# ==============================
# MAIN
# ==============================

def main():

    state = load_state()

    errors = False
    total_found = 0
    total_sent = 0
    total_skipped = 0

    logging.info(
        "Game Sale Bot 2.1 started"
    )

    logging.info(
        "Region: %s | Original >= $25 | Final <= $7 | Discount >= 80%%",
        COUNTRY
    )

    with ThreadPoolExecutor(
        max_workers=2
    ) as executor:

        futures = {
            executor.submit(
                get_steam_deals
            ): "steam",

            executor.submit(
                get_epic_deals
            ): "epic"
        }

        for future in as_completed(futures):

            platform = futures[future]

            try:

                deals, complete = future.result()

            except Exception as error:

                logging.error(
                    "%s failed: %s - %s",
                    platform,
                    type(error).__name__,
                    str(error)
                )

                errors = True
                continue

            if not complete:

                errors = True

                logging.warning(
                    "%s scan incomplete. Sending collected deals anyway.",
                    platform
                )

            total_found += len(deals)

            active_ids = set()

            for deal in deals:

                key = deal["id"]

                active_ids.add(key)

                signature = {
                    "original": deal["original"],
                    "final": deal["final"]
                }

                if state.get(key) == signature:

                    total_skipped += 1

                    logging.info(
                        "Already sent: %s",
                        deal["name"]
                    )

                    continue

                try:

                    send_telegram(deal)

                    state[key] = signature

                    save_state(state)

                    total_sent += 1

                except Exception:

                    logging.error(
                        "Telegram delivery failed: %s",
                        deal["name"]
                    )

                    errors = True

            if complete:

                for key in list(state):

                    if key.startswith(
                        platform + "-"
                    ):

                        if key not in active_ids:

                            del state[key]

            else:

                logging.warning(
                    "%s: preserving old history because scan was incomplete",
                    platform
                )

            save_state(state)

    logging.info(
        "================================"
    )

    logging.info(
        "DAILY CHECK SUMMARY"
    )

    logging.info(
        "Total found: %s",
        total_found
    )

    logging.info(
        "Telegram sent: %s",
        total_sent
    )

    logging.info(
        "Already sent: %s",
        total_skipped
    )

    logging.info(
        "Errors: %s",
        errors
    )

    logging.info(
        "================================"
    )

    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
