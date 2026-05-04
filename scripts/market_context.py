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

def fetch_binance():
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": "BTCUSDT"}, timeout=10,
        )
        r.raise_for_status()
        t = r.json()
        result = {
            "symbol": "BTCUSDT",
            "price": round(float(t["lastPrice"]), 2),
            "change_pct_24h": round(float(t["priceChangePercent"]), 2),
            "volume_24h_usd": round(float(t["quoteVolume"]), 0),
        }
        # Funding rate (perpetual futures)
        fr = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": "BTCUSDT", "limit": 1}, timeout=10,
        )
        fr.raise_for_status()
        funding = fr.json()
        if funding:
            rate = round(float(funding[0]["fundingRate"]) * 100, 4)
            result["funding_rate_pct"] = rate
            result["funding_bias"] = "longs_paying" if rate > 0 else "shorts_paying"
        # Open interest
        oi = requests.get(
            "https://fapi.binance.com/fapi/v1/openInterest",
            params={"symbol": "BTCUSDT"}, timeout=10,
        )
        oi.raise_for_status()
        result["open_interest_btc"] = round(float(oi.json()["openInterest"]), 0)
        return result
    except Exception as e:
        print(f"  [warn] Binance fetch failed: {e}")
        return None


def fetch_cnn_fear_greed():
    try:
        r = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        data = r.json()
        fg = data.get("fear_and_greed", {})
        if not fg:
            return None
        value = round(float(fg["score"]), 1)
        label = fg.get("rating", "")
        prev  = fg.get("previous_close")
        return {
            "value": value,
            "label": label,
            "prev":  round(float(prev), 1) if prev is not None else None,
            "prev_1w":  round(float(fg["previous_1_week"]),  1) if fg.get("previous_1_week")  else None,
            "prev_1m":  round(float(fg["previous_1_month"]), 1) if fg.get("previous_1_month") else None,
        }
    except Exception as e:
        print(f"  [warn] CNN Fear & Greed fetch failed: {e}")
        return None


def fetch_bls():
    def _get(sid):
        r = requests.get(
            f"https://api.bls.gov/publicAPI/v1/timeseries/data/{sid}",
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "REQUEST_SUCCEEDED":
            return None
        obs = data.get("Results", {}).get("series", [{}])[0].get("data", [])
        if not obs:
            return None
        o = obs[0]
        return {"value": o["value"], "date": f"{o['year']}-{o['period'].replace('M', '')}"}
    try:
        nfp = _get("CES0000000001")   # Total nonfarm payroll (thousands)
        cpi = _get("CUUR0000SA0")     # CPI All items
        result = {}
        if nfp: result["nonfarm_payroll_k"] = nfp
        if cpi: result["cpi_index"]         = cpi
        return result or None
    except Exception as e:
        print(f"  [warn] BLS fetch failed: {e}")
        return None


def fetch_ecb():
    def _parse_sdmx(data):
        datasets  = data.get("dataSets", [])
        structure = data.get("structure", {})
        if not datasets:
            return None
        obs_dims   = structure.get("dimensions", {}).get("observation", [])
        time_vals  = next(
            (d.get("values", []) for d in obs_dims if d.get("id") == "TIME_PERIOD"), []
        )
        series_map = datasets[0].get("series", {})
        if not series_map:
            return None
        observations = next(iter(series_map.values())).get("observations", {})
        dated = []
        for idx_str, val_arr in observations.items():
            idx = int(idx_str)
            if idx < len(time_vals) and val_arr and val_arr[0] is not None:
                dated.append((time_vals[idx]["id"], round(float(val_arr[0]), 3)))
        dated.sort(key=lambda x: x[0])
        return dated

    def _fetch_rate(flow_ref):
        r = requests.get(
            f"https://data-api.ecb.europa.eu/service/data/{flow_ref}"
            "?lastNObservations=3&format=jsondata",
            timeout=10, headers={"Accept": "application/json"},
        )
        r.raise_for_status()
        dated = _parse_sdmx(r.json())
        if not dated:
            return None
        cur_date, cur_val = dated[-1]
        entry = {"value": cur_val, "date": cur_date}
        if len(dated) >= 2:
            _, prev_val = dated[-2]
            entry["prev"]   = prev_val
            entry["change"] = round(cur_val - prev_val, 3)
        return entry

    try:
        result = {}
        dfr = _fetch_rate("FM/M.U2.EUR.4F.KR.DFR.LEV")    # Deposit Facility Rate
        mrr = _fetch_rate("FM/M.U2.EUR.4F.KR.MRR_FR.LEV") # Main Refinancing Rate
        if dfr: result["deposit_rate"]      = dfr
        if mrr: result["main_refi_rate"]    = mrr
        return result or None
    except Exception as e:
        print(f"  [warn] ECB fetch failed: {e}")
        return None


def build_macro_snapshot():
    print("  Fetching VIX...")
    vix = fetch_vix()
    print("  Fetching Fear & Greed crypto...")
    fg = fetch_fear_greed_crypto()
    print("  Fetching CNN Fear & Greed (equities)...")
    fg_equities = fetch_cnn_fear_greed()
    print("  Fetching economic calendar...")
    calendar = fetch_economic_calendar()
    print("  Fetching FRED (DXY / yield curve)...")
    fred = fetch_fred()
    print("  Fetching BLS (NFP / CPI)...")
    bls = fetch_bls()
    print("  Fetching ECB rates...")
    ecb = fetch_ecb()
    print("  Fetching CFTC COT...")
    cot = fetch_cot()
    print("  Fetching Binance (BTC funding / OI)...")
    binance = fetch_binance()
    return {
        "vix": vix,
        "fear_greed_crypto": fg,
        "fear_greed_equities": fg_equities,
        "high_impact_events": calendar,
        "fred": fred,
        "bls": bls,
        "ecb": ecb,
        "cot": cot,
        "binance": binance,
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
Equities Fear & Greed (CNN): {json.dumps(macro.get("fear_greed_equities"), ensure_ascii=True) if macro.get("fear_greed_equities") else "unavailable"}
High-impact economic events this week: {json.dumps(macro["high_impact_events"], ensure_ascii=True)}
FRED - USD strength and yield curve: {json.dumps(macro.get("fred"), ensure_ascii=True) if macro.get("fred") else "unavailable"}
BLS - latest NFP and CPI: {json.dumps(macro.get("bls"), ensure_ascii=True) if macro.get("bls") else "unavailable"}
ECB - deposit and main refinancing rates: {json.dumps(macro.get("ecb"), ensure_ascii=True) if macro.get("ecb") else "unavailable"}
CFTC COT - non-commercial (speculative) net positioning: {json.dumps(macro.get("cot"), ensure_ascii=True) if macro.get("cot") else "unavailable"}
Binance - BTC funding rate and open interest: {json.dumps(macro.get("binance"), ensure_ascii=True) if macro.get("binance") else "unavailable"}

Generate 4-6 specific, actionable cards for the trader. Focus on:
- Upcoming high-impact events that require pausing or adjusting this bot
- Whether current volatility regime (VIX) favors or challenges this strategy
- Crypto sentiment (Fear & Greed crypto) and BTC funding rate / open interest if the bot trades crypto
- Equities Fear & Greed (CNN) if the bot trades indices (US30, NAS100, GER40, SPX, etc.)
- USD strength (DXY, BLS NFP/CPI) and yield curve regime impact on forex pairs this bot trades
- ECB deposit/refi rates and spread vs Fed Funds for EUR/GBP pairs if relevant
- CFTC COT institutional positioning bias for EUR, GBP, JPY, or Gold if relevant to this bot
- BTC funding rate bias (longs_paying = crowded long = mean-reversion risk; shorts_paying = capitulation signal)
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
    print(f"  VIX:            {macro['vix']}")
    print(f"  F&G crypto:     {macro['fear_greed_crypto']}")
    print(f"  F&G equities:   {macro['fear_greed_equities']}")
    print(f"  Events:         {len(macro['high_impact_events'])} high-impact")
    print(f"  BLS:            {macro['bls']}")
    print(f"  ECB:            {macro['ecb']}")
    print(f"  Binance:        {macro['binance']}")

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
