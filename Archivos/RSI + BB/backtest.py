"""
Backtest runner — Strategy 1: RSI + Bollinger Bands Mean Reversion
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
from shared.data_loader import get_bars, get_all_bars
from shared.backtest_engine import BacktestEngine
from shared.metrics import compute_metrics, print_metrics, print_summary_table
from .strategy import build_features, generate_signals

SYMBOLS       = ['XAUUSD', 'XAGUSD', 'NETH25', 'SE30', 'IT40', 'XAUEUR', 'XAGEUR']
N_BARS_DEF    = 15_000
RISK_PCT      = 2.0
MAX_POS       = 4
TIME_EXIT     = 48
SESSION_START = 1
SESSION_END   = 20


def run(symbol: str, cost_model=None, n_bars: int = None):
    nb = n_bars or N_BARS_DEF
    print(f"  Loading {symbol}...", end=' ', flush=True)
    df_m5 = get_all_bars(symbol, 'M5') if n_bars is None else get_bars(symbol, 'M5', nb)
    df    = build_features(df_m5)
    df    = generate_signals(df)
    print(f"{len(df):,} bars")

    engine = BacktestEngine(risk_per_trade=RISK_PCT, max_simultaneous=MAX_POS,
                            cost_model=cost_model)

    for i in range(1, len(df)):
        bar  = df.iloc[i]
        prev = df.iloc[i - 1]
        time = df.index[i]

        if not (SESSION_START <= time.hour < SESSION_END):
            engine.close_all(bar, reason='SESSION')
            continue

        engine.process_bar(bar)

        if prev['signal'] != 0:
            sp = float(bar['spread']) if 'spread' in bar.index else 0.0
            engine.open_trade(
                time=time, direction=int(prev['signal']),
                entry=bar['open'], sl=prev['sl'], tp1=prev['tp1'], tp2=prev['tp2'],
                entry_spread_pts=sp, max_bars=TIME_EXIT,
            )

    trades  = engine.get_trades_df()
    metrics = compute_metrics(trades)
    return metrics, trades


if __name__ == '__main__':
    results = {}
    for sym in SYMBOLS:
        try:
            m, t = run(sym)
            print_metrics('RSI+BB MR', sym, m)
            results[sym] = m
        except Exception as e:
            print(f"  ERROR {sym}: {e}")
    if results:
        print_summary_table('RSI + Bollinger Bands Mean Reversion', results)
