"""
╔══════════════════════════════════════════════════════════╗
║         LIQUIDITY CANDLE — Backtester v3                 ║
║              IC Markets · MT5 · M15                      ║
╠══════════════════════════════════════════════════════════╣
║  Cambios v3 (sobre v2):                                  ║
║  · Capital FIJO para cálculo de lots — sin compounding   ║
║    Cada trade arriesga siempre INITIAL_CAPITAL * RISK%   ║
║    Objetivo: ver WR y PF reales, sin distorsión          ║
║  · Símbolos desde lc_whitelist.txt (filtro de liquidez)  ║
║  · ATR D1 usa día anterior cerrado (sin lookahead)       ║
║  · Análisis temporal: WR y PF por trimestre              ║
║  · Un trade a la vez por símbolo                         ║
╠══════════════════════════════════════════════════════════╣
║  Estrategia:                                             ║
║  1. Vela M15 con rango >= 25% del ATR diario             ║
║  2. Fibonacci sobre la vela (low=0, high=1)              ║
║                                                          ║
║  VELA VERDE → SHORT                                      ║
║    Entry : 1.18  (extensión sobre el high)               ║
║    TP    : 0.618 (retroceso fibonacci)                   ║
║    SL    : 1.38  (extensión mayor)                       ║
║                                                          ║
║  VELA ROJA → LONG                                        ║
║    Entry : -0.18 (extensión bajo el low)                 ║
║    TP    : 0.382 (retroceso fibonacci)                   ║
║    SL    : -0.38 (extensión mayor)                       ║
║                                                          ║
║  Orden límite válida por 90 minutos (6 barras M15).      ║
╚══════════════════════════════════════════════════════════╝
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import logging
import json
import os
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional, List

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("lc_backtest_v3.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("LC_v3")

# ─── Configuración ────────────────────────────────────────────────────────────
LC_MIN_PCT       = 0.25    # vela >= 25% del ATR diario
ATR_PERIOD       = 14      # período ATR diario (D1)
ORDER_EXPIRY_MIN = 90      # minutos de validez de la orden límite (6 barras M15)

# Fibonacci
FIBO = {
    "short_entry": 1.18,
    "short_tp":    0.618,
    "short_sl":    1.38,
    "long_entry":  -0.18,
    "long_tp":     0.382,
    "long_sl":     -0.38,
}

# Riesgo — capital FIJO (sin compounding)
RISK_PCT        = 2.0
INITIAL_CAPITAL = 10_000.0
RISK_USD_FIXED  = INITIAL_CAPITAL * RISK_PCT / 100   # siempre $200, nunca cambia

# Comisión IC Markets Raw round-trip por lote
COMMISSION_PER_LOT = 7.0

# Spread fallback por símbolo (en unidades de precio)
SPREAD_FALLBACK = {
    "XAUUSD": 0.30,  "XAGUSD": 0.025, "XTIUSD": 0.03,
    "XBRUSD": 0.03,  "XNGUSD": 0.003, "BTCUSD": 10.0,
    "US500":  0.40,  "US30":   2.0,   "USTEC":  1.5,
    "DE40":   1.0,   "UK100":  1.0,   "STOXX50":1.0,
    "EURUSD": 0.00010, "GBPUSD": 0.00012, "USDJPY": 0.015,
}

WHITELIST_FILE = "lc_whitelist.txt"
CACHE_FILE     = "lc_trades_v3.json"
OUTPUT_DIR     = "out_lc_v3"
os.makedirs(OUTPUT_DIR, exist_ok=True)

run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")


# ─── Dataclass de Trade ───────────────────────────────────────────────────────
@dataclass
class Trade:
    symbol:        str
    direction:     str       # "BUY" | "SELL"
    signal_time:   datetime
    open_time:     datetime
    close_time:    datetime
    candle_low:    float
    candle_high:   float
    candle_range:  float
    atr_daily:     float
    entry:         float
    sl:            float
    tp:            float
    close_price:   float
    outcome:       str       # "WIN" | "LOSS" | "EXPIRED"
    pnl_usd:       float     # con capital fijo
    spread:        float
    commission:    float
    bars_to_entry: int
    quarter:       str = field(default="")   # "2024-Q1" etc.
    month:         str = field(default="")   # "2024-01" etc.


# ─── Conexión ─────────────────────────────────────────────────────────────────
def connect() -> bool:
    if not mt5.initialize():
        log.error(f"MT5 initialize() falló: {mt5.last_error()}")
        return False
    info = mt5.account_info()
    log.info(f"Conectado: #{info.login}  balance=${info.balance:.2f}  @ {info.server}")
    return True

def disconnect():
    mt5.shutdown()


# ─── Whitelist ────────────────────────────────────────────────────────────────
def load_whitelist() -> List[str]:
    """Carga la whitelist generada por lc_liquidity_filter.py."""
    if not os.path.exists(WHITELIST_FILE):
        log.warning(f"Whitelist no encontrada ({WHITELIST_FILE}). "
                    "Corrí primero lc_liquidity_filter.py")
        return []
    syms = []
    with open(WHITELIST_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                syms.append(line)
    log.info(f"Whitelist cargada: {len(syms)} símbolos")
    return syms


# ─── Carga de Datos ───────────────────────────────────────────────────────────
def get_spread(symbol: str) -> float:
    info = mt5.symbol_info(symbol)
    if info is not None and info.spread > 0:
        return info.spread * info.point
    return SPREAD_FALLBACK.get(symbol, 0.0005)

def load_m15(symbol: str) -> Optional[pd.DataFrame]:
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 60_000)
    if rates is None or len(rates) < 500:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.reset_index(drop=True, inplace=True)
    return df

def load_daily_atr(symbol: str) -> Optional[pd.Series]:
    """
    ATR diario con shift(1): usa el ATR del día anterior CERRADO.
    Sin lookahead — en live tenés este mismo valor disponible
    en cualquier momento del día.
    """
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 600)
    if rates is None or len(rates) < ATR_PERIOD + 5:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"]  - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)

    # shift(1): el valor de hoy en D1 es el ATR calculado al cierre de ayer
    df["atr"] = tr.rolling(ATR_PERIOD).mean().shift(1)
    df.set_index("time", inplace=True)
    return df["atr"]


# ─── Detección y Fibonacci ────────────────────────────────────────────────────
def is_liquidity_candle(row: pd.Series, daily_atr: float) -> bool:
    return (row["high"] - row["low"]) >= daily_atr * LC_MIN_PCT

def get_fibo_levels(low: float, high: float, direction: str, spread: float) -> dict:
    rng = high - low
    if rng == 0:
        return {}

    def lvl(fib):
        return low + rng * fib

    if direction == "SELL":
        entry = lvl(FIBO["short_entry"])
        tp    = lvl(FIBO["short_tp"])
        sl    = lvl(FIBO["short_sl"]) + spread
    else:
        entry = lvl(FIBO["long_entry"])
        tp    = lvl(FIBO["long_tp"])
        sl    = lvl(FIBO["long_sl"]) - spread
        entry = entry + spread   # compramos al ask

    return {"entry": entry, "tp": tp, "sl": sl, "range": rng}


# ─── Simulación de Trade ──────────────────────────────────────────────────────
def simulate_trade(
    df: pd.DataFrame,
    signal_idx: int,
    direction: str,
    levels: dict,
    signal_row: pd.Series,
    daily_atr: float,
    symbol: str,
    spread: float,
) -> Optional[Trade]:
    """
    Simula una orden límite a partir de la barra siguiente al signal.
    Capital FIJO: siempre RISK_USD_FIXED como riesgo base.
    Sin trailing stop — TP/SL simples para medir edge puro.
    """
    entry    = levels["entry"]
    tp       = levels["tp"]
    sl       = levels["sl"]
    expiry   = ORDER_EXPIRY_MIN // 15   # barras M15

    signal_time    = signal_row["time"]
    open_time      = None
    close_time     = None
    close_price    = None
    outcome        = "EXPIRED"
    bars_to_entry  = 0

    activated = False

    for j in range(signal_idx + 1, min(signal_idx + 1 + expiry + 50, len(df))):
        bar = df.iloc[j]

        if not activated:
            # Verificar si la orden límite se activa
            elapsed = (bar["time"] - signal_time).total_seconds() / 60
            if elapsed > ORDER_EXPIRY_MIN:
                outcome = "EXPIRED"
                break

            if direction == "SELL" and bar["high"] >= entry:
                activated     = True
                open_time     = bar["time"]
                bars_to_entry = j - signal_idx
                actual_entry  = entry
            elif direction == "BUY" and bar["low"] <= entry:
                activated     = True
                open_time     = bar["time"]
                bars_to_entry = j - signal_idx
                actual_entry  = entry
            else:
                continue

        # Trade activado — verificar TP y SL
        if direction == "SELL":
            if bar["high"] >= sl:
                outcome     = "LOSS"
                close_price = sl
                close_time  = bar["time"]
                break
            if bar["low"] <= tp:
                outcome     = "WIN"
                close_price = tp
                close_time  = bar["time"]
                break
        else:
            if bar["low"] <= sl:
                outcome     = "LOSS"
                close_price = sl
                close_time  = bar["time"]
                break
            if bar["high"] >= tp:
                outcome     = "WIN"
                close_price = tp
                close_time  = bar["time"]
                break

    if outcome == "EXPIRED" or not activated:
        return None

    # ── PnL con capital FIJO ───────────────────────────────────────────────────
    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        return None

    sl_distance = abs(actual_entry - sl)
    if sl_distance == 0:
        return None

    tick_size  = sym_info.trade_tick_size
    tick_value = sym_info.trade_tick_value
    if tick_size == 0:
        return None

    value_per_lot = (sl_distance / tick_size) * tick_value
    if value_per_lot == 0:
        return None

    # Lot calculado sobre capital FIJO (siempre $200 de riesgo)
    lot = RISK_USD_FIXED / value_per_lot
    lot = max(sym_info.volume_min, min(lot, sym_info.volume_max))

    # PnL por R múltiple (simple, sin trailing)
    move = abs(close_price - actual_entry)
    rr   = move / sl_distance

    pnl_gross  = RISK_USD_FIXED * rr if outcome == "WIN" else -RISK_USD_FIXED
    commission = COMMISSION_PER_LOT * lot
    pnl_net    = pnl_gross - commission

    # Período temporal
    q_map = {1: "Q1", 2: "Q1", 3: "Q1",
             4: "Q2", 5: "Q2", 6: "Q2",
             7: "Q3", 8: "Q3", 9: "Q3",
             10: "Q4", 11: "Q4", 12: "Q4"}
    quarter = f"{open_time.year}-{q_map[open_time.month]}"
    month   = open_time.strftime("%Y-%m")

    return Trade(
        symbol        = symbol,
        direction     = direction,
        signal_time   = signal_time,
        open_time     = open_time,
        close_time    = close_time,
        candle_low    = signal_row["low"],
        candle_high   = signal_row["high"],
        candle_range  = signal_row["high"] - signal_row["low"],
        atr_daily     = daily_atr,
        entry         = actual_entry,
        sl            = sl,
        tp            = tp,
        close_price   = close_price,
        outcome       = outcome,
        pnl_usd       = round(pnl_net, 2),
        spread        = spread,
        commission    = round(commission, 2),
        bars_to_entry = bars_to_entry,
        quarter       = quarter,
        month         = month,
    )


# ─── Motor del Backtest ───────────────────────────────────────────────────────
def run_backtest(symbols: List[str]) -> List[Trade]:
    all_trades = []
    total = len(symbols)

    for idx, symbol in enumerate(symbols):
        log.info(f"[{idx+1}/{total}] {symbol}")
        mt5.symbol_select(symbol, True)

        df_m15 = load_m15(symbol)
        if df_m15 is None:
            log.debug(f"  {symbol}: sin datos M15")
            continue

        daily_atr_series = load_daily_atr(symbol)
        if daily_atr_series is None:
            log.debug(f"  {symbol}: sin ATR diario")
            continue

        spread   = get_spread(symbol)
        trades   = []
        in_trade = False
        cnt_lc   = 0

        for i in range(50, len(df_m15) - 1):
            row  = df_m15.iloc[i]
            date = row["time"].date()

            # ATR del día anterior (shift ya aplicado en load_daily_atr)
            atr_dates  = daily_atr_series.index.date
            past_dates = daily_atr_series[atr_dates <= date]
            if len(past_dates) == 0:
                continue
            daily_atr = past_dates.iloc[-1]
            if pd.isna(daily_atr) or daily_atr == 0:
                continue

            # Si hay trade abierto, esperar a que cierre
            if in_trade:
                last = trades[-1]
                if row["time"] >= last.close_time:
                    in_trade = False
                continue

            # Verificar vela de liquidez
            if not is_liquidity_candle(row, daily_atr):
                continue

            cnt_lc += 1
            bullish   = row["close"] >= row["open"]
            direction = "SELL" if bullish else "BUY"

            levels = get_fibo_levels(row["low"], row["high"], direction, spread)
            if not levels:
                continue

            trade = simulate_trade(
                df_m15, i, direction, levels,
                row, daily_atr, symbol, spread,
            )
            if trade is None:
                continue

            trades.append(trade)
            in_trade = True

        if trades:
            wins = sum(1 for t in trades if t.outcome == "WIN")
            wr   = wins / len(trades) * 100
            gw   = sum(t.pnl_usd for t in trades if t.outcome == "WIN")
            gl   = abs(sum(t.pnl_usd for t in trades if t.outcome != "WIN"))
            pf   = round(gw / gl, 2) if gl > 0 else float("inf")
            pnl  = sum(t.pnl_usd for t in trades)
            log.info(
                f"  {symbol}: LC={cnt_lc}  trades={len(trades)}  "
                f"WR={wr:.1f}%  PF={pf}  PnL=${pnl:+.2f}"
            )
            all_trades.extend(trades)

    return all_trades


# ─── Análisis temporal ────────────────────────────────────────────────────────
def temporal_analysis(all_trades: List[Trade]):
    """Imprime WR y PF por trimestre y por mes."""
    if not all_trades:
        return

    df = pd.DataFrame([t.__dict__ for t in all_trades])

    log.info("\n" + "=" * 60)
    log.info("  ANÁLISIS TEMPORAL — POR TRIMESTRE")
    log.info("=" * 60)
    log.info(f"  {'Período':<12} {'Trades':>7} {'WR':>8} {'PF':>7} {'PnL':>12}")
    log.info("  " + "─" * 52)

    for q in sorted(df["quarter"].unique()):
        qdf = df[df["quarter"] == q]
        wins = (qdf["outcome"] == "WIN").sum()
        n    = len(qdf)
        wr   = wins / n * 100
        gw   = qdf.loc[qdf["outcome"] == "WIN", "pnl_usd"].sum()
        gl   = abs(qdf.loc[qdf["outcome"] != "WIN", "pnl_usd"].sum())
        pf   = round(gw / gl, 2) if gl > 0 else float("inf")
        pnl  = qdf["pnl_usd"].sum()
        flag = " ◄ NEGATIVO" if pf < 1.0 else ""
        log.info(f"  {q:<12} {n:>7}  {wr:>6.1f}%  {pf:>6.2f}  ${pnl:>+10.2f}{flag}")

    log.info("\n" + "=" * 60)
    log.info("  ANÁLISIS TEMPORAL — POR MES")
    log.info("=" * 60)
    log.info(f"  {'Mes':<10} {'Trades':>7} {'WR':>8} {'PF':>7} {'PnL':>12}")
    log.info("  " + "─" * 50)

    for m in sorted(df["month"].unique()):
        mdf  = df[df["month"] == m]
        wins = (mdf["outcome"] == "WIN").sum()
        n    = len(mdf)
        wr   = wins / n * 100
        gw   = mdf.loc[mdf["outcome"] == "WIN", "pnl_usd"].sum()
        gl   = abs(mdf.loc[mdf["outcome"] != "WIN", "pnl_usd"].sum())
        pf   = round(gw / gl, 2) if gl > 0 else float("inf")
        pnl  = mdf["pnl_usd"].sum()
        flag = " ◄" if pf < 1.0 else ""
        log.info(f"  {m:<10} {n:>7}  {wr:>6.1f}%  {pf:>6.2f}  ${pnl:>+10.2f}{flag}")


# ─── Cache ────────────────────────────────────────────────────────────────────
def save_trades(trades: List[Trade]):
    data = []
    for t in trades:
        d = t.__dict__.copy()
        for k in ("signal_time", "open_time", "close_time"):
            d[k] = d[k].isoformat()
        data.append(d)
    path = os.path.join(OUTPUT_DIR, f"trades_{run_ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log.info(f"Trades guardados: {path}")

def load_cache() -> List[Trade]:
    # Busca el JSON más reciente en OUTPUT_DIR
    files = [f for f in os.listdir(OUTPUT_DIR) if f.startswith("trades_") and f.endswith(".json")]
    if not files:
        return []
    latest = sorted(files)[-1]
    path   = os.path.join(OUTPUT_DIR, latest)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    trades = []
    for d in data:
        for k in ("signal_time", "open_time", "close_time"):
            d[k] = datetime.fromisoformat(d[k])
        trades.append(Trade(**d))
    log.info(f"Cache cargado: {len(trades)} trades desde {latest}")
    return trades


# ─── Entry Point ──────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("   LIQUIDITY CANDLE v3 — Backtest iniciado")
    log.info(f"   LC >= {LC_MIN_PCT*100:.0f}% ATR D1 (día anterior) | Fibo 1.18/0.62/1.38")
    log.info(f"   Expiry: {ORDER_EXPIRY_MIN} min | Riesgo FIJO: ${RISK_USD_FIXED:.0f} por trade")
    log.info(f"   Sin trailing | TP/SL simples | Sin compounding")
    log.info("=" * 60)

    # Intentar cargar cache primero
    cached = load_cache()
    if cached:
        log.info("Cache encontrado — regenerando análisis...")
        temporal_analysis(cached)
        import report_lc_v3 as report
        report.generate(cached, INITIAL_CAPITAL,
                        os.path.join(OUTPUT_DIR, f"report_{run_ts}.html"))
        return

    if not connect():
        return

    try:
        symbols = load_whitelist()
        if not symbols:
            return

        log.info(f"Corriendo backtest sobre {len(symbols)} símbolos...")
        trades = run_backtest(symbols)

        if not trades:
            log.warning("Sin trades generados.")
            return

        save_trades(trades)
        temporal_analysis(trades)

        import report_lc_v3 as report
        report_path = os.path.join(OUTPUT_DIR, f"report_{run_ts}.html")
        report.generate(trades, INITIAL_CAPITAL, report_path)
        log.info(f"Reporte generado: {report_path}")

    finally:
        disconnect()


if __name__ == "__main__":
    main()
