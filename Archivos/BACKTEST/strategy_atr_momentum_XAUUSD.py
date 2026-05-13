"""
Strategy 3 — ATR Momentum (Trend Following)

Lógica multi-timeframe:
  D1  régimen  : ADX(14) > adx_thresh  AND  close vs EMA(200)
  H4  bias     : Donchian channel breakout (donchian_n barras completadas)
  M15 entrada  : ATR(14) > ATR_MA(20) × atr_expansion  (impulso confirmado)

Gestión del trade:
  SL  = entry ± atr_mult_sl × ATR14_M15
  TP1 = 1.5 × riesgo  →  mueve SL a breakeven
  TP2 = tp_ratio × riesgo
  Sin time exit — cierra por SL o TP únicamente

Mejores parámetros (IS/OOS 2023-2026, ~3 años M15):
  XAUUSD: donchian_n=30, adx_thresh=25, atr_expansion=1.2, atr_mult_sl=2.0, tp_ratio=3.0

Universo testeado:
  Con edge: XAUUSD (OOS PF=1.86, return +237%, MC prob>0=99.9%)
  Sin edge: BTCUSD, XNGUSD, XTIUSD, XAGUSD, USDJPY
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.data_loader import resample_to_tf
from shared.indicators import atr, adx, ema, align_htf

# ── Parámetros optimizados por símbolo (IS/OOS 2023-2026, 70/30) ─────────────
# Solo XAUUSD mostró edge real (OOS PF=1.86, +237%, MC prob>0=99.9%)
# Los demás se listan por referencia pero no se recomiendan en producción

OPTIMIZED_PARAMS = {
    'XAUUSD': dict(donchian_n=30, adx_thresh=25, atr_expansion=1.2, atr_mult_sl=2.0, tp_ratio=3.0),
}

DEFAULT_PARAMS = OPTIMIZED_PARAMS['XAUUSD']


# ── Features ──────────────────────────────────────────────────────────────────

def build_features(df_m15: pd.DataFrame) -> pd.DataFrame:
    """
    Añade indicadores al DataFrame M15.
    Columnas resultantes:
      atr14, atr_ma20                  (M15)
      d1_adx14, d1_ema200              (D1 → forward-filled a M15)
    """
    df = df_m15.copy()

    df['atr14']    = atr(df['high'], df['low'], df['close'], 14)
    df['atr_ma20'] = df['atr14'].rolling(20).mean()

    ohlcv = [c for c in ['open', 'high', 'low', 'close', 'volume'] if c in df.columns]
    df_d1 = resample_to_tf(df[ohlcv], 'D1').copy()
    df_d1['adx14']  = adx(df_d1['high'], df_d1['low'], df_d1['close'], 14)[0]
    df_d1['ema200'] = ema(df_d1['close'], 200)
    df = align_htf(df, df_d1, ['adx14', 'ema200'], 'd1')

    return df


# ── Canal Donchian H4 ─────────────────────────────────────────────────────────

def build_donchian(df: pd.DataFrame, donchian_n: int) -> pd.DataFrame:
    """
    Agrega canal Donchian H4 alineado al índice M15.
    shift(1) en H4 garantiza que solo se usan barras cerradas (sin lookahead).
    Columnas añadidas: don_h, don_l
    """
    ohlcv = [c for c in ['open', 'high', 'low', 'close', 'volume'] if c in df.columns]
    df_h4 = resample_to_tf(df[ohlcv], 'H4').copy()
    df_h4['don_h'] = df_h4['high'].shift(1).rolling(donchian_n).max()
    df_h4['don_l'] = df_h4['low'].shift(1).rolling(donchian_n).min()

    df = df.copy()
    df['don_h'] = df_h4['don_h'].reindex(df.index, method='ffill')
    df['don_l'] = df_h4['don_l'].reindex(df.index, method='ffill')
    return df


# ── Señales ───────────────────────────────────────────────────────────────────

def generate_signals(df: pd.DataFrame,
                     donchian_n:    int   = DEFAULT_PARAMS['donchian_n'],
                     adx_thresh:    float = DEFAULT_PARAMS['adx_thresh'],
                     atr_expansion: float = DEFAULT_PARAMS['atr_expansion'],
                     atr_mult_sl:   float = DEFAULT_PARAMS['atr_mult_sl'],
                     tp_ratio:      float = DEFAULT_PARAMS['tp_ratio']) -> pd.DataFrame:
    """
    Genera señales sobre df con features ya computadas (build_features).
    Añade: signal (1=long, -1=short, 0=neutro), sl, tp1, tp2.

    Flujo:
        df = build_features(df_m15)
        df = generate_signals(df)
        # señal en bar[i] → entrada en bar[i+1].open
    """
    df = build_donchian(df, donchian_n)

    close  = df['close'].values
    atr14  = df['atr14'].values
    atr_ma = df['atr_ma20'].values
    d1_adx = df['d1_adx14'].values
    d1_ema = df['d1_ema200'].values
    don_h  = df['don_h'].values
    don_l  = df['don_l'].values
    n      = len(df)

    signal  = np.zeros(n, dtype=np.int8)
    sl_arr  = np.full(n, np.nan)
    tp1_arr = np.full(n, np.nan)
    tp2_arr = np.full(n, np.nan)

    valid_adx = d1_adx >= adx_thresh
    valid_atr = atr14 > (atr_ma * atr_expansion)
    valid_don = ~np.isnan(don_h) & ~np.isnan(don_l)
    valid_ema = ~np.isnan(d1_ema)

    long_mask  = valid_adx & valid_atr & valid_don & valid_ema & (close > d1_ema) & (close > don_h)
    short_mask = valid_adx & valid_atr & valid_don & valid_ema & (close < d1_ema) & (close < don_l)

    risk = atr14 * atr_mult_sl

    signal[long_mask]   = 1
    sl_arr[long_mask]   = close[long_mask]  - risk[long_mask]
    tp1_arr[long_mask]  = close[long_mask]  + 1.5 * risk[long_mask]
    tp2_arr[long_mask]  = close[long_mask]  + tp_ratio * risk[long_mask]

    signal[short_mask]  = -1
    sl_arr[short_mask]  = close[short_mask] + risk[short_mask]
    tp1_arr[short_mask] = close[short_mask] - 1.5 * risk[short_mask]
    tp2_arr[short_mask] = close[short_mask] - tp_ratio * risk[short_mask]

    df = df.copy()
    df['signal'] = signal
    df['sl']     = sl_arr
    df['tp1']    = tp1_arr
    df['tp2']    = tp2_arr
    return df


# ── Uso standalone ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    from shared.data_loader import get_bars_cached
    from shared.backtest_engine import BacktestEngine
    from shared.metrics import compute_metrics, print_metrics
    from shared.costs import CostModel

    SYMBOL = 'XAUUSD'
    params = OPTIMIZED_PARAMS[SYMBOL]
    print(f'Cargando {SYMBOL} M15...')
    print(f'Parámetros: {params}')
    df_raw = get_bars_cached(SYMBOL, 'M15')
    df     = build_features(df_raw)
    df     = generate_signals(df, **params)

    sigs = (df['signal'] != 0).sum()
    print(f'{len(df):,} barras | {sigs} señales ({sigs / len(df) * 100:.1f}%)')
    print(f'Longs: {(df["signal"] == 1).sum()} | Shorts: {(df["signal"] == -1).sum()}')

    cost_model = CostModel.recomendado(point=0.01)
    engine = BacktestEngine(risk_per_trade=2.0, max_simultaneous=3, cost_model=cost_model)
    opens   = df['open'].values
    spreads = df['spread'].values if 'spread' in df.columns else [0] * len(df)

    for i in range(1, len(df)):
        engine.process_bar(df.iloc[i])
        if df['signal'].iloc[i - 1] != 0:
            engine.open_trade(
                time=df.index[i],
                direction=int(df['signal'].iloc[i - 1]),
                entry=opens[i],
                sl=df['sl'].iloc[i - 1],
                tp1=df['tp1'].iloc[i - 1],
                tp2=df['tp2'].iloc[i - 1],
                entry_spread_pts=float(spreads[i]),
                max_bars=9999,
            )

    trades  = engine.get_trades_df()
    metrics = compute_metrics(trades)
    print_metrics('ATR Momentum', SYMBOL, metrics)
