"""
Market Context Generator — runs daily via GitHub Actions cron.
Fetches live macro data and generates M5 briefings for each active bot.
"""

import os, json, sys, requests
from datetime import datetime, timezone
from urllib.parse import quote
from anthropic import Anthropic

WORKER_URL = os.environ.get("WORKER_URL", "https://trading-upload.nestragues.workers.dev")
client = Anthropic()

# ── Data access ───────────────────────────────────────────────────────────────

def get_data():
    r = requests.get(f"{WORKER_URL}/data", timeout=30)
    r.raise_for_status()
    return r.json()

def put_data(data):
    payload = json.dumps(data, ensure_ascii=True).encode("utf-8")
    r = requests.put(
        f"{WORKER_URL}/data",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=60,
    )
    r.raise_for_status()

# ── External data fetchers ────────────────────────────────────────────────────

def fetch_vix():
    try:
        import yfinance as yf
        hist = yf.Ticker("^VIX").history(period="5d")
        if hist.empty:
            return None
        current = round(float(hist["Close"].iloc[-1]), 2)
        prev = round(float(hist["Close"].iloc[-2]), 2) if len(hist) >= 2 else current
        if current > 30:
            regime = "extreme_fear"
        elif current > 20:
            regime = "elevated"
        elif current > 15:
            regime = "normal"
        else:
            regime = "calm"
        return {"value": current, "prev": prev, "change": round(current - prev, 2), "regime": regime}
    except Exception as e:
        print(f"  [warn] VIX fetch failed: {e}")
        return None

def fetch_fear_greed_crypto():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=2", timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            return None
        cur = data[0]
        return {
            "value": int(cur["value"]),
            "label": cur["value_classification"],
            "prev": int(data[1]["value"]) if len(data) > 1 else None,
        }
    except Exception as e:
        print(f"  [warn] Fear&Greed fetch failed: {e}")
        return None

def fetch_economic_calendar():
    try:
        r = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        events = r.json()
        high = [
            {
                "event": e.get("title", ""),
                "currency": e.get("country", ""),
                "date": e.get("date", ""),
                "time": e.get("time", ""),
                "forecast": e.get("forecast", ""),
                "previous": e.get("previous", ""),
            }
            for e in events
            if e.get("impact") == "High"
        ]
        return high[:12]
    except Exception as e:
        print(f"  [warn] Calendar fetch failed: {e}")
        return []

def fetch_fred():
    api_key = os.environ.get("FRED_API_KEY", "")
    if not api_key:
        print("  [warn] FRED_API_KEY not set — skipping")
        return None
    def get_series(sid):
        url = (
            "https://api.stlouisfed.org/fred/series/observations"
            f"?series_id={sid}&api_key={api_key}&limit=5&sort_order=desc&file_type=json"
        )
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        for obs in r.json().get("observations", []):
            if obs["value"] != ".":
                return {"value": round(float(obs["value"]), 3), "date": obs["date"]}
        return None
    try:
        dxy  = get_series("DTWEXBGS")
        t10  = get_series("DGS10")
        t2   = get_series("DGS2")
        sprd = get_series("T10Y2Y")
        result = {}
        if dxy:  result["dxy"]          = dxy
        if t10:  result["t10y"]         = t10
        if t2:   result["t2y"]          = t2
        if sprd:
            result["yield_spread"] = sprd
            v = sprd["value"]
            if v > 0.5:    result["curve_regime"] = "normal"
            elif v > 0:    result["curve_regime"] = "flat"
            elif v > -0.5: result["curve_regime"] = "mildly_inverted"
            else:          result["curve_regime"] = "inverted"
        return result or None
    except Exception as e:
        print(f"  [warn] FRED fetch failed: {e}")
        return None

def fetch_cot():
    targets = {
        "EURO FX - CHICAGO MERCANTILE EXCHANGE":             "EUR",
        "BRITISH POUND STERLING - CHICAGO MERCANTILE EXCHANGE": "GBP",
        "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE":        "JPY",
        "GOLD - COMMODITY EXCHANGE INC.":                    "XAU",
    }
    base = "https://publicreporting.cftc.gov/resource/jun7-fc8e.json"
    result = {}
    for market_name, label in targets.items():
        try:
            r = requests.get(base, params={
                "$where": f"market_and_exchange_names='{market_name}'",
                "$limit": "1",
                "$order": "report_date_as_yyyy_mm_dd DESC",
            }, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            rows = r.json()
            if not rows:
                continue
            row = rows[0]
            long_nc  = int(float(row.get("noncomm_positions_long_all",  0)))
            short_nc = int(float(row.get("noncomm_positions_short_all", 0)))
            net = long_nc - short_nc
            result[label] = {
                "net":   net,
                "long":  long_nc,
                "short": short_nc,
                "bias":  "bullish" if net > 0 else "bearish",
                "date":  (row.get("report_date_as_yyyy_mm_dd") or "")[:10],
            }
        except Exception as e:
            print(f"  [warn] COT {label} failed: {e}")
    return result or None

def build_macro_snapshot():
    print("  Fetching VIX...")
    vix = fetch_vix()
    print("  Fetching Fear & Greed...")
    fg = fetch_fear_greed_crypto()
    print("  Fetching economic calendar...")
    calendar = fetch_economic_calendar()
    print("  Fetching FRED (DXY / yield curve)...")
    fred = fetch_fred()
    print("  Fetching CFTC COT...")
    cot = fetch_cot()
    return {
        "vix": vix,
        "fear_greed_crypto": fg,
        "high_impact_events": calendar,
        "fred": fred,
        "cot": cot,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

# ── Claude prompt ─────────────────────────────────────────────────────────────

M5_SCHEMA = """
{
  "resumen": "One sentence describing overall market conditions for this bot today.",
  "cards": [
    {
      "id": "M5-01",
      "tipo": "alerta",
      "title": "Short title (max 8 words)",
      "desc": "Specific, actionable description for the trader. What to do, when, why.",
      "horizonte": "now | 24h | 48h | semana"
    }
  ]
}
"""

def build_m5_prompt(group, macro):
    return f"""You are generating a daily market context briefing for a live algorithmic trading bot.

Bot name: {group["name"]}
Strategy summary: {group.get("category", "")}
{group.get("summary", "")}

Current market data (as of {macro["fetched_at"]}):

VIX: {json.dumps(macro["vix"], ensure_ascii=True) if macro["vix"] else "unavailable"}
Crypto Fear & Greed: {json.dumps(macro["fear_greed_crypto"], ensure_ascii=True) if macro["fear_greed_crypto"] else "unavailable"}
High-impact economic events this week: {json.dumps(macro["high_impact_events"], ensure_ascii=True)}
FRED - USD strength and yield curve: {json.dumps(macro.get("fred"), ensure_ascii=True) if macro.get("fred") else "unavailable"}
CFTC COT - non-commercial (speculative) net positioning: {json.dumps(macro.get("cot"), ensure_ascii=True) if macro.get("cot") else "unavailable"}

Generate 4-6 specific, actionable cards for the trader. Focus on:
- Upcoming high-impact events that require pausing or adjusting this bot
- Whether current volatility regime (VIX) favors or challenges this strategy
- Crypto sentiment if the bot trades crypto instruments
- USD strength (DXY) and yield curve regime impact on forex pairs this bot trades
- CFTC COT institutional positioning bias for EUR, GBP, JPY, or Gold if relevant to this bot
- Concrete timing and action items

Rules:
- tipo must be one of: alerta, oportunidad, neutral
- alerta = risk event or unfavorable condition requiring action
- oportunidad = favorable condition the trader should exploit
- neutral = informational context
- horizonte: now (act immediately), 24h, 48h, semana
- All text in Spanish with proper accents (á é í ó ú ü ñ). Avoid em-dashes and smart quotes.
- Order: alertas first, then oportunidades, then neutral.
- OUTPUT ONLY VALID JSON matching this schema exactly:
{M5_SCHEMA}
"""

def _call_claude_m5(group, macro):
    prompt = build_m5_prompt(group, macro)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    result = json.loads(text)
    result["last_updated"] = macro["fetched_at"]
    result["macro_snapshot"] = macro
    return result

def generate_m5(group, macro):
    existing_notes = (group.get("m5") or {}).get("trader_notes", "")
    result = _call_claude_m5(group, macro)
    if existing_notes:
        result["trader_notes"] = existing_notes
    return result

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== Market Context Generator ===")

    print("\nFetching macro data...")
    macro = build_macro_snapshot()
    print(f"  VIX: {macro['vix']}")
    print(f"  F&G: {macro['fear_greed_crypto']}")
    print(f"  Events: {len(macro['high_impact_events'])} high-impact")

    data = get_data()
    groups = data.get("groups", [])
    active = [g for g in groups if g.get("status") in ("activo", "en_revision")]

    if not active:
        print("No active or pending-review groups — nothing to update.")
        return

    changed = False
    had_error = False

    for g in active:
        print(f"\n[M5] {g['badge']} — {g['name']}")
        try:
            g["m5"] = generate_m5(g, macro)
            n_cards = len(g["m5"].get("cards", []))
            print(f"  -> {n_cards} cards generated")
            changed = True
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            had_error = True

    if changed:
        print("\nWriting to Worker...")
        put_data(data)
        print("Done.")
    else:
        print("No changes.")

    if had_error:
        sys.exit(1)

if __name__ == "__main__":
    main()
