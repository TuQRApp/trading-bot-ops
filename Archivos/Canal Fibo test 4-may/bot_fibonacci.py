"""
=============================================================================
  BOT LIVE — Fibonacci Channel Mean Reversion  v1.0
  Estrategia: Spike fuera del canal AlgoAlpha → retorno al interior
=============================================================================
  Timeframe:    M3  |  Canal + Smart Money en la misma temporalidad
  Entrada:      Nivel Fibonacci -0.191 (long) / 1.191 (short)
  TP:           0.382 (long) / 0.618 (short)
  SL:           -0.382 (long) / 1.382 (short)   →  R:R ~3:1

  Seguridad:
    ✅ Filtro spread máximo por barra
    ✅ Deduplicación de señales por símbolo
    ✅ Validación de orden confirmada por broker
    ✅ Límite de posiciones simultáneas
    ✅ Reconexión automática MT5 con backoff
    ✅ Validación de tamaño de lote
    ✅ Alertas Telegram
    ✅ Heartbeat horario
    ✅ Límite de pérdida diaria
    ✅ Detección de gaps en datos
    ✅ Timezone UTC en todo el código
    ✅ Control básico de correlación
    ✅ Circuit breaker
    ✅ Salida segura al detener
=============================================================================
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import os, sys, json, time, logging, requests, signal
from datetime import datetime, timedelta
from collections import defaultdict
import pytz
import warnings
warnings.filterwarnings("ignore")

# ── Importar indicadores del backtest ─────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from backtest_fibonacci_channels import (
    compute_channel, compute_smart_money,
    NORM_LENGTH, BOX_LENGTH, MIN_DURATION,
    SM_INDEX_P, SM_RSI_P, SM_NORM_P,
    ALL_SYMBOLS,
)

UTC = pytz.utc

# =============================================================================
#  CONFIG — ajustar antes de correr
# =============================================================================
RISK_PCT          = 0.25    # % riesgo por trade (0.25 en fase observación, luego 1.0)
SM_THRESHOLD      = 0.3     # net_index mínimo Smart Money
COOLDOWN_BARS     = 3       # barras entre señales del mismo canal
MIN_TICK_VOL      = 150     # liquidez mínima por barra
MAX_SPREAD_MULT   = 2.0     # spread máximo = N × promedio del símbolo (más estricto en live)
MAX_POSITIONS     = 3       # máximo posiciones abiertas simultáneas (reducido por apalancamiento 100x)
MAX_CORR_CLASS    = 1       # máximo posiciones por clase de activo — 1 a la vez con 100x
DAILY_LOSS_LIMIT  = 10.0    # % pérdida diaria máxima → circuit breaker
                            # Con 100x y RISK_PCT=0.25%, necesitas ~40 SL seguidos para tocar este límite.
                            # Se mantiene como red de seguridad ante slippage extremo o bug de sizing.
WEEKLY_LOSS_LIMIT = 20.0    # % pérdida semanal máxima → circuit breaker
WARMUP_BARS       = 700     # barras mínimas para calcular indicadores
BARS_HISTORY      = 1500    # barras a pedir en cada refresh

# Telegram (completar con tu bot token y chat ID)
TG_TOKEN   = "8621269639:AAHUoJrijb8vjSeVnU-WNX2KRH5oW9o7HGU"
TG_CHAT_ID = "6864631928"

# Directorios
BOT_DIR  = "bot_fib"
LOG_DIR  = os.path.join(BOT_DIR, "logs")
for d in [BOT_DIR, LOG_DIR]:
    os.makedirs(d, exist_ok=True)

# =============================================================================
#  LOGGING
# =============================================================================
ts_run = datetime.now().strftime("%Y%m%d_%H%M%S")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(
            os.path.join(LOG_DIR, f"bot_{ts_run}.log"),
            encoding="utf-8"
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# =============================================================================
#  TELEGRAM
# =============================================================================
def tg(msg: str, level="info"):
    """Envía mensaje a Telegram. Falla silenciosamente si no está configurado."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    icons = {"info": "ℹ️", "trade": "📈", "warn": "⚠️", "error": "🚨", "ok": "✅"}
    icon  = icons.get(level, "")
    text  = f"{icon} *FibBot* | {datetime.now(UTC).strftime('%H:%M:%S UTC')}\n{msg}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as e:
        log.debug(f"Telegram error: {e}")


# =============================================================================
#  MT5 — conexión y reconexión automática
# =============================================================================
def connect_mt5(retries=5, backoff=10) -> bool:
    """Conecta al terminal MT5 ya abierto. Reintenta con backoff exponencial."""
    for attempt in range(1, retries + 1):
        if mt5.initialize():
            acc = mt5.account_info()
            if acc:
                log.info(f"MT5 conectado: #{acc.login}  balance=${acc.balance:.2f}  @ {acc.server}")
                tg(f"Bot iniciado\nCuenta: #{acc.login} | Balance: ${acc.balance:.2f}", "ok")
                return True
        wait = backoff * attempt
        log.warning(f"MT5 init falló (intento {attempt}/{retries}). Reintentando en {wait}s...")
        time.sleep(wait)
    log.error("No se pudo conectar a MT5.")
    tg("Error crítico: no se pudo conectar a MT5", "error")
    return False


def ensure_connected() -> bool:
    """Verifica conexión activa y reconecta si es necesario."""
    info = mt5.account_info()
    if info is not None:
        return True
    log.warning("MT5 desconectado. Intentando reconexión...")
    mt5.shutdown()
    time.sleep(2)
    return connect_mt5(retries=3, backoff=5)


# =============================================================================
#  CLASES DE ACTIVOS (para control de correlación)
# =============================================================================
ASSET_CLASS = {}
for s in ["XAUUSD","XAGUSD"]:
    ASSET_CLASS[s] = "metals"
for s in ["BTCUSD","ETHUSD"]:
    ASSET_CLASS[s] = "crypto"
for s in ["US30","US500","USTEC","US2000"]:
    ASSET_CLASS[s] = "us_idx"
for s in ["DE40","UK100","F40","ES35","IT40","JP225","AUS200","HK50","STOXX50","TecDE30"]:
    ASSET_CLASS[s] = "eu_as_idx"


# =============================================================================
#  DATOS — descarga barras M3
# =============================================================================
def get_bars(symbol: str, n: int = BARS_HISTORY) -> pd.DataFrame | None:
    """Descarga las últimas n barras M3 del terminal MT5."""
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M3, 0, n)
    if rates is None or len(rates) < WARMUP_BARS:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    return df[["open","high","low","close","tick_volume","spread"]].rename(
        columns={"tick_volume":"volume"}
    )


def detect_gap(df: pd.DataFrame, sym: str) -> bool:
    """
    Detecta gaps anómalos entre barras.
    Un gap es cuando el open de la última barra difiere >3×ATR del close anterior.
    """
    if len(df) < 20:
        return False
    close_prev  = df["close"].iloc[-2]
    open_curr   = df["open"].iloc[-1]
    atr         = df["high"].sub(df["low"]).rolling(14).mean().iloc[-1]
    gap_size    = abs(open_curr - close_prev)
    if gap_size > 3 * atr:
        log.warning(f"  {sym}: gap detectado ({gap_size:.4f} > 3×ATR={3*atr:.4f}) — señal ignorada")
        return True
    return False


# =============================================================================
#  SIZING
# =============================================================================
def calc_lots(symbol: str, entry: float, sl: float, equity: float) -> float | None:
    """
    Calcula lotes según riesgo % del equity.
    Valida contra los límites del broker antes de retornar.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        return None

    risk_usd     = equity * RISK_PCT / 100.0
    dist         = abs(entry - sl)
    if dist <= 0:
        return None

    risk_per_lot = (dist / info.trade_tick_size) * info.trade_tick_value
    if risk_per_lot <= 0:
        return None

    raw  = risk_usd / risk_per_lot
    step = info.volume_step
    lots = round(raw / step) * step
    lots = max(info.volume_min, min(lots, info.volume_max))

    # Validación adicional: no más de 10× el lote mínimo en fase observación
    max_obs_lots = info.volume_min * 10
    if RISK_PCT <= 0.5 and lots > max_obs_lots:
        lots = max_obs_lots
        log.debug(f"  {symbol}: lots recortados a {lots} (fase observación)")

    return lots


# =============================================================================
#  EJECUCIÓN DE ÓRDENES
# =============================================================================
def send_order(symbol: str, direction: str, entry: float,
               tp: float, sl: float, lots: float) -> dict | None:
    """
    Envía una orden de mercado a MT5 y confirma el fill.
    Retorna el resultado de la orden o None si falló.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        log.error(f"  {symbol}: symbol_info None al enviar orden")
        return None

    # Asegurar símbolo visible
    if not info.visible:
        mt5.symbol_select(symbol, True)
        time.sleep(0.1)

    order_type = mt5.ORDER_TYPE_BUY if direction == "long" else mt5.ORDER_TYPE_SELL
    price      = mt5.symbol_info_tick(symbol).ask if direction == "long" \
                 else mt5.symbol_info_tick(symbol).bid

    digits = info.digits

    # Verificar spread live en el momento exacto de ejecución
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log.warning(f"  {symbol}: no se pudo obtener tick al ejecutar")
        return None
    live_spread_pts = (tick.ask - tick.bid) / info.point
    # Comparar contra el spread promedio del símbolo
    sym_info_spread = float(info.spread)   # spread típico según MT5
    max_allowed_pts = max(sym_info_spread * MAX_SPREAD_MULT, sym_info_spread + 5)
    if live_spread_pts > max_allowed_pts:
        log.info(f"  {symbol}: spread live elevado al ejecutar "
                 f"({live_spread_pts:.1f} pts > {max_allowed_pts:.1f} pts) — orden cancelada")
        tg(f"⚠️ {symbol}: orden cancelada por spread live elevado "
           f"({live_spread_pts:.1f} pts)", "warn")
        return None

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       lots,
        "type":         order_type,
        "price":        price,
        "sl":           round(sl, digits),
        "tp":           round(tp, digits),
        "deviation":    10,
        "magic":        202601,
        "comment":      "FibBot_MR",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        code = result.retcode if result else "None"
        log.error(f"  {symbol}: orden rechazada — retcode={code}")
        return None

    log.info(f"  ✅ {symbol} {direction.upper()} ejecutado | "
             f"ticket={result.order}  lots={lots}  fill={result.price:.4f}  "
             f"TP={round(tp,digits)}  SL={round(sl,digits)}")
    return {
        "ticket":    result.order,
        "symbol":    symbol,
        "direction": direction,
        "lots":      lots,
        "fill_price":result.price,
        "tp":        round(tp, digits),
        "sl":        round(sl, digits),
        "open_time": datetime.now(UTC).isoformat(),
    }


# =============================================================================
#  INDICADORES (reutilizados del backtest)
# =============================================================================
def fib(top, bot, level):
    return bot + level * (top - bot)


def compute_signals(df: pd.DataFrame) -> dict:
    """
    Calcula el canal AlgoAlpha y Smart Money sobre las barras dadas.
    Retorna el estado del canal en la última barra completada ([-2]).
    """
    ch  = compute_channel(df)
    sm  = compute_smart_money(df)

    # Usamos la barra [-2]: la última barra CERRADA ([-1] es la barra en curso)
    i   = len(df) - 2
    if i < WARMUP_BARS:
        return {"ready": False}

    return {
        "ready":    True,
        "ucl":      bool(ch["ucl"][i]),
        "duration": int(ch["duration"][i]),
        "h":        float(ch["h"][i]),
        "l":        float(ch["l"][i]),
        "sm":       float(sm[i]) if not np.isnan(sm[i]) else 0.0,
        "close":    float(df["close"].iloc[i]),
        "high":     float(df["high"].iloc[i]),
        "low":      float(df["low"].iloc[i]),
        "volume":   float(df["volume"].iloc[i]),
        "spread":   float(df["spread"].iloc[i]),
        # Spread promedio robusto: percentil 75 excluye outliers de noticias
        "avg_spread": float(df["spread"].quantile(0.75)),
    }


# =============================================================================
#  ESTADO DEL BOT
# =============================================================================
class BotState:
    def __init__(self):
        self.active_channels:  dict = {}   # sym → canal activo
        self.last_bar_time:    dict = {}   # sym → time del último bar procesado
        self.last_trade_bar:   dict = {}   # sym → índice de barra del último trade
        self.open_positions:   dict = {}   # ticket → info del trade
        self.daily_pnl:        float = 0.0
        self.weekly_pnl:       float = 0.0
        self.circuit_broken:   bool  = False
        self.trades_today:     list  = []
        self.start_balance:    float = 0.0
        self.day_start_balance:float = 0.0
        self.last_heartbeat:   datetime = datetime.now(UTC)
        self.last_day_reset:   str  = ""
        self.last_week_reset:  str  = ""
        self.bar_counter:      dict = defaultdict(int)  # sym → barras procesadas

    def save(self):
        path = os.path.join(BOT_DIR, "state.json")
        data = {
            "active_channels":  self.active_channels,
            "daily_pnl":        self.daily_pnl,
            "weekly_pnl":       self.weekly_pnl,
            "circuit_broken":   self.circuit_broken,
            "start_balance":    self.start_balance,
            "day_start_balance":self.day_start_balance,
            "last_day_reset":   self.last_day_reset,
            "last_week_reset":  self.last_week_reset,
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def load(self):
        path = os.path.join(BOT_DIR, "state.json")
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
            self.active_channels   = data.get("active_channels", {})
            self.daily_pnl         = data.get("daily_pnl", 0.0)
            self.weekly_pnl        = data.get("weekly_pnl", 0.0)
            self.circuit_broken    = data.get("circuit_broken", False)
            self.start_balance     = data.get("start_balance", 0.0)
            self.day_start_balance = data.get("day_start_balance", 0.0)
            self.last_day_reset    = data.get("last_day_reset", "")
            self.last_week_reset   = data.get("last_week_reset", "")
            log.info(f"Estado restaurado: daily_pnl=${self.daily_pnl:.2f}  circuit={self.circuit_broken}")
        except Exception as e:
            log.warning(f"No se pudo cargar estado previo: {e}")


# =============================================================================
#  CIRCUIT BREAKER Y LÍMITES
# =============================================================================
def check_limits(state: BotState) -> bool:
    """
    Verifica límites de pérdida diaria y semanal.
    Activa circuit breaker si se superan.
    """
    if state.circuit_broken:
        return False

    acc = mt5.account_info()
    if acc is None:
        return True

    daily_loss_pct  = (state.daily_pnl / state.day_start_balance * 100
                       if state.day_start_balance > 0 else 0)
    weekly_loss_pct = (state.weekly_pnl / state.start_balance * 100
                       if state.start_balance > 0 else 0)

    if daily_loss_pct < -DAILY_LOSS_LIMIT:
        log.warning(f"🔴 CIRCUIT BREAKER: pérdida diaria {daily_loss_pct:.1f}% > {DAILY_LOSS_LIMIT}%")
        tg(f"🔴 CIRCUIT BREAKER activado\nPérdida diaria: {daily_loss_pct:.1f}%\nNo se abrirán nuevas posiciones hoy.", "error")
        state.circuit_broken = True
        state.save()
        return False

    if weekly_loss_pct < -WEEKLY_LOSS_LIMIT:
        log.warning(f"🔴 CIRCUIT BREAKER: pérdida semanal {weekly_loss_pct:.1f}% > {WEEKLY_LOSS_LIMIT}%")
        tg(f"🔴 CIRCUIT BREAKER semanal\nPérdida: {weekly_loss_pct:.1f}%", "error")
        state.circuit_broken = True
        state.save()
        return False

    return True


def check_correlation(symbol: str, state: BotState) -> bool:
    """Verifica que no se supere el límite de posiciones por clase de activo."""
    asset_class = ASSET_CLASS.get(symbol, "other")
    class_count = sum(
        1 for t in state.open_positions.values()
        if ASSET_CLASS.get(t["symbol"], "other") == asset_class
    )
    if class_count >= MAX_CORR_CLASS:
        log.info(f"  {symbol}: límite correlación alcanzado ({class_count}/{MAX_CORR_CLASS} en {asset_class})")
        return False
    return True


# =============================================================================
#  SINCRONIZACIÓN DE POSICIONES ABIERTAS
# =============================================================================
def sync_positions(state: BotState):
    """
    Sincroniza las posiciones abiertas en MT5 con el estado interno del bot.
    También actualiza el P&L diario desde los deals del día.
    """
    positions = mt5.positions_get(magic=202601)
    if positions is None:
        positions = []

    # Actualizar open_positions con lo que hay en MT5
    current_tickets = {p.ticket for p in positions}
    bot_tickets     = set(state.open_positions.keys())

    # Posiciones cerradas desde el último ciclo → calcular P&L
    closed = bot_tickets - current_tickets
    for ticket in closed:
        trade = state.open_positions.pop(ticket, {})
        if trade:
            # Buscar el deal de cierre
            deals = mt5.history_deals_get(
                datetime.now(UTC) - timedelta(hours=24),
                datetime.now(UTC),
                position=ticket
            )
            if deals:
                pnl = sum(d.profit for d in deals)
                state.daily_pnl  += pnl
                state.weekly_pnl += pnl
                result = "TP" if pnl > 0 else "SL"
                log.info(f"  ← {trade.get('symbol','?')} {trade.get('direction','?').upper()} "
                         f"cerrado | {result} | P&L=${pnl:+.2f} | "
                         f"Daily={state.daily_pnl:+.2f}")
                tg(
                    f"{result} | {trade.get('symbol','?')} {trade.get('direction','?').upper()}\n"
                    f"P&L: ${pnl:+.2f} | Daily: ${state.daily_pnl:+.2f}",
                    "trade"
                )
                state.trades_today.append({
                    "symbol":    trade.get("symbol"),
                    "direction": trade.get("direction"),
                    "result":    result,
                    "pnl":       round(pnl, 2),
                    "time":      datetime.now(UTC).isoformat(),
                })

    # Agregar posiciones nuevas abiertas fuera del bot (poco probable pero seguro)
    for p in positions:
        if p.ticket not in state.open_positions:
            state.open_positions[p.ticket] = {
                "symbol":    p.symbol,
                "direction": "long" if p.type == mt5.ORDER_TYPE_BUY else "short",
                "lots":      p.volume,
                "fill_price":p.price_open,
                "tp":        p.tp,
                "sl":        p.sl,
                "open_time": datetime.fromtimestamp(p.time, UTC).isoformat(),
            }


# =============================================================================
#  RESET DIARIO / SEMANAL
# =============================================================================
def handle_resets(state: BotState):
    """Resetea contadores diarios y semanales en los momentos correctos (UTC)."""
    now     = datetime.now(UTC)
    today   = now.strftime("%Y-%m-%d")
    week    = now.strftime("%Y-W%W")

    if state.last_day_reset != today:
        acc = mt5.account_info()
        if acc:
            state.day_start_balance = acc.balance
        state.daily_pnl     = 0.0
        state.circuit_broken = False   # reset diario del circuit breaker
        state.trades_today  = []
        state.last_day_reset = today
        log.info(f"Reset diario | balance=${state.day_start_balance:.2f}")
        state.save()

    if state.last_week_reset != week:
        acc = mt5.account_info()
        if acc and state.start_balance == 0:
            state.start_balance = acc.balance
        state.weekly_pnl    = 0.0
        state.last_week_reset = week
        log.info(f"Reset semanal")
        state.save()


# =============================================================================
#  HEARTBEAT
# =============================================================================
def maybe_heartbeat(state: BotState):
    """Emite heartbeat horario al log y Telegram."""
    now = datetime.now(UTC)
    if (now - state.last_heartbeat).total_seconds() < 3600:
        return

    acc       = mt5.account_info()
    balance   = acc.balance if acc else 0
    n_pos     = len(state.open_positions)
    n_trades  = len(state.trades_today)
    cb_status = "🔴 ACTIVO" if state.circuit_broken else "🟢 OK"

    msg = (f"💓 Heartbeat\n"
           f"Balance: ${balance:.2f} | Daily P&L: ${state.daily_pnl:+.2f}\n"
           f"Posiciones abiertas: {n_pos} | Trades hoy: {n_trades}\n"
           f"Circuit breaker: {cb_status}")

    log.info(f"HEARTBEAT | balance=${balance:.2f} | daily_pnl=${state.daily_pnl:+.2f} | "
             f"posiciones={n_pos} | trades_hoy={n_trades} | circuit={cb_status}")
    tg(msg, "info")
    state.last_heartbeat = now


# =============================================================================
#  LÓGICA DE SEÑAL POR SÍMBOLO
# =============================================================================
def process_symbol(symbol: str, state: BotState):
    """
    Procesa un símbolo en el ciclo actual:
      1. Descarga barras M3
      2. Detecta gap
      3. Calcula indicadores
      4. Actualiza canal activo
      5. Busca señal de mean reversion
      6. Ejecuta orden si se cumplen todos los filtros
    """
    # ── 1. Datos ──────────────────────────────────────────────────────────────
    df = get_bars(symbol)
    if df is None or len(df) < WARMUP_BARS + 10:
        return

    # ── 2. Deduplicación: solo procesar cuando hay una barra nueva ─────────────
    last_bar = df.index[-2]   # barra cerrada más reciente
    if state.last_bar_time.get(symbol) == last_bar:
        return
    state.last_bar_time[symbol] = last_bar
    state.bar_counter[symbol]  += 1

    # ── 3. Gap detection ───────────────────────────────────────────────────────
    if detect_gap(df, symbol):
        return

    # ── 4. Indicadores ────────────────────────────────────────────────────────
    sig = compute_signals(df)
    if not sig["ready"]:
        return

    bc     = sig["close"]; bh = sig["high"]; bl = sig["low"]
    sm_val = sig["sm"]
    spread = sig["spread"]
    avg_sp = sig["avg_spread"]

    # ── 5. Actualizar canal activo ────────────────────────────────────────────
    ach = state.active_channels.get(symbol)

    # Nuevo canal formado
    if sig["ucl"] and sig["duration"] > MIN_DURATION and ach is None:
        h = sig["h"]; l = sig["l"]
        if not (np.isnan(h) or np.isnan(l)) and h > l:
            ach = {
                "top":     h, "bottom": l,
                "sl_short": fib(h,l,1.382), "en_short": fib(h,l,1.191),
                "tp_short": fib(h,l,0.618), "tp_long":  fib(h,l,0.382),
                "en_long":  fib(h,l,-0.191),"sl_long":  fib(h,l,-0.382),
                "formed_bar": state.bar_counter[symbol],
            }
            state.active_channels[symbol] = ach
            log.info(f"  {symbol}: nuevo canal | top={h:.4f} bot={l:.4f}")

    # Invalidar canal si strong close cruza el nivel SL
    if ach is not None:
        sc = (bc + df["open"].iloc[-2]) / 2.0
        if sc >= ach["sl_short"] or sc <= ach["sl_long"]:
            log.info(f"  {symbol}: canal invalidado (strong close fuera de SL)")
            state.active_channels.pop(symbol, None)
            return

    if ach is None:
        return

    # ── 6. Filtros de entrada ─────────────────────────────────────────────────
    # Ya hay posición abierta en este símbolo → no duplicar
    if any(t["symbol"] == symbol for t in state.open_positions.values()):
        return

    # Cooldown entre trades del mismo símbolo
    bars_since = state.bar_counter[symbol] - state.last_trade_bar.get(symbol, -9999)
    if bars_since < COOLDOWN_BARS:
        return

    # Límite de posiciones totales
    if len(state.open_positions) >= MAX_POSITIONS:
        return

    # Correlación por clase de activo
    if not check_correlation(symbol, state):
        return

    # Liquidez
    if sig["volume"] < MIN_TICK_VOL:
        return

    # Spread
    if avg_sp > 0 and spread > MAX_SPREAD_MULT * avg_sp:
        log.info(f"  {symbol}: spread elevado ({spread:.1f} > {MAX_SPREAD_MULT}×{avg_sp:.1f}) — skip")
        return

    # Circuit breaker
    if not check_limits(state):
        return

    # ── 7. Señal ──────────────────────────────────────────────────────────────
    direction = None

    if bl <= ach["en_long"] and bc > ach["bottom"] and sm_val > SM_THRESHOLD:
        direction = "long"
        entry = ach["en_long"]
        tp    = ach["tp_long"]
        sl    = ach["sl_long"]

    elif bh >= ach["en_short"] and bc < ach["top"] and sm_val < -SM_THRESHOLD:
        direction = "short"
        entry = ach["en_short"]
        tp    = ach["tp_short"]
        sl    = ach["sl_short"]

    if direction is None:
        return

    # ── 8. Sizing y ejecución ─────────────────────────────────────────────────
    acc = mt5.account_info()
    if acc is None:
        return
    equity = acc.equity

    lots = calc_lots(symbol, entry, sl, equity)
    if lots is None or lots <= 0:
        log.warning(f"  {symbol}: sizing inválido (entry={entry} sl={sl})")
        return

    log.info(f"  ▶ SEÑAL {symbol} {direction.upper()} | "
             f"entry={entry:.4f} TP={tp:.4f} SL={sl:.4f} | "
             f"SM={sm_val:.3f} | lots={lots} | spread={spread:.1f}pts")

    tg(
        f"▶ Señal {symbol} {direction.upper()}\n"
        f"Entry: {entry:.4f} | TP: {tp:.4f} | SL: {sl:.4f}\n"
        f"SM: {sm_val:.3f} | Lots: {lots}",
        "trade"
    )

    result = send_order(symbol, direction, entry, tp, sl, lots)
    if result:
        state.open_positions[result["ticket"]] = result
        state.last_trade_bar[symbol] = state.bar_counter[symbol]
        state.save()


# =============================================================================
#  REPORTE HTML DIARIO
# =============================================================================
def gen_daily_report(state: BotState):
    """Genera reporte HTML simple con el resumen del día."""
    ts   = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    acc  = mt5.account_info()
    bal  = acc.balance if acc else 0
    eq   = acc.equity  if acc else 0
    pos  = len(state.open_positions)
    tds  = state.trades_today
    wins = sum(1 for t in tds if t["result"]=="TP")
    pnl  = state.daily_pnl
    wr   = round(wins/len(tds)*100,1) if tds else 0

    def _row(t):
        dc = "#4ade80" if t["direction"] == "long" else "#f87171"
        rc = "#4ade80" if t["result"]    == "TP"   else "#f87171"
        pc = "#4ade80" if t["pnl"]       >= 0      else "#f87171"
        return (
            f"<tr><td>{t['time'][11:16]}</td><td>{t['symbol']}</td>"
            f"<td style='color:{dc}'>{t['direction']}</td>"
            f"<td style='color:{rc}'>{t['result']}</td>"
            f"<td style='color:{pc}'>${t['pnl']:+.2f}</td></tr>"
        )
    rows = "".join(_row(t) for t in tds)

    html = f"""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<title>FibBot — Resumen diario</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,sans-serif;background:#0f0f0f;color:#d4d4d4;padding:24px;font-size:13px}}
h1{{font-size:16px;font-weight:500;margin-bottom:4px}}
.sub{{color:#444;font-size:11px;margin-bottom:18px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px;margin-bottom:18px}}
.kpi{{background:#161616;border:0.5px solid #222;border-radius:8px;padding:12px}}
.kl{{display:block;font-size:9px;color:#444;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}}
.kv{{display:block;font-size:18px;font-weight:600}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{color:#444;font-weight:400;padding:5px 8px;border-bottom:0.5px solid #1e1e1e;text-align:left}}
td{{padding:4px 8px;border-bottom:0.5px solid #191919}}
</style></head><body>
<h1>FibBot — Resumen diario</h1>
<div class="sub">Generado: {ts} | Riesgo: {RISK_PCT}% por trade</div>
<div class="kpis">
  <div class="kpi"><span class="kl">Balance</span><span class="kv">${bal:,.2f}</span></div>
  <div class="kpi"><span class="kl">Equity</span><span class="kv">${eq:,.2f}</span></div>
  <div class="kpi"><span class="kl">P&L diario</span>
    <span class="kv" style="color:{'#4ade80' if pnl>=0 else '#f87171'}">${pnl:+.2f}</span></div>
  <div class="kpi"><span class="kl">Trades hoy</span><span class="kv">{len(tds)}</span></div>
  <div class="kpi"><span class="kl">WR hoy</span><span class="kv">{wr}%</span></div>
  <div class="kpi"><span class="kl">Pos. abiertas</span><span class="kv">{pos}</span></div>
</div>
<table><thead><tr><th>Hora</th><th>Símbolo</th><th>Dir</th><th>Resultado</th><th>P&L</th></tr></thead>
<tbody>{rows if rows else '<tr><td colspan="5" style="color:#444;text-align:center">Sin trades hoy</td></tr>'}</tbody>
</table>
</body></html>"""

    path = os.path.join(BOT_DIR, f"report_{datetime.now(UTC).strftime('%Y%m%d')}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


# =============================================================================
#  SALIDA SEGURA
# =============================================================================
_shutdown = False

def handle_signal(signum, frame):
    global _shutdown
    log.info(f"Señal {signum} recibida — iniciando cierre seguro...")
    tg("Bot detenido manualmente. Posiciones abiertas se mantienen con SL/TP activos.", "warn")
    _shutdown = True

signal.signal(signal.SIGINT,  handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


# =============================================================================
#  MAIN LOOP
# =============================================================================
def main():
    global _shutdown

    log.info("=" * 65)
    log.info("  BOT LIVE — Fibonacci Channel Mean Reversion  v1.0")
    log.info(f"  Inicio: {datetime.now(UTC):%Y-%m-%d %H:%M:%S UTC}")
    log.info(f"  Riesgo: {RISK_PCT}% por trade | Símbolos: {len(ALL_SYMBOLS)}")
    log.info(f"  Max posiciones: {MAX_POSITIONS} | Max por clase: {MAX_CORR_CLASS}")
    log.info(f"  Circuit breaker: {DAILY_LOSS_LIMIT}% diario / {WEEKLY_LOSS_LIMIT}% semanal")
    log.info("=" * 65)

    if not connect_mt5():
        sys.exit(1)

    acc = mt5.account_info()
    state = BotState()
    state.load()
    state.start_balance     = acc.balance
    state.day_start_balance = acc.balance
    handle_resets(state)
    sync_positions(state)
    state.save()

    log.info(f"Balance inicial: ${acc.balance:.2f} | Posiciones heredadas: {len(state.open_positions)}")

    CYCLE_SECS = 30   # revisar cada 30 segundos
    last_report_day = ""

    while not _shutdown:
        cycle_start = time.time()

        # ── Reconexión si MT5 se desconecta ───────────────────────────────────
        if not ensure_connected():
            log.error("No se pudo reconectar a MT5. Esperando 60s...")
            tg("Error: MT5 desconectado. Reintentando...", "error")
            time.sleep(60)
            continue

        # ── Resets diario/semanal ─────────────────────────────────────────────
        handle_resets(state)

        # ── Sincronizar posiciones ────────────────────────────────────────────
        sync_positions(state)

        # ── Heartbeat ─────────────────────────────────────────────────────────
        maybe_heartbeat(state)

        # ── Procesar cada símbolo ─────────────────────────────────────────────
        if not state.circuit_broken:
            for symbol in ALL_SYMBOLS:
                if _shutdown:
                    break
                try:
                    process_symbol(symbol, state)
                except Exception as e:
                    log.error(f"  {symbol}: error inesperado: {e}", exc_info=True)
                    tg(f"Error en {symbol}: {e}", "error")

        # ── Reporte HTML diario (a las 00:05 UTC) ────────────────────────────
        now = datetime.now(UTC)
        today_str = now.strftime("%Y-%m-%d")
        if now.hour == 0 and now.minute < 6 and last_report_day != today_str:
            path = gen_daily_report(state)
            log.info(f"Reporte diario guardado: {path}")
            last_report_day = today_str

        # ── Guardar estado ────────────────────────────────────────────────────
        state.save()

        # ── Esperar hasta el próximo ciclo ────────────────────────────────────
        elapsed = time.time() - cycle_start
        sleep_t = max(0, CYCLE_SECS - elapsed)
        if sleep_t > 0:
            time.sleep(sleep_t)

    # ── Cierre seguro ────────────────────────────────────────────────────────
    log.info("Bot detenido. Posiciones abiertas mantienen SL/TP en MT5.")
    log.info(f"Posiciones abiertas al cierre: {len(state.open_positions)}")
    for ticket, t in state.open_positions.items():
        log.info(f"  #{ticket} {t['symbol']} {t['direction']} | SL={t['sl']} TP={t['tp']}")
    gen_daily_report(state)
    state.save()
    mt5.shutdown()
    log.info("Cierre completo.")


if __name__ == "__main__":
    main()
