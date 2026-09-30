#!/usr/bin/env python3
"""
pokecenter_watcher.py — Drop-Modus für pokemoncenter.com.

Prüft in kurzen Abständen (Default ~75 s) die in targets.json unter
"pokemon_center" konfigurierten Seiten und meldet via Discord:

- 🚨 neue Produkte, die zu den Keywords passen (z.B. Elite Trainer Box)
- 🚦 wenn die Warteschlange / Waiting Room aktiv wird (= Drop läuft)
- ⚠️ einmalig, wenn der Bot blockiert wird (Imperva/Incapsula)

State liegt separat in pc_state.json, damit es keine Konflikte mit dem
normalen Watcher (state.json) gibt.

Beispiele:
    python pokecenter_watcher.py --once
    python pokecenter_watcher.py --until 18:15 --interval 75   # UTC
"""

import argparse
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from poke_watcher import (
    CONFIG_FILE,
    HEADERS,
    REQUEST_TIMEOUT,
    load_json,
    normalize_text,
    notify,
    save_json,
)


HERE = Path(__file__).resolve().parent

PC_STATE_FILE = HERE / "pc_state.json"

BASE = "https://www.pokemoncenter.com"

# Produktpfade, z.B. /product/100-10653/pokemon-tcg-...-elite-trainer-box
# oder /en-gb/product/...
PRODUCT_PATH_RE = re.compile(
    r"(?:/[a-z]{2}-[a-z]{2})?/product/[0-9A-Za-z-]+/[a-z0-9-]+"
)

QUEUE_TERMS = [
    "waiting room",
    "you are now in line",
    "you're in line",
    "virtual queue",
    "high volume of traffic",
    "experiencing high traffic",
]

# Texte einer Imperva-Sperrseite. Achtung: "_Incapsula_Resource" steckt
# auch in normalen Seiten und zählt nur bei sehr kurzen Antworten.
BLOCK_TERMS = [
    "incapsula incident",
    "request unsuccessful",
]

# Kürzer als das ist eher eine Sperr-/Challenge-Seite als ein Shop
CHALLENGE_MAX_BYTES = 15000


def region_home(url):
    """
    Startseite der Region (US, /en-gb, /en-ca).

    In die Warteschlange soll man nur über die Startseite,
    nie über einen Produkt- oder Suchlink.
    """

    match = re.search(r"pokemoncenter\.com(/[a-z]{2}-[a-z]{2})", url)

    return BASE + (match.group(1) if match else "/")


# ---------------------------------------------------------------------------
# Seite abrufen und einordnen
# ---------------------------------------------------------------------------

def fetch_page(url):
    """
    Gibt (status, html) zurück.

    status: "ok", "queue", "blocked" oder "error"
    """

    try:

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT
        )

    except requests.RequestException as e:

        print(f"[warn] {url}: {e}")

        return "error", ""

    html = response.text or ""
    low = html.lower()

    print(
        f"[http] {url}: HTTP {response.status_code}, "
        f"{len(html)} Zeichen"
    )

    # Warteschlange zuerst prüfen: Die Queue-Seite kann auch
    # Imperva-Skripte enthalten.
    if any(term in low for term in QUEUE_TERMS):
        return "queue", html

    if (
        response.status_code in (401, 403, 429)
        or any(term in low for term in BLOCK_TERMS)
        or (
            len(html) < CHALLENGE_MAX_BYTES
            and "_incapsula_resource" in low
        )
    ):
        print(f"[blocked] {url}: {html[:300]!r}")

        return "blocked", html

    if response.status_code != 200:
        print(f"[warn] {url}: HTTP {response.status_code}")
        return "error", html

    return "ok", html


def slug_title(path):
    """
    Macht aus dem Slug einen lesbaren Titel (Fallback).
    """

    slug = path.rstrip("/").rsplit("/", 1)[-1]

    return slug.replace("-", " ").strip()


def extract_products(html, keywords, exclude):
    """
    Sucht Produktlinks in HTML und eingebettetem JSON.

    Gibt {pfad: titel} zurück.
    """

    found = {}

    soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a", href=True):

        match = PRODUCT_PATH_RE.search(a["href"])

        if not match:
            continue

        title = a.get_text(" ", strip=True)

        if not title:

            image = a.find("img")

            if image and image.get("alt"):
                title = image["alt"].strip()

        found.setdefault(match.group(0), title)

    # Fallback: Pfade aus Skript-/JSON-Daten (Client-Rendering)
    for match in PRODUCT_PATH_RE.finditer(html):
        found.setdefault(match.group(0), "")

    out = {}

    for path, title in found.items():

        title = title or slug_title(path)

        # Titel und Slug zusammen prüfen, weil der Linktext
        # manchmal gekürzt ist.
        haystack = normalize_text(
            title + " " + slug_title(path)
        )

        if not any(
            normalize_text(k) in haystack
            for k in keywords
        ):
            continue

        if any(
            normalize_text(x) in haystack
            for x in exclude
        ):
            continue

        out[path] = title[:180]

    return out


# ---------------------------------------------------------------------------
# Ein Durchgang
# ---------------------------------------------------------------------------

def check_pages(cfg, state, webhook):

    keywords = cfg.get("keywords", [])
    exclude = cfg.get("exclude", [])

    # Schon gemeldete Produkte (über alle Seiten), gegen Doppel-Alerts
    alerted = set(state.get("_alerted", []))

    for page in cfg.get("pages", []):

        name = page.get("name") or page.get("url")
        url = page.get("url")

        if not url:
            continue

        entry = state.setdefault(
            url,
            {"items": None, "status": None}
        )

        status, html = fetch_page(url)

        previous_status = entry.get("status")

        # --------------------------------------------------------------
        # Statuswechsel melden
        # --------------------------------------------------------------

        if status == "queue" and previous_status != "queue":

            notify(
                webhook,
                (
                    f"🚦 **POKÉMON CENTER – WARTESCHLANGE AKTIV**\n\n"
                    f"**Seite:** {name}\n"
                    f"Der Drop läuft wahrscheinlich gerade!\n"
                    f"Nur über die Startseite rein "
                    f"(nie über einen Produktlink):\n"
                    f"{region_home(url)}"
                )
            )

        elif status == "blocked" and previous_status != "blocked":

            notify(
                webhook,
                (
                    f"⚠️ **Pokémon Center blockiert den Bot**\n\n"
                    f"**Seite:** {name}\n"
                    f"Ich sehe gerade nichts – bitte selber "
                    f"im Auge behalten:\n{url}"
                )
            )

        elif (
            status == "ok"
            and previous_status in ("queue", "blocked")
        ):

            print(f"[info] {name}: wieder erreichbar")

        if status != "error":
            entry["status"] = status

        print(f"[pc] {name}: {status}")

        if status != "ok":
            continue

        # --------------------------------------------------------------
        # Produkte vergleichen
        # --------------------------------------------------------------

        current = extract_products(html, keywords, exclude)

        previous_items = entry.get("items")

        if previous_items is None:

            entry["items"] = sorted(current)

            print(
                f"[base] {name}: {len(current)} Treffer "
                f"als Baseline gespeichert"
            )

            continue

        new_paths = [
            p for p in current
            if p not in set(previous_items)
        ]

        print(
            f"[check] {name}: {len(current)} Treffer, "
            f"{len(new_paths)} neu"
        )

        for path in new_paths[:10]:

            if path in alerted:
                continue

            alerted.add(path)

            notify(
                webhook,
                (
                    f"🚨 **POKÉMON CENTER – NEUER TREFFER**\n\n"
                    f"**Seite:** {name}\n"
                    f"**Produkt:** {current[path]}\n\n"
                    f"{urljoin(BASE, path)}"
                )
            )

        # Bekannte Produkte behalten, damit ein kurz
        # verschwundenes Produkt nicht erneut gemeldet wird.
        entry["items"] = sorted(
            set(previous_items) | set(current)
        )

    state["_alerted"] = sorted(alerted)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def parse_until(value):
    """
    "HH:MM" (UTC, heute) → datetime.
    """

    hour, minute = (int(x) for x in value.split(":"))

    now = datetime.now(timezone.utc)

    return now.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0
    )


def main():

    parser = argparse.ArgumentParser(
        description="Pokémon Center Drop-Watcher"
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Nur einen Durchgang ausführen"
    )

    parser.add_argument(
        "--until",
        default="18:15",
        help="Bis wann (UTC, HH:MM) geprüft wird (Default: 18:15)"
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=75,
        help="Sekunden zwischen den Prüfungen (Default: 75)"
    )

    args = parser.parse_args()

    config = load_json(CONFIG_FILE, {}) or {}

    cfg = config.get("pokemon_center")

    if not cfg or not cfg.get("enabled", True):

        print("[info] pokemon_center nicht konfiguriert/aktiv.")

        return

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    if not webhook:
        print("[warn] DISCORD_WEBHOOK_URL nicht gesetzt.")

    state = load_json(PC_STATE_FILE, {})

    if args.once:

        check_pages(cfg, state, webhook)
        save_json(PC_STATE_FILE, state)

        return

    until = parse_until(args.until)

    if datetime.now(timezone.utc) >= until:

        print(f"[info] Schon nach {args.until} UTC – nichts zu tun.")

        return

    print(
        f"[info] Drop-Modus bis {args.until} UTC, "
        f"alle ~{args.interval}s."
    )

    while datetime.now(timezone.utc) < until:

        try:
            check_pages(cfg, state, webhook)

        except Exception as e:
            print(f"[error] {type(e).__name__}: {e}")

        save_json(PC_STATE_FILE, state)

        time.sleep(
            args.interval
            + random.uniform(0, args.interval * 0.3)
        )

    print("[info] Drop-Modus beendet.")


if __name__ == "__main__":
    sys.exit(main())
