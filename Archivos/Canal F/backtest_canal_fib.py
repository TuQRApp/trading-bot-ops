"""
=============================================================
  BACKTEST — Canal Fibonacci
  IC Markets | MT5 | M3 | Capital compuesto
=============================================================
  - Spread y comisión descargados desde MT5 en tiempo real
  - Capital compuesto: lote se recalcula con capital actual
  - 3 TPs parciales (1/3 cada uno) + breakeven en TP1
  - Reporte HTML generado en la carpeta del script
=============================================================
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import pytz
import os
import base64
from datetime import datetime, timedelta
from io import BytesIO
import warnings
warnings.filterwarnings('ignore')

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ============================================================
#  CONFIGURACIÓN
# ============================================================
SYMBOLS      = ["XAUUSD", "XAGUSD", "BTCUSD"]
TIMEFRAME    = mt5.TIMEFRAME_M3
PERIOD_DAYS  = 180
CAPITAL_INI  = 10_000.0
RISK_PCT     = 0.02        # 2.0% por trade (corregido desde 2.5%)
CANAL_LENGTH = 20

SESSION_HOURS = {
    "XAUUSD": list(range(1, 13)),
    "XAGUSD": list(range(1, 13)),
    "BTCUSD": list(range(14, 22)),
}

SHORT_ENTRY =  1.191
SHORT_TP1   =  0.809
SHORT_TP2   =  0.618
SHORT_TP3   =  0.500
SHORT_SL    =  1.382

LONG_ENTRY  = -0.191
LONG_TP1    =  0.191
LONG_TP2    =  0.382
LONG_TP3    =  0.500
LONG_SL     = -0.382

TP_SPLIT    = 1/3
MIN_TRADES  = 5
# ============================================================


# ============================================================
#  MT5
# ============================================================
def conectar():
    if not mt5.initialize():
        print(f"❌ MT5: {mt5.last_error()}")
        return False
    info = mt5.account_info()
    if info is None:
        print("❌ Sin cuenta conectada. Abre MT5 primero.")
        return False
    print(f"✅ {info.company} | {info.server} | Balance: ${info.balance:.2f}")
    return True


def get_costos_reales(symbol):
    """
    Spread: en unidades de precio (ask - bid).
    Comisión: USD por lote por lado desde historial MT5.
    Fallback a valores reales de IC Markets Raw si no hay historial.

    IC Markets Raw (comisiones reales aproximadas):
      XAUUSD : $3.50/lote/lado  (contrato 100oz)
      XAGUSD : $3.50/lote/lado
      BTCUSD : $5.00/lote/lado  (contrato 1 BTC)
      Forex  : $3.50/lote/lado  (contrato 100k)
    """
    # Comisiones default IC Markets Raw por símbolo
    COMM_DEFAULT = {
        "XAUUSD": 3.50,
        "XAGUSD": 3.50,
        "BTCUSD": 5.00,
        "ETHUSD": 5.00,
    }
    COMM_FOREX_DEFAULT = 3.50   # USD/lote/lado para forex

    info = mt5.symbol_info(symbol)
    if info is None:
        return 0.0, COMM_DEFAULT.get(symbol, COMM_FOREX_DEFAULT)

    # Spread en unidades de precio
    spread_price = 0.0
    tick = mt5.symbol_info_tick(symbol)
    if tick and tick.bid > 0:
        raw = tick.ask - tick.bid
        if raw / max(tick.bid, 1e-10) < 0.02:
            spread_price = raw

    # Comisión desde historial de deals (últimos 30 días)
    comm_usd = 0.0
    try:
        desde = datetime.now(pytz.UTC) - timedelta(days=30)
        deals = mt5.history_deals_get(desde, datetime.now(pytz.UTC))
        if deals and tick and tick.bid > 0 and info.trade_contract_size > 0:
            comms = [(abs(d.commission), d.volume) for d in deals
                     if d.symbol == symbol and d.commission != 0 and d.volume > 0]
            if comms:
                cv, vv = zip(*comms)
                cpl = np.mean(cv) / np.mean(vv)
                notional = tick.bid * info.trade_contract_size
                if notional > 0:
                    pct = cpl / notional * 100
                    if 0.001 < pct < 0.1:
                        comm_usd = cpl
    except Exception:
        pass

    # Fallback si historial vacío o comisión = 0
    if comm_usd == 0.0:
        comm_usd = COMM_DEFAULT.get(symbol, COMM_FOREX_DEFAULT)

    return round(spread_price, 8), round(comm_usd, 4)


def get_data(symbol):
    utc_to   = datetime.now(pytz.UTC)
    utc_from = utc_to - timedelta(days=PERIOD_DAYS)
    rates = mt5.copy_rates_range(symbol, TIMEFRAME, utc_from, utc_to)
    if rates is None or len(rates) < CANAL_LENGTH + 10:
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df.set_index('time', inplace=True)
    return df[['open','high','low','close']]


# ============================================================
#  HELPERS
# ============================================================
def fib_price(top, bot, level):
    return bot + level * (top - bot)


def calc_lot(symbol, entry_px, sl_px, capital):
    """
    Lote calculado con order_calc_profit — le pregunta al broker
    exactamente cuánto vale 1 lote para esa distancia SL en USD.
    Evita el error de tick_value / tick_size que falla en XAUUSD y BTC.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        return 0.01

    sl_dist  = abs(entry_px - sl_px)
    risk_usd = capital * RISK_PCT

    if sl_dist <= 0:
        return info.volume_min

    # Determinar dirección para order_calc_profit
    if entry_px > sl_px:
        order_type = mt5.ORDER_TYPE_BUY
        exit_px    = sl_px
    else:
        order_type = mt5.ORDER_TYPE_SELL
        exit_px    = sl_px

    pnl_1lot = mt5.order_calc_profit(order_type, symbol, 1.0, entry_px, exit_px)

    if pnl_1lot is None or abs(pnl_1lot) < 1e-10:
        # Fallback: tick_value manual
        tick_v = info.trade_tick_value
        tick_s = info.trade_tick_size
        if tick_s <= 0 or tick_v <= 0:
            return info.volume_min
        sl_ticks = sl_dist / tick_s
        pnl_1lot_abs = sl_ticks * tick_v
    else:
        pnl_1lot_abs = abs(pnl_1lot)

    if pnl_1lot_abs <= 0:
        return info.volume_min

    lot = risk_usd / pnl_1lot_abs
    lot = max(info.volume_min,
              min(round(lot / info.volume_step) * info.volume_step,
                  info.volume_max))
    return round(lot, 8)


def pnl_usd(symbol, entry_px, exit_px, direction, lot):
    info = mt5.symbol_info(symbol)
    if info is None or info.trade_tick_size == 0:
        return 0.0
    ticks = abs(exit_px - entry_px) / info.trade_tick_size
    sign  = 1 if ((direction == 'LONG' and exit_px > entry_px) or
                   (direction == 'SHORT' and exit_px < entry_px)) else -1
    return sign * ticks * info.trade_tick_value * lot


# ============================================================
#  BACKTEST
# ============================================================
def backtest(df, symbol, spread_usd, comm_usd, capital_ini):
    trades  = []
    capital = capital_ini
    n       = len(df)
    i       = CANAL_LENGTH
    hours   = SESSION_HOURS.get(symbol, list(range(24)))

    while i < n - 60:
        bar      = df.iloc[i]
        bar_time = df.index[i]

        if bar_time.hour not in hours:
            i += 1
            continue

        # ── Canal: 20 barras antes de la barra de señal ──────
        # Igual que el bot: excluye la barra de señal y la siguiente
        # para evitar lookahead bias
        canal_window = df.iloc[i-CANAL_LENGTH:i]
        canal_top = float(canal_window['high'].max())
        canal_bot = float(canal_window['low'].min())
        canal_rng = canal_top - canal_bot
        if canal_rng == 0:
            i += 1
            continue

        c_high  = float(bar['high'])
        c_low   = float(bar['low'])
        c_close = float(bar['close'])

        entry_short = fib_price(canal_top, canal_bot, SHORT_ENTRY)
        entry_long  = fib_price(canal_top, canal_bot, LONG_ENTRY)
        lvl_top     = fib_price(canal_top, canal_bot, 1.0)
        lvl_bot     = fib_price(canal_top, canal_bot, 0.0)

        direction = None
        if c_high >= entry_short and c_close < lvl_top:
            direction = 'SHORT'
        elif c_low <= entry_long and c_close > lvl_bot:
            direction = 'LONG'

        if direction is None:
            i += 1
            continue

        # ── Niveles con spread aplicado — igual que el bot ───
        spread = spread_usd   # spread en unidades de precio

        if direction == 'SHORT':
            entry_px = fib_price(canal_top, canal_bot, SHORT_ENTRY)
            tp_pxs   = [fib_price(canal_top, canal_bot, l) - spread
                        for l in [SHORT_TP1, SHORT_TP2, SHORT_TP3]]
            sl_px    = fib_price(canal_top, canal_bot, SHORT_SL) + spread
        else:
            entry_px = fib_price(canal_top, canal_bot, LONG_ENTRY) + spread
            tp_pxs   = [fib_price(canal_top, canal_bot, l) + spread
                        for l in [LONG_TP1, LONG_TP2, LONG_TP3]]
            sl_px    = fib_price(canal_top, canal_bot, LONG_SL) + spread

        if entry_px <= 0:
            i += 1
            continue

        # Verificar ejecución en barra actual
        filled = ((direction == 'SHORT' and c_high >= entry_px) or
                  (direction == 'LONG'  and c_low  <= entry_px))
        if not filled:
            i += 1
            continue

        lot = calc_lot(symbol, entry_px, sl_px, capital)

        # ── Simular ──────────────────────────────────────────
        remaining   = 1.0
        pnl_bruto   = 0.0
        sl_hit      = False
        tps_hit     = [False, False, False]
        current_sl  = sl_px

        for j in range(i + 1, min(i + 200, n)):
            f      = df.iloc[j]
            f_high = float(f['high'])
            f_low  = float(f['low'])

            if remaining <= 0:
                break

            tp_hit_bar = False
            for t_idx, (tp_px, done) in enumerate(zip(tp_pxs, tps_hit)):
                if done:
                    continue
                hit = ((direction == 'SHORT' and f_low  <= tp_px) or
                       (direction == 'LONG'  and f_high >= tp_px))
                if hit:
                    pnl_bruto     += pnl_usd(symbol, entry_px, tp_px,
                                              direction, lot * TP_SPLIT)
                    remaining     -= TP_SPLIT
                    tps_hit[t_idx] = True
                    tp_hit_bar     = True
                    if t_idx == 0:
                        current_sl = entry_px   # breakeven

            if remaining <= 0:
                break

            if not tp_hit_bar:
                sl_trig = ((direction == 'SHORT' and f_high >= current_sl) or
                           (direction == 'LONG'  and f_low  <= current_sl))
                if sl_trig:
                    pnl_bruto += pnl_usd(symbol, entry_px, current_sl,
                                          direction, lot * remaining)
                    sl_hit = True
                    break

        # Costo: solo comisión RT (spread ya aplicado en niveles)
        costo    = comm_usd * 2 * lot
        pnl_neto = pnl_bruto - costo
        capital += pnl_neto

        if tps_hit[0]:
            result = 'BREAKEVEN' if (sl_hit and not tps_hit[1] and not tps_hit[2]) else 'WIN'
        elif sl_hit:
            result = 'LOSS'
        else:
            result = 'OPEN'

        if result == 'OPEN':
            i += 1
            continue

        trades.append({
            'time'        : bar_time,
            'direction'   : direction,
            'entry'       : round(entry_px, 5),
            'sl'          : round(sl_px, 5),
            'lot'         : round(lot, 4),
            'pnl_bruto'   : round(pnl_bruto, 2),
            'costo'       : round(costo, 2),
            'pnl_neto'    : round(pnl_neto, 2),
            'tps_hit'     : sum(tps_hit),
            'sl_hit'      : sl_hit,
            'result'      : result,
            'capital'     : round(capital, 2),
        })

        i += CANAL_LENGTH

    return pd.DataFrame(trades), capital


# ============================================================
#  MÉTRICAS
# ============================================================
def calc_metricas(df, capital_ini, capital_fin, spread_usd, comm_usd):
    if df.empty or len(df) < MIN_TRADES:
        return None

    wins = df[df['result'] == 'WIN']
    loss = df[df['result'] == 'LOSS']
    be   = df[df['result'] == 'BREAKEVEN']
    total = len(wins) + len(loss)

    wr    = len(wins) / total * 100 if total > 0 else 0
    gp    = wins['pnl_neto'].sum()
    gl    = abs(loss['pnl_neto'].sum())
    pf    = round(gp / gl, 2) if gl > 0 else 999.0

    eq    = df['capital']
    peak  = eq.cummax()
    maxdd = ((peak - eq) / peak * 100).max()
    ret   = (capital_fin / capital_ini - 1) * 100

    return {
        'capital_ini'  : capital_ini,
        'capital_fin'  : round(capital_fin, 2),
        'ret_pct'      : round(ret, 2),
        'win_rate'     : round(wr, 1),
        'profit_factor': pf,
        'max_dd'       : round(maxdd, 2),
        'n_trades'     : len(df),
        'n_win'        : len(wins),
        'n_loss'       : len(loss),
        'n_be'         : len(be),
        'avg_tps'      : round(df['tps_hit'].mean(), 2),
        'pnl_bruto'    : round(df['pnl_bruto'].sum(), 2),
        'costo_total'  : round(df['costo'].sum(), 2),
        'pnl_neto'     : round(df['pnl_neto'].sum(), 2),
        'spread_usd'   : round(spread_usd, 4),
        'comm_usd'     : round(comm_usd, 4),
        'costo_rt'     : round(spread_usd + comm_usd * 2, 4),
    }


# ============================================================
#  GRÁFICOS
# ============================================================
def b64(fig):
    buf = BytesIO()
    fig.savefig(buf, format='png', dpi=130,
                bbox_inches='tight', facecolor='#0d1117')
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def plot_equity(df, sym, capital_ini):
    fig, ax = plt.subplots(figsize=(13, 4), facecolor='#0d1117')
    ax.set_facecolor('#0d1117')
    eq = df['capital']
    c  = '#00d4aa' if eq.iloc[-1] >= capital_ini else '#ff4d4d'
    ax.plot(range(len(eq)), eq.values, color=c, lw=1.5)
    ax.axhline(capital_ini, color='#555', lw=0.8, ls='--')
    ax.fill_between(range(len(eq)), eq.values, capital_ini,
                    where=(eq.values >= capital_ini),
                    alpha=0.12, color='#00d4aa', interpolate=True)
    ax.fill_between(range(len(eq)), eq.values, capital_ini,
                    where=(eq.values < capital_ini),
                    alpha=0.12, color='#ff4d4d', interpolate=True)
    ax.set_title(f'Equity Curve — {sym}', color='white', fontsize=13)
    ax.tick_params(colors='white')
    [s.set_edgecolor('#333') for s in ax.spines.values()]
    ax.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))
    ax.set_xlabel('# Trade', color='white', fontsize=9)
    fig.tight_layout()
    return b64(fig)


def plot_dd(df, sym):
    eq   = df['capital']
    peak = eq.cummax()
    dd   = (eq - peak) / peak * 100
    fig, ax = plt.subplots(figsize=(13, 3), facecolor='#0d1117')
    ax.set_facecolor('#0d1117')
    ax.fill_between(range(len(dd)), dd.values, 0,
                    color='#ff4d4d', alpha=0.6)
    ax.plot(range(len(dd)), dd.values, color='#ff6b6b', lw=1)
    ax.set_title(f'Drawdown % — {sym}', color='white', fontsize=13)
    ax.tick_params(colors='white')
    [s.set_edgecolor('#333') for s in ax.spines.values()]
    ax.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda x, _: f'{x:.1f}%'))
    fig.tight_layout()
    return b64(fig)


def plot_mensual(df, sym):
    d = df.copy()
    d['mes'] = pd.to_datetime(
        d['time'].astype(str)).dt.to_period('M')
    m    = d.groupby('mes')['pnl_neto'].sum()
    cols = ['#00d4aa' if v >= 0 else '#ff4d4d' for v in m]
    fig, ax = plt.subplots(figsize=(13, 3.5), facecolor='#0d1117')
    ax.set_facecolor('#0d1117')
    ax.bar(range(len(m)), m.values, color=cols, width=0.7)
    ax.axhline(0, color='#555', lw=0.8)
    ax.set_xticks(range(len(m)))
    ax.set_xticklabels([str(p) for p in m.index],
                        rotation=30, ha='right',
                        fontsize=9, color='white')
    ax.set_title(f'PnL Mensual Neto — {sym}',
                  color='white', fontsize=12)
    ax.tick_params(colors='white')
    [s.set_edgecolor('#333') for s in ax.spines.values()]
    ax.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))
    fig.tight_layout()
    return b64(fig)


def plot_resultado_pie(m):
    vals   = [m['n_win'], m['n_loss'], m['n_be']]
    labels = [f"WIN ({m['n_win']})",
               f"LOSS ({m['n_loss']})",
               f"BE ({m['n_be']})"]
    colors = ['#00d4aa', '#ff4d4d', '#ffaa00']
    fig, ax = plt.subplots(figsize=(5, 4), facecolor='#0d1117')
    ax.set_facecolor('#0d1117')
    wedges, texts, autotexts = ax.pie(
        vals, labels=labels, colors=colors,
        autopct='%1.1f%%', startangle=90,
        textprops={'color': 'white', 'fontsize': 10})
    for at in autotexts:
        at.set_fontsize(9)
    ax.set_title('Distribución de resultados',
                  color='white', fontsize=11)
    fig.tight_layout()
    return b64(fig)


# ============================================================
#  HTML
# ============================================================
def trades_html(df):
    rows = ''
    for _, t in df.iterrows():
        pc  = '#00d4aa' if t['pnl_neto'] > 0 else '#ff4d4d'
        dc  = '#00d4aa' if t['direction'] == 'LONG' else '#ff9500'
        rc  = {'WIN': '#00d4aa', 'LOSS': '#ff4d4d',
               'BREAKEVEN': '#ffaa00'}.get(t['result'], '#aaa')
        rows += f"""<tr>
          <td>{str(t['time'])[:16]}</td>
          <td style="color:{dc};font-weight:bold">{t['direction']}</td>
          <td>${t['entry']:,.4f}</td>
          <td style="color:#8b949e">${t['sl']:,.4f}</td>
          <td>{t['lot']:.4f}</td>
          <td>${t['pnl_bruto']:,.2f}</td>
          <td style="color:#ff4d4d">-${t['costo']:,.2f}</td>
          <td style="color:{pc};font-weight:bold">${t['pnl_neto']:,.2f}</td>
          <td>{t['tps_hit']}/3</td>
          <td style="color:{rc};font-weight:bold">{t['result']}</td>
          <td>${t['capital']:,.2f}</td>
        </tr>"""
    return rows


def build_html(results):
    sections = ''
    for sym, (m, df_t, i_eq, i_dd, i_pnl, i_pie) in results.items():
        rc  = '#00d4aa' if m['ret_pct'] >= 0 else '#ff4d4d'
        pnl_img = (f'<img src="data:image/png;base64,{i_pnl}"'
                   f' style="width:100%">') if i_pnl else ''
        pie_img = (f'<img src="data:image/png;base64,{i_pie}"'
                   f' style="max-width:320px;margin:0 auto;display:block">') if i_pie else ''

        sections += f"""
        <div class="sym-block">
          <h2 class="sym-title">{sym}</h2>

          <!-- KPIs -->
          <div class="grid">
            <div class="card"><div class="lbl">Capital Inicial</div>
              <div class="val" style="color:#58a6ff">${m['capital_ini']:,.0f}</div></div>
            <div class="card"><div class="lbl">Capital Final</div>
              <div class="val" style="color:{rc}">${m['capital_fin']:,.0f}</div></div>
            <div class="card"><div class="lbl">Retorno</div>
              <div class="val" style="color:{rc}">{m['ret_pct']:+,.2f}%</div></div>
            <div class="card"><div class="lbl">Win Rate</div>
              <div class="val" style="color:{'#00d4aa' if m['win_rate']>=60 else '#ffaa00'}">{m['win_rate']:.1f}%</div></div>
            <div class="card"><div class="lbl">Profit Factor</div>
              <div class="val" style="color:{'#00d4aa' if m['profit_factor']>1.5 else '#ffaa00'}">{m['profit_factor']:.2f}</div></div>
            <div class="card"><div class="lbl">Max Drawdown</div>
              <div class="val" style="color:{'#ff4d4d' if m['max_dd']>10 else '#ffaa00'}">{m['max_dd']:.2f}%</div></div>
            <div class="card"><div class="lbl">Trades</div>
              <div class="val">{m['n_trades']}</div></div>
            <div class="card"><div class="lbl">WIN / LOSS / BE</div>
              <div class="val" style="font-size:14px"
                >{m['n_win']} / {m['n_loss']} / {m['n_be']}</div></div>
            <div class="card"><div class="lbl">Avg TPs</div>
              <div class="val">{m['avg_tps']:.2f}/3</div></div>
            <div class="card"><div class="lbl">PnL Bruto</div>
              <div class="val" style="color:#378ADD">${m['pnl_bruto']:,.0f}</div></div>
            <div class="card"><div class="lbl">Costos Totales</div>
              <div class="val" style="color:#ff4d4d">-${m['costo_total']:,.0f}</div></div>
            <div class="card"><div class="lbl">PnL Neto</div>
              <div class="val" style="color:{rc}">${m['pnl_neto']:,.0f}</div></div>
          </div>

          <!-- Costos reales -->
          <div class="costos-row">
            <span>📡 Spread real (MT5): <b>${m['spread_usd']:.4f}/lote</b></span>
            <span>💼 Comisión real (MT5): <b>${m['comm_usd']:.4f}/lado</b></span>
            <span>💰 Costo RT/lote: <b style="color:#ff4d4d">${m['costo_rt']:.4f}</b></span>
            <span>🎯 Riesgo/trade: <b>2.5% compuesto</b></span>
          </div>

          <!-- Charts -->
          <div class="section">
            <h3>📈 Equity Curve</h3>
            <img src="data:image/png;base64,{i_eq}" style="width:100%">
          </div>
          <div class="section">
            <h3>📉 Drawdown</h3>
            <img src="data:image/png;base64,{i_dd}" style="width:100%">
          </div>
          <div class="section">
            <h3>📅 PnL Mensual Neto</h3>{pnl_img}
          </div>
          <div class="section">
            <h3>🥧 Distribución de Resultados</h3>{pie_img}
          </div>

          <!-- Trades -->
          <div class="section">
            <h3>📋 Historial de Trades ({len(df_t)} totales)</h3>
            <table>
              <thead><tr>
                <th>Fecha</th><th>Dir</th><th>Entrada</th><th>SL</th>
                <th>Lote</th><th>PnL Bruto</th><th>Costo</th>
                <th>PnL Neto</th><th>TPs</th><th>Resultado</th>
                <th>Capital</th>
              </tr></thead>
              <tbody>{trades_html(df_t)}</tbody>
            </table>
          </div>
          <hr style="border-color:#21262d;margin:2.5rem 0">
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<title>Canal Fibonacci — Backtest</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#e6edf3;
     font-family:'Segoe UI',sans-serif;padding:28px}}
h1{{font-size:22px;color:#58a6ff;margin-bottom:4px}}
.sub{{color:#8b949e;font-size:12px;margin-bottom:24px}}
.grid{{display:grid;
       grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
       gap:10px;margin-bottom:14px}}
.card{{background:#161b22;border:1px solid #30363d;
       border-radius:10px;padding:13px;text-align:center}}
.val{{font-size:18px;font-weight:700;margin:4px 0 2px}}
.lbl{{font-size:10px;color:#8b949e;text-transform:uppercase;
      letter-spacing:.5px}}
.section{{background:#161b22;border:1px solid #30363d;
          border-radius:10px;padding:16px;margin-bottom:16px}}
.section h3{{font-size:13px;color:#58a6ff;margin-bottom:12px;
             border-bottom:1px solid #30363d;padding-bottom:7px}}
.sym-block{{margin-bottom:2rem}}
.sym-title{{font-size:17px;color:#58a6ff;margin-bottom:14px;
            padding-bottom:6px;border-bottom:1px solid #30363d}}
.costos-row{{display:flex;flex-wrap:wrap;gap:16px;font-size:12px;
             color:#8b949e;margin-bottom:16px;
             background:#161b22;border:1px solid #30363d;
             border-radius:8px;padding:10px 14px}}
.costos-row b{{color:#e6edf3}}
table{{width:100%;border-collapse:collapse;font-size:11px}}
th{{background:#0d1117;color:#8b949e;padding:7px 8px;
    text-align:left;font-weight:600;white-space:nowrap}}
td{{padding:6px 8px;border-bottom:1px solid #21262d}}
tr:hover td{{background:#1c2128}}
img{{border-radius:8px}}
.tag{{background:#1a3a2a;color:#00d4aa;border-radius:5px;
      padding:2px 8px;font-size:11px}}
</style></head><body>

<h1>📊 Canal Fibonacci v2 — Backtest con Costos Reales</h1>
<div class="sub">
  {' · '.join(results.keys())} &nbsp;|&nbsp; M3 &nbsp;|&nbsp;
  6 meses &nbsp;|&nbsp;
  Spread aplicado en niveles + Comisión MT5 &nbsp;|&nbsp;
  Capital ${CAPITAL_INI:,.0f} · Riesgo {RISK_PCT*100:.1f}% compuesto &nbsp;|&nbsp;
  Lote vía order_calc_profit &nbsp;|&nbsp;
  {datetime.now().strftime('%Y-%m-%d %H:%M')}
  &nbsp;<span class="tag">v2 — costos corregidos</span>
</div>

{sections}

<div style="text-align:center;color:#555;font-size:10px;margin-top:16px">
  Solo investigación — No es recomendación de inversión.
</div>
</body></html>"""


# ============================================================
#  MAIN
# ============================================================
def run():
    print("=" * 55)
    print("  Canal Fibonacci — Backtest con Costos Reales")
    print(f"  {' | '.join(SYMBOLS)} | M3 | {PERIOD_DAYS} días")
    print(f"  Capital: ${CAPITAL_INI:,.0f} | Riesgo: {RISK_PCT*100:.1f}%")
    print("=" * 55)

    if not conectar():
        return

    results = {}

    for sym in SYMBOLS:
        print(f"\n⏳ {sym}...")
        mt5.symbol_select(sym, True)

        df = get_data(sym)
        if df is None:
            print(f"  ❌ Sin datos"); continue
        print(f"  {len(df):,} barras M3")

        spread_price, comm_usd = get_costos_reales(sym)
        print(f"  Spread real (precio) : {spread_price:.6f}")
        print(f"  Comisión RT/lote     : ${comm_usd*2:.4f} (${comm_usd:.4f} × 2 lados)")
        print(f"  Fuente comisión      : {'historial MT5' if comm_usd not in [3.5, 5.0] else 'default IC Markets'}")

        df_trades, cap_fin = backtest(df, sym, spread_price, comm_usd, CAPITAL_INI)
        print(f"  {len(df_trades)} trades | Capital final: ${cap_fin:,.2f}")

        m = calc_metricas(df_trades, CAPITAL_INI, cap_fin, spread_price, comm_usd)
        if m is None:
            print("  ❌ Pocos trades"); continue

        print(f"  Retorno : {m['ret_pct']:+,.2f}%")
        print(f"  WR      : {m['win_rate']:.1f}%  |  PF: {m['profit_factor']:.2f}")
        print(f"  Max DD  : {m['max_dd']:.2f}%")

        i_eq  = plot_equity(df_trades, sym, CAPITAL_INI)
        i_dd  = plot_dd(df_trades, sym)
        i_pnl = plot_mensual(df_trades, sym)
        i_pie = plot_resultado_pie(m)

        results[sym] = (m, df_trades, i_eq, i_dd, i_pnl, i_pie)

        # CSV individual en carpeta del script
        out_dir = os.path.dirname(os.path.abspath(__file__))
        df_trades.to_csv(
            os.path.join(out_dir, f'canal_fib_{sym}.csv'),
            index=False)

    mt5.shutdown()

    if not results:
        print("\n❌ Sin resultados."); return

    # Resumen consola
    print(f"\n{'='*55}")
    print(f"  {'SYM':8s} {'RETORNO':>12s} {'WR':>7s} "
          f"{'PF':>7s} {'MAX DD':>8s} {'TRADES':>7s}")
    print(f"{'─'*55}")
    for sym, (m, *_) in results.items():
        print(f"  {sym:8s} {m['ret_pct']:>+11.2f}% "
              f"{m['win_rate']:>6.1f}% "
              f"{m['profit_factor']:>7.2f} "
              f"{m['max_dd']:>7.2f}% "
              f"{m['n_trades']:>7d}")
    print(f"{'='*55}")

    # HTML en carpeta del script
    out_dir   = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(out_dir, 'canal_fib_backtest.html')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(build_html(results))

    print(f"\n✅ Reporte: {html_path}")


if __name__ == "__main__":
    run()
