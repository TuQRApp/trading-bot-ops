"""
Strategy 1 — RSI + Bollinger Bands Mean Reversion
Fade statistical extremes. Entry on confirmation candle closing back inside bands.

Bias   : H4  — EMA(200) direction filter
Setup  : M30 — BB(20,2) + RSI(14)
Entry  : M5  — confirmation candle closes back inside bands
SL     : 2 × ATR(14) M30 beyond the extreme band
TP1    : BB midline (SMA 20 M30)  → move SL to BE
TP2    : Opposite band
Time   : 48 M5 bars (~4 h) if neither TP reached
Session: 01:00–20:00 UTC (exclude NY close)
"""
import pandas as pd
import numpy as np
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from shared.indicators import ema, atr, rsi, bollinger_bands, align_htf
from shared.data_loader import resample_to_tf


def build_features(df_m5: pd.DataFrame) -> pd.DataFrame:
    """
    Compute all indicators aligned to M5 index.
    Returns enriched M5 DataFrame.
    """
    # --- H4: EMA(200) bias ---
    df_h4 = resample_to_tf(df_m5, 'H4').copy()
    df_h4['ema200'] = ema(df_h4['close'], 200)

    # --- M30: BB(20,2) + RSI(14) + ATR(14) ---
    df_m30 = resample_to_tf(df_m5, 'M30').copy()
    bb_lo, bb_mid, bb_hi = bollinger_bands(df_m30['close'], 20, 2.0)
    df_m30['bb_lower'] = bb_lo
    df_m30['bb_mid']   = bb_mid
    df_m30['bb_upper'] = bb_hi
    df_m30['rsi14']    = rsi(df_m30['close'], 14)
    df_m30['atr14']    = atr(df_m30['high'], df_m30['low'], df_m30['close'], 14)

    # --- Align to M5 ---
    df = align_htf(df_m5, df_h4, ['ema200'], 'h4')
    df = align_htf(df, df_m30, ['bb_lower', 'bb_mid', 'bb_upper', 'rsi14', 'atr14'], 'm30')
    return df


def generate_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add columns: signal (1/−1/0), sl, tp1, tp2.
    Signal is on bar[i]; trade enters on bar[i+1] open.
    """
    df = df.copy()
    prev = df.shift(1)

    # LONG: previous bar touched BB lower AND RSI < 30
    #       current bar closes back inside (close > bb_lower)
    #       H4 filter: price above EMA200 (only longs in uptrend)
    long_cond = (
        (prev['low'] <= prev['m30_bb_lower']) &
        (prev['m30_rsi14'] < 30) &
        (df['close'] > df['m30_bb_lower']) &
        (df['close'] > df['h4_ema200'])
    )

    # SHORT: previous bar touched BB upper AND RSI > 70
    #        current bar closes back inside (close < bb_upper)
    #        H4 filter: price below EMA200
    short_cond = (
        (prev['high'] >= prev['m30_bb_upper']) &
        (prev['m30_rsi14'] > 70) &
        (df['close'] < df['m30_bb_upper']) &
        (df['close'] < df['h4_ema200'])
    )

    df['signal'] = 0
    df.loc[long_cond,  'signal'] =  1
    df.loc[short_cond, 'signal'] = -1

    # SL, TP1, TP2 (computed on signal bar; engine uses these on next bar open)
    df['sl']  = np.nan
    df['tp1'] = np.nan
    df['tp2'] = np.nan

    lm = df['signal'] == 1
    df.loc[lm, 'sl']  = df.loc[lm, 'm30_bb_lower'] - 2 * df.loc[lm, 'm30_atr14']
    df.loc[lm, 'tp1'] = df.loc[lm, 'm30_bb_mid']
    df.loc[lm, 'tp2'] = df.loc[lm, 'm30_bb_upper']

    sm = df['signal'] == -1
    df.loc[sm, 'sl']  = df.loc[sm, 'm30_bb_upper'] + 2 * df.loc[sm, 'm30_atr14']
    df.loc[sm, 'tp1'] = df.loc[sm, 'm30_bb_mid']
    df.loc[sm, 'tp2'] = df.loc[sm, 'm30_bb_lower']

    return df
