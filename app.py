import csv
import logging
import os
import re
from dataclasses import dataclass, asdict
from typing import List, Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup

# ----------------- Configuration -----------------

ZIP_CODE = "23112"
TARGET_MODEL_KEYWORDS = ["escalade", "esv"]  # all must appear in listing text
SEARCH_NEW_ONLY = True  # try to exclude used / pre-owned vehicles
OUTPUT_CSV = "results.csv"

# ----------------- Logging setup -----------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ----------------- Data model -----------------


@dataclass
class VehicleOffer:
    dealer_name: str
    title: str
    price: int
    listing_url: str
    location: Optional[str] = None
    distance_miles: Optional[float] = None


# ----------------- Helper functions -----------------


def load_dealers(path: str = "dealers.csv") -> List[dict]:
    """Load dealers (name + inventory URL) from CSV."""
    dealers = []
    if not os.path.exists(path):
        logging.error(f"{path} not found. Please create it with dealer_name,inventory_url.")
        return dealers

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("dealer_name", "").strip()
            url = row.get("inventory_url", "").strip()
            if not name or not url:
                continue
            dealers.append({"dealer_name": name, "inventory_url": url})

    logging.info(f"Loaded {len(dealers)} dealers from {path}.")
    return dealers


def fetch_html(url: str, timeout: int = 25) -> Optional[str]:
    """Download HTML for a given URL."""
    logging.info(f"Fetching inventory page: {url}")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logging.error(f"Error fetching {url}: {e}")
        return None


def text_contains_all_keywords(text: str, keywords: List[str]) -> bool:
    text_lower = text.lower()
    return all(k.lower() in text_lower for k in keywords)


def is_used_vehicle_text(text: str) -> bool:
    """Heuristic to exclude used / pre-owned vehicles."""
    text_l = text.lower()
    used_keywords = ["used", "pre-owned", "pre owned", "cpo", "certified pre-owned"]
    return any(k in text_l for k in used_keywords)


def extract_price(text: str) -> Optional[int]:
    """Find first price in the text like $98,765."""
    match = re.search(r"\$\s*([\d,]{4,8})", text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_inventory_page(
    html: str,
    dealer_name: str,
    inventory_url: str,
    model_keywords: List[str],
    new_only: bool = True,
) -> List[VehicleOffer]:
    """
    Very generic parser:
    - finds elements mentioning Escalade ESV
    - then walks up a few levels to capture the listing 'card'
    - extracts price from the card text
    """
    soup = BeautifulSoup(html, "lxml")
    offers: List[VehicleOffer] = []

    # Find any text node that has all model keywords
    text_nodes = soup.find_all(
        string=lambda s: isinstance(s, str)
        and text_contains_all_keywords(s, model_keywords)
    )

    logging.info(f"Found {len(text_nodes)} potential Escalade ESV mentions for {dealer_name}")

    seen = set()

    for node in text_nodes:
        try:
            # climb up the DOM tree a bit to catch the full listing card
            card = node
            for _ in range(4):
                if card.parent is None:
                    break
                card = card.parent

            card_text = card.get_text(separator=" ", strip=True)
            if not text_contains_all_keywords(card_text, model_keywords):
                continue

            if new_only and is_used_vehicle_text(card_text):
                # Skip obvious used/pre-owned mentions
                continue

            price = extract_price(card_text)
            if price is None:
                continue

            # Build a short title snippet
            title = card_text
            if len(title) > 140:
                title = title[:137] + "..."

            # Avoid duplicates based on (dealer, title, price)
            key = (dealer_name, title, price)
            if key in seen:
                continue
            seen.add(key)

            offer = VehicleOffer(
                dealer_name=dealer_name,
                title=title,
                price=price,
                listing_url=inventory_url,  # generic – you can later try to refine per-car URLs
                location=None,
                distance_miles=None,
            )
            offers.append(offer)
        except Exception as e:
            logging.debug(f"Error parsing a card for {dealer_name}: {e}")
            continue

    logging.info(f"Parsed {len(offers)} possible NEW Escalade ESV offers at {dealer_name}")
    return offers


def find_best_offers() -> List[VehicleOffer]:
    dealers = load_dealers()
    all_offers: List[VehicleOffer] = []

    for dealer in dealers:
        dealer_name = dealer["dealer_name"]
        inv_url = dealer["inventory_url"]

        html = fetch_html(inv_url)
        if not html:
            continue

        offers = parse_inventory_page(
            html,
            dealer_name=dealer_name,
            inventory_url=inv_url,
            model_keywords=TARGET_MODEL_KEYWORDS,
            new_only=SEARCH_NEW_ONLY,
        )
        all_offers.extend(offers)

    if not all_offers:
        logging.warning("No matching vehicles were found. " 
                        "You might need to adjust dealers.csv or parsing logic.")
        return []

    # Sort by lowest price
    all_offers.sort(key=lambda o: o.price)
    return all_offers


def save_offers_to_csv(offers: List[VehicleOffer], path: str = OUTPUT_CSV) -> None:
    df = pd.DataFrame([asdict(o) for o in offers])
    df.to_csv(path, index=False)
    logging.info(f"Saved {len(offers)} offers to {path}")


def print_summary(offers: List[VehicleOffer], top_n: int = 5) -> None:
    if not offers:
        print("No offers found.")
        return

    best = offers[0]
    print("\n=== BEST PRICE FOUND ===")
    print(f"Dealer  : {best.dealer_name}")
    print(f"Price   : ${best.price:,.0f}")
    print(f"Listing : {best.listing_url}")
    print(f"Title   : {best.title}")
    print("========================\n")

    print(f"Top {min(top_n, len(offers))} offers:")
    for i, o in enumerate(offers[:top_n], start=1):
        print(f"{i}. ${o.price:,.0f} - {o.dealer_name}")
        print(f"   URL  : {o.listing_url}")
        print(f"   Desc : {o.title}\n")


# ----------------- Main entrypoint -----------------


if __name__ == "__main__":
    logging.info(f"Starting Cadillac Escalade ESV finder for zip {ZIP_CODE}...")
    offers = find_best_offers()
    if offers:
        save_offers_to_csv(offers)
        print_summary(offers, top_n=5)
    logging.info("Done.")
