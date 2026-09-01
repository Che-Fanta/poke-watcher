#!/usr/bin/env python3
"""
poke_watcher.py — Schweizer Pokémon-Shop-Watcher.

Überwacht die in targets.json konfigurierten Shops und meldet neue
Pokémon-Produkte, die zu den Keywords passen, nicht ausgeschlossen sind
und unter dem definierten Maximalpreis liegen.

Shopify:
- /products.json wird verwendet
- Preis und Verfügbarkeit werden direkt geprüft

Andere Shops:
- HTML wird durchsucht
- Produktlinks werden erkannt
- Preis wird soweit möglich aus dem HTML gelesen
- Produkte ohne erkennbaren Preis werden trotzdem gefunden,
  aber im Discord-Alert entsprechend gekennzeichnet

Erste Ausführung pro Shop:
- Baseline wird erstellt
- KEINE Discord-Meldung

Danach:
- Nur neu auftauchende Produkte werden gemeldet

Setup:
    pip install requests beautifulsoup4

Einmaliger Durchgang:
    python poke_watcher.py --once

Dauerbetrieb:
    python poke_watcher.py

Environment:
    DISCORD_WEBHOOK_URL
"""

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Dateien
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent

CONFIG_FILE = HERE / "targets.json"
STATE_FILE = HERE / "state.json"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------

MAX_ALERTS_PER_SHOP = 15

REQUEST_TIMEOUT = 25

# Kleine Pause zwischen Shops
MIN_SHOP_DELAY = 1.0
MAX_SHOP_DELAY = 2.5


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def load_json(path, default):
    """
    JSON-Datei laden.
    """
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))

    except json.JSONDecodeError:
        print(
            f"[warn] {path.name} ist ungültig. "
            f"Verwende Default."
        )
        return default

    except OSError as e:
        print(
            f"[warn] {path.name} konnte nicht gelesen werden: {e}"
        )
        return default


def save_json(path, data):
    """
    JSON-Datei speichern.
    """
    try:
        path.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

    except OSError as e:
        print(
            f"[error] {path.name} konnte nicht gespeichert werden: {e}"
        )


# ---------------------------------------------------------------------------
# Shop-Key
# ---------------------------------------------------------------------------

def shop_key(url):
    """
    Erstellt einen stabilen Key für state.json.
    """
    return (
        urlparse(url)
        .netloc
        .lower()
        .replace("www.", "")
    )


# ---------------------------------------------------------------------------
# Textbereinigung
# ---------------------------------------------------------------------------

def normalize_text(text):
    """
    Vereinheitlicht Text für Suchvergleiche.
    """
    if not text:
        return ""

    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    return text.strip().lower()


# ---------------------------------------------------------------------------
# Produktfilter
# ---------------------------------------------------------------------------

def title_matches(title, keywords, exclude):
    """
    Prüft, ob ein Produkt:

    1. ein Pokémon-Produkt ist
    2. ein gewünschtes Keyword enthält
    3. keinen Ausschlussbegriff enthält
    """

    t = normalize_text(title)

    if not t:
        return False

    # -------------------------------------------------------
    # Pokémon muss enthalten sein
    # -------------------------------------------------------

    pokemon_terms = [
        "pokemon",
        "pokémon",
        "pokemon tcg",
        "pokémon tcg",
    ]

    if not any(term in t for term in pokemon_terms):
        return False

    # -------------------------------------------------------
    # Gewünschte Keywords
    # -------------------------------------------------------

    if not any(
        normalize_text(keyword) in t
        for keyword in keywords
    ):
        return False

    # -------------------------------------------------------
    # Ausschlüsse
    # -------------------------------------------------------

    if any(
        normalize_text(exclude_word) in t
        for exclude_word in exclude
    ):
        return False

    return True


# ---------------------------------------------------------------------------
# Preis
# ---------------------------------------------------------------------------

def extract_price(text):
    """
    Versucht einen Schweizer Preis aus Text zu erkennen.

    Beispiele:
        CHF 149.90
        CHF 149.-
        CHF 149
        Fr. 149.90
        Fr 149
    """

    if not text:
        return None

    # Geschützte Leerzeichen normalisieren
    text = text.replace("\xa0", " ")

    patterns = [
        r"(?:CHF|Fr\.?)\s*([0-9]{1,5}(?:['\s][0-9]{3})*(?:[.,][0-9]{1,2})?)",
        r"([0-9]{1,5}(?:['\s][0-9]{3})*(?:[.,][0-9]{2}))\s*CHF",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        if not match:
            continue

        value = match.group(1)

        value = (
            value
            .replace("'", "")
            .replace(" ", "")
            .replace(",", ".")
        )

        try:
            return float(value)

        except ValueError:
            continue

    return None


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def notify(webhook, text):
    """
    Sendet Discord-Nachricht.
    """

    if not webhook:
        print(
            "[alert] (kein Webhook) "
            + text
        )
        return

    try:

        response = requests.post(
            webhook,
            json={
                "content": text[:1900]
            },
            headers={
                "Content-Type": "application/json"
            },
            timeout=15
        )

        response.raise_for_status()

    except requests.RequestException as e:

        print(
            f"[error] Discord-Notify fehlgeschlagen: {e}"
        )

    time.sleep(0.5)


# ===========================================================================
# SHOPIFY
# ===========================================================================

def fetch_shopify(base):
    """
    Versucht Produkte über Shopify /products.json abzurufen.

    Gibt None zurück, wenn die Website kein kompatibles Shopify-Endpoint
    besitzt.
    """

    products = []

    for page in range(1, 6):

        url = urljoin(
            base,
            f"/products.json?limit=250&page={page}"
        )

        try:

            response = requests.get(
                url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT
            )

        except requests.RequestException as e:

            print(
                f"[warn] Shopify {base}: {e}"
            )

            return None

        if response.status_code != 200:
            return None

        try:

            data = response.json()

        except ValueError:

            return None

        if not isinstance(data, dict):
            return None

        if "products" not in data:
            return None

        batch = data["products"]

        if not isinstance(batch, list):
            return None

        products.extend(batch)

        if len(batch) < 250:
            break

        time.sleep(1)

    return products


def shopify_candidates(
    base,
    products,
    keywords,
    exclude,
    max_price
):
    """
    Filtert Shopify-Produkte.
    """

    out = []

    for product in products:

        title = product.get(
            "title",
            ""
        )

        if not title_matches(
            title,
            keywords,
            exclude
        ):
            continue

        variants = (
            product.get(
                "variants",
                []
            )
            or []
        )

        # ---------------------------------------------------
        # Verfügbarkeit
        # ---------------------------------------------------

        available = any(
            bool(
                variant.get("available")
            )
            for variant in variants
        )

        if not available:
            continue

        # ---------------------------------------------------
        # Preis
        # ---------------------------------------------------

        prices = []

        for variant in variants:

            try:

                price = float(
                    variant.get("price")
                )

                prices.append(price)

            except (
                TypeError,
                ValueError
            ):
                pass

        price = (
            min(prices)
            if prices
            else None
        )

        # ---------------------------------------------------
        # Maximalpreis
        # ---------------------------------------------------

        if (
            max_price is not None
            and price is not None
            and price > max_price
        ):
            continue

        handle = product.get(
            "handle",
            ""
        )

        link = urljoin(
            base,
            "/products/"
            + handle
        )

        out.append(
            {
                "key": handle or title,
                "title": title,
                "price": price,
                "available": True,
                "link": link,
            }
        )

    return out


# ===========================================================================
# HTML FALLBACK
# ===========================================================================

def find_product_title(a):
    """
    Ermittelt möglichst zuverlässig den Produkttitel eines Links.
    """

    text = a.get_text(
        " ",
        strip=True
    )

    if text:
        return text

    image = a.find("img")

    if image:

        alt = image.get(
            "alt",
            ""
        )

        if alt:
            return alt.strip()

    return ""


def html_candidates(
    url,
    keywords,
    exclude,
    max_price
):
    """
    HTML-Fallback.

    Durchsucht Links der angegebenen Seite nach relevanten
    Pokémon-Produkten.
    """

    try:

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT
        )

        response.raise_for_status()

    except requests.RequestException as e:

        print(
            f"[error] HTML {url}: {e}"
        )

        return None

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    seen = set()
    out = []

    for a in soup.find_all(
        "a",
        href=True
    ):

        title = find_product_title(a)

        if not title:
            continue

        if not title_matches(
            title,
            keywords,
            exclude
        ):
            continue

        href = urljoin(
            url,
            a["href"]
        )

        # ---------------------------------------------------
        # Nur HTTP(S)
        # ---------------------------------------------------

        parsed = urlparse(href)

        if parsed.scheme not in (
            "http",
            "https"
        ):
            continue

        # ---------------------------------------------------
        # Doppelte Links verhindern
        # ---------------------------------------------------

        if href in seen:
            continue

        seen.add(href)

        # ---------------------------------------------------
        # Umgebung nach Preis durchsuchen
        # ---------------------------------------------------

        parent_text = ""

        parent = a.parent

        if parent:
            parent_text = parent.get_text(
                " ",
                strip=True
            )

        # Etwas mehr Kontext versuchen
        container = a.find_parent(
            [
                "article",
                "li",
                "div"
            ]
        )

        if container:

            container_text = container.get_text(
                " ",
                strip=True
            )

            if len(container_text) > len(parent_text):

                parent_text = container_text

        price = extract_price(
            parent_text
        )

        # ---------------------------------------------------
        # Preisfilter
        # ---------------------------------------------------

        if (
            max_price is not None
            and price is not None
            and price > max_price
        ):
            continue

        # ---------------------------------------------------
        # Verfügbarkeit
        #
        # Bei reinem HTML-Fallback können wir nicht
        # zuverlässig garantieren, dass der Artikel lagernd ist.
        # Deshalb speichern wir "unknown".
        # ---------------------------------------------------

        available = None

        combined_text = normalize_text(
            parent_text
        )

        unavailable_terms = [
            "ausverkauft",
            "nicht verfügbar",
            "nicht lieferbar",
            "out of stock",
            "sold out",
            "derzeit nicht verfügbar",
            "momentan nicht verfügbar",
        ]

        available_terms = [
            "auf lager",
            "lagernd",
            "sofort lieferbar",
            "lieferbar",
            "verfügbar",
            "in stock",
        ]

        if any(
            term in combined_text
            for term in unavailable_terms
        ):
            available = False

        elif any(
            term in combined_text
            for term in available_terms
        ):
            available = True

        out.append(
            {
                "key": href,
                "title": title[:180],
                "price": price,
                "available": available,
                "link": href,
            }
        )

    return out


# ===========================================================================
# SHOP PRÜFEN
# ===========================================================================

def check_shop(
    shop,
    cfg,
    state,
    webhook
):
    """
    Prüft einen einzelnen Shop.
    """

    name = shop["name"]
    url = shop["url"]

    parsed_url = urlparse(url)

    base = (
        f"{parsed_url.scheme}://"
        f"{parsed_url.netloc}"
    )

    keywords = cfg.get(
        "keywords",
        []
    )

    exclude = cfg.get(
        "exclude",
        []
    )

    max_price = cfg.get(
        "max_price"
    )

    # ------------------------------------------------------------------
    # Shopify versuchen
    # ------------------------------------------------------------------

    products = fetch_shopify(
        base
    )

    if products is not None:

        candidates = shopify_candidates(
            base,
            products,
            keywords,
            exclude,
            max_price
        )

        source = "shopify"

    else:

        candidates = html_candidates(
            url,
            keywords,
            exclude,
            max_price
        )

        source = "html"

    # ------------------------------------------------------------------
    # Shop nicht erreichbar
    # ------------------------------------------------------------------

    if candidates is None:

        print(
            f"[skip] {name}: "
            f"nicht erreichbar"
        )

        return

    # ------------------------------------------------------------------
    # Aktuelle Treffer
    # ------------------------------------------------------------------

    current = {
        candidate["key"]: candidate
        for candidate in candidates
    }

    key = shop_key(url)

    previous = state.get(key)

    # ------------------------------------------------------------------
    # Erste Ausführung = Baseline
    # ------------------------------------------------------------------

    if previous is None:

        state[key] = sorted(
            current.keys()
        )

        print(
            f"[base] {name} "
            f"({source}): "
            f"{len(current)} Treffer "
            f"als Baseline gespeichert"
        )

        return

    previous_set = set(
        previous
    )

    new_keys = [
        item_key
        for item_key in current
        if item_key not in previous_set
    ]

    print(
        f"[check] {name} "
        f"({source}): "
        f"{len(current)} Treffer, "
        f"{len(new_keys)} neu"
    )

    # ------------------------------------------------------------------
    # Zu viele neue Treffer
    # ------------------------------------------------------------------

    if len(new_keys) > MAX_ALERTS_PER_SHOP:

        notify(
            webhook,
            (
                f"🟡 **{name}**\n"
                f"{len(new_keys)} neue Pokémon-Treffer "
                f"auf einmal.\n\n"
                f"Bitte selber prüfen:\n"
                f"{url}"
            )
        )

    else:

        for item_key in new_keys:

            candidate = current[
                item_key
            ]

            title = candidate.get(
                "title",
                "Unbekanntes Produkt"
            )

            price = candidate.get(
                "price"
            )

            available = candidate.get(
                "available"
            )

            # ------------------------------------------------------
            # Preis
            # ------------------------------------------------------

            if price is not None:

                price_text = (
                    f"CHF {price:.2f}"
                )

            else:

                price_text = (
                    "Preis nicht erkannt"
                )

            # ------------------------------------------------------
            # Verfügbarkeit
            # ------------------------------------------------------

            if available is True:

                availability_text = (
                    "🟢 AUF LAGER / VERFÜGBAR"
                )

            elif available is False:

                availability_text = (
                    "🔴 NICHT VERFÜGBAR"
                )

            else:

                availability_text = (
                    "🟡 VERFÜGBARKEIT NICHT "
                    "EINDEUTIG ERKANNT"
                )

            # ------------------------------------------------------
            # Discord Alert
            # ------------------------------------------------------

            notify(
                webhook,
                (
                    f"🚨 **POKÉMON 30TH – NEUER TREFFER**\n\n"
                    f"**Shop:** {name}\n"
                    f"**Produkt:** {title}\n"
                    f"**Preis:** {price_text}\n"
                    f"**Status:** {availability_text}\n\n"
                    f"{candidate['link']}"
                )
            )

    # ------------------------------------------------------------------
    # State aktualisieren
    # ------------------------------------------------------------------

    state[key] = sorted(
        current.keys()
    )


# ===========================================================================
# MAIN RUN
# ===========================================================================

def run_once(webhook):
    """
    Führt einen kompletten Durchlauf aus.
    """

    config = load_json(
        CONFIG_FILE,
        None
    )

    if not config:

        print(
            "[error] targets.json fehlt "
            "oder ist leer."
        )

        sys.exit(1)

    if "shops" not in config:

        print(
            "[error] targets.json enthält "
            "keinen 'shops'-Eintrag."
        )

        sys.exit(1)

    shops = config["shops"]

    if not isinstance(
        shops,
        list
    ):

        print(
            "[error] 'shops' muss eine "
            "Liste sein."
        )

        sys.exit(1)

    state = load_json(
        STATE_FILE,
        {}
    )

    print(
        f"[info] Prüfe {len(shops)} Shops..."
    )

    for shop in shops:

        if not isinstance(
            shop,
            dict
        ):
            continue

        if not shop.get("name"):
            continue

        if not shop.get("url"):
            continue

        try:

            check_shop(
                shop,
                config,
                state,
                webhook
            )

        except Exception as e:

            print(
                f"[error] {shop.get('name')}: "
                f"{type(e).__name__}: {e}"
            )

        # Zufällige Pause
        time.sleep(
            random.uniform(
                MIN_SHOP_DELAY,
                MAX_SHOP_DELAY
            )
        )

    save_json(
        STATE_FILE,
        state
    )

    print(
        "[info] Durchgang abgeschlossen."
    )


# ===========================================================================
# MAIN
# ===========================================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Pokémon Shop Watcher"
        )
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Nur einen Durchgang "
            "ausführen"
        )
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=1800,
        help=(
            "Intervall in Sekunden "
            "(Default: 1800 = 30 Minuten)"
        )
    )

    args = parser.parse_args()

    webhook = os.environ.get(
        "DISCORD_WEBHOOK_URL",
        ""
    ).strip()

    if not webhook:

        print(
            "[warn] DISCORD_WEBHOOK_URL "
            "nicht gesetzt. "
            "Alerts werden nur in der "
            "Konsole ausgegeben."
        )

    # ------------------------------------------------------------------
    # Einmalig
    # ------------------------------------------------------------------

    if args.once:

        run_once(
            webhook
        )

        return

    # ------------------------------------------------------------------
    # Dauerbetrieb
    # ------------------------------------------------------------------

    print(
        f"[info] Dauerbetrieb. "
        f"Alle {args.interval}s. "
        f"Ctrl+C zum Stoppen."
    )

    while True:

        run_once(
            webhook
        )

        # Zufällige zusätzliche Pause,
        # damit nicht immer exakt zur gleichen
        # Sekunde angefragt wird.

        extra_delay = random.uniform(
            0,
            args.interval * 0.2
        )

        time.sleep(
            args.interval
            + extra_delay
        )


# ===========================================================================
# START
# ===========================================================================

if __name__ == "__main__":
    main()
