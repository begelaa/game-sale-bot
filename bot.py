
import os
import json
import time
import logging
import requests

from bs4 import BeautifulSoup
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

# ==============================
# CONFIGURATION
# ==============================

COUNTRY = "GE"
MIN_ORIGINAL = 2500
MAX_FINAL = 700
MIN_DISCOUNT = 80

TIMEZONE = ZoneInfo("Asia/Tbilisi")
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_FILE = Path("sent_deals.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 GameSaleBot/1.0",
    "Accept-Language": "en-US,en;q=0.9"
})


# ==============================
# PRICE FILTER
# ==============================

def qualifies(original, final, currency):
    if currency != "USD":
        return False

    if original < MIN_ORIGINAL:
        return False

    if final < 0 or final > MAX_FINAL:
        return False

    return (original - final) * 100 >= (
        original * MIN_DISCOUNT
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
            dt = datetime.fromtimestamp(value, TIMEZONE)
        else:
            dt = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            ).astimezone(TIMEZONE)

        return dt.strftime("%d.%m.%Y %H:%M") + " (თბილისი)"
    except (ValueError, TypeError, OverflowError):
        return "უცნობია"


# ==============================
# STEAM
# ==============================

def get_steam_deals():
    deals = []
    start = 0
    seen = set()

    while True:
        response = session.get(
            "https://store.steampowered.com/search/results/",
            params={
                "query": "",
                "start": start,
                "count": 100,
                "specials": 1,
                "category1": 998,
                "cc": "ge",
                "l": "english",
                "infinite": 1
            },
            timeout=40
        )

        response.raise_for_status()
        data = response.json()

        soup = BeautifulSoup(
            data.get("results_html", ""),
            "html.parser"
        )

        rows = soup.select("a.search_result_row")

        if not rows:
            break

        for row in rows:
            appid = row.get("data-ds-appid", "")

            if not appid.isdigit() or appid in seen:
                continue

            seen.add(appid)

            discount_tag = row.select_one(
                ".discount_pct, .search_discount"
            )

            if not discount_tag:
                continue

            try:
                pct = int(
                    discount_tag.get_text(strip=True)
                    .replace("-", "")
                    .replace("%", "")
                )
            except ValueError:
                continue

            if pct < MIN_DISCOUNT:
                continue

            price_tag = row.select_one(
                "[data-price-final]"
            )

            if price_tag:
                try:
                    if int(price_tag["data-price-final"]) > MAX_FINAL:
                        continue
                except (ValueError, TypeError):
                    pass

            try:
                time.sleep(1.5)

                detail = session.get(
                    "https://store.steampowered.com/api/appdetails",
                    params={
                        "appids": appid,
                        "cc": "ge",
                        "l": "english"
                    },
                    timeout=30
                )

                detail.raise_for_status()

                item = detail.json().get(appid, {})

                if not item.get("success"):
                    continue

                game = item.get("data", {})

                if game.get("type") != "game":
                    continue

                price = game.get("price_overview", {})

                original = price.get("initial", 0)
                final = price.get("final", 0)
                currency = price.get("currency", "")

                if not qualifies(original, final, currency):
                    continue

                deals.append({
                    "id": "steam-" + appid,
                    "name": game.get("name", "Unknown"),
                    "platform": "Steam",
                    "original": original,
                    "final": final,
                    "discount": discount_percent(original, final),
                    "end": price.get("discount_expiration"),
                    "url": (
                        "https://store.steampowered.com/app/"
                        + appid + "/"
                    )
                })

            except (requests.RequestException, ValueError):
                logging.warning(
                    "Steam app lookup failed: %s", appid
                )

        start += 100

        if start >= data.get("total_count", 0):
            break

        time.sleep(2)

    logging.info("Steam matches: %s", len(deals))
    return deals


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
    start = 0

    while True:
        response = session.post(
            "https://store.epicgames.com/graphql",
            json={
                "query": EPIC_QUERY,
                "variables": {
                    "country": COUNTRY,
                    "start": start,
                    "count": 100
                }
            },
            timeout=40
        )

        response.raise_for_status()
        result = response.json()

        if result.get("errors"):
            raise RuntimeError("Epic GraphQL query failed")

        catalog = result["data"]["Catalog"]["searchStore"]
        elements = catalog.get("elements", [])

        if not elements:
            break

        for game in elements:
            if game.get("offerType") != "BASE_GAME":
                continue

            price = (game.get("price") or {}).get(
                "totalPrice", {}
            ) or {}

            original = price.get("originalPrice", 0)
            final = price.get("discountPrice", 0)
            currency = price.get("currencyCode", "")

            if not qualifies(original, final, currency):
                continue

            end = None

            promotions = game.get("promotions") or {}

            for group in promotions.get(
                "promotionalOffers", []
            ):
                for promo in group.get(
                    "promotionalOffers", []
                ):
                    if promo.get("endDate"):
                        end = promo["endDate"]
                        break

            slug = game.get("productSlug") or game.get("urlSlug")

            if not slug:
                continue

            slug = slug.removesuffix("/home").strip("/")

            deals.append({
                "id": "epic-" + game["id"],
                "name": game["title"],
                "platform": "Epic Games",
                "original": original,
                "final": final,
                "discount": discount_percent(original, final),
                "end": end,
                "url": (
                    "https://store.epicgames.com/en-US/p/"
                    + slug
                )
            })

        start += len(elements)

        if start >= catalog["paging"]["total"]:
            break

        time.sleep(1.5)

    logging.info("Epic matches: %s", len(deals))
    return deals


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

        if not response.ok or not response.json().get("ok"):
            raise RuntimeError("Telegram rejected message")

    except (requests.RequestException, ValueError):
        raise RuntimeError(
            "Telegram request failed"
        ) from None

    logging.info("Sent: %s", deal["name"])
    time.sleep(1)


# ==============================
# SAVE SENT DEALS
# ==============================

def load_state():
    if STATE_FILE.exists():
        return json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


# ==============================
# MAIN
# ==============================

def main():
    state = load_state()
    errors = False

    for platform, getter in [
        ("steam", get_steam_deals),
        ("epic", get_epic_deals)
    ]:
        try:
            deals = getter()
        except Exception as error:
            logging.error(
                "%s check failed: %s",
                platform,
                type(error).__name__
            )
            errors = True
            continue

        active_ids = set()

        for deal in deals:
            key = deal["id"]
            active_ids.add(key)

            signature = {
                "original": deal["original"],
                "final": deal["final"]
            }

            if state.get(key) == signature:
                continue

            try:
                send_telegram(deal)
                state[key] = signature
                save_state(state)
            except Exception:
                logging.error("Telegram delivery failed")
                errors = True

        for key in list(state):
            if key.startswith(platform + "-"):
                if key not in active_ids:
                    del state[key]

        save_state(state)

    logging.info("Daily check finished")

    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
