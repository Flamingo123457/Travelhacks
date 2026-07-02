#!/usr/bin/env python3
"""
✈️ Amsterdam Vluchtdeal Monitor — één ronde
Bedoeld voor GitHub Actions (cron). Leest TELEGRAM_TOKEN en TELEGRAM_CHAT_ID
uit omgevingsvariabelen zodat je niets hardcoded in je repo hoeft te zetten.

Gebruik:
  TELEGRAM_TOKEN=xxx TELEGRAM_CHAT_ID=yyy python3 scripts/vlucht_monitor_once.py

In GitHub Actions: sla de waarden op als repository secrets en verwijs ernaar
via ${{ secrets.TELEGRAM_TOKEN }} en ${{ secrets.TELEGRAM_CHAT_ID }}.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

import requests

# ─── CONFIGURATIE (via omgevingsvariabelen) ───────────────────────────────────

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("❌  Stel TELEGRAM_TOKEN en TELEGRAM_CHAT_ID in als omgevingsvariabelen.")
    sys.exit(1)

BIJNA_DEAL_MARGE = 25
# State-bestanden (in GitHub Actions: per run opnieuw leeg, dus elke ronde vers)
STATE_FILE = "/tmp/deals_gevonden.json"
BIJNA_FILE = "/tmp/bijna_deals_gevonden.json"

MAANDEN_VOORUIT  = 3          # 3 maanden vooruit (was 6)
DAGEN_PER_MAAND  = [7, 21]   # 2 datums per maand (was 4) → 6 checks per bestemming

# ─── MCP ENDPOINTS ────────────────────────────────────────────────────────────

SKIPLAGGED_MCP = "https://mcp.skiplagged.com/mcp"
KIWI_MCP       = "https://mcp.kiwi.com"

# ─── BESTEMMINGEN ─────────────────────────────────────────────────────────────

BESTEMMINGEN = [
    # ── Lange vluchten ──────────────────────────────────────────────────────
    {"naam": "Bali",           "iata": "DPS", "max_prijs": 500, "min_nachten": 5,  "max_nachten": 30, "emoji": "🌴"},
    {"naam": "Jakarta",        "iata": "CGK", "max_prijs": 400, "min_nachten": 5,  "max_nachten": 30, "emoji": "🏙️"},
    {"naam": "Kaapstad",       "iata": "CPT", "max_prijs": 500, "min_nachten": 5,  "max_nachten": 28, "emoji": "🦁"},
    {"naam": "Los Angeles",    "iata": "LAX", "max_prijs": 500, "min_nachten": 7,  "max_nachten": 28, "emoji": "🎬"},
    {"naam": "Miami",          "iata": "MIA", "max_prijs": 400, "min_nachten": 7,  "max_nachten": 21, "emoji": "🌊"},
    {"naam": "Cancún",         "iata": "CUN", "max_prijs": 500, "min_nachten": 7,  "max_nachten": 21, "emoji": "🇲🇽"},
    {"naam": "Las Vegas",      "iata": "LAS", "max_prijs": 450, "min_nachten": 7,  "max_nachten": 21, "emoji": "🎰"},
    {"naam": "Buenos Aires",   "iata": "EZE", "max_prijs": 500, "min_nachten": 7,  "max_nachten": 28, "emoji": "💃"},
    {"naam": "Tokyo",          "iata": "NRT", "max_prijs": 500, "min_nachten": 7,  "max_nachten": 28, "emoji": "🗼"},
    # ── Korte vluchten ──────────────────────────────────────────────────────
    {"naam": "Londen",         "iata": "LHR", "max_prijs": 50,  "min_nachten": 1,  "max_nachten": 10, "emoji": "🇬🇧"},
    {"naam": "Londen Gatwick", "iata": "LGW", "max_prijs": 50,  "min_nachten": 1,  "max_nachten": 10, "emoji": "🎡"},
    {"naam": "Barcelona",      "iata": "BCN", "max_prijs": 70,  "min_nachten": 2,  "max_nachten": 14, "emoji": "🥘"},
]

# ─── MCP CLIENT ───────────────────────────────────────────────────────────────

USD_NAAR_EUR = 0.92


def call_mcp(endpoint: str, tool_name: str, arguments: dict, timeout: int = 30) -> dict | None:
    payload = {
        "jsonrpc": "2.0", "id": "1", "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    try:
        r = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
        ct = r.headers.get("Content-Type", "")
        if "application/json" in ct:
            return _extract_mcp_content(r.json())
        last = None
        for line in r.text.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                if raw and raw != "[DONE]":
                    last = raw
        if last:
            return _extract_mcp_content(json.loads(last))
        return None
    except requests.exceptions.Timeout:
        return None
    except Exception as e:
        print(f"[MCP] {tool_name}: {e}")
        return None


def _extract_mcp_content(data: dict) -> dict | None:
    if "error" in data:
        return None
    result = data.get("result", {})
    for item in result.get("content", []):
        if item.get("type") == "text":
            tekst = item["text"]
            try:
                parsed = json.loads(tekst)
                if isinstance(parsed, (dict, list)):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
            return {"raw": tekst}
    return None


SKIPLAGGED_ERROR_MARKERS = ("failed to fetch", "request failed", "429", "503", "error", "unavailable")
MIN_PRIJS_EUR = 25  # Elk bedrag lager dan dit is een parse-fout, geen echte vlucht


def _parse_skiplagged_tekst(text: str) -> float | None:
    # Gooi response weg als het een foutmelding is (429, 503, etc.)
    if any(m in text.lower() for m in SKIPLAGGED_ERROR_MARKERS):
        return None
    # Zoek expliciet 'Price: $NNN' patronen (meest betrouwbaar)
    matches = re.findall(r"Price:\s*\$(\d+(?:\.\d+)?)", text)
    if matches:
        prijs = round(min(float(m) for m in matches) * USD_NAAR_EUR, 2)
        return prijs if prijs >= MIN_PRIJS_EUR else None
    return None


def extract_prijs_skiplagged(data: dict | None) -> float | None:
    if not data:
        return None
    if isinstance(data, list):
        prijzen = [float(i.get("price", 0)) * USD_NAAR_EUR for i in data if i.get("price")]
        return min(prijzen) if prijzen else None
    if isinstance(data, dict):
        for key in ("price", "totalPrice", "cheapestPrice", "lowestPrice", "cost"):
            if key in data:
                try:
                    return round(float(data[key]) * USD_NAAR_EUR, 2)
                except (TypeError, ValueError):
                    pass
        raw = data.get("raw", "")
        return _parse_skiplagged_tekst(raw) if raw else None
    return None


def extract_prijs_kiwi(data: dict | None) -> float | None:
    if not data:
        return None
    if isinstance(data, dict):
        itins = data.get("itineraries", [])
        if itins:
            prijzen = [float(i["price"]) for i in itins if "price" in i]
            return min(prijzen) if prijzen else None
        raw = data.get("raw", "")
        if raw:
            m = re.findall(r'"price":\s*(\d+)', raw)
            if m:
                return min(float(x) for x in m)
    return None


# ─── VLUCHT ZOEKEN ────────────────────────────────────────────────────────────

def gemiddeld_nachten(b: dict) -> int:
    return (b["min_nachten"] + b["max_nachten"]) // 2


def zoek_skiplagged_kalender(iata: str, vertrek: str, nachten: int) -> float | None:
    terug = (datetime.strptime(vertrek, "%Y-%m-%d") + timedelta(days=nachten)).strftime("%Y-%m-%d")
    data = call_mcp(SKIPLAGGED_MCP, "sk_flex_departure_calendar", {
        "origin": "AMS", "destination": iata,
        "departureDate": vertrek, "returnDate": terug,
        "sort": "price", "renderMode": "text",
    })
    return extract_prijs_skiplagged(data)


def zoek_skiplagged(iata: str, vertrek: str, terug: str) -> float | None:
    data = call_mcp(SKIPLAGGED_MCP, "sk_flights_search", {
        "origin": "AMS", "destination": iata,
        "departureDate": vertrek, "returnDate": terug,
        "maxStops": "one", "sort": "price", "limit": 5, "renderMode": "text",
    })
    return extract_prijs_skiplagged(data)


def zoek_kiwi(iata: str, vertrek: str, terug: str) -> float | None:
    def fmt(d: str) -> str:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m/%Y")
    data = call_mcp(KIWI_MCP, "search-flight", {
        "flyFrom": "AMS", "flyTo": iata,
        "departureDate": fmt(vertrek), "returnDate": fmt(terug),
        "currency": "EUR",
    })
    return extract_prijs_kiwi(data)


def zoek_beste_prijs(bestemming: dict) -> tuple:
    iata    = bestemming["iata"]
    nachten = gemiddeld_nachten(bestemming)
    nu      = datetime.now()

    beste_prijs, beste_vertrek, beste_terug, beste_bron = None, "", "", ""

    for maand_offset in range(MAANDEN_VOORUIT):
        for dag in DAGEN_PER_MAAND:
            try:
                check = (nu + timedelta(days=maand_offset * 30)).replace(day=dag)
            except ValueError:
                continue
            if check < nu + timedelta(days=3):
                continue

            vertrek = check.strftime("%Y-%m-%d")
            terug   = (check + timedelta(days=nachten)).strftime("%Y-%m-%d")

            prijs = zoek_skiplagged_kalender(iata, vertrek, nachten)
            bron  = "Skiplagged kalender"

            if prijs is None:
                prijs = zoek_skiplagged(iata, vertrek, terug)
                bron  = "Skiplagged"

            if prijs is None:
                prijs = zoek_kiwi(iata, vertrek, terug)
                bron  = "Kiwi"

            if prijs and (beste_prijs is None or prijs < beste_prijs):
                beste_prijs, beste_vertrek, beste_terug, beste_bron = prijs, vertrek, terug, bron

            time.sleep(0.8)

    return beste_prijs, beste_vertrek, beste_terug, beste_bron


# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

def nu() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_datum(d: str) -> str:
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b %Y")
    except ValueError:
        return d


def skyscanner_link(iata, vertrek, terug):
    v, t = vertrek.replace("-", ""), terug.replace("-", "")
    return f"https://www.skyscanner.nl/transport/vluchten/ams/{iata.lower()}/{v}/{t}/?adults=1&cabinclass=economy"


def google_flights_link(iata, vertrek, terug):
    return f"https://www.google.com/travel/flights/search?q=vluchten+Amsterdam+{iata}+{vertrek}+retour+{terug}&hl=nl&gl=NL"


def kiwi_link(iata, vertrek, terug):
    return f"https://www.kiwi.com/nl/search/results/amsterdam/airport-ams/{iata}/{vertrek}/{terug}/2?adults=1&currency=EUR"


def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID, "text": msg,
            "parse_mode": "HTML", "disable_web_page_preview": False,
        }, timeout=10)
        if not r.ok:
            # Log volledige foutmelding van Telegram zodat we kunnen debuggen
            print(f"[{nu()}] ❌ Telegram fout {r.status_code}: {r.text}")
            # Probeer opnieuw zonder HTML-opmaak (HTML-tags kunnen 400 veroorzaken)
            r2 = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": re.sub(r"<[^>]+>", "", msg),  # strip HTML tags
            }, timeout=10)
            if r2.ok:
                print(f"[{nu()}] ✅ Telegram verstuurd (zonder opmaak)")
            else:
                print(f"[{nu()}] ❌ Telegram ook zonder opmaak mislukt: {r2.text}")
        else:
            print(f"[{nu()}] ✅ Telegram verstuurd")
    except Exception as e:
        print(f"[{nu()}] ❌ Telegram fout: {e}")


def format_bericht(prijs, vertrek, terug, bron, bestemming, is_bijna):
    naam    = bestemming["naam"]
    emoji   = bestemming["emoji"]
    iata    = bestemming["iata"]
    nachten = (datetime.strptime(terug, "%Y-%m-%d") - datetime.strptime(vertrek, "%Y-%m-%d")).days
    sky = skyscanner_link(iata, vertrek, terug)
    gfl = google_flights_link(iata, vertrek, terug)
    kwi = kiwi_link(iata, vertrek, terug)
    if is_bijna:
        diff   = round(prijs) - bestemming["max_prijs"]
        header = (f"⚠️ <b>BIJNA DEAL — {emoji} {naam}</b>\n"
                  f"<i>Nog €{diff} boven jouw limiet van €{bestemming['max_prijs']}</i>")
    else:
        header = f"🚨 <b>DEAL GEVONDEN — {emoji} {naam}</b>"
    return (
        f"{header}\n\n"
        f"💶 <b>€{round(prijs)} p.p. retour</b>\n\n"
        f"✈️  <b>Vertrek AMS</b>    📅 {fmt_datum(vertrek)}\n"
        f"🔄 <b>Terug naar AMS</b>  📅 {fmt_datum(terug)}\n"
        f"🏝️  <b>Verblijf:</b> {nachten} nachten\n"
        f"📡 <b>Bron:</b> {bron}\n\n"
        f'🔍 <a href="{sky}">👉 Skyscanner</a>  '
        f'<a href="{gfl}">Google Flights</a>  '
        f'<a href="{kwi}">Kiwi.com</a>\n\n'
        f"<i>Limiet: ≤€{bestemming['max_prijs']} | "
        f"{bestemming['min_nachten']}–{bestemming['max_nachten']} nachten</i>\n"
        f"🕐 {nu()}"
    )


def deal_id(iata, vertrek, prijs):
    return f"{iata}-{vertrek[:7]}-{round(prijs)}"


def load_set(bestand: str) -> set:
    if os.path.exists(bestand):
        with open(bestand) as f:
            return set(json.load(f))
    return set()


def save_set(data: set, bestand: str):
    with open(bestand, "w") as f:
        json.dump(list(data), f)


# ─── HOOFDFUNCTIE (één ronde) ─────────────────────────────────────────────────

def main():
    print(f"[{nu()}] 🔍 Ronde gestart ({len(BESTEMMINGEN)} bestemmingen)...")

    gevonden       = load_set(STATE_FILE)
    bijna_gevonden = load_set(BIJNA_FILE)
    n_deals = n_bijna = 0

    for b in BESTEMMINGEN:
        print(f"[{nu()}]   → {b['naam']} ({b['iata']})...", end=" ", flush=True)
        prijs, vertrek, terug, bron = zoek_beste_prijs(b)

        if prijs is None:
            print("geen resultaat")
            continue

        print(f"€{round(prijs)} ({bron})")
        fid      = deal_id(b["iata"], vertrek, prijs)
        is_deal  = prijs <= b["max_prijs"]
        is_bijna = (not is_deal) and prijs <= b["max_prijs"] + BIJNA_DEAL_MARGE

        if is_deal and fid not in gevonden:
            print(f"[{nu()}] 🎉 DEAL: {b['naam']} €{round(prijs)}!")
            send_telegram(format_bericht(prijs, vertrek, terug, bron, b, False))
            gevonden.add(fid)
            n_deals += 1
            time.sleep(1)
        elif is_bijna and f"bijna-{fid}" not in bijna_gevonden:
            diff = round(prijs) - b["max_prijs"]
            print(f"[{nu()}] ⚠️  BIJNA: {b['naam']} €{round(prijs)} (+€{diff})")
            send_telegram(format_bericht(prijs, vertrek, terug, bron, b, True))
            bijna_gevonden.add(f"bijna-{fid}")
            n_bijna += 1
            time.sleep(1)

    save_set(gevonden,       STATE_FILE)
    save_set(bijna_gevonden, BIJNA_FILE)
    print(f"[{nu()}] ✅ Klaar — {n_deals} deals, {n_bijna} bijna-deals")


if __name__ == "__main__":
    main()
