"""
╔══════════════════════════════════════════════════════════════╗
║   BOT EN VIVO — Canal Fibonacci v3                          ║
║   IC Markets · MT5 · XAUUSD · XAGUSD · BTCUSD · M3        ║
╠══════════════════════════════════════════════════════════════╣
║  Reglas idénticas al backtest v3:                           ║
║  · Canal: max/min últimas 20 barras M3 (sin barra actual)  ║
║  · SHORT: mecha >= 1.191 y cierre < 1.0                    ║
║  · LONG:  mecha <= -0.191 y cierre > 0.0                   ║
║  · Entrada límite en 1.191 (SHORT) / -0.191 (LONG)        ║
║  · TP1: 0.809/0.191 (1/3) → breakeven en SL               ║
║  · TP2: 0.618/0.382 (1/3)                                  ║
║  · TP3: 0.500 (1/3)                                        ║
║  · SL SHORT: 1.382 | SL LONG: -0.382                      ║
║  · Spread aplicado a todos los niveles                      ║
║  · Lote: order_calc_profit (2% del EQUITY)                 ║
║  · Cap lote: equity efectivo máx 5× capital base           ║
║  · Comisión: historial MT5 o default IC Markets            ║
║  · Horarios: metales 01-12h UTC | BTC 14-22h UTC           ║
╚══════════════════════════════════════════════════════════════╝
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import time
import logging
import warnings
import csv
from pathlib import Path
from datetime import datetime, timedelta
import pytz

warnings.filterwarnings('ignore')
UTC = pytz.UTC

TRADES_LOG   = "canal_fib_trades.csv"
TRADES_COLS  = [
    "timestamp", "symbol", "direction", "ticket", "comment",
    "entry", "sl", "tp", "lot",
    "equity_pre", "riesgo_usd",
    "canal_top", "canal_bot",
    "evento",   # ORDEN_ABIERTA | TP_EJECUTADO | SL_EJECUTADO | BREAKEVEN
    "pnl_usd",  # solo en cierre
]

def log_trade(row: dict):
    """Append a row to the trades CSV log."""
    exists = Path(TRADES_LOG).exists()
    with open(TRADES_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADES_COLS, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


# ══════════════════════════════════════════════════════════════
#  MONITOR DE CIERRES — detecta deals ejecutados y los registra
# ══════════════════════════════════════════════════════════════
_last_deal_id = 0

def registrar_cierres(cb=None):
    """
    Revisa el historial de deals recientes y registra en el CSV
    los TP/SL ejecutados de nuestro bot (por MAGIC).
    Notifica al circuit breaker el resultado de cada cierre.
    """
    global _last_deal_id
    desde = datetime.now(UTC) - timedelta(hours=2)
    deals = mt5.history_deals_get(desde, datetime.now(UTC))
    if not deals:
        return

    for d in deals:
        if d.magic != MAGIC:
            continue
        if d.ticket <= _last_deal_id:
            continue
        if d.entry not in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT):
            continue

        _last_deal_id = d.ticket

        es_loss = d.reason == mt5.DEAL_REASON_SL
        evento  = "TP_EJECUTADO" if d.reason == mt5.DEAL_REASON_TP else \
                  "SL_EJECUTADO" if d.reason == mt5.DEAL_REASON_SL else \
                  "CIERRE_MANUAL"

        # Notificar al circuit breaker
        if cb:
            cb.registrar_resultado(es_loss=es_loss)

        log_trade({
            "timestamp":  datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":     d.symbol,
            "direction":  "LONG" if d.type == mt5.DEAL_TYPE_SELL else "SHORT",
            "ticket":     d.deal,
            "comment":    d.comment,
            "entry":      round(d.price, 5),
            "lot":        d.volume,
            "pnl_usd":    round(d.profit + d.commission + d.swap, 2),
            "evento":     evento,
        })
        log.info(f"📋 {evento} | {d.symbol} {d.comment} | "
                 f"precio={d.price:.5f} | PnL=${d.profit:.2f} | "
                 f"comisión=${d.commission:.2f} | swap=${d.swap:.2f}")


# ══════════════════════════════════════════════════════════════
#  CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════
SYMBOLS      = ["XAUUSD", "XAGUSD", "BTCUSD"]
TIMEFRAME    = mt5.TIMEFRAME_M3
CANAL_LENGTH = 20
RISK_PCT     = 0.02          # 2% del EQUITY (no del balance)

SHORT_ENTRY  =  1.191
SHORT_TP1    =  0.809
SHORT_TP2    =  0.618
SHORT_TP3    =  0.500
SHORT_SL     =  1.382

LONG_ENTRY   = -0.191
LONG_TP1     =  0.191
LONG_TP2     =  0.382
LONG_TP3     =  0.500
LONG_SL      = -0.382

MAGIC         = 20250501
SCAN_INTERVAL = 20           # segundos entre escaneos
DEVIATION     = 20           # slippage máximo en puntos

# Cap de lote — idéntico al backtest v3
# El lote deja de escalar cuando el equity supera CAPITAL_INI × LOT_CAP_X
# Garantiza que el 2% siga siendo 2% real aunque el equity crezca mucho
CAPITAL_INI  = 10_000.0     # capital base de referencia (ajustar al tuyo)
LOT_CAP_X    = 5.0          # máximo 5× = $50k base para $10k ini

# Comisiones default IC Markets Raw (USD/lote/lado)
COMM_DEFAULT = {
    "XAUUSD": 3.50,
    "XAGUSD": 3.50,
    "BTCUSD": 5.00,
}

# Horarios operativos (UTC) — alineados con el backtest
SESSION_HOURS = {
    "XAUUSD": list(range(1, 13)),   # 01:00–12:00 UTC
    "XAGUSD": list(range(1, 13)),   # 01:00–12:00 UTC
    "BTCUSD": list(range(14, 22)),  # 14:00–22:00 UTC
}


# ══════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("bot_canal_fib_v2.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
#  CONEXIÓN
# ══════════════════════════════════════════════════════════════
def connect():
    if not mt5.initialize():
        log.error(f"MT5 init: {mt5.last_error()}")
        return False
    info = mt5.account_info()
    if info is None:
        log.error("❌ Sin cuenta conectada. Abre MT5 primero.")
        return False
    log.info(f"✅ {info.company} | {info.server} | {info.login}")
    log.info(f"   Balance: ${info.balance:,.2f} | Equity: ${info.equity:,.2f}")
    return True


def check_connection():
    if mt5.account_info() is None:
        log.warning("Conexión perdida — reconectando...")
        return connect()
    return True


# ══════════════════════════════════════════════════════════════
#  COSTOS REALES
# ══════════════════════════════════════════════════════════════
_costos_cache = {}

def get_costos(symbol):
    """
    Spread: ask − bid en unidades de precio.
    Comisión: historial MT5 o default IC Markets Raw.
    Cachea los valores para no recalcular en cada ciclo.
    """
    if symbol in _costos_cache:
        return _costos_cache[symbol]

    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)

    # Spread en precio
    spread_price = 0.0
    if tick and tick.bid > 0:
        raw = tick.ask - tick.bid
        if raw / max(tick.bid, 1e-10) < 0.02:
            spread_price = raw

    # Comisión desde historial
    comm_usd = 0.0
    try:
        desde = datetime.now(UTC) - timedelta(days=30)
        deals = mt5.history_deals_get(desde, datetime.now(UTC))
        if deals and tick and tick.bid > 0 and info and info.trade_contract_size > 0:
            comms = [(abs(d.commission), d.volume) for d in deals
                     if d.symbol == symbol and d.commission != 0 and d.volume > 0]
            if comms:
                cv, vv = zip(*comms)
                cpl = np.mean(cv) / np.mean(vv)
                notional = tick.bid * info.trade_contract_size
                if notional > 0 and 0.001 < cpl / notional * 100 < 0.1:
                    comm_usd = cpl
    except Exception:
        pass

    if comm_usd == 0.0:
        comm_usd = COMM_DEFAULT.get(symbol, 3.50)

    _costos_cache[symbol] = (spread_price, comm_usd)
    log.info(f"   {symbol} — spread: {spread_price:.6f} | "
             f"comisión: ${comm_usd:.2f}/lote/lado")
    return spread_price, comm_usd


# ══════════════════════════════════════════════════════════════
#  FIBONACCI
# ══════════════════════════════════════════════════════════════
def fib(top, bot, level):
    return bot + level * (top - bot)


# ══════════════════════════════════════════════════════════════
#  SIZING — idéntico al backtest v2
# ══════════════════════════════════════════════════════════════
def calc_lot(symbol, entry_px, sl_px, direction):
    """
    Lote basado en 2% del EQUITY usando order_calc_profit.
    Cap de lote: el equity efectivo no supera CAPITAL_INI × LOT_CAP_X
    para garantizar que el 2% siga siendo real aunque el equity crezca.
    """
    account = mt5.account_info()
    info    = mt5.symbol_info(symbol)
    if account is None or info is None:
        return info.volume_min if info else 0.01

    # Cap de equity — idéntico al backtest v3
    equity_efectivo = min(account.equity, CAPITAL_INI * LOT_CAP_X)
    risk_usd        = equity_efectivo * RISK_PCT

    if direction == "SHORT":
        order_type = mt5.ORDER_TYPE_SELL
    else:
        order_type = mt5.ORDER_TYPE_BUY

    pnl_1lot = mt5.order_calc_profit(order_type, symbol, 1.0, entry_px, sl_px)

    if pnl_1lot is None or abs(pnl_1lot) < 1e-10:
        # Fallback tick_value
        tick_v = info.trade_tick_value
        tick_s = info.trade_tick_size
        if tick_s > 0 and tick_v > 0:
            sl_ticks = abs(entry_px - sl_px) / tick_s
            pnl_1lot_abs = sl_ticks * tick_v
        else:
            return info.volume_min
    else:
        pnl_1lot_abs = abs(pnl_1lot)

    if pnl_1lot_abs <= 0:
        return info.volume_min

    lot  = risk_usd / pnl_1lot_abs
    step = info.volume_step
    lot  = max(info.volume_min,
               min(round(lot / step) * step, info.volume_max))
    return round(lot, 8)


def lot_tercio(symbol, lot_total):
    """Divide el lote total en 3 partes iguales respetando el step."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return round(lot_total / 3, 2)
    step = info.volume_step
    t    = round(lot_total / 3 / step) * step
    return max(info.volume_min, round(t, 8))


# ══════════════════════════════════════════════════════════════
#  DATOS DE MERCADO
# ══════════════════════════════════════════════════════════════
def get_bars(symbol, n=60):
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, n)
    if rates is None or len(rates) < CANAL_LENGTH + 5:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    return df[["open", "high", "low", "close"]]


# ══════════════════════════════════════════════════════════════
#  DETECCIÓN DE SEÑAL — idéntica al backtest
# ══════════════════════════════════════════════════════════════
def detect_signal(symbol, last_bar_times):
    df = get_bars(symbol)
    if df is None:
        return None

    # Última vela cerrada = índice -2
    bar      = df.iloc[-2]
    bar_time = df.index[-2]

    # No reprocesar la misma barra
    if last_bar_times.get(symbol) == bar_time:
        return None

    # Filtro horario
    if bar_time.hour not in SESSION_HOURS.get(symbol, list(range(24))):
        last_bar_times[symbol] = bar_time
        return None

    # Canal: 20 barras ANTES de la barra de señal (sin incluirla)
    # Índice -2 es la señal, entonces el canal es iloc[-22:-2]
    canal_window = df.iloc[-22:-2]
    canal_top    = float(canal_window["high"].max())
    canal_bot    = float(canal_window["low"].min())
    canal_rng    = canal_top - canal_bot

    if canal_rng == 0:
        last_bar_times[symbol] = bar_time
        return None

    c_high  = float(bar["high"])
    c_low   = float(bar["low"])
    c_close = float(bar["close"])

    # Condición de señal
    entry_short_raw = fib(canal_top, canal_bot, SHORT_ENTRY)
    entry_long_raw  = fib(canal_top, canal_bot, LONG_ENTRY)

    direction = None
    if c_high >= entry_short_raw and c_close < canal_top:
        direction = "SHORT"
    elif c_low <= entry_long_raw and c_close > canal_bot:
        direction = "LONG"

    last_bar_times[symbol] = bar_time

    if direction is None:
        return None

    return {
        "symbol":     symbol,
        "direction":  direction,
        "canal_top":  canal_top,
        "canal_bot":  canal_bot,
        "canal_rng":  canal_rng,
        "bar_time":   bar_time,
    }


# ══════════════════════════════════════════════════════════════
#  COLOCAR ÓRDENES — spread aplicado en niveles
# ══════════════════════════════════════════════════════════════
def place_orders(signal):
    symbol    = signal["symbol"]
    direction = signal["direction"]
    top       = signal["canal_top"]
    bot       = signal["canal_bot"]

    # No abrir si ya hay posición u orden activa
    if mt5.positions_get(symbol=symbol):
        log.debug(f"  {symbol}: posición ya abierta — omitiendo")
        return False
    if mt5.orders_get(symbol=symbol):
        log.debug(f"  {symbol}: orden pendiente — omitiendo")
        return False

    spread, comm = get_costos(symbol)
    info         = mt5.symbol_info(symbol)
    digits       = info.digits if info else 5

    # ── Niveles con spread — idéntico al backtest ─────────────
    if direction == "SHORT":
        entry_px = fib(top, bot, SHORT_ENTRY)
        tp1_px   = fib(top, bot, SHORT_TP1) - spread
        tp2_px   = fib(top, bot, SHORT_TP2) - spread
        tp3_px   = fib(top, bot, SHORT_TP3) - spread
        sl_px    = fib(top, bot, SHORT_SL)  + spread
        order_type = mt5.ORDER_TYPE_SELL_LIMIT
    else:
        entry_px = fib(top, bot, LONG_ENTRY) + spread
        tp1_px   = fib(top, bot, LONG_TP1)   + spread
        tp2_px   = fib(top, bot, LONG_TP2)   + spread
        tp3_px   = fib(top, bot, LONG_TP3)   + spread
        sl_px    = fib(top, bot, LONG_SL)    + spread
        order_type = mt5.ORDER_TYPE_BUY_LIMIT

    # Calcular lote total sobre 2% del equity
    lot_total = calc_lot(symbol, entry_px, sl_px, direction)
    lot_1_3   = lot_tercio(symbol, lot_total)

    if lot_1_3 <= 0:
        log.warning(f"  {symbol}: lote inválido — omitiendo")
        return False

    # Verificar margen disponible
    account    = mt5.account_info()
    margin_req = mt5.order_calc_margin(order_type, symbol,
                                        lot_total, entry_px)
    if margin_req and account and margin_req > account.margin_free * 0.90:
        log.warning(f"  {symbol}: margen insuficiente "
                    f"(req ${margin_req:.2f} | libre ${account.margin_free:.2f})")
        return False

    # ── Enviar 3 órdenes límite ───────────────────────────────
    tickets = []
    for tp_px, comment in [
        (tp1_px, f"CF_{'S' if direction=='SHORT' else 'L'}_TP1"),
        (tp2_px, f"CF_{'S' if direction=='SHORT' else 'L'}_TP2"),
        (tp3_px, f"CF_{'S' if direction=='SHORT' else 'L'}_TP3"),
    ]:
        request = {
            "action":       mt5.TRADE_ACTION_PENDING,
            "symbol":       symbol,
            "volume":       lot_1_3,
            "type":         order_type,
            "price":        round(entry_px, digits),
            "sl":           round(sl_px,    digits),
            "tp":           round(tp_px,    digits),
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
            "magic":        MAGIC,
            "comment":      comment,
            "deviation":    DEVIATION,
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            tickets.append(result.order)
        else:
            rc = result.retcode if result else "None"
            cm = result.comment if result else mt5.last_error()
            log.warning(f"  {symbol} {comment}: error {rc} — {cm}")

    if tickets:
        account       = mt5.account_info()
        equity_ef     = min(account.equity, CAPITAL_INI * LOT_CAP_X) if account else 0
        risk_usd      = equity_ef * RISK_PCT
        log.info(
            f"✅ {symbol} {direction} | "
            f"entry={entry_px:.5f} | "
            f"SL={sl_px:.5f} | "
            f"TP1={tp1_px:.5f} TP2={tp2_px:.5f} TP3={tp3_px:.5f} | "
            f"lot×3={lot_1_3} (total={lot_total:.4f}) | "
            f"equity_ef=${equity_ef:,.0f} | "
            f"riesgo=${risk_usd:.2f} ({RISK_PCT*100:.0f}%) | "
            f"tickets={tickets}"
        )
        # Registrar apertura en CSV
        log_trade({
            "timestamp":  datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":     symbol,
            "direction":  direction,
            "ticket":     str(tickets),
            "comment":    f"CF_{'S' if direction=='SHORT' else 'L'}_TP1/2/3",
            "entry":      round(entry_px, 5),
            "sl":         round(sl_px, 5),
            "tp":         f"{tp1_px:.5f}/{tp2_px:.5f}/{tp3_px:.5f}",
            "lot":        lot_1_3,
            "equity_pre": round(equity_ef, 2),
            "riesgo_usd": round(risk_usd, 2),
            "canal_top":  round(top, 5),
            "canal_bot":  round(bot, 5),
            "evento":     "ORDEN_ABIERTA",
        })
        return True

    return False


# ══════════════════════════════════════════════════════════════
#  GESTIÓN BREAKEVEN — cuando TP1 se ejecuta
# ══════════════════════════════════════════════════════════════
def manage_breakeven():
    """
    Detecta cuando TP1 ya se ejecutó (no hay orden TP1 pendiente)
    y mueve el SL de TP2 y TP3 al precio de entrada (breakeven).
    """
    positions = mt5.positions_get()
    if not positions:
        return

    from collections import defaultdict
    sym_pos = defaultdict(list)
    for p in positions:
        if p.magic == MAGIC:
            sym_pos[p.symbol].append(p)

    for symbol, pos_list in sym_pos.items():
        orders = mt5.orders_get(symbol=symbol) or []
        tp1_pending = any(
            o.magic == MAGIC and "TP1" in o.comment
            for o in orders
        )
        if tp1_pending:
            continue  # TP1 aún no se ejecutó

        info = mt5.symbol_info(symbol)
        if info is None:
            continue

        for p in pos_list:
            if "TP2" not in p.comment and "TP3" not in p.comment:
                continue

            entry = p.price_open
            pt    = info.point

            # LONG: SL debe subir al entry
            if p.type == mt5.POSITION_TYPE_BUY and p.sl < entry - pt:
                res = mt5.order_send({
                    "action":   mt5.TRADE_ACTION_SLTP,
                    "symbol":   symbol,
                    "position": p.ticket,
                    "sl":       round(entry, info.digits),
                    "tp":       p.tp,
                })
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    log.info(f"🔒 BREAKEVEN | {symbol} {p.comment} | "
                             f"SL → {entry:.5f}")

            # SHORT: SL debe bajar al entry
            elif p.type == mt5.POSITION_TYPE_SELL and p.sl > entry + pt:
                res = mt5.order_send({
                    "action":   mt5.TRADE_ACTION_SLTP,
                    "symbol":   symbol,
                    "position": p.ticket,
                    "sl":       round(entry, info.digits),
                    "tp":       p.tp,
                })
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    log.info(f"🔒 BREAKEVEN | {symbol} {p.comment} | "
                             f"SL → {entry:.5f}")


# ══════════════════════════════════════════════════════════════
#  CIRCUIT BREAKER
# ══════════════════════════════════════════════════════════════
CB_MAX_CONSEC_LOSS  = 3      # pérdidas consecutivas → pausa temporal
CB_PAUSA_HORAS      = 4      # horas de pausa tras CB nivel 1
CB_MAX_DD_DIARIO    = 0.04   # 4% DD en el día → no operar más hoy
CB_MAX_DD_TOTAL     = 0.10   # 10% DD acumulado → detención completa

class CircuitBreaker:
    def __init__(self, equity_ini):
        self.equity_ini      = equity_ini     # equity al arrancar el bot
        self.equity_dia      = equity_ini     # equity al inicio del día
        self.consec_loss     = 0              # pérdidas consecutivas actuales
        self.pausa_hasta     = None           # datetime UTC fin de pausa temporal
        self.detenido        = False          # detención completa
        self.fecha_dia       = datetime.now(UTC).date()

    def nuevo_dia(self):
        hoy = datetime.now(UTC).date()
        if hoy != self.fecha_dia:
            self.fecha_dia  = hoy
            acct = mt5.account_info()
            self.equity_dia = acct.equity if acct else self.equity_ini
            log.info(f"🔄 Nuevo día — equity reset: ${self.equity_dia:,.2f}")

    def registrar_resultado(self, es_loss: bool):
        """Llamar después de cada cierre de trade."""
        if es_loss:
            self.consec_loss += 1
            log.info(f"⚠️  Pérdidas consecutivas: {self.consec_loss}/{CB_MAX_CONSEC_LOSS}")
            if self.consec_loss >= CB_MAX_CONSEC_LOSS:
                self.pausa_hasta = datetime.now(UTC) + timedelta(hours=CB_PAUSA_HORAS)
                log.warning(
                    f"🛑 CIRCUIT BREAKER Nivel 1 — {self.consec_loss} pérdidas consecutivas | "
                    f"Pausa hasta {self.pausa_hasta.strftime('%H:%M')} UTC ({CB_PAUSA_HORAS}h)"
                )
        else:
            if self.consec_loss > 0:
                log.info(f"✅ Racha de pérdidas cortada (había {self.consec_loss})")
            self.consec_loss = 0

    def puede_operar(self) -> bool:
        """Retorna True si el bot puede abrir nuevas operaciones."""
        if self.detenido:
            return False

        ahora  = datetime.now(UTC)
        acct   = mt5.account_info()
        equity = acct.equity if acct else self.equity_ini

        # Verificar DD total
        dd_total = (self.equity_ini - equity) / self.equity_ini
        if dd_total >= CB_MAX_DD_TOTAL:
            self.detenido = True
            log.error(
                f"🚨 CIRCUIT BREAKER Nivel 3 — DD total {dd_total*100:.1f}% "
                f"supera límite {CB_MAX_DD_TOTAL*100:.0f}% | "
                f"DETENCIÓN COMPLETA — requiere reinicio manual"
            )
            return False

        # Verificar DD diario
        dd_diario = (self.equity_dia - equity) / self.equity_dia
        if dd_diario >= CB_MAX_DD_DIARIO:
            log.warning(
                f"🛑 CIRCUIT BREAKER Nivel 2 — DD diario {dd_diario*100:.1f}% "
                f"supera límite {CB_MAX_DD_DIARIO*100:.0f}% | "
                f"Sin operaciones por hoy"
            )
            return False

        # Verificar pausa temporal
        if self.pausa_hasta and ahora < self.pausa_hasta:
            mins = int((self.pausa_hasta - ahora).total_seconds() / 60)
            if mins % 30 == 0:  # log cada 30 min para no saturar
                log.info(f"⏸  Pausa activa — reanuda en {mins} min")
            return False
        elif self.pausa_hasta and ahora >= self.pausa_hasta:
            log.info(f"▶️  Pausa terminada — reanudando operaciones")
            self.pausa_hasta  = None
            self.consec_loss  = 0

        return True


# ══════════════════════════════════════════════════════════════
#  LOOP PRINCIPAL
# ══════════════════════════════════════════════════════════════
def run():
    log.info("=" * 62)
    log.info("  BOT Canal Fibonacci v3 — IC Markets")
    log.info(f"  Activos : {', '.join(SYMBOLS)} | M3")
    log.info(f"  Riesgo  : {RISK_PCT*100:.0f}% del EQUITY por trade")
    log.info(f"  Cap lote: equity máx ${CAPITAL_INI*LOT_CAP_X:,.0f} ({LOT_CAP_X:.0f}× ${CAPITAL_INI:,.0f})")
    log.info(f"  Horarios: XAU/XAG 01-12h UTC | BTC 14-22h UTC")
    log.info(f"  Circuit breaker: {CB_MAX_CONSEC_LOSS} pérdidas → pausa {CB_PAUSA_HORAS}h | "
             f"DD día {CB_MAX_DD_DIARIO*100:.0f}% | DD total {CB_MAX_DD_TOTAL*100:.0f}%")
    log.info("=" * 62)

    if not connect():
        return

    # Activar símbolos y precargar costos
    available = []
    for sym in SYMBOLS:
        if mt5.symbol_info(sym) is None:
            log.warning(f"  ⚠️  {sym} no disponible — omitido")
            continue
        mt5.symbol_select(sym, True)
        get_costos(sym)
        available.append(sym)
        log.info(f"  ✅ {sym}")

    if not available:
        log.error("❌ Sin símbolos disponibles.")
        return

    # Inicializar circuit breaker con el equity actual
    acct = mt5.account_info()
    cb   = CircuitBreaker(equity_ini=acct.equity if acct else CAPITAL_INI)
    log.info(f"\n🚀 Bot activo | equity ini: ${cb.equity_ini:,.2f}\n")

    last_bar_times = {}
    ciclo = 0

    while True:
        try:
            ciclo += 1

            if not check_connection():
                time.sleep(30)
                continue

            ahora = datetime.now(UTC)

            # Fin de semana
            if ahora.weekday() >= 5:
                if ciclo % 30 == 0:
                    log.info("Fin de semana — en espera...")
                time.sleep(120)
                continue

            # Reset diario del circuit breaker
            cb.nuevo_dia()

            # Detención completa — requiere reinicio manual
            if cb.detenido:
                if ciclo % 60 == 0:
                    log.error("🚨 Bot detenido por circuit breaker nivel 3. Reinicia manualmente.")
                time.sleep(60)
                continue

            # Gestionar breakeven en posiciones abiertas
            manage_breakeven()

            # Registrar cierres y actualizar circuit breaker
            registrar_cierres(cb)

            # Escanear señales — solo si el circuit breaker lo permite
            if cb.puede_operar():
                for symbol in available:
                    signal = detect_signal(symbol, last_bar_times)
                    if signal:
                        log.info(
                            f"🔔 SEÑAL | {symbol} {signal['direction']} | "
                            f"{signal['bar_time'].strftime('%H:%M')} UTC | "
                            f"Canal: {signal['canal_bot']:.5f}–{signal['canal_top']:.5f}"
                        )
                        place_orders(signal)

            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log.info("\n⛔ Bot detenido por usuario.")
            mt5.shutdown()
            break
        except Exception as e:
            log.error(f"Error inesperado: {e}", exc_info=True)
            time.sleep(30)


if __name__ == "__main__":
    run()
