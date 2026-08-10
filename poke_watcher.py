#!/usr/bin/env python3
"""
poke_watcher.py — Schwiizer Pokémon-Shop-Watcher (Shopify-aware).

Meldet, wenn bi konfigurierte Shops NEUI Produkt uftauchid, wo zu de
Stichwörter passid, verfüegbar sind und under em max_price bliibid.

- Shopify-Shops: liest /products.json (Titel, Preis, Verfügbarkeit sufer).
- Andri Shops: Fallback uf HTML, sucht neui passendi Produkt-Links.
- Erste Lauf pro Shop = Baseline (kei Alert), damit's kei Spam git.
  Alerts chömed erst bi spöter neu uftauchende Produkt.

Setup:
    pip install requests beautifulsoup4
    python poke_watcher.py --once     # ei Durchgang (GitHub Actions)
    python poke_watcher.py            # Dauerlauf

Env:
    DISCORD_WEBHOOK_URL   Webhook für Alerts

Konfig: targets.json (siehe Beispiel im Repo).
"""

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

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

MAX_ALERTS_PER_SHOP = 15   # Schutz gäge Spam bi grosse Ändrige


# ---------- Hilfsfunktione ----------------------------------------------------

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[warn] {path.name} kaputt, nimm Default")
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def shop_key(url):
    return urlparse(url).netloc.lower().replace("www.", "")


def title_matches(title, keywords, exclude):
    t = (title or "").lower()
    if not any(k.lower() in t for k in keywords):
        return False
    if any(x.lower() in t for x in exclude):
        return False
    return True


def notify(webhook, text):
    if not webhook:
        print("[alert] (kei Webhook) " + text)
        return
    try:
        r = requests.post(webhook, json={"content": text[:1900]}, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[error] Discord-Notify gschiterat: {e}")
    time.sleep(0.5)


# ---------- Shopify -----------------------------------------------------------

def fetch_shopify(base):
    """Git Liste vo Produkt zrugg, oder None wenn's kei Shopify isch."""
    products = []
    for page in range(1, 6):  # max 5 Site à 250 = 1250 Produkt
        url = urljoin(base, f"/products.json?limit=250&page={page}")
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        if not isinstance(data, dict) or "products" not in data:
            return None
        batch = data["products"]
        products.extend(batch)
        if len(batch) < 250:
            break
        time.sleep(1)
    return products


def shopify_candidates(base, products, keywords, exclude, max_price):
    out = []
    for p in products:
        title = p.get("title", "")
        if not title_matches(title, keywords, exclude):
            continue
        variants = p.get("variants", []) or []
        available = any(v.get("available") for v in variants)
        if not available:
            continue
        prices = []
        for v in variants:
            try:
                prices.append(float(v.get("price")))
            except (TypeError, ValueError):
                pass
        price = min(prices) if prices else None
        if max_price is not None and price is not None and price > max_price:
            continue
        out.append({
            "key": p.get("handle", title),
            "title": title,
            "price": price,
            "link": urljoin(base, "/products/" + p.get("handle", "")),
        })
    return out


# ---------- HTML-Fallback -----------------------------------------------------

def html_candidates(url, keywords, exclude):
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[error] HTML {url}: {e}")
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not title_matches(text, keywords, exclude):
            continue
        href = urljoin(url, a["href"])
        if href in seen:
            continue
        seen.add(href)
        out.append({"key": href, "title": text[:120], "price": None, "link": href})
    return out


# ---------- Ei Shop prüefe ----------------------------------------------------

def check_shop(shop, cfg, state, webhook):
    name = shop["name"]
    url = shop["url"]
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    kw = cfg["keywords"]
    ex = cfg.get("exclude", [])
    max_price = cfg.get("max_price")

    products = fetch_shopify(base)
    if products is not None:
        cands = shopify_candidates(base, products, kw, ex, max_price)
        source = "shopify"
    else:
        cands = html_candidates(url, kw, ex)
        source = "html"

    if cands is None:
        print(f"[skip] {name}: nöd erreichbar")
        return

    key = shop_key(url)
    current = {c["key"]: c for c in cands}
    prev = state.get(key)

    if prev is None:
        state[key] = sorted(current.keys())
        print(f"[base] {name} ({source}): {len(current)} Treffer als Baseline gmerkt")
        return

    new_keys = [k for k in current if k not in set(prev)]
    print(f"[check] {name} ({source}): {len(current)} Treffer, {len(new_keys)} neu")

    if len(new_keys) > MAX_ALERTS_PER_SHOP:
        notify(webhook, f"🟡 **{name}**: {len(new_keys)} neui Treffer uf eimal — "
                        f"bitte selber luege:\n{url}")
    else:
        for k in new_keys:
            c = current[k]
            price = f"CHF {c['price']:.2f}" if c["price"] is not None else "Preis?"
            extra = "" if source == "shopify" else "  (Verfügbarkeit/Preis n\u00f6d gpr\u00fceft)"
            notify(webhook, f"🟢 **NEU – {name}**{extra}\n{c['title']}\n{price}\n{c['link']}")

    state[key] = sorted(current.keys())


# ---------- Main --------------------------------------------------------------

def run_once(webhook):
    cfg = load_json(CONFIG_FILE, None)
    if not cfg or "shops" not in cfg:
        print("[error] targets.json fäält oder het kei 'shops'")
        sys.exit(1)
    state = load_json(STATE_FILE, {})
    for shop in cfg["shops"]:
        try:
            check_shop(shop, cfg, state, webhook)
        except Exception as e:
            print(f"[error] {shop.get('name')}: {e}")
        time.sleep(random.uniform(1.0, 2.5))
    save_json(STATE_FILE, state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=1800)
    args = ap.parse_args()

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("[warn] DISCORD_WEBHOOK_URL nöd gsetzt — Alerts nur i de Konsole.")

    if args.once:
        run_once(webhook)
        return
    print(f"[info] Dauerlauf alli {args.interval}s. Ctrl+C zum stoppe.")
    while True:
        run_once(webhook)
        time.sleep(args.interval + random.uniform(0, args.interval * 0.2))


if __name__ == "__main__":
    main()
