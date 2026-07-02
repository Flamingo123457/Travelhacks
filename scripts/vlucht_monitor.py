#!/usr/bin/env python3
"""
✈️ Amsterdam Vluchtdeal Monitor — Skiplagged + Kiwi MCP
Gebruikt live MCP servers (geen Travelpayouts token nodig).

Werking:
  1. Skiplagged flex-kalender → goedkoopste vertrekdata per bestemming
  2. Skiplagged round-trip search → exacte retourprijs voor beste data
  3. Kiwi.com als automatische fallback als Skiplagged niets vindt
  4. Telegram melding bij deal of bijna-deal

Setup (1x):
  pip install requests
  # Vul TELEGRAM_TOKEN en TELEGRAM_CHAT_ID in hieronder
  # Geen andere API-sleutels nodig!
"""

import json
import os
import re
import time
from datetime import datetime, timedelta

import requests

# ─── CONFIGURATIE ─────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "JOUW_BOT_TOKEN_HIER")   # via @BotFather of fly secrets
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "JOUW_CHAT_ID_HIER")      # via @userinfobot of fly secrets

BIJNA_DEAL_MARGE = 25    # €25 boven limiet = bijna-deal melding
CHECK_INTERVAL   = 600   # seconden tussen rondes (10 minuten)
STATE_FILE       = "deals_gevonden.json"
BIJNA_FILE       = "bijna_deals_gevonden.json"

# Hoeveel maanden vooruit zoeken
MAANDEN_VOORUIT  = 6
# Per maand: probeer deze dag-offsets (1 = begin maand, 15 = midden)
DAGEN_PER_MAAND  = [1, 8, 15, 22]

# ─── MCP ENDPOINTS ────────────────────────────────────────────────────────────

SKIPLAGGED_MCP = "https://mcp.skiplagged.com/mcp"
KIWI_MCP       = "https://mcp.kiwi.com"

# ─── VERTREKPUNTEN ────────────────────────────────────────────────────────────
# AMS = Schiphol | RTM = Rotterdam (15 min) | EIN = Eindhoven Ryanair (90 min)

VERTREK_LUCHTHAVENS = ["AMS", "RTM", "EIN"]

# ─── BESTEMMINGEN ─────────────────────────────────────────────────────────────

BESTEMMINGEN = [
    # ── Lange vluchten ─────────────────────────────────────────────────────────
    {"naam": "Bali",            "iata": "DPS", "max_prijs": 500, "min_nachten": 5,  "max_nachten": 30, "emoji": "🌴"},
    {"naam": "Jakarta",         "iata": "CGK", "max_prijs": 400, "min_nachten": 5,  "max_nachten": 30, "emoji": "🏙️"},
    {"naam": "Kaapstad",        "iata": "CPT", "max_prijs": 500, "min_nachten": 5,  "max_nachten": 28, "emoji": "🦁"},
    {"naam": "Los Angeles",     "iata": "LAX", "max_prijs": 500, "min_nachten": 7,  "max_nachten": 28, "emoji": "🎬"},
    {"naam": "Miami",           "iata": "MIA", "max_prijs": 400, "min_nachten": 7,  "max_nachten": 21, "emoji": "🌊"},
    {"naam": "Cancún",          "iata": "CUN", "max_prijs": 500, "min_nachten": 7,  "max_nachten": 21, "emoji": "🇲🇽"},
    {"naam": "Las Vegas",       "iata": "LAS", "max_prijs": 450, "min_nachten": 7,  "max_nachten": 21, "emoji": "🎰"},
    {"naam": "Buenos Aires",    "iata": "EZE", "max_prijs": 500, "min_nachten": 7,  "max_nachten": 28, "emoji": "💃"},
    {"naam": "Tokyo",           "iata": "NRT", "max_prijs": 500, "min_nachten": 7,  "max_nachten": 28, "emoji": "🗼"},
    # ── Korte vluchten: Londen ─────────────────────────────────────────────────
    {"naam": "Londen Heathrow", "iata": "LHR", "max_prijs": 50,  "min_nachten": 1,  "max_nachten": 10, "emoji": "🇬🇧"},
    {"naam": "Londen Gatwick",  "iata": "LGW", "max_prijs": 50,  "min_nachten": 1,  "max_nachten": 10, "emoji": "🎡"},
    {"naam": "Londen Stansted", "iata": "STN", "max_prijs": 40,  "min_nachten": 1,  "max_nachten": 10, "emoji": "✈️"},
    # ── Korte vluchten: Frankrijk ──────────────────────────────────────────────
    {"naam": "Parijs",          "iata": "CDG", "max_prijs": 60,  "min_nachten": 2,  "max_nachten": 7,  "emoji": "🗼"},
    {"naam": "Nice",            "iata": "NCE", "max_prijs": 80,  "min_nachten": 3,  "max_nachten": 10, "emoji": "🌊"},
    {"naam": "Lyon",            "iata": "LYS", "max_prijs": 70,  "min_nachten": 2,  "max_nachten": 7,  "emoji": "🍷"},
    {"naam": "Marseille",       "iata": "MRS", "max_prijs": 70,  "min_nachten": 3,  "max_nachten": 10, "emoji": "⛵"},
    {"naam": "Bordeaux",        "iata": "BOD", "max_prijs": 70,  "min_nachten": 3,  "max_nachten": 7,  "emoji": "🍇"},
    # ── Korte vluchten: België & overig ───────────────────────────────────────
    {"naam": "Brussel",         "iata": "BRU", "max_prijs": 50,  "min_nachten": 1,  "max_nachten": 5,  "emoji": "🇧🇪"},
    {"naam": "Barcelona",       "iata": "BCN", "max_prijs": 70,  "min_nachten": 2,  "max_nachten": 14, "emoji": "🥘"},
    {"naam": "Lissabon",        "iata": "LIS", "max_prijs": 80,  "min_nachten": 3,  "max_nachten": 10, "emoji": "🇵🇹"},
    {"naam": "Rome",            "iata": "FCO", "max_prijs": 80,  "min_nachten": 3,  "max_nachten": 10, "emoji": "🏛️"},
]

# ─── MCP CLIENT ───────────────────────────────────────────────────────────────

def call_mcp(endpoint: str, tool_name: str, arguments: dict, timeout: int = 30) -> dict | None:
    """
    Roep een MCP tool aan via streamable HTTP.
    Probeert eerst JSON response, parset SSE als fallback.
    """
    payload = {
        "jsonrpc": "2.0",
        "id":      "1",
        "method":  "tools/call",
        "params":  {"name": tool_name, "arguments": arguments},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept":        "application/json, text/event-stream",
    }
    try:
        r = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
        content_type = r.headers.get("Content-Type", "")

        # Streamable HTTP: plain JSON response
        if "application/json" in content_type:
            data = r.json()
            return _extract_mcp_content(data)

        # SSE response: parse event stream
        if "text/event-stream" in content_type or r.text.startswith("data:"):
            return _parse_sse(r.text)

        # Fallback: try JSON anyway
        try:
            data = r.json()
            return _extract_mcp_content(data)
        except Exception:
            return None

    except requests.exceptions.Timeout:
        return None
    except Exception as e:
        print(f"[MCP] {tool_name} @ {endpoint}: {e}")
        return None


def _extract_mcp_content(data: dict) -> dict | None:
    """Haal tekst-inhoud uit MCP JSON-RPC response."""
    if "error" in data:
        return None
    result = data.get("result", {})
    for item in result.get("content", []):
        if item.get("type") == "text":
            tekst = item["text"]
            # Probeer JSON te parsen (bijv. Kiwi geeft JSON terug)
            try:
                parsed = json.loads(tekst)
                if isinstance(parsed, (dict, list)):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
            # Markdown tekst (bijv. Skiplagged): geef door als raw
            return {"raw": tekst}
    return None


def _parse_sse(text: str) -> dict | None:
    """Parse Server-Sent Events response, pak laatste data-event."""
    last_data = None
    for line in text.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            if raw and raw != "[DONE]":
                last_data = raw
    if not last_data:
        return None
    try:
        envelope = json.loads(last_data)
        return _extract_mcp_content(envelope)
    except (json.JSONDecodeError, TypeError):
        return None

# ─── PRIJS EXTRACTIE ──────────────────────────────────────────────────────────

# Skiplagged levert prijzen in USD. Ruwe conversiefactor naar EUR.
USD_NAAR_EUR = 0.92


SKIPLAGGED_ERROR_MARKERS = ("failed to fetch", "request failed", "429", "503", "error", "unavailable")
MIN_PRIJS_EUR = 25


def _parse_skiplagged_tekst(text: str) -> float | None:
    """
    Parse Skiplagged markdown-tekst en geef goedkoopste prijs (omgezet naar EUR).
    Skiplagged geeft: '- Price: $186 | ...' of '- Departure: ... | Price: $204'
    Gooit foutmeldingen weg (429-rate-limit, 503, etc.) zodat statuscodes
    niet als prijzen worden geïnterpreteerd.
    """
    if any(m in text.lower() for m in SKIPLAGGED_ERROR_MARKERS):
        return None
    matches = re.findall(r"Price:\s*\$(\d+(?:\.\d+)?)", text)
    if matches:
        prijs = round(min(float(m) for m in matches) * USD_NAAR_EUR, 2)
        return prijs if prijs >= MIN_PRIJS_EUR else None
    return None


def extract_prijs_skiplagged(data: dict | None) -> float | None:
    """Extraheer goedkoopste prijs (in EUR) uit Skiplagged MCP response."""
    if not data:
        return None

    # Structured JSON list
    if isinstance(data, list):
        prijzen = []
        for item in data:
            p = item.get("price") or item.get("totalPrice") or item.get("cost")
            if p:
                try:
                    prijzen.append(float(p) * USD_NAAR_EUR)
                except (TypeError, ValueError):
                    pass
        return min(prijzen) if prijzen else None

    if isinstance(data, dict):
        # Structured dict met price-veld (USD → EUR)
        for key in ("price", "totalPrice", "cheapestPrice", "lowestPrice", "cost"):
            if key in data:
                try:
                    return round(float(data[key]) * USD_NAAR_EUR, 2)
                except (TypeError, ValueError):
                    pass
        # Markdown tekst-response (meest voorkomend bij renderMode=text)
        raw = data.get("raw", "")
        if raw:
            return _parse_skiplagged_tekst(raw)

    return None


def extract_prijs_kiwi(data: dict | None) -> float | None:
    """Extraheer goedkoopste prijs (EUR) uit Kiwi MCP response."""
    if not data:
        return None
    if isinstance(data, dict):
        itins = data.get("itineraries", [])
        if itins:
            prijzen = [float(i["price"]) for i in itins if "price" in i]
            return min(prijzen) if prijzen else None
        # Fallback: parse ruwe tekst
        raw = data.get("raw", "")
        if raw:
            m = re.findall(r'"price":\s*(\d+)', raw)
            if m:
                return min(float(x) for x in m)
    return None

# ─── VLUCHT ZOEKEN ────────────────────────────────────────────────────────────

def gemiddeld_nachten(b: dict) -> int:
    return (b["min_nachten"] + b["max_nachten"]) // 2


def zoek_skiplagged(origin: str, iata: str, vertrek: str, terug: str) -> float | None:
    data = call_mcp(SKIPLAGGED_MCP, "sk_flights_search", {
        "origin": origin, "destination": iata,
        "departureDate": vertrek, "returnDate": terug,
        "maxStops": "one", "sort": "price", "limit": 5, "renderMode": "text",
    })
    return extract_prijs_skiplagged(data)


def zoek_skiplagged_kalender(origin: str, iata: str, vertrek: str, nachten: int) -> float | None:
    terug = (datetime.strptime(vertrek, "%Y-%m-%d") + timedelta(days=nachten)).strftime("%Y-%m-%d")
    data = call_mcp(SKIPLAGGED_MCP, "sk_flex_departure_calendar", {
        "origin": origin, "destination": iata,
        "departureDate": vertrek, "returnDate": terug,
        "sort": "price", "renderMode": "text",
    })
    return extract_prijs_skiplagged(data)


def zoek_kiwi(origin: str, iata: str, vertrek: str, terug: str) -> float | None:
    def fmt(d: str) -> str:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m/%Y")
    data = call_mcp(KIWI_MCP, "search-flight", {
        "flyFrom": origin, "flyTo": iata,
        "departureDate": fmt(vertrek), "returnDate": fmt(terug),
        "currency": "EUR",
    })
    return extract_prijs_kiwi(data)


def zoek_beste_prijs(bestemming: dict) -> tuple[float | None, str, str, str]:
    """
    Zoek goedkoopste retourvlucht over alle vertrekpunten × datums.
    Geeft (prijs, vertrek_datum, terug_datum, bron) terug.
    """
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

            for origin in VERTREK_LUCHTHAVENS:
                prijs = zoek_skiplagged_kalender(origin, iata, vertrek, nachten)
                bron  = f"Skiplagged kalender ({origin})"

                if prijs is None:
                    prijs = zoek_skiplagged(origin, iata, vertrek, terug)
                    bron  = f"Skiplagged ({origin})"

                if prijs is None:
                    prijs = zoek_kiwi(origin, iata, vertrek, terug)
                    bron  = f"Kiwi ({origin})"

                if prijs and (beste_prijs is None or prijs < beste_prijs):
                    beste_prijs, beste_vertrek, beste_terug, beste_bron = prijs, vertrek, terug, bron

                time.sleep(0.5)

    return beste_prijs, beste_vertrek, beste_terug, beste_bron

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID, "text": msg,
            "parse_mode": "HTML", "disable_web_page_preview": False,
        }, timeout=10)
        if not r.ok:
            print(f"[{nu()}] ❌ Telegram fout {r.status_code}: {r.text}")
            # Probeer opnieuw zonder HTML-opmaak
            r2 = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": re.sub(r"<[^>]+>", "", msg),
            }, timeout=10)
            if r2.ok:
                print(f"[{nu()}] ✅ Telegram verstuurd (zonder opmaak)")
            else:
                print(f"[{nu()}] ❌ Telegram ook zonder opmaak mislukt: {r2.text}")
        else:
            print(f"[{nu()}] ✅ Telegram verstuurd")
    except Exception as e:
        print(f"[{nu()}] ❌ Telegram fout: {e}")


def nu() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_datum(d: str) -> str:
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b %Y")
    except ValueError:
        return d


def _origin_uit_bron(bron: str) -> str:
    for o in VERTREK_LUCHTHAVENS:
        if f"({o})" in bron:
            return o
    return "AMS"


def skyscanner_link(iata: str, vertrek: str, terug: str, bron: str = "") -> str:
    v, t   = vertrek.replace("-", ""), terug.replace("-", "")
    origin = _origin_uit_bron(bron).lower()
    return f"https://www.skyscanner.nl/transport/vluchten/{origin}/{iata.lower()}/{v}/{t}/?adults=1&cabinclass=economy"


def google_flights_link(iata: str, vertrek: str, terug: str, bron: str = "") -> str:
    origin = _origin_uit_bron(bron)
    return f"https://www.google.com/travel/flights/search?q=vluchten+{origin}+{iata}+{vertrek}+retour+{terug}&hl=nl&gl=NL"


def kiwi_link(iata: str, vertrek: str, terug: str, bron: str = "") -> str:
    origin = _origin_uit_bron(bron).lower()
    namen  = {"ams": "amsterdam/airport-ams", "rtm": "rotterdam/airport-rtm", "ein": "eindhoven/airport-ein"}
    loc    = namen.get(origin, f"amsterdam/airport-{origin}")
    return f"https://www.kiwi.com/nl/search/results/{loc}/{iata}/{vertrek}/{terug}/2?adults=1&currency=EUR"


def format_bericht(prijs: float, vertrek: str, terug: str, bron: str,
                   bestemming: dict, is_bijna: bool) -> str:
    naam    = bestemming["naam"]
    emoji   = bestemming["emoji"]
    iata    = bestemming["iata"]
    origin  = _origin_uit_bron(bron)
    nachten = (datetime.strptime(terug, "%Y-%m-%d") -
               datetime.strptime(vertrek, "%Y-%m-%d")).days

    sky = skyscanner_link(iata, vertrek, terug, bron)
    gfl = google_flights_link(iata, vertrek, terug, bron)
    kwi = kiwi_link(iata, vertrek, terug, bron)

    if is_bijna:
        diff   = round(prijs) - bestemming["max_prijs"]
        header = (
            f"⚠️ <b>BIJNA DEAL — {emoji} {naam}</b>\n"
            f"<i>Nog €{diff} boven jouw limiet van €{bestemming['max_prijs']}</i>"
        )
    else:
        header = f"🚨 <b>DEAL GEVONDEN — {emoji} {naam}</b>"

    return (
        f"{header}\n\n"
        f"💶 <b>€{round(prijs)} p.p. retour</b>\n\n"
        f"✈️  <b>Vertrek {origin}</b>    📅 {fmt_datum(vertrek)}\n"
        f"🔄 <b>Terug naar {origin}</b>  📅 {fmt_datum(terug)}\n"
        f"🏝️  <b>Verblijf:</b> {nachten} nachten\n"
        f"📡 <b>Bron:</b> {bron}\n\n"
        f"🔍 <a href=\"{sky}\">👉 Skyscanner</a>  "
        f"<a href=\"{gfl}\">Google Flights</a>  "
        f"<a href=\"{kwi}\">Kiwi.com</a>\n\n"
        f"<i>Limiet: ≤€{bestemming['max_prijs']} | "
        f"{bestemming['min_nachten']}–{bestemming['max_nachten']} nachten</i>\n"
        f"🕐 {nu()}"
    )


def deal_id(iata: str, vertrek: str, prijs: float) -> str:
    return f"{iata}-{vertrek[:7]}-{round(prijs)}"

# ─── STATE ────────────────────────────────────────────────────────────────────

def load_set(bestand: str) -> set:
    if os.path.exists(bestand):
        with open(bestand) as f:
            return set(json.load(f))
    return set()


def save_set(data: set, bestand: str):
    with open(bestand, "w") as f:
        json.dump(list(data), f)

# ─── STARTMELDING ─────────────────────────────────────────────────────────────

def stuur_startmelding():
    regels = "\n".join(
        f"{b['emoji']} <b>{b['naam']}</b>  ≤€{b['max_prijs']} | "
        f"{b['min_nachten']}–{b['max_nachten']} nachten"
        for b in BESTEMMINGEN
    )
    send_telegram(
        f"✈️ <b>Vluchtdeal Monitor gestart</b>\n\n"
        f"📡 <b>Bronnen:</b> Skiplagged + Kiwi (live MCP)\n\n"
        f"<b>{len(BESTEMMINGEN)} bestemmingen:</b>\n\n"
        f"{regels}\n\n"
        f"⚠️ Bijna-deal bij: limiet +€{BIJNA_DEAL_MARGE}\n"
        f"🔄 Check elke {CHECK_INTERVAL // 60} minuten\n"
        f"🕐 {nu()}"
    )

# ─── HOOFDLOOP ────────────────────────────────────────────────────────────────

def main():
    print(f"""
✈️  AMSTERDAM VLUCHTDEAL MONITOR — Skiplagged + Kiwi MCP
{'=' * 60}
  Bronnen  : Skiplagged (live) + Kiwi (fallback)
  Interval : elke {CHECK_INTERVAL // 60} minuten
{'=' * 60}""")
    for b in BESTEMMINGEN:
        print(f"  {b['emoji']} {b['naam']:22} ≤€{b['max_prijs']:>3} | "
              f"{b['min_nachten']}–{b['max_nachten']} nachten")
    print(f"{'=' * 60}\n")

    if "JOUW" in TELEGRAM_TOKEN:
        print("❌  Vul TELEGRAM_TOKEN en TELEGRAM_CHAT_ID in bovenaan het script.")
        print("    Bot aanmaken: zoek @BotFather op Telegram.")
        print("    Chat-ID vinden: zoek @userinfobot op Telegram.\n")
        return

    gevonden       = load_set(STATE_FILE)
    bijna_gevonden = load_set(BIJNA_FILE)

    stuur_startmelding()

    while True:
        print(f"\n[{nu()}] 🔍 Nieuwe ronde ({len(BESTEMMINGEN)} bestemmingen)...")
        n_deals = 0
        n_bijna = 0

        for b in BESTEMMINGEN:
            naam = b["naam"]
            iata = b["iata"]
            print(f"[{nu()}]   → {naam} ({iata})...", end=" ", flush=True)

            prijs, vertrek, terug, bron = zoek_beste_prijs(b)

            if prijs is None:
                print("geen resultaat")
                continue

            print(f"€{round(prijs)} ({bron})")

            fid       = deal_id(iata, vertrek, prijs)
            is_deal   = prijs <= b["max_prijs"]
            is_bijna  = (not is_deal) and prijs <= b["max_prijs"] + BIJNA_DEAL_MARGE

            if is_deal and fid not in gevonden:
                print(f"[{nu()}] 🎉 DEAL: {naam} €{round(prijs)}!")
                send_telegram(format_bericht(prijs, vertrek, terug, bron, b, False))
                gevonden.add(fid)
                n_deals += 1
                time.sleep(1)

            elif is_bijna and f"bijna-{fid}" not in bijna_gevonden:
                diff = round(prijs) - b["max_prijs"]
                print(f"[{nu()}] ⚠️  BIJNA: {naam} €{round(prijs)} (+€{diff})")
                send_telegram(format_bericht(prijs, vertrek, terug, bron, b, True))
                bijna_gevonden.add(f"bijna-{fid}")
                n_bijna += 1
                time.sleep(1)

        save_set(gevonden,       STATE_FILE)
        save_set(bijna_gevonden, BIJNA_FILE)

        print(f"[{nu()}] 📊 Ronde klaar — {n_deals} deals, {n_bijna} bijna-deals")
        print(f"[{nu()}] ⏳ Volgende ronde over {CHECK_INTERVAL // 60} min...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n[{nu()}] 👋 Bot gestopt.")
