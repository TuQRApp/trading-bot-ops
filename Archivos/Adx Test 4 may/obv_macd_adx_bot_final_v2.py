#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║   BOT EN VIVO — OBV + MACD DIVERGENCIA + ADX                        ║
║   8 instrumentos  |  M5/M15/H1  |  IC Markets MT5                   ║
╠══════════════════════════════════════════════════════════════════════╣
║  Índices : STOXX50 DE40 NETH25 SE30 UK100                           ║
║  Energía : XTIUSD                                                   ║
║  Metales : XAUUSD                                                   ║
║  Cripto  : BTCUSD                                                   ║
╠══════════════════════════════════════════════════════════════════════╣
║  Uso: python obv_macd_adx_bot.py                                    ║
║  Stop: Ctrl+C                                                       ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import sys, warnings, json, os, base64
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta, timezone
from io import BytesIO

warnings.filterwarnings("ignore")

# ══════════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════════
CONFIG = {
    "symbols"        : [
        # Índices (5)
        "STOXX50", "DE40", "NETH25", "SE30", "UK100",
        # Energía (1)
        "XTIUSD",
        # Metales (1)
        "XAUUSD",
        # Cripto (1)
        "BTCUSD",
    ],
    "days_back"      : 60,

    # ADX
    "adx_period"     : 14,
    "adx_threshold"  : 25,     # solo operar si ADX > 25

    # OBV divergencia
    "obv_lookback"   : 40,     # velas M5 hacia atrás
    "swing_bars"     : 3,      # barras a cada lado para confirmar swing

    # MACD divergencia
    "macd_fast"      : 12,
    "macd_slow"      : 26,
    "macd_signal"    : 9,
    "macd_lookback"  : 40,

    # Riesgo
    "capital_ini"    : 10_000,
    "risk_pct"       : 0.02,
    "atr_period"     : 14,
    "sl_mult"        : 1.5,
    "tp_mult"        : 3.0,    # RR 1:2
    "max_hold_bars"  : 24,     # 24 velas M5 = 2 horas
    # Fallback spreads/comisiones si MT5 no los devuelve
    "default_spread"  : 5.0,   # USD genérico
    "default_comm"    : 3.5,   # USD/lado genérico
    "spread_overrides": {      # símbolo → spread USD si MT5 falla
        "BTCUSD"  : 12.0,
        "XAUUSD"  : 10.5,
        "WTI_M6"  : 3.0,
        "XTIUSD"  : 3.0,
        "XNGUSD"  : 2.0,
        "US30"    : 2.0,
        "US500"   : 1.5,
        "USTEC"   : 2.0,
    },
    "comm_overrides"  : {
        "BTCUSD" : 7.0,
        "XAUUSD" : 3.5,
    },

    # Bot
    "loop_seconds"   : 30,       # revisar cada 30s
    "max_positions"  : 6,        # máximo 6 posiciones simultáneas
    "risk_pct"       : 0.02,     # 2% por trade

    # ── Circuit Breaker (calibrado para 100x leverage) ───────
    "cb_max_consec_loss" : 3,    # pérdidas consecutivas → pausa
    "cb_pausa_horas"     : 4,    # horas de pausa nivel 1
    "cb_max_dd_diario"   : 0.10, # 10% DD en el día → no operar hoy
    "cb_max_dd_total"    : 0.20, # 20% DD acumulado → detención completa
    "demo_mode"      : False,    # cuenta real
    "magic"          : 20260001, # ID único del bot
    "bars_m5"        : 150,      # velas M5 a descargar
    "bars_m15"       : 80,
    "bars_h1"        : 50,
    "log_file"       : "obv_macd_adx_bot.log",
    "state_file"     : "obv_macd_adx_bot_estado.json",
}

# ══════════════════════════════════════════════════════════════════════
#  DEPENDENCIAS
# ══════════════════════════════════════════════════════════════════════
try:
    import MetaTrader5 as mt5
except ImportError:
    print("❌ pip install MetaTrader5"); sys.exit(1)


# ══════════════════════════════════════════════════════════════════════
#  1. MT5
# ══════════════════════════════════════════════════════════════════════
def conectar():
    if not mt5.initialize():
        print(f"❌ MT5: {mt5.last_error()}"); sys.exit(1)
    info = mt5.account_info()
    print(f"✅ {info.company} | {info.server} | Balance: {info.balance:,.2f}")
    return info

def descargar(symbol, tf, desde, hasta):
    rates = mt5.copy_rates_range(symbol, tf, desde, hasta)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_localize(None)
    df.set_index("time", inplace=True)
    df.rename(columns={"open":"Open","high":"High","low":"Low",
                        "close":"Close","tick_volume":"Volume"}, inplace=True)
    return df[["Open","High","Low","Close","Volume"]]

def get_costos_reales(symbol):
    """Extrae spread y comisión reales de MT5, con fallback a CONFIG."""
    spread_usd = 0.0
    comm_usd   = 0.0

    info = mt5.symbol_info(symbol)
    if info is not None:
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick:
                raw = (tick.ask - tick.bid) * info.trade_contract_size
                if raw > 0:
                    spread_usd = raw
        except: pass

        try:
            desde_hist = datetime.now(timezone.utc) - timedelta(days=14)
            deals = mt5.history_deals_get(desde_hist, datetime.now(timezone.utc))
            if deals:
                ds = [d for d in deals if d.symbol == symbol and d.commission != 0]
                if ds:
                    comm_usd = abs(np.mean([d.commission for d in ds]))
        except: pass

    # Fallback a overrides del CONFIG
    if spread_usd == 0:
        spread_usd = CONFIG["spread_overrides"].get(symbol,
                     CONFIG["default_spread"])
    if comm_usd == 0:
        comm_usd = CONFIG["comm_overrides"].get(symbol,
                   CONFIG["default_comm"])

    return round(spread_usd, 4), round(comm_usd, 4)


# ══════════════════════════════════════════════════════════════════════
#  2. INDICADORES
# ══════════════════════════════════════════════════════════════════════
def calc_obv(df):
    direction = np.sign(df["Close"].diff()).fillna(0)
    obv = (direction * df["Volume"]).cumsum()
    return obv

def calc_macd(close, fast, slow, signal):
    ema_f = close.ewm(span=fast,   adjust=False).mean()
    ema_s = close.ewm(span=slow,   adjust=False).mean()
    line  = ema_f - ema_s
    sig   = line.ewm(span=signal,  adjust=False).mean()
    hist  = line - sig
    return line, sig, hist

def calc_adx(df, period):
    high, low, close = df["High"], df["Low"], df["Close"]
    plus_dm  = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    overlap  = plus_dm > minus_dm
    plus_dm[~overlap]  = 0
    minus_dm[overlap]  = 0
    tr  = pd.concat([high - low,
                     (high - close.shift()).abs(),
                     (low  - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(span=period,  adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr
    dx  = (100 * (plus_di - minus_di).abs() /
           (plus_di + minus_di).replace(0, np.nan))
    adx = dx.ewm(span=period, adjust=False).mean()
    return adx, plus_di, minus_di

def calc_atr(df, period):
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift()).abs()
    lc = (df["Low"]  - df["Close"].shift()).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(period).mean()

def add_indicators(df):
    df = df.copy()
    df["obv"]              = calc_obv(df)
    ml, ms, mh             = calc_macd(df["Close"],
                                        CONFIG["macd_fast"],
                                        CONFIG["macd_slow"],
                                        CONFIG["macd_signal"])
    df["macd_line"]        = ml
    df["macd_sig"]         = ms
    df["macd_hist"]        = mh
    adx, pdi, mdi          = calc_adx(df, CONFIG["adx_period"])
    df["adx"]              = adx
    df["plus_di"]          = pdi
    df["minus_di"]         = mdi
    df["atr"]              = calc_atr(df, CONFIG["atr_period"])
    df["trend_bull"]       = pdi > mdi   # +DI > -DI = tendencia alcista
    df["trend_bear"]       = mdi > pdi
    return df.dropna()


# ══════════════════════════════════════════════════════════════════════
#  3. DETECCIÓN DE SWINGS
# ══════════════════════════════════════════════════════════════════════
def es_swing_low(s, i, n):
    if i < n or i >= len(s) - n: return False
    v = s.iloc[i]
    return bool((s.iloc[i-n:i] > v).all() and (s.iloc[i+1:i+n+1] > v).all())

def es_swing_high(s, i, n):
    if i < n or i >= len(s) - n: return False
    v = s.iloc[i]
    return bool((s.iloc[i-n:i] < v).all() and (s.iloc[i+1:i+n+1] < v).all())


# ══════════════════════════════════════════════════════════════════════
#  4. DIVERGENCIAS OBV + MACD (doble divergencia)
# ══════════════════════════════════════════════════════════════════════
def detectar_doble_divergencia(df):
    """
    Bullish: precio LL + OBV HL + MACD HL  (ambas divergencias alcistas)
    Bearish: precio HH + OBV LH + MACD LH  (ambas divergencias bajistas)
    ADX > threshold para confirmar que hay tendencia
    """
    price = df["Close"]
    obv   = df["obv"]
    macd  = df["macd_line"]
    adx   = df["adx"]
    lb    = CONFIG["obv_lookback"]
    sb    = CONFIG["swing_bars"]
    n     = len(df)

    señales = pd.Series(0, index=df.index)

    for i in range(lb + sb, n - sb):

        # Filtro ADX: solo en tendencia
        if adx.iloc[i] < CONFIG["adx_threshold"]:
            continue

        # ── BULLISH: precio LL + OBV HL + MACD HL ──
        if es_swing_low(price, i, sb):
            for j in range(i - sb - 1, max(i - lb, sb) - 1, -1):
                if es_swing_low(price, j, sb):
                    precio_ll  = price.iloc[i] < price.iloc[j]
                    obv_hl     = obv.iloc[i]   > obv.iloc[j]
                    macd_hl    = macd.iloc[i]  > macd.iloc[j]
                    # Doble divergencia: OBV Y MACD deben confirmar
                    if precio_ll and obv_hl and macd_hl:
                        señales.iloc[i] = 1
                    # Divergencia simple (solo una): media señal
                    elif precio_ll and (obv_hl or macd_hl):
                        señales.iloc[i] = 1  # igual entramos
                    break

        # ── BEARISH: precio HH + OBV LH + MACD LH ──
        if es_swing_high(price, i, sb):
            for j in range(i - sb - 1, max(i - lb, sb) - 1, -1):
                if es_swing_high(price, j, sb):
                    precio_hh  = price.iloc[i] > price.iloc[j]
                    obv_lh     = obv.iloc[i]   < obv.iloc[j]
                    macd_lh    = macd.iloc[i]  < macd.iloc[j]
                    if precio_hh and obv_lh and macd_lh:
                        señales.iloc[i] = -1
                    elif precio_hh and (obv_lh or macd_lh):
                        señales.iloc[i] = -1
                    break

    return señales


# ══════════════════════════════════════════════════════════════════════
#  5. SEÑAL FINAL CON FILTRO H1 Y M15
# ══════════════════════════════════════════════════════════════════════
def construir_señales(m5, m15, h1):
    """
    H1  → sesgo tendencia: +DI > -DI = bull  /  -DI > +DI = bear
    M15 → confirma ADX > 25 en TF medio
    M5  → doble divergencia OBV + MACD en dirección del sesgo
    """
    base = m5.index

    # Sesgo H1
    sesgo_bull_h1 = h1["trend_bull"].reindex(base, method="ffill").fillna(False)
    sesgo_bear_h1 = h1["trend_bear"].reindex(base, method="ffill").fillna(False)

    # Confirmación M15: ADX > threshold
    adx_m15_ok = (m15["adx"] > CONFIG["adx_threshold"]).reindex(
        base, method="ffill").fillna(False)

    # Divergencias M5
    div_m5 = detectar_doble_divergencia(m5)

    # Señal final
    señal = pd.Series(0, index=base)
    bull  = sesgo_bull_h1 & adx_m15_ok & (div_m5 == 1)
    bear  = sesgo_bear_h1 & adx_m15_ok & (div_m5 == -1)
    señal[bull] =  1
    señal[bear] = -1

    n_bull_div = (div_m5 ==  1).sum()
    n_bear_div = (div_m5 == -1).sum()
    print(f"   Div bullish M5       : {n_bull_div}")
    print(f"   Div bearish M5       : {n_bear_div}")
    print(f"   ADX M15 OK           : {adx_m15_ok.sum()} velas")
    print(f"   → Señales LONG align : {bull.sum()}")
    print(f"   → Señales SHORT align: {bear.sum()}")
    return señal


# ══════════════════════════════════════════════════════════════════════
#  6. BACKTEST
# ══════════════════════════════════════════════════════════════════════
def backtest(m5, señales, spread, commission):
    capital  = CONFIG["capital_ini"]
    trades   = []
    equity   = [capital]
    eq_dates = [m5.index[0]]
    en_pos   = False
    entrada  = dir_ = sl = tp = 0.0
    f_entrada= None
    barras   = 0

    for i, (fecha, row) in enumerate(m5.iterrows()):
        sig = señales.iloc[i] if i < len(señales) else 0

        if en_pos:
            barras += 1
            p = row["Close"]
            hit_tp  = (dir_== 1 and p >= tp) or (dir_==-1 and p <= tp)
            hit_sl  = (dir_== 1 and p <= sl) or (dir_==-1 and p >= sl)
            timeout = barras >= CONFIG["max_hold_bars"]

            if hit_tp or hit_sl or timeout:
                cierre  = tp if hit_tp else (sl if hit_sl else p)
                razon   = "TP" if hit_tp else ("SL" if hit_sl else "Timeout")
                atr_v   = m5["atr"].iloc[i]
                pnl_pct = (cierre / entrada - 1) * dir_
                cap_r   = capital * CONFIG["risk_pct"]
                pnl_usd = pnl_pct / (CONFIG["sl_mult"] * atr_v / entrada) * cap_r
                pnl_usd -= (spread + commission * 2)
                capital += pnl_usd

                trades.append({
                    "fecha_entrada" : f_entrada,
                    "fecha_salida"  : fecha,
                    "direccion"     : "LONG" if dir_==1 else "SHORT",
                    "precio_entrada": round(entrada, 2),
                    "precio_salida" : round(cierre, 2),
                    "sl"            : round(sl, 2),
                    "tp"            : round(tp, 2),
                    "razon"         : razon,
                    "barras_m5"     : barras,
                    "adx_entrada"   : round(float(m5["adx"].iloc[i]), 2),
                    "pnl_usd"       : round(pnl_usd, 2),
                    "pnl_pct"       : round(pnl_pct * 100, 3),
                    "capital"       : round(capital, 2),
                })
                en_pos = False; barras = 0

        if not en_pos and sig != 0:
            atr_v = row["atr"] if not np.isnan(row["atr"]) else 0
            if atr_v > 0:
                entrada   = row["Close"]
                f_entrada = fecha
                dir_      = int(sig)
                sl = entrada - dir_ * CONFIG["sl_mult"] * atr_v
                tp = entrada + dir_ * CONFIG["tp_mult"] * atr_v
                en_pos = True

        equity.append(capital)
        eq_dates.append(fecha)

    return (pd.Series(equity, index=eq_dates[:len(equity)]),
            pd.DataFrame(trades))


# ══════════════════════════════════════════════════════════════════════
#  7. MÉTRICAS
# ══════════════════════════════════════════════════════════════════════
def metricas(equity, trades):
    ret   = (equity.iloc[-1] / equity.iloc[0] - 1) * 100
    dr    = equity.pct_change().dropna()
    sharpe= dr.mean() / dr.std() * np.sqrt(525_600/5) if dr.std() > 0 else 0
    dd    = (equity - equity.cummax()) / equity.cummax() * 100
    if not trades.empty:
        wins = trades[trades["pnl_usd"] > 0]
        loss = trades[trades["pnl_usd"] <= 0]
        wr   = len(wins) / len(trades) * 100
        avgw = wins["pnl_usd"].mean() if len(wins) else 0
        avgl = loss["pnl_usd"].mean() if len(loss) else 0
        pf   = wins["pnl_usd"].sum() / abs(loss["pnl_usd"].sum()) \
               if len(loss) and loss["pnl_usd"].sum() != 0 else 0
        tp_n = len(trades[trades["razon"]=="TP"])
        sl_n = len(trades[trades["razon"]=="SL"])
        to_n = len(trades[trades["razon"]=="Timeout"])
    else:
        wr=avgw=avgl=pf=tp_n=sl_n=to_n=0
    return {
        "capital_ini"  : CONFIG["capital_ini"],
        "capital_fin"  : round(float(equity.iloc[-1]), 2),
        "ret_pct"      : round(float(ret), 2),
        "sharpe"       : round(float(sharpe), 3),
        "max_dd_pct"   : round(float(dd.min()), 2),
        "n_trades"     : int(len(trades)),
        "win_rate"     : round(float(wr), 2),
        "avg_win"      : round(float(avgw), 2),
        "avg_loss"     : round(float(avgl), 2),
        "profit_factor": round(float(pf), 3),
        "tp_hits"      : int(tp_n),
        "sl_hits"      : int(sl_n),
        "timeout_hits" : int(to_n),
        "expectancy"   : round(float(wr/100*avgw + (1-wr/100)*avgl), 2),
    }


# ══════════════════════════════════════════════════════════════════════
#  8. GRÁFICOS
# ══════════════════════════════════════════════════════════════════════
def b64(fig):
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor="#0d1117")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()

def plot_equity(eq, ini, sym):
    fig, ax = plt.subplots(figsize=(13, 4), facecolor="#0d1117")
    ax.set_facecolor("#0d1117")
    c = "#00d4aa" if eq.iloc[-1] >= ini else "#ff4d4d"
    ax.plot(eq.index, eq.values, color=c, lw=1.5)
    ax.axhline(ini, color="#555", lw=0.8, ls="--")
    ax.fill_between(eq.index, eq.values, ini,
                    where=(eq.values >= ini), alpha=0.12,
                    color="#00d4aa", interpolate=True)
    ax.fill_between(eq.index, eq.values, ini,
                    where=(eq.values < ini), alpha=0.12,
                    color="#ff4d4d", interpolate=True)
    ax.set_title(f"Equity — {sym}", color="white", fontsize=13)
    ax.tick_params(colors="white")
    [s.set_edgecolor("#333") for s in ax.spines.values()]
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x,_: f"${x:,.0f}"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    plt.xticks(rotation=30); fig.tight_layout()
    return b64(fig)

def plot_dd(eq, sym):
    dd = (eq - eq.cummax()) / eq.cummax() * 100
    fig, ax = plt.subplots(figsize=(13, 3), facecolor="#0d1117")
    ax.set_facecolor("#0d1117")
    ax.fill_between(dd.index, dd.values, 0, color="#ff4d4d", alpha=0.6)
    ax.plot(dd.index, dd.values, color="#ff6b6b", lw=1)
    ax.set_title(f"Drawdown % — {sym}", color="white", fontsize=13)
    ax.tick_params(colors="white")
    [s.set_edgecolor("#333") for s in ax.spines.values()]
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x,_: f"{x:.1f}%"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    plt.xticks(rotation=30); fig.tight_layout()
    return b64(fig)

def plot_chart(m5, señales, sym, n=500):
    df  = m5.iloc[:n].copy()
    sig = señales.iloc[:n]
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(14, 9),
        facecolor="#0d1117",
        gridspec_kw={"height_ratios":[2.5, 1, 1]})
    for ax in [ax1, ax2, ax3]:
        ax.set_facecolor("#0d1117"); ax.tick_params(colors="white")
        [s.set_edgecolor("#333") for s in ax.spines.values()]

    # Precio + OBV solapado normalizado
    ax1.plot(df.index, df["Close"], color="#58a6ff", lw=1, label=sym)
    bp = df["Close"].reindex(sig[sig== 1].index).dropna()
    sp = df["Close"].reindex(sig[sig==-1].index).dropna()
    ax1.scatter(bp.index, bp.values, marker="^", color="#00d4aa", s=90, zorder=5, label="LONG")
    ax1.scatter(sp.index, sp.values, marker="v", color="#ff4d4d", s=90, zorder=5, label="SHORT")
    # OBV normalizado al eje de precio
    obv_n = (df["obv"] - df["obv"].min()) / (df["obv"].max() - df["obv"].min())
    p_range = df["Close"].max() - df["Close"].min()
    obv_scaled = obv_n * p_range * 0.4 + df["Close"].min()
    ax1.plot(df.index, obv_scaled, color="#ffaa00", lw=0.8, alpha=0.6, label="OBV (scaled)")
    ax1.set_title(f"{sym} M5 — OBV+MACD Div+ADX (primeras 500 velas)", color="white", fontsize=12)
    ax1.legend(facecolor="#161b22", labelcolor="white", fontsize=9)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x,_: f"${x:,.0f}"))

    # MACD
    ch = ["#00d4aa" if v >= 0 else "#ff4d4d" for v in df["macd_hist"]]
    ax2.bar(df.index, df["macd_hist"], color=ch, width=0.003)
    ax2.plot(df.index, df["macd_line"], color="#f7931a", lw=1, label="MACD")
    ax2.plot(df.index, df["macd_sig"],  color="#fff",    lw=0.8, alpha=0.5, label="Signal")
    ax2.axhline(0, color="#555", lw=0.6)
    ax2.set_title("MACD (12,26,9)", color="white", fontsize=10)
    ax2.legend(facecolor="#161b22", labelcolor="white", fontsize=8)

    # ADX
    ax3.plot(df.index, df["adx"],      color="#ba68c8", lw=1.2, label="ADX")
    ax3.plot(df.index, df["plus_di"],  color="#00d4aa", lw=0.8, alpha=0.7, label="+DI")
    ax3.plot(df.index, df["minus_di"], color="#ff4d4d", lw=0.8, alpha=0.7, label="-DI")
    ax3.axhline(CONFIG["adx_threshold"], color="#ffaa00", lw=0.8, ls="--",
                label=f"ADX={CONFIG['adx_threshold']}")
    ax3.set_title("ADX + DI", color="white", fontsize=10)
    ax3.legend(facecolor="#161b22", labelcolor="white", fontsize=8)

    for ax in [ax1, ax2, ax3]:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %H:%M"))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=30,
                 color="white", fontsize=7)
    fig.tight_layout(); return b64(fig)

def plot_pnl_diario(trades, sym):
    if trades.empty: return ""
    t = trades.copy()
    t["dia"] = pd.to_datetime(t["fecha_salida"]).dt.date
    d = t.groupby("dia")["pnl_usd"].sum()
    cols = ["#00d4aa" if v >= 0 else "#ff4d4d" for v in d]
    fig, ax = plt.subplots(figsize=(13, 3), facecolor="#0d1117")
    ax.set_facecolor("#0d1117")
    ax.bar(range(len(d)), d.values, color=cols, width=0.7)
    ax.axhline(0, color="#555", lw=0.8)
    step = max(1, len(d)//15)
    ax.set_xticks(range(0, len(d), step))
    ax.set_xticklabels([str(x) for x in d.index[::step]],
                        rotation=45, ha="right", fontsize=7, color="white")
    ax.set_title(f"PnL Diario — {sym}", color="white", fontsize=12)
    ax.tick_params(colors="white")
    [s.set_edgecolor("#333") for s in ax.spines.values()]
    fig.tight_layout(); return b64(fig)


# ══════════════════════════════════════════════════════════════════════
#  9. HTML
# ══════════════════════════════════════════════════════════════════════
def make_kpi_grid(m, sym):
    rc = "#00d4aa" if m["ret_pct"] >= 0 else "#ff4d4d"
    dc = "#ff4d4d" if m["max_dd_pct"] < -5 else "#ffaa00"
    return f"""
    <h3 style="color:#58a6ff;font-size:15px;margin:0 0 12px">{sym}</h3>
    <div class="grid">
      <div class="card"><div class="lbl">Capital ini</div>
        <div class="val" style="color:#58a6ff">${m['capital_ini']:,.0f}</div></div>
      <div class="card"><div class="lbl">Capital fin</div>
        <div class="val" style="color:{rc}">${m['capital_fin']:,.0f}</div></div>
      <div class="card"><div class="lbl">Retorno</div>
        <div class="val" style="color:{rc}">{m['ret_pct']:+.2f}%</div></div>
      <div class="card"><div class="lbl">Sharpe</div>
        <div class="val" style="color:{'#00d4aa' if m['sharpe']>1 else '#ffaa00'}">{m['sharpe']:.3f}</div></div>
      <div class="card"><div class="lbl">Max DD</div>
        <div class="val" style="color:{dc}">{m['max_dd_pct']:.2f}%</div></div>
      <div class="card"><div class="lbl">Win Rate</div>
        <div class="val" style="color:{'#00d4aa' if m['win_rate']>=50 else '#ffaa00'}">{m['win_rate']:.1f}%</div></div>
      <div class="card"><div class="lbl">Profit Factor</div>
        <div class="val" style="color:{'#00d4aa' if m['profit_factor']>1.2 else '#ffaa00'}">{m['profit_factor']:.3f}</div></div>
      <div class="card"><div class="lbl">Trades</div>
        <div class="val">{m['n_trades']}</div></div>
      <div class="card"><div class="lbl">Expectativa</div>
        <div class="val" style="color:{'#00d4aa' if m['expectancy']>0 else '#ff4d4d'}">${m['expectancy']:,.2f}</div></div>
      <div class="card"><div class="lbl">TP / SL / TO</div>
        <div class="val" style="font-size:14px">{m['tp_hits']} / {m['sl_hits']} / {m['timeout_hits']}</div></div>
    </div>"""

def make_trades_rows(trades):
    rows = ""
    for _, t in (trades.tail(300) if not trades.empty else trades).iterrows():
        pc  = "#00d4aa" if t["pnl_usd"] > 0 else "#ff4d4d"
        dc2 = "#00d4aa" if t["direccion"]=="LONG" else "#ff9500"
        rows += f"""<tr>
          <td>{str(t.get('fecha_entrada',''))[:16]}</td>
          <td>{str(t.get('fecha_salida',''))[:16]}</td>
          <td style="color:{dc2};font-weight:bold">{t.get('direccion','')}</td>
          <td>${t.get('precio_entrada',0):,.2f}</td>
          <td>${t.get('precio_salida',0):,.2f}</td>
          <td style="color:#8b949e">${t.get('sl',0):,.2f}</td>
          <td style="color:#8b949e">${t.get('tp',0):,.2f}</td>
          <td style="color:{pc};font-weight:bold">${t.get('pnl_usd',0):,.2f}</td>
          <td style="color:{pc}">{t.get('pnl_pct',0):+.3f}%</td>
          <td style="color:#8b949e">{t.get('razon','')}</td>
          <td style="color:#ffaa00">{t.get('adx_entrada',0):.1f}</td>
          <td>${t.get('capital',0):,.2f}</td>
        </tr>"""
    return rows

def html_report(results, broker, costos):
    sections = ""
    for sym, (m, trades, i_eq, i_dd, i_pnl, i_chart) in results.items():
        costo = costos.get(sym, {})
        pnl_img   = f'<img src="data:image/png;base64,{i_pnl}"   style="width:100%">' if i_pnl   else ""
        chart_img = f'<img src="data:image/png;base64,{i_chart}" style="width:100%">' if i_chart else ""
        sections += f"""
        <div class="symbol-block">
          {make_kpi_grid(m, sym)}
          <div style="font-size:12px;color:#8b949e;margin-bottom:14px">
            💰 Spread: <b style="color:#fff">${costo.get('spread',0):.2f}</b> &nbsp;|&nbsp;
            Comisión: <b style="color:#fff">${costo.get('commission',0):.2f}/lado</b> &nbsp;|&nbsp;
            Costo total/trade: <b style="color:#ffaa00">${costo.get('spread',0) + costo.get('commission',0)*2:.2f}</b>
          </div>
          <div class="section"><h2>📈 Equity Curve</h2>
            <img src="data:image/png;base64,{i_eq}" style="width:100%"></div>
          <div class="section"><h2>📉 Drawdown</h2>
            <img src="data:image/png;base64,{i_dd}" style="width:100%"></div>
          <div class="section"><h2>📊 PnL Diario</h2>{pnl_img}</div>
          <div class="section"><h2>📡 M5 — OBV+MACD Div+ADX (500 velas)</h2>{chart_img}</div>
          <div class="section">
            <h2>📋 Trades {sym} (últimos 300)</h2>
            <table><thead><tr>
              <th>Entrada</th><th>Salida</th><th>Dir</th>
              <th>Px Entrada</th><th>Px Salida</th><th>SL</th><th>TP</th>
              <th>PnL USD</th><th>PnL %</th><th>Cierre</th><th>ADX</th><th>Capital</th>
            </tr></thead><tbody>{make_trades_rows(trades)}</tbody></table>
          </div>
          <hr style="border-color:#21262d;margin:2rem 0">
        </div>"""

    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<title>OBV+MACD Div+ADX — Índices + Commodities + BTC</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;padding:28px}}
h1{{font-size:21px;color:#58a6ff;margin-bottom:4px}}
.sub{{color:#8b949e;font-size:12px;margin-bottom:24px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:14px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:13px;text-align:center}}
.val{{font-size:19px;font-weight:700;margin:4px 0 2px}}
.lbl{{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px}}
.section{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px;margin-bottom:16px}}
.section h2{{font-size:13px;color:#58a6ff;margin-bottom:12px;border-bottom:1px solid #30363d;padding-bottom:7px}}
.symbol-block{{margin-bottom:2rem}}
table{{width:100%;border-collapse:collapse;font-size:11px}}
th{{background:#0d1117;color:#8b949e;padding:7px 8px;text-align:left;font-weight:600}}
td{{padding:6px 8px;border-bottom:1px solid #21262d}}
tr:hover td{{background:#1c2128}}
img{{border-radius:8px}}
.tag{{background:#1a3a2a;color:#00d4aa;border-radius:5px;padding:2px 8px;font-size:11px}}
.params{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;font-size:12px;margin-bottom:20px}}
.param{{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:7px 12px;color:#8b949e}}
.param span{{color:#e6edf3;font-weight:600}}
</style></head><body>

<h1>⚡ OBV + MACD Divergencia + ADX — Todos los Instrumentos</h1>
<div class="sub">
  Índices + Commodities + Cripto &nbsp;|&nbsp; M5/M15/H1 &nbsp;|&nbsp;
  60 días &nbsp;|&nbsp; Broker: {broker.get('broker','—')} &nbsp;|&nbsp;
  {datetime.now().strftime('%Y-%m-%d %H:%M')}
  &nbsp;<span class="tag">Doble Divergencia</span>
</div>

<div class="section" style="margin-bottom:20px">
  <h2>⚙️ Parámetros</h2>
  <div class="params">
    <div class="param">Señal: <span>Doble divergencia OBV+MACD</span></div>
    <div class="param">Filtro tendencia: <span>ADX &gt; {CONFIG['adx_threshold']}</span></div>
    <div class="param">TF sesgo: <span>H1 (+DI vs -DI)</span></div>
    <div class="param">TF confirmación: <span>M15 (ADX &gt; 25)</span></div>
    <div class="param">TF entrada: <span>M5 (div OBV+MACD)</span></div>
    <div class="param">MACD: <span>({CONFIG['macd_fast']},{CONFIG['macd_slow']},{CONFIG['macd_signal']})</span></div>
    <div class="param">Lookback divergencia: <span>{CONFIG['obv_lookback']} velas M5</span></div>
    <div class="param">SL/TP: <span>{CONFIG['sl_mult']}× / {CONFIG['tp_mult']}× ATR</span></div>
    <div class="param">Riesgo/trade: <span>2%</span></div>
    <div class="param">Max hold: <span>{CONFIG['max_hold_bars']} velas M5 (2h)</span></div>
  </div>
</div>

{sections}

<div style="text-align:center;color:#555;font-size:10px;margin-top:14px">
  Solo investigación — No es recomendación de inversión.
</div></body></html>"""


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════
import logging, time

def get_signal_live(symbol):
    """Calcula señal para una barra en tiempo real."""
    m5_raw  = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5,  0, CONFIG["bars_m5"])
    m15_raw = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, CONFIG["bars_m15"])
    h1_raw  = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1,  0, CONFIG["bars_h1"])

    for r in [m5_raw, m15_raw, h1_raw]:
        if r is None or len(r) == 0:
            return 0, {}

    def to_df(r):
        df = pd.DataFrame(r)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_localize(None)
        df.set_index("time", inplace=True)
        df.rename(columns={"open":"Open","high":"High","low":"Low",
                            "close":"Close","tick_volume":"Volume"}, inplace=True)
        return df[["Open","High","Low","Close","Volume"]]

    try:
        m5  = add_indicators(to_df(m5_raw))
        m15 = add_indicators(to_df(m15_raw))
        h1  = add_indicators(to_df(h1_raw))
    except Exception as e:
        return 0, {"error": str(e)}

    if m5.empty or m15.empty or h1.empty:
        return 0, {}

    sesgo_bull = bool(h1["trend_bull"].iloc[-2])
    sesgo_bear = bool(h1["trend_bear"].iloc[-2])
    adx_m15_ok = float(m15["adx"].iloc[-2]) > CONFIG["adx_threshold"]

    señales_m5 = detectar_doble_divergencia(m5)
    sig = 0
    for k in range(-4, -1):
        if len(señales_m5) + k >= 0:
            v = int(señales_m5.iloc[k])
            if v != 0:
                sig = v
                break

    atr_val = float(m5["atr"].iloc[-2])
    estado = {
        "sesgo_h1"  : "BULL" if sesgo_bull else "BEAR" if sesgo_bear else "N",
        "adx_m15"   : round(float(m15["adx"].iloc[-2]), 1),
        "adx_m15_ok": adx_m15_ok,
        "div_m5"    : sig,
        "atr"       : round(atr_val, 4),
        "precio"    : round(float(m5["Close"].iloc[-1]), 4),
    }

    final = 0
    if sesgo_bull and adx_m15_ok and sig == 1:
        final = 1
    elif sesgo_bear and adx_m15_ok and sig == -1:
        final = -1

    return final, estado


def posiciones_del_bot():
    pos = mt5.positions_get()
    if not pos:
        return []
    return [p for p in pos if p.magic == CONFIG["magic"]]

def abrir_orden(symbol, direccion, precio, atr_val, log):
    info = mt5.symbol_info(symbol)
    if info is None:
        log.error(f"  {symbol}: symbol_info None")
        return None

    digits  = info.digits
    sl_dist = CONFIG["sl_mult"] * atr_val
    tp_dist = CONFIG["tp_mult"] * atr_val

    if direccion == 1:
        otype = mt5.ORDER_TYPE_BUY
        sl    = round(precio - sl_dist, digits)
        tp    = round(precio + tp_dist, digits)
    else:
        otype = mt5.ORDER_TYPE_SELL
        sl    = round(precio + sl_dist, digits)
        tp    = round(precio - tp_dist, digits)

    account = mt5.account_info()
    riesgo  = account.equity * CONFIG["risk_pct"]   # equity, no balance

    # Calcular PnL de 1 lote si toca el SL — preguntarle al broker directamente
    # Esto evita errores de conversión tick_value en instrumentos exóticos
    price_sl_1lot = sl  # precio de salida si SL tocado
    pnl_1lot = mt5.order_calc_profit(otype, symbol, 1.0, precio, price_sl_1lot)

    if pnl_1lot is None or abs(pnl_1lot) < 1e-8:
        # Fallback: método tick_value original
        tick_v = info.trade_tick_value
        tick_s = info.trade_tick_size
        if tick_s > 0 and tick_v > 0:
            sl_ticks = sl_dist / tick_s
            lot = riesgo / (sl_ticks * tick_v) if sl_ticks > 0 else info.volume_min
        else:
            lot = info.volume_min
    else:
        lot = riesgo / abs(pnl_1lot)

    lot = max(info.volume_min,
              min(round(lot / info.volume_step) * info.volume_step,
                  info.volume_max))

    # Verificar margen real
    margen_req = mt5.order_calc_margin(otype, symbol, lot, precio)
    if margen_req and margen_req > account.margin_free * 0.80:
        if margen_req > 0:
            lot = lot * (account.margin_free * 0.80) / margen_req
            lot = max(info.volume_min,
                      round(lot / info.volume_step) * info.volume_step)

    # Log para verificar riesgo real
    pnl_check = mt5.order_calc_profit(otype, symbol, lot, precio, price_sl_1lot)
    riesgo_real = abs(pnl_check) if pnl_check else 0
    log.info(f"  📊 {symbol}: lot={lot:.3f} | "
             f"riesgo=${riesgo_real:.2f} ({riesgo_real/account.equity*100:.1f}%)")

    request = {
        "action"      : mt5.TRADE_ACTION_DEAL,
        "symbol"      : symbol,
        "volume"      : lot,
        "type"        : otype,
        "price"       : precio,
        "sl"          : sl,
        "tp"          : tp,
        "deviation"   : 30,
        "magic"       : CONFIG["magic"],
        "comment"     : "OBV_MACD_ADX",
        "type_time"   : mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    if CONFIG["demo_mode"]:
        log.info(f"  [DEMO] {symbol} {'LONG' if direccion==1 else 'SHORT'} "
                  f"lot={lot:.3f} px={precio} SL={sl} TP={tp}")
        return {"demo": True}

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        rc = result.retcode if result else "None"
        cm = result.comment if result else ""
        log.error(f"  {symbol}: error retcode={rc} {cm}")
        return None

    log.info(f"  ✅ ORDEN {symbol} {'LONG' if direccion==1 else 'SHORT'} "
              f"ticket={result.order} lot={lot:.3f} px={precio} SL={sl} TP={tp}")
    return result

def cerrar_timeout(pos, log):
    sym   = pos.symbol
    info  = mt5.symbol_info(sym)
    if info is None: return
    tick  = mt5.symbol_info_tick(sym)
    precio= tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
    otype = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY             else mt5.ORDER_TYPE_BUY
    request = {
        "action"      : mt5.TRADE_ACTION_DEAL,
        "symbol"      : sym,
        "volume"      : pos.volume,
        "type"        : otype,
        "position"    : pos.ticket,
        "price"       : precio,
        "deviation"   : 30,
        "magic"       : CONFIG["magic"],
        "comment"     : "OBV_TIMEOUT",
        "type_time"   : mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    if CONFIG["demo_mode"]:
        log.info(f"  [DEMO] Cerraría timeout ticket={pos.ticket}")
        return
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"  ⏱ TIMEOUT ticket={pos.ticket} {sym} PnL={pos.profit:+.2f}")

def guardar_estado(data):
    out  = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(out, CONFIG["state_file"])
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def setup_logging():
    out = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(out, CONFIG["log_file"])
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ]
    )
    return logging.getLogger("BOT")

_pos_entry_time = {}   # ticket → datetime UTC de entrada

# ══════════════════════════════════════════════════════════════
#  CIRCUIT BREAKER
# ══════════════════════════════════════════════════════════════
class CircuitBreaker:
    def __init__(self, equity_ini):
        self.equity_ini  = equity_ini
        self.equity_dia  = equity_ini
        self.consec_loss = 0
        self.pausa_hasta = None
        self.detenido    = False
        self.fecha_dia   = datetime.utcnow().date()

    def nuevo_dia(self, log):
        hoy = datetime.utcnow().date()
        if hoy != self.fecha_dia:
            self.fecha_dia = hoy
            acct = mt5.account_info()
            self.equity_dia = acct.equity if acct else self.equity_ini
            log.info(f"🔄 Circuit breaker — nuevo día: equity reset ${self.equity_dia:,.2f}")

    def registrar_resultado(self, es_loss: bool, log):
        if es_loss:
            self.consec_loss += 1
            log.info(f"⚠️  Pérdidas consecutivas: {self.consec_loss}/{CONFIG['cb_max_consec_loss']}")
            if self.consec_loss >= CONFIG["cb_max_consec_loss"]:
                self.pausa_hasta = datetime.utcnow() + timedelta(hours=CONFIG["cb_pausa_horas"])
                log.warning(
                    f"🛑 CB Nivel 1 — {self.consec_loss} pérdidas consecutivas | "
                    f"Pausa hasta {self.pausa_hasta.strftime('%H:%M')} UTC"
                )
        else:
            if self.consec_loss > 0:
                log.info(f"✅ Racha cortada (había {self.consec_loss} pérdidas)")
            self.consec_loss = 0

    def puede_operar(self, log) -> bool:
        if self.detenido:
            return False

        ahora  = datetime.utcnow()
        acct   = mt5.account_info()
        equity = acct.equity if acct else self.equity_ini

        # Nivel 3 — DD total
        dd_total = (self.equity_ini - equity) / max(self.equity_ini, 1)
        if dd_total >= CONFIG["cb_max_dd_total"]:
            self.detenido = True
            log.error(
                f"🚨 CB Nivel 3 — DD total {dd_total*100:.1f}% >= "
                f"{CONFIG['cb_max_dd_total']*100:.0f}% | DETENCIÓN COMPLETA"
            )
            return False

        # Nivel 2 — DD diario
        dd_dia = (self.equity_dia - equity) / max(self.equity_dia, 1)
        if dd_dia >= CONFIG["cb_max_dd_diario"]:
            log.warning(
                f"🛑 CB Nivel 2 — DD diario {dd_dia*100:.1f}% >= "
                f"{CONFIG['cb_max_dd_diario']*100:.0f}% | Sin operaciones hoy"
            )
            return False

        # Nivel 1 — pausa temporal
        if self.pausa_hasta and ahora < self.pausa_hasta:
            mins = int((self.pausa_hasta - ahora).total_seconds() / 60)
            if mins % 30 == 0:
                log.info(f"⏸  Pausa activa — reanuda en {mins} min")
            return False
        elif self.pausa_hasta and ahora >= self.pausa_hasta:
            log.info("▶️  Pausa terminada — reanudando operaciones")
            self.pausa_hasta = None
            self.consec_loss = 0

        return True


def detectar_cierres_cb(cb, log, ultimo_deal_id):
    """
    Detecta deals cerrados por TP/SL del bot y notifica al circuit breaker.
    Retorna el último deal_id procesado.
    """
    desde = datetime.utcnow() - timedelta(hours=2)
    deals = mt5.history_deals_get(desde, datetime.utcnow())
    if not deals:
        return ultimo_deal_id

    for d in deals:
        if d.magic != CONFIG.get("magic", 20250201):
            continue
        if d.ticket <= ultimo_deal_id:
            continue
        if d.entry not in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT):
            continue

        ultimo_deal_id = d.ticket
        es_loss = d.reason == mt5.DEAL_REASON_SL
        cb.registrar_resultado(es_loss=es_loss, log=log)

    return ultimo_deal_id


def run_bot():
    log = setup_logging()

    log.info("=" * 60)
    log.info("  OBV + MACD DIVERGENCIA + ADX — BOT EN VIVO")
    log.info(f"  Instrumentos : {len(CONFIG['symbols'])}")
    log.info(f"  Riesgo/trade : {CONFIG['risk_pct']*100:.0f}%")
    log.info(f"  Max posiciones: {CONFIG['max_positions']}")
    log.info(f"  SL/TP        : {CONFIG['sl_mult']}× / {CONFIG['tp_mult']}× ATR")
    log.info(f"  Max hold     : {CONFIG['max_hold_bars']} velas M5")
    log.info(f"  Demo mode    : {CONFIG['demo_mode']}")
    log.info(f"  Loop         : {CONFIG['loop_seconds']}s")
    log.info(f"  Circuit breaker: {CONFIG['cb_max_consec_loss']} pérdidas → pausa {CONFIG['cb_pausa_horas']}h | "
             f"DD día {CONFIG['cb_max_dd_diario']*100:.0f}% | DD total {CONFIG['cb_max_dd_total']*100:.0f}%")
    log.info("=" * 60)

    if CONFIG["demo_mode"]:
        log.info("⚠️  DEMO ACTIVO — no se ejecutan órdenes reales")

    # Inicializar circuit breaker
    acct_ini = mt5.account_info()
    cb       = CircuitBreaker(equity_ini=acct_ini.equity if acct_ini else 10000.0)
    ultimo_deal_id = 0

    ciclos      = 0
    trades_hoy  = 0

    while True:
        try:
            ciclos += 1
            ahora   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            account = mt5.account_info()
            pos_bot = posiciones_del_bot()
            n_pos   = len(pos_bot)

            # ── Reset diario y detección de cierres ──
            cb.nuevo_dia(log)
            ultimo_deal_id = detectar_cierres_cb(cb, log, ultimo_deal_id)

            # ── Detención completa ──
            if cb.detenido:
                if ciclos % 60 == 0:
                    log.error("🚨 Bot detenido por CB nivel 3. Reinicia manualmente.")
                time.sleep(CONFIG["loop_seconds"])
                continue

            # ── Timeout check (tiempo real: 24 velas M5 = 120 minutos) ──
            ahora_dt = datetime.utcnow()
            for p in pos_bot:
                if p.ticket not in _pos_entry_time:
                    _pos_entry_time[p.ticket] = datetime.utcfromtimestamp(p.time)
                minutos = (ahora_dt - _pos_entry_time[p.ticket]).total_seconds() / 60
                max_min = CONFIG["max_hold_bars"] * 5
                if minutos >= max_min:
                    log.info(f"  ⏱ {p.symbol} ticket={p.ticket} {minutos:.0f} min >= {max_min} min")
                    cerrar_timeout(p, log)
                    _pos_entry_time.pop(p.ticket, None)

            pos_bot = posiciones_del_bot()
            n_pos   = len(pos_bot)
            syms_en_pos = {p.symbol for p in pos_bot}
            # Limpiar tickets cerrados por TP/SL de MT5
            for t in list(_pos_entry_time.keys()):
                if t not in {p.ticket for p in pos_bot}:
                    _pos_entry_time.pop(t, None)

            # ── Escanear señales ──
            señales_activas = []
            log.info(f"[{ahora}] ciclo={ciclos} pos={n_pos}/{CONFIG['max_positions']} "
                      f"balance=${account.balance:,.2f} equity=${account.equity:,.2f}")

            for sym in CONFIG["symbols"]:
                try:
                    sig, estado = get_signal_live(sym)
                    if sig != 0:
                        señales_activas.append((sym, sig, estado))
                        dir_str = "LONG ↑" if sig == 1 else "SHORT ↓"
                        log.info(f"  ★ SEÑAL {sym} {dir_str} | "
                                  f"H1={estado.get('sesgo_h1','?')} "
                                  f"ADX_M15={estado.get('adx_m15',0):.0f} "
                                  f"ATR={estado.get('atr',0):.2f}")
                except Exception as e:
                    log.warning(f"  {sym}: error señal — {e}")

            # ── Abrir posiciones — solo si CB permite ──
            if cb.puede_operar(log):
                for sym, sig, estado in señales_activas:
                    if n_pos >= CONFIG["max_positions"]:
                        log.info(f"  → {sym} señal ignorada (max_positions)")
                        break
                    if sym in syms_en_pos:
                        log.info(f"  → {sym} ya tiene posición abierta")
                        continue

                    atr_v = estado.get("atr", 0)
                    if atr_v <= 0:
                        log.warning(f"  → {sym} ATR=0, saltando")
                        continue

                    tick  = mt5.symbol_info_tick(sym)
                    if tick is None:
                        log.warning(f"  → {sym} sin tick")
                        continue
                    precio = tick.ask if sig == 1 else tick.bid

                    resultado = abrir_orden(sym, sig, precio, atr_v, log)
                    if resultado:
                        n_pos += 1
                        trades_hoy += 1
                        syms_en_pos.add(sym)

            # ── Guardar estado ──
            guardar_estado({
                "timestamp"     : ahora,
                "ciclos"        : ciclos,
                "balance"       : round(account.balance, 2),
                "equity"        : round(account.equity, 2),
                "profit_abierto": round(account.equity - account.balance, 2),
                "posiciones"    : n_pos,
                "symbols_pos"   : list(syms_en_pos),
                "trades_sesion" : trades_hoy,
                "señales_ciclo" : [(s, d, e.get('sesgo_h1')) for s,d,e in señales_activas],
                "demo_mode"     : CONFIG["demo_mode"],
                "cb_consec_loss": cb.consec_loss,
                "cb_pausa_hasta": str(cb.pausa_hasta) if cb.pausa_hasta else None,
                "cb_detenido"   : cb.detenido,
                "cb_puede_operar": cb.puede_operar(log),
                # ── Info estática del bot (leída por el dashboard) ──
                "bot_info": {
                    "nombre"      : "OBV + MACD + ADX",
                    "descripcion" : "Divergencia OBV/MACD con filtro de tendencia ADX en H1",
                    "timeframes"  : "M5 · M15 · H1",
                    "horario"     : "24/7 (limitado por mercado de cada activo)",
                    "symbols"     : CONFIG["symbols"],
                    "loop_seg"    : CONFIG["loop_seconds"],
                    "max_pos"     : CONFIG["max_positions"],
                    "risk_pct"    : CONFIG["risk_pct"],
                    "sl_mult"     : CONFIG["sl_mult"],
                    "tp_mult"     : CONFIG["tp_mult"],
                    "adx_umbral"  : CONFIG["adx_threshold"],
                    "max_hold_min": CONFIG["max_hold_bars"] * 5,
                },
            })

        except KeyboardInterrupt:
            log.info("\n⛔ Bot detenido por el usuario.")
            break
        except Exception as e:
            log.error(f"Error en ciclo {ciclos}: {e}", exc_info=True)

        time.sleep(CONFIG["loop_seconds"])

    mt5.shutdown()
    log.info("MT5 desconectado. Bot cerrado.")


# ══════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════════════════╗
║   OBV + MACD DIVERGENCIA + ADX — BOT EN VIVO                        ║
║   8 instrumentos · IC Markets MT5                                   ║
╠══════════════════════════════════════════════════════════════════════╣
║   Ctrl+C para detener                                               ║
║   Logs: obv_macd_adx_bot.log                                        ║
║   Estado: obv_macd_adx_bot_estado.json                              ║
╚══════════════════════════════════════════════════════════════════════╝
    """)

    print("🔴 CUENTA REAL | 2% riesgo/trade | máx 3 posiciones | SL 1.5×ATR | TP 3×ATR")

    conectar()
    run_bot()
