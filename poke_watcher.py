#!/usr/bin/env python3
"""
poke_watcher.py — Verfüegbarkeits-Monitor für Schwiizer Pokémon-Shops.

Prüeft konfigurierti Produkt-URLs uf Verfüegbarkeit + fairs Preisniveau
und schickt bi Transition "usverkauft -> a Lager" en Discord-Alert.

Setup:
    pip install requests beautifulsoup4
    python poke_watcher.py --once        # ei Durchgang (für GitHub Actions cron)
    python poke_watcher.py                # Dauerlauf mit Poll-Intervall

Env-Variable:
    DISCORD_WEBHOOK_URL   Webhook für Alerts (Pflicht für Notifications)

Konfig: siehe targets.json näbedra (Beispiel wird automatisch aagleit, wenn's fäält).
"""

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "targets.json"
STATE_FILE = HERE / "state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
}

# Preis wie "CHF 149.90", "149.-", "1'299.00" robust erkenne
PRICE_RE = re.compile(r"(\d[\d'’.\s]*[\d])(?:\.-|\.\d{2})?")


# ---------- Beispiel-Konfig ----------------------------------------------------

EXAMPLE_TARGETS = [
    {
        "name": "BEISPIEL – World of Games 30th ETB",
        "url": "https://www.worldofgames.ch/de/DEIN-PRODUKT-LINK",
        "max_price": 60,
        # Preis: entweder CSS-Selector ODER weglah -> Regex uf ganze Site
        "price_selector": None,
        # Stock-Regle. type isch eis vo:
        #   text_present  -> a Lager wenn "value" im Text vorchunnt
        #   text_absent   -> a Lager wenn "value" NÖD vorchunnt
        #   css_present   -> a Lager wenn Selector existiert
        #   css_absent    -> a Lager wenn Selector NÖD existiert
        "stock_rule": {"type": "text_absent", "value": "ausverkauft"},
        # Alert au wenn a Lager aber über max_price? (Scalper-Warnig)
        "alert_over_price": False,
    },
]


# ---------- Hilfsfunktione -----------------------------------------------------

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[warn] {path.name} isch kaputt, nimm Default")
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_config():
    if not CONFIG_FILE.exists():
        save_json(CONFIG_FILE, EXAMPLE_TARGETS)
        print(f"[info] {CONFIG_FILE.name} aagleit — bitte mit dine echte Links füelle.")
        sys.exit(0)


def notify(webhook, text):
    if not webhook:
        print("[alert] (kei Webhook gsetzt) " + text)
        return
    try:
        r = requests.post(webhook, json={"content": text}, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[error] Discord-Notify gschiterat: {e}")


def parse_price(soup, target):
    sel = target.get("price_selector")
    text = None
    if sel:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(" ", strip=True)
    if text is None:
        # Fallback: erschte plausibel Preis irgendwo im Text
        text = soup.get_text(" ", strip=True)
    m = PRICE_RE.search(text)
    if not m:
        return None
    raw = m.group(1)
    cleaned = raw.replace("'", "").replace("’", "").replace(" ", "").replace(",", ".")
    # Falls mehreri Punkt (Tuusiger), nur letschte als Dezimal behalte
    if cleaned.count(".") > 1:
        parts = cleaned.split(".")
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(cleaned)
    except ValueError:
        return None


def is_in_stock(soup, rule):
    rtype = rule.get("type")
    val = rule.get("value", "")
    page_text = soup.get_text(" ", strip=True).lower()
    if rtype == "text_present":
        return val.lower() in page_text
    if rtype == "text_absent":
        return val.lower() not in page_text
    if rtype == "css_present":
        return soup.select_one(val) is not None
    if rtype == "css_absent":
        return soup.select_one(val) is None
    raise ValueError(f"Unbekannti stock_rule: {rtype}")


def check_target(target, state, webhook):
    name = target["name"]
    url = target["url"]
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[error] {name}: {e}")
        return

    soup = BeautifulSoup(r.text, "html.parser")
    in_stock = is_in_stock(soup, target["stock_rule"])
    price = parse_price(soup, target)
    max_price = target.get("max_price")
    over_price = (max_price is not None and price is not None and price > max_price)

    prev = state.get(name, {}).get("in_stock", False)
    price_str = f"CHF {price:.2f}" if price is not None else "Preis unklar"
    print(f"[check] {name}: stock={in_stock} {price_str} (vorher stock={prev})")

    # Alert nur bi Transition usverkauft -> a Lager
    if in_stock and not prev:
        if over_price and not target.get("alert_over_price", False):
            print(f"[skip]  {name} a Lager aber über Limit ({price_str} > {max_price})")
        else:
            flag = " ⚠️ ÜBER LIMIT" if over_price else " ✅"
            notify(
                webhook,
                f"🔴 **A LAGER**{flag}\n**{name}**\n{price_str}"
                + (f" (Limit {max_price})" if max_price else "")
                + f"\n{url}",
            )

    state[name] = {"in_stock": in_stock, "price": price}


# ---------- Main ---------------------------------------------------------------

def run_once(webhook):
    targets = load_json(CONFIG_FILE, [])
    state = load_json(STATE_FILE, {})
    for t in targets:
        check_target(t, state, webhook)
        time.sleep(random.uniform(1.5, 4.0))  # höflich, kei Hammering
    save_json(STATE_FILE, state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="Ei Durchgang, denn Ändi")
    ap.add_argument("--interval", type=int, default=600,
                    help="Sekunde zwüsche Durchgäng im Dauerlauf (default 600)")
    args = ap.parse_args()

    ensure_config()
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("[warn] DISCORD_WEBHOOK_URL nöd gsetzt — Alerts nur i de Konsole.")

    if args.once:
        run_once(webhook)
        return

    print(f"[info] Dauerlauf, alli {args.interval}s. Abbruch mit Ctrl+C.")
    while True:
        run_once(webhook)
        jitter = random.uniform(0, args.interval * 0.2)
        time.sleep(args.interval + jitter)


if __name__ == "__main__":
    main()
