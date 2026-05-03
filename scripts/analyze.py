"""
Trading Bot Analyzer — GitHub Actions script
Runs automatically when data.json changes.
Processes groups with status 'pending' or 'pendiente_final'.

Analysis pipeline per group:
  Pre-analysis (ast + pandas + sklearn)
  → Pass 1: Claude Sonnet — initial analysis
  → Pass 2: Claude critic — gap audit
  → Pass 3: GPT-4o code review (optional, requires OPENAI_API_KEY secret)
"""

import ast
import os
import json
import sys
from pathlib import Path
from datetime import date
import requests
from anthropic import Anthropic

try:
    from market_context import build_macro_snapshot, generate_m5 as _generate_m5
    _M5_AVAILABLE = True
except Exception:
    _M5_AVAILABLE = False

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

def read_file(folder, filename):
    path = Path("Archivos") / folder / filename
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    print(f"  [warn] File not found: {path}")
    return None

# ── Pre-processing ─────────────────────────────────────────────────────────────

def preprocess_python(filename, content):
    """Extract structural facts from a Python file using the ast stdlib."""
    facts = {"filename": filename, "lines": len(content.splitlines())}
    try:
        tree = ast.parse(content)

        facts["functions"] = [
            n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
        ]
        imports = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imports += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module:
                imports.append(n.module)
        facts["imports"] = list(dict.fromkeys(imports))

        facts["complexity"] = {
            "if_blocks":  sum(1 for n in ast.walk(tree) if isinstance(n, ast.If)),
            "loops":      sum(1 for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While))),
            "try_blocks": sum(1 for n in ast.walk(tree) if isinstance(n, ast.Try)),
        }

        magic = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
                if n.value not in {0, 1, -1, 2, 100, 0.0, 1.0}:
                    magic.add(n.value)
        facts["magic_numbers"] = sorted(magic)[:20]

    except SyntaxError as e:
        facts["syntax_error"] = str(e)

    return facts


def _detect_obfuscation(filename, content):
    """
    Returns a reason string if the file looks obfuscated, else None.
    Conservative: only flags clear cases to avoid false positives on minified helpers.
    """
    import re

    lines = content.splitlines()
    if len(lines) < 5:
        return None

    long_line_count = sum(1 for l in lines if len(l) > 500)
    exec_encoded    = bool(re.search(r'\bexec\s*\(\s*(base64|zlib|b64decode|compile|decode)', content))
    eval_encoded    = bool(re.search(r'\beval\s*\(\s*(base64|zlib|b64decode|compile|decode)', content))
    base64_blob     = bool(re.search(r'[A-Za-z0-9+/]{300,}={0,2}', content))

    try:
        tree    = ast.parse(content)
        n_funcs = sum(1 for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        n_lines = len(lines)
        sparse  = n_lines > 100 and n_funcs < 2 and (base64_blob or exec_encoded or long_line_count > 3)
    except SyntaxError:
        if base64_blob or exec_encoded:
            return "error de sintaxis con indicadores de ofuscacion"
        sparse = False

    if exec_encoded or eval_encoded:
        return "exec/eval con datos codificados detectado"
    if base64_blob and long_line_count > 3:
        return f"cadenas codificadas largas ({long_line_count} lineas >500 chars)"
    if sparse:
        return f"estructura anormalmente compacta ({n_funcs} funciones en {n_lines} lineas)"

    return None


def _ml_cluster_trades(df, pnl_col):
    """
    KMeans clustering on trade features to detect regime/time dependencies.
    Returns a dict with cluster profiles and a plain-text insight.
    Only called when len(df) >= 50.
    """
    import numpy as np
    import pandas as pd
    from sklearn.preprocessing import StandardScaler
    from sklearn.cluster import KMeans

    features = {}

    pnl = pd.to_numeric(df[pnl_col], errors="coerce")
    features["pnl_norm"] = pnl.fillna(0)

    date_col = next(
        (c for c in df.columns if "date" in c.lower() or "time" in c.lower()), None
    )
    if date_col:
        try:
            dt = pd.to_datetime(df[date_col], errors="coerce")
            features["hour"]        = dt.dt.hour.fillna(12)
            features["day_of_week"] = dt.dt.dayofweek.fillna(2)
        except Exception:
            pass

    dir_col = next(
        (c for c in df.columns if c.lower() in {"type", "direction", "side", "order_type"}), None
    )
    if dir_col:
        features["direction"] = df[dir_col].astype(str).str.lower().map(
            lambda x: 1 if any(k in x for k in ("buy", "long", "1")) else 0
        ).fillna(0.5)

    if len(features) < 2:
        return None

    X = pd.DataFrame(features).dropna()
    if len(X) < 50:
        return None

    X_scaled = StandardScaler().fit_transform(X)
    n_clusters = min(3, len(X) // 15)
    km = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
    labels = km.fit_predict(X_scaled)

    pnl_aligned = pnl.iloc[X.index].values
    clusters = []
    for cid in range(n_clusters):
        mask = labels == cid
        c_pnl = pnl_aligned[mask]
        c_wins = (c_pnl > 0).sum()
        c_total = len(c_pnl)
        profile = {
            "id": cid,
            "size": int(c_total),
            "win_rate_pct": round(c_wins / c_total * 100, 1) if c_total else None,
            "avg_pnl": round(float(c_pnl.mean()), 4) if c_total else None,
        }
        if "hour" in features:
            profile["dominant_hour"] = int(X["hour"].iloc[mask.nonzero()[0]].mode().iloc[0])
        if "day_of_week" in features:
            days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            dom_day = int(X["day_of_week"].iloc[mask.nonzero()[0]].mode().iloc[0])
            profile["dominant_day"] = days[dom_day] if dom_day < 7 else str(dom_day)
        clusters.append(profile)

    clusters.sort(key=lambda c: c.get("win_rate_pct") or 0, reverse=True)

    best  = clusters[0]
    worst = clusters[-1]
    insight_parts = []
    if best["win_rate_pct"] and worst["win_rate_pct"]:
        diff = best["win_rate_pct"] - worst["win_rate_pct"]
        if diff >= 15:
            desc = f"Best cluster (n={best['size']}): {best['win_rate_pct']}% WR"
            if "dominant_hour" in best:
                desc += f", ~{best['dominant_hour']}h"
            if "dominant_day" in best:
                desc += f", {best['dominant_day']}"
            desc += f". Worst cluster (n={worst['size']}): {worst['win_rate_pct']}% WR."
            desc += f" {round(diff, 1)}pp gap — strategy may be time/regime dependent."
            insight_parts.append(desc)
        else:
            insight_parts.append(
                f"Clusters show similar performance ({worst['win_rate_pct']}–{best['win_rate_pct']}% WR) — no strong time dependency detected."
            )

    return {
        "n_clusters": n_clusters,
        "clusters": clusters,
        "insight": " ".join(insight_parts) if insight_parts else "No significant pattern differences between clusters.",
    }


def _walk_forward_test(df, pnl_col, train_pct=0.7):
    """
    Chronological 70/30 split to detect overfitting.
    Returns None if there are fewer than 40 trades.
    """
    import pandas as pd

    pnl   = pd.to_numeric(df[pnl_col], errors="coerce").dropna()
    n     = len(pnl)
    split = int(n * train_pct)

    if split < 28 or (n - split) < 12:
        return None

    def _m(series):
        wins   = series[series > 0]
        losses = series[series < 0]
        t      = len(series)
        gp = float(wins.sum())        if len(wins)   > 0 else 0.0
        gl = float(abs(losses.sum())) if len(losses) > 0 else 0.0
        cumul = series.cumsum()
        return {
            "n_trades":      t,
            "win_rate_pct":  round(len(wins) / t * 100, 2) if t else None,
            "profit_factor": round(gp / gl, 3)             if gl > 0 else None,
            "max_drawdown":  round(float((cumul - cumul.cummax()).min()), 4),
        }

    train = _m(pnl.iloc[:split])
    test  = _m(pnl.iloc[split:])

    wr_delta     = None
    pf_delta_pct = None
    if train["win_rate_pct"] and test["win_rate_pct"]:
        wr_delta = round(train["win_rate_pct"] - test["win_rate_pct"], 1)
    if train["profit_factor"] and test["profit_factor"]:
        pf_delta_pct = round(
            (train["profit_factor"] - test["profit_factor"]) / train["profit_factor"] * 100, 1
        )

    if wr_delta is not None:
        if   wr_delta > 15 or (pf_delta_pct or 0) > 40: verdict = "high_overfit_risk"
        elif wr_delta >  8 or (pf_delta_pct or 0) > 20: verdict = "moderate_overfit_risk"
        else:                                             verdict = "stable"
    else:
        verdict = "insufficient_data"

    return {
        "split":              f"{int(train_pct*100)}/{int((1-train_pct)*100)}",
        "train":              train,
        "test":               test,
        "wr_delta_pp":        wr_delta,
        "pf_degradation_pct": pf_delta_pct,
        "verdict":            verdict,
    }


def preprocess_csv(filename, content):
    """Compute quantitative stats from a CSV file using pandas."""
    import io
    import pandas as pd
    import numpy as np

    facts = {"filename": filename}
    try:
        df = pd.read_csv(io.StringIO(content), sep=None, engine="python")
        facts["rows"] = len(df)
        facts["columns"] = list(df.columns)

        pnl_col = next(
            (c for c in df.columns if c.lower() in {"profit", "pnl", "return", "returns", "gain", "net"}),
            None,
        )

        if pnl_col:
            pnl = pd.to_numeric(df[pnl_col], errors="coerce").dropna()
            wins   = pnl[pnl > 0]
            losses = pnl[pnl < 0]
            total  = len(pnl)

            gross_profit = float(wins.sum())        if len(wins)   > 0 else 0.0
            gross_loss   = float(abs(losses.sum())) if len(losses) > 0 else 0.0

            stats = {
                "total_trades":  total,
                "win_rate_pct":  round(len(wins) / total * 100, 2) if total else None,
                "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
                "avg_win":       round(float(wins.mean()),   4) if len(wins)   > 0 else None,
                "avg_loss":      round(float(losses.mean()), 4) if len(losses) > 0 else None,
                "gross_profit":  round(gross_profit, 4),
                "gross_loss":    round(gross_loss,   4),
                "net_pnl":       round(gross_profit - gross_loss, 4),
            }

            cumul = pnl.cumsum()
            stats["max_drawdown"] = round(float((cumul - cumul.cummax()).min()), 4)

            if pnl.std() > 0:
                stats["sharpe_approx"] = round(float(pnl.mean() / pnl.std() * (252 ** 0.5)), 3)

            max_win_streak = max_loss_streak = streak = 0
            prev = None
            for v in pnl:
                cur = 1 if v > 0 else (-1 if v < 0 else 0)
                streak = (streak + 1) if cur == prev and cur != 0 else 1
                if cur == 1:
                    max_win_streak = max(max_win_streak, streak)
                elif cur == -1:
                    max_loss_streak = max(max_loss_streak, streak)
                prev = cur
            stats["max_win_streak"]  = max_win_streak
            stats["max_loss_streak"] = max_loss_streak

            try:
                import quantstats as qs
                balance_col = next(
                    (c for c in df.columns if c.lower() in {"balance", "equity", "account"}), None
                )
                date_col = next(
                    (c for c in df.columns if "date" in c.lower() or "time" in c.lower()), None
                )
                if balance_col and len(df) > 5:
                    balance = pd.to_numeric(df[balance_col], errors="coerce").dropna()
                    ret = balance.pct_change().dropna()
                    if date_col:
                        try:
                            ret.index = pd.to_datetime(df[date_col].iloc[1 : len(ret) + 1].values)
                        except Exception:
                            pass
                    stats["quantstats"] = {
                        "sharpe":           round(float(qs.stats.sharpe(ret)),        3),
                        "sortino":          round(float(qs.stats.sortino(ret)),       3),
                        "calmar":           round(float(qs.stats.calmar(ret)),        3),
                        "max_drawdown_pct": round(float(qs.stats.max_drawdown(ret)) * 100, 2),
                    }
            except Exception:
                pass

            if total >= 50:
                try:
                    cluster_result = _ml_cluster_trades(df, pnl_col)
                    if cluster_result:
                        stats["trade_clusters"] = cluster_result
                        print(f"    ML clustering: {cluster_result['n_clusters']} clusters — {cluster_result['insight'][:80]}")
                except Exception as e:
                    print(f"    [warn] ML clustering failed: {e}")

            if total >= 40:
                try:
                    wf = _walk_forward_test(df, pnl_col)
                    if wf:
                        stats["walk_forward"] = wf
                        print(f"    Walk-forward (70/30): train WR={wf['train']['win_rate_pct']}% "
                              f"test WR={wf['test']['win_rate_pct']}% "
                              f"delta={wf['wr_delta_pp']}pp — {wf['verdict']}")
                except Exception as e:
                    print(f"    [warn] Walk-forward failed: {e}")

            facts["pnl_stats"] = stats

        else:
            facts["note"] = "No P&L column detected — may be OHLCV or other format"
            facts["sample_columns"] = list(df.columns)[:10]

    except Exception as e:
        facts["error"] = str(e)

    return facts


HTML_LARGE_THRESHOLD = 5 * 1024 * 1024   # 5 MB — above this, skip full DOM parse
HTML_HEAD_BYTES      = 200_000           # read only first 200 KB for large files

SKIP_EXTENSIONS = frozenset({
    "pdf", "png", "jpg", "jpeg", "gif", "bmp", "tiff", "svg",
    "docx", "xlsx", "xls", "pptx", "zip", "rar", "7z",
})


def _parse_numeric(s):
    """Convert strings like '55.4%', '-7.87%', '2.162', '$10,000' to float or None."""
    import re
    s = re.sub(r"[%$,\s]", "", str(s))
    try:
        return float(s)
    except ValueError:
        return None


def _extract_card_stats(html_fragment):
    """
    Extract per-instrument stats from custom div-based backtest HTML.
    Handles the .symbol-block / h3 / .card / .lbl / .val pattern.
    Returns a list of dicts, one per instrument.
    """
    import re

    tag_re    = re.compile(r"<[^>]+>")
    h3_re     = re.compile(r"<h3[^>]*>(.*?)</h3>", re.DOTALL)
    lbl_re    = re.compile(
        r'class="lbl"[^>]*>(.*?)</div>\s*<div[^>]*class="val"[^>]*>(.*?)</div>',
        re.DOTALL,
    )

    instruments = []
    blocks = re.split(r'<div[^>]*class="symbol-block"', html_fragment)

    for block in blocks[1:]:
        h3 = h3_re.search(block[:600])
        if not h3:
            continue
        symbol = tag_re.sub("", h3.group(1)).strip()

        cards = {}
        for lbl, val in lbl_re.findall(block[:3000]):
            k = tag_re.sub("", lbl).strip()
            v = tag_re.sub("", val).strip()
            if k and v:
                cards[k] = v
        if cards:
            instruments.append({"symbol": symbol, **cards})

    return instruments


def _aggregate_card_instruments(instruments):
    """
    Given a list of per-instrument card dicts, aggregate into overall pnl_stats-like dict.
    Handles Spanish and English label names.
    """
    WR_KEYS = {"win rate", "win rate %", "wr", "tasa de acierto"}
    PF_KEYS = {"profit factor", "pf", "factor de beneficio"}
    DD_KEYS = {"max dd", "max drawdown", "drawdown", "dd máx", "dd max"}
    TR_KEYS = {"trades", "total trades", "operaciones", "n trades"}
    SH_KEYS = {"sharpe", "sharpe ratio"}

    wrs, pfs, dds, trades, sharpes = [], [], [], [], []

    for inst in instruments:
        for k, v in inst.items():
            kl = k.lower()
            n = _parse_numeric(v)
            if n is None:
                continue
            if kl in WR_KEYS:
                wrs.append(n)
            elif kl in PF_KEYS:
                pfs.append(n)
            elif kl in DD_KEYS:
                dds.append(n)
            elif kl in TR_KEYS:
                trades.append(int(n))
            elif kl in SH_KEYS:
                sharpes.append(n)

    if not wrs:
        return None

    def _avg(lst): return round(sum(lst) / len(lst), 3) if lst else None
    def _worst_dd(lst): return round(min(lst), 4) if lst else None

    return {
        "total_trades":    sum(trades) if trades else None,
        "n_instruments":   len(instruments),
        "win_rate_pct":    _avg(wrs),
        "profit_factor":   _avg(pfs),
        "max_drawdown":    _worst_dd(dds),
        "sharpe_approx":   _avg(sharpes),
        "per_instrument":  [
            {"symbol": i.get("symbol"), "win_rate": i.get("Win Rate") or i.get("Tasa de acierto"),
             "profit_factor": i.get("Profit Factor"), "trades": i.get("Trades") or i.get("Operaciones")}
            for i in instruments
        ],
    }


def _extract_trades_html(content):
    """
    Extract all individual trade rows from a custom div-based HTML backtest report.
    Uses string split on </tr> — no full DOM parse, works on 20MB+ files.
    Returns a pandas DataFrame or None.

    Expected column structure (12 cols):
      0=Entrada  1=Salida  2=Dir  3=Px Entrada  4=Px Salida
      5=SL  6=TP  7=PnL USD  8=PnL %  9=Cierre  10=ADX  11=Capital
    """
    import re
    import pandas as pd

    tag_re = re.compile(r"<[^>]+>")
    all_trades = []

    # Split by symbol-block to track instrument name
    blocks = re.split(r'<div[^>]*class="symbol-block"', content)

    for block in blocks[1:]:
        # Symbol name from h3 (first 500 chars of block)
        h3 = re.search(r"<h3[^>]*>(.*?)</h3>", block[:500], re.DOTALL)
        if not h3:
            continue
        symbol = tag_re.sub("", h3.group(1)).strip()

        if "PnL USD" not in block:
            continue

        # Extract trade rows: split by </tr>, find rows with exactly 12 <td> cells
        for row in block.split("</tr>"):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL | re.IGNORECASE)
            if len(tds) != 12:
                continue
            cells = [tag_re.sub("", td).strip() for td in tds]
            try:
                pnl = float(
                    cells[7].replace("$", "").replace(",", "").replace("+", "").replace(" ", "")
                )
                cap_str = cells[11].replace("$", "").replace(",", "").replace(" ", "")
                cap = float(cap_str) if cap_str else None
                all_trades.append({
                    "Entrada": cells[0],
                    "Dir":     cells[2],
                    "profit":  pnl,
                    "Capital": cap,
                    "Symbol":  symbol,
                })
            except (ValueError, IndexError):
                continue

    if not all_trades:
        return None

    df = pd.DataFrame(all_trades)
    df["Entrada"] = pd.to_datetime(df["Entrada"], errors="coerce")
    df = df.dropna(subset=["profit"]).sort_values("Entrada").reset_index(drop=True)
    return df


def _extract_trades_mt5_standard(content):
    """
    Extract trade rows from a standard MT5 Strategy Tester HTML export.
    Streams through </tr> splits — no DOM parse, works on any file size.
    Detects the header row by column name aliases (EN + ES), then maps column
    indices and parses every subsequent trade row.
    Returns (DataFrame or None, summary_stats dict or None).
    """
    import re
    import pandas as pd

    tag_re = re.compile(r"<[^>]+>")

    PROFIT_ALIASES  = {"profit", "net profit", "ganancia", "ganancias", "beneficio", "neto"}
    TIME_ALIASES    = {"time", "open time", "time open", "tiempo", "hora", "datetime", "fecha"}
    SYMBOL_ALIASES  = {"symbol", "sim", "simbolo", "símbolo", "instrumento"}
    TYPE_ALIASES    = {"type", "direction", "dirección", "side", "tipo", "deal type"}
    BALANCE_ALIASES = {"balance", "saldo"}

    header_idx = {}
    trades     = []
    summary_kv = {}

    for row in content.split("</tr>"):
        cells_raw = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL | re.IGNORECASE)
        if not cells_raw:
            continue
        cells       = [tag_re.sub("", c).strip() for c in cells_raw]
        cells_lower = [c.lower() for c in cells]

        # Before header: collect 2-col summary stats
        if not header_idx and len(cells) == 2 and cells[0] and cells[1]:
            kl = cells_lower[0]
            if any(kw in kl for kw in ("profit", "factor", "drawdown", "trades", "win", "ganancia", "operaciones")):
                num = _parse_numeric(cells[1])
                if num is not None:
                    summary_kv[cells[0]] = cells[1]
            continue

        # Header row: must contain both a profit and a time column
        if not header_idx and len(cells) >= 5:
            has_profit = any(c in PROFIT_ALIASES for c in cells_lower)
            has_time   = any(c in TIME_ALIASES   for c in cells_lower)
            if has_profit and has_time:
                for i, cl in enumerate(cells_lower):
                    if cl in PROFIT_ALIASES  and "profit"  not in header_idx: header_idx["profit"]  = i
                    elif cl in TIME_ALIASES  and "time"    not in header_idx: header_idx["time"]    = i
                    elif cl in SYMBOL_ALIASES and "symbol" not in header_idx: header_idx["symbol"]  = i
                    elif cl in TYPE_ALIASES  and "type"    not in header_idx: header_idx["type"]    = i
                    elif cl in BALANCE_ALIASES and "balance" not in header_idx: header_idx["balance"] = i
                continue

        # Trade row
        if header_idx and "profit" in header_idx:
            pi = header_idx["profit"]
            if len(cells) <= pi:
                continue
            pnl = _parse_numeric(cells[pi])
            if pnl is None:
                continue
            trade = {"profit": pnl}
            for dest, src in (("time", "time"), ("Symbol", "symbol"), ("Dir", "type"), ("Capital", "balance")):
                if src in header_idx and len(cells) > header_idx[src]:
                    raw = cells[header_idx[src]]
                    trade[dest] = _parse_numeric(raw) if dest == "Capital" else raw
            trades.append(trade)

    if not trades:
        return None, summary_kv or None

    df = pd.DataFrame(trades)
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.sort_values("time").reset_index(drop=True)
    df = df.dropna(subset=["profit"]).reset_index(drop=True)
    return df, summary_kv or None


def preprocess_html(filename, content):
    """
    Extract trading statistics from a backtest HTML report.

    Priority order:
    1. _extract_trades_html: symbol-block split + </tr> scan — works on any size,
       no DOM parse. Extracts all individual trades → full pipeline (walk-forward,
       clustering, Sharpe, etc.).
    2. pandas.read_html: for standard MT5 table format (small files only).
    3. _extract_card_stats: regex on .lbl/.val divs — gets summary stats per instrument.
    4. Text extract: strip tags, return first 3000 chars.
    """
    import io
    import re
    import pandas as pd

    facts = {"filename": filename, "source": "html_backtest"}

    # ── Path 1: extract individual trades (all file sizes) ────────────────────
    try:
        df = _extract_trades_html(content)
        if df is not None and len(df) > 0:
            n_sym = df["Symbol"].nunique() if "Symbol" in df.columns else 1
            print(f"    [{filename}] trade extraction: {len(df)} trades, {n_sym} instruments")

            # Run full preprocess_csv pipeline on aggregated trades
            csv_facts = preprocess_csv(f"{filename}[trades]", df.to_csv(index=False))
            if "pnl_stats" in csv_facts:
                facts["pnl_stats"] = csv_facts["pnl_stats"]
                facts["rows"]      = len(df)

            # Per-instrument summary alongside
            per_inst = []
            for sym, grp in df.groupby("Symbol"):
                wins   = grp["profit"][grp["profit"] > 0]
                losses = grp["profit"][grp["profit"] < 0]
                gp = float(wins.sum())        if len(wins)   > 0 else 0.0
                gl = float(abs(losses.sum())) if len(losses) > 0 else 0.0
                per_inst.append({
                    "symbol":        sym,
                    "n_trades":      len(grp),
                    "win_rate_pct":  round(len(wins) / len(grp) * 100, 2) if len(grp) else None,
                    "profit_factor": round(gp / gl, 3) if gl > 0 else None,
                })
            if per_inst:
                facts["per_instrument"] = per_inst

            return facts
    except Exception as e:
        print(f"    [{filename}] trade extraction failed: {e}")

    # ── Path 1b: standard MT5 trade table (any size) ──────────────────────────
    if "pnl_stats" not in facts:
        try:
            df_mt5, mt5_summary = _extract_trades_mt5_standard(content)
            if df_mt5 is not None and len(df_mt5) > 0:
                n_sym = df_mt5["Symbol"].nunique() if "Symbol" in df_mt5.columns else 1
                print(f"    [{filename}] standard MT5: {len(df_mt5)} trades, {n_sym} instruments")
                csv_facts = preprocess_csv(f"{filename}[mt5]", df_mt5.to_csv(index=False))
                if "pnl_stats" in csv_facts:
                    facts["pnl_stats"] = csv_facts["pnl_stats"]
                    facts["rows"]      = len(df_mt5)
                if mt5_summary:
                    facts["summary_stats"] = mt5_summary
                if "Symbol" in df_mt5.columns:
                    per_inst = []
                    for sym, grp in df_mt5.groupby("Symbol"):
                        wins   = grp["profit"][grp["profit"] > 0]
                        losses = grp["profit"][grp["profit"] < 0]
                        gp     = float(wins.sum())        if len(wins)   > 0 else 0.0
                        gl     = float(abs(losses.sum())) if len(losses) > 0 else 0.0
                        per_inst.append({
                            "symbol":        sym,
                            "n_trades":      len(grp),
                            "win_rate_pct":  round(len(wins) / len(grp) * 100, 2) if len(grp) else None,
                            "profit_factor": round(gp / gl, 3) if gl > 0 else None,
                        })
                    if per_inst:
                        facts["per_instrument"] = per_inst
                return facts
        except Exception as e:
            print(f"    [{filename}] standard MT5 extraction failed: {e}")

    # ── Path 2: pandas.read_html (small files only) ───────────────────────────
    if len(content) <= HTML_LARGE_THRESHOLD:
        try:
            tables = pd.read_html(io.StringIO(content), flavor="html.parser")
            for t in tables:
                text = t.to_string().lower()
                if t.shape[1] <= 4 and any(
                    kw in text for kw in ["profit factor", "drawdown", "total trades", "win rate"]
                ):
                    kv = {}
                    for _, row in t.iterrows():
                        vals = [str(v).strip() for v in row if str(v).strip() not in ("nan", "")]
                        for i in range(0, len(vals) - 1, 2):
                            kv[vals[i]] = vals[i + 1]
                    facts["summary_stats"] = kv
                    print(f"    [{filename}] HTML summary table: {len(kv)} stats")
                    break
        except Exception:
            pass

    # ── Path 3: card pattern (.lbl/.val divs) ─────────────────────────────────
    if "pnl_stats" not in facts and "summary_stats" not in facts:
        head = content[:HTML_HEAD_BYTES]
        instruments = _extract_card_stats(head)
        if instruments:
            agg = _aggregate_card_instruments(instruments)
            if agg:
                facts["pnl_stats"] = agg
                print(f"    [{filename}] card stats: {agg['n_instruments']} instruments "
                      f"(summary only — trades not extracted)")
                return facts

    # ── Path 4: text fallback ─────────────────────────────────────────────────
    if "pnl_stats" not in facts and "summary_stats" not in facts:
        head = content[:HTML_HEAD_BYTES]
        text = re.sub(r"<[^>]+>", " ", head)
        text = re.sub(r"\s+", " ", text).strip()
        facts["text_extract"] = text[:3000]
        facts["extract_note"] = "HTML parsing failed — showing text extract"
        print(f"    [{filename}] HTML: all parsers failed, text fallback")

    return facts


def preprocess_files(group_files, folder):
    """Run pre-analysis on all files before calling Claude."""
    results = []
    for f in group_files:
        ext = f["name"].rsplit(".", 1)[-1].lower()
        if ext in SKIP_EXTENSIONS:
            continue
        content = read_file(folder, f["name"])
        if not content:
            continue
        if ext == "py":
            results.append(preprocess_python(f["name"], content))
        elif ext == "csv":
            results.append(preprocess_csv(f["name"], content))
        elif ext == "html":
            results.append(preprocess_html(f["name"], content))
    return results or None

# ── Prompts ───────────────────────────────────────────────────────────────────

SCHEMA = """
{
  "category": "Type | N instruments | Platform | Strategy (short, use | as separator)",
  "summary": "2-3 dense sentences: what the file does, key technology, main metrics, current state.",
  "m1": {
    "OPTION A — use when CSV/backtest data is present (type=quality)": {
      "type": "quality",
      "last_updated": "YYYY-MM-DDTHH:MM:SSZ",
      "last_updated_meta": "file1.py · file2.csv",
      "fsh_count": "N trades · N instruments",
      "score": {
        "valor": 7.5,
        "max": 10,
        "label": "Short verdict label",
        "bullets": [
          {"type": "ok",   "text": "positive point"},
          {"type": "warn", "text": "warning"},
          {"type": "bad",  "text": "serious problem"}
        ]
      },
      "metrics": [
        "SINGLE INSTRUMENT (<=5): one QM card per key metric (WR, PF, DD, Sharpe, walk-forward, etc.)",
        "MULTI INSTRUMENT (>5): MAX 8 QM cards total — use aggregate metrics only:",
        {
          "id": "QM-01", "label": "Instruments",
          "value": "21 instruments",    "status": "ok",
          "note": "US30 WR 55% PF 2.16 | NAS100 WR 58% PF 2.40 | GER40 WR 51% PF 1.90 | ... (all in one note)"
        },
        {
          "id": "QM-02", "label": "Total Trades",
          "value": "2100",              "status": "ok",
          "note": "avg 100 per instrument"
        },
        {
          "id": "QM-03", "label": "Avg Win Rate",
          "value": "55.4%",            "status": "ok",
          "note": "range min%-max% across instruments"
        },
        {
          "id": "QM-04", "label": "Avg Profit Factor",
          "value": "2.16",             "status": "ok",
          "note": "range min-max"
        },
        {
          "id": "QM-05", "label": "Worst Max Drawdown",
          "value": "-9.2%",            "status": "warn",
          "note": "instrument with worst DD"
        },
        {
          "id": "QM-06", "label": "Walk-forward 70/30",
          "value": "WR 55% -> 51%",   "status": "ok",
          "note": "stable / moderate_overfit_risk / high_overfit_risk"
        }
      ]
    },
    "OPTION B — use when only .py files, no backtest data (type=empty)": {
      "type": "empty",
      "last_updated": "YYYY-MM-DDTHH:MM:SSZ",
      "last_updated_meta": "filename.py",
      "empty_title": "Short title",
      "empty_desc": "Explanation of why no backtest results are available.",
      "empty_trigger": "filename.py"
    }
  },
  "m2": [
    {
      "id": "R-01",
      "tipo": "param",
      "tipo_label": "Parametro",
      "prioridad": "alta",
      "estado": "pendiente",
      "title": "Concise title",
      "desc": "Detailed description of the problem and the proposed solution.",
      "comment": ""
    }
  ],
  "m3": [
    {
      "id": "OBS-001",
      "tipo": "warn",
      "origin": "filename.py",
      "title": "Observation title",
      "desc": "Detailed description.",
      "comment": ""
    }
  ],
  "m4": [
    {
      "id": "H-01",
      "categoria": "bug",
      "title": "Finding title",
      "desc": "Problem description.",
      "code": "relevant_code_here()",
      "fix": "proposed_fix_here()",
      "comment": ""
    }
  ]
}
"""

ANALYSIS_SYSTEM = [
    {
        "type": "text",
        "text": (
            "You are a senior quantitative trading systems analyst reviewing a Python trading bot.\n\n"
            "Analyze the file(s) provided and return a JSON object following this schema exactly.\n"
            "OUTPUT ONLY VALID JSON. No markdown fences, no text before or after the JSON object.\n"
            "Use only ASCII characters (no em-dashes, no special quotes, no accented vowels) — this is critical for JSON safety.\n"
            "All descriptive text must be in Spanish.\n\n"
            f"Schema:\n{SCHEMA}\n\n"
            "Rules:\n"
            "- If PRE-ANALYSIS FACTS are present: treat them as verified ground truth. "
            "Use pnl_stats directly as the basis for m1 metrics — do not recalculate or contradict them. "
            "If trade_clusters is present, use the insight to generate or strengthen m2/m3 recommendations about regime dependency. "
            "Use complexity and magic_numbers from Python facts to strengthen m4 findings. "
            "If source='html_backtest': the file is an MT5 HTML report — use pnl_stats and summary_stats "
            "exactly as you would CSV data. Use type 'quality' for m1 if pnl_stats is present.\n"
            "- If walk_forward is in pnl_stats: include it as a QM metric in m1 "
            "(label='Walk-forward 70/30', value='WR X% -> Y%', status matches verdict: "
            "stable=ok, moderate_overfit_risk=warn, high_overfit_risk=bad). "
            "verdict='high_overfit_risk' MUST lower the m1 score and generate an alta-priority m2 recommendation.\n"
            "- If LIVE VS BACKTEST COMPARISON block is present: generate a dedicated alta-priority m2 card "
            "explaining the expected gap between backtest and live performance using the provided ranges. "
            "If live_data_available=False, add an m2 card recommending to upload live trades CSV. "
            "Reference the live bot badges by name.\n"
            "- If PREVIOUS VERSION CONTEXT is present: this is a new version of an already-reviewed bot. "
            "Do NOT re-flag issues that appear in the 'descartado' list — the trader consciously chose not to fix them. "
            "For issues in the 'implementado' list: verify in the new code whether they were actually resolved; "
            "if still present, flag it with higher priority. "
            "For issues in the 'pendiente' list: check whether they persist and if so, escalate their priority. "
            "Focus the analysis on what changed from the previous version, not on re-discovering the same findings.\n"
            "- If TRADER PROFILE is present: use it to calibrate the analysis. "
            "Reduce or strengthen card types with high discard rates (noise for this trader). "
            "Improve depth in categories that are frequently corrected. "
            "Adjust tone and detail level to match the trader's correction patterns.\n"
            "- m1: Use type \"quality\" when CSV/backtest data is present. Use type \"empty\" when only .py files. "
            "For quality: last_updated must be ISO 8601 (e.g. 2026-05-03T04:30:00Z). "
            "metrics[].status must be ok/warn/bad. Use pnl_stats from PRE-ANALYSIS FACTS directly. "
            "CRITICAL — token budget rule: if n_instruments > 5, use MAX 8 QM cards in m1 total. "
            "Pack all per-instrument data into QM-01 note field as a compact pipe-separated list "
            "(e.g. 'US30 WR 55% PF 2.16 | NAS100 WR 58% PF 2.40 | ...'). "
            "Never generate one QM card per instrument — it exceeds the output token limit.\n"
            "- m2: 5-10 recommendations. tipo must be one of: param, logic, risk, data, meta. prioridad: alta/media/baja. estado always \"pendiente\". comment always \"\".\n"
            "- m3: 5-8 observations. tipo must be one of: warn, error, info. comment always \"\".\n"
            "- m4: 5-10 code findings. categoria must be one of: bug, riesgo, ausencia, mejora. Use \\n for line breaks inside code/fix strings. comment always \"\".\n"
            "- Recommendations in m2 ordered alta -> media -> baja.\n"
            "- m4 bugs and riesgos first, then ausencias, then mejoras."
        ),
        "cache_control": {"type": "ephemeral"},
    }
]

CRITIC_SYSTEM = [
    {
        "type": "text",
        "text": (
            "You are a QA auditor reviewing a completed trading bot analysis for gaps and errors.\n\n"
            "Your task: identify findings NOT already covered. Focus on:\n"
            "1. Look-ahead bias: does the code use future data to make past decisions?\n"
            "2. Survivorship/overfitting: is sample size too small, or backtest period too short?\n"
            "3. Missing risk controls: slippage, spread, commission not accounted for?\n"
            "4. Internal inconsistency: do m2 recommendations logically address the m3/m4 findings?\n"
            "5. Score calibration: is m1 score too generous given the severity of bugs found?\n"
            "6. Python facts: are magic_numbers or high complexity scores not reflected in findings?\n\n"
            "Return ONLY genuinely new findings not already in the analysis. "
            "If the analysis is complete, return empty arrays.\n"
            "Limit: 0-3 cards per module.\n\n"
            "OUTPUT ONLY VALID JSON. ASCII only. All text in Spanish.\n\n"
            "Schema:\n"
            "{\n"
            "  \"additional_m2\": [same schema as m2 cards, IDs start at R-20],\n"
            "  \"additional_m3\": [same schema as m3 cards, IDs start at OBS-20],\n"
            "  \"additional_m4\": [same schema as m4 cards, IDs start at H-20]\n"
            "}"
        ),
        "cache_control": {"type": "ephemeral"},
    }
]

GPT4O_SYSTEM = (
    "You are a senior Python engineer specializing in algorithmic trading systems. "
    "Review the Python code provided and return ONLY findings not already covered in the existing analysis. "
    "Focus on Python-specific issues: threading/concurrency bugs, exception handling gaps in order "
    "execution paths, race conditions, memory leaks, hardcoded values that break under live conditions, "
    "logic errors in signal generation, missing input validation, and MT5/broker API misuse patterns. "
    "IDs: m2 starts at R-30, m4 starts at H-30. "
    "Limit: 0-3 cards per module. Return empty arrays if nothing new found. "
    "Output valid JSON: {\"additional_m2\": [...], \"additional_m4\": [...]}. "
    "ASCII only (no accented vowels, no em-dashes). All descriptions in Spanish."
)

FINALIZE_SYSTEM = [
    {
        "type": "text",
        "text": (
            "You are updating specific cards in a trading bot analysis based on trader feedback.\n\n"
            "You receive ONLY the cards that need changes — not the full analysis.\n"
            "CRITICAL: Apply every correction exactly as the trader specified. Do not soften or ignore any correction.\n\n"
            "Instructions:\n"
            "- Rewrite each card's title, desc, and/or fix/note to incorporate the correction substantively.\n"
            "- Set the card's \"comment\" field to a brief note summarizing what was adjusted.\n"
            "- Do NOT change IDs, tipos, prioridad, or card structure.\n"
            "- Remove the \"correction\" key from every card.\n"
            "- If m1 is included, update it accordingly and remove its \"correction\" key.\n"
            "- Return ONLY the cards you received — do not add or invent cards.\n"
            "- OUTPUT ONLY VALID JSON. No markdown fences. ASCII only. All text in Spanish.\n\n"
            "Output: {\"m1\": {...} (only if included in input), "
            "\"m2\": [...], \"m3\": [...], \"m4\": [...]} — only include modules present in input."
        ),
        "cache_control": {"type": "ephemeral"},
    }
]

TIPO_MAP = {
    "param": "Parametro",
    "logic": "Logica",
    "risk": "Riesgo",
    "data": "Datos",
    "meta": "Meta",
}


def build_analysis_user(files_text, date_str, preproc_facts=None, comparison=None,
                        version_context=None, profile_hint=None):
    parts = [f"Today: {date_str}"]
    if profile_hint:
        parts.append("=== TRADER PROFILE ===\n" + profile_hint)
    if version_context:
        parts.append(
            "=== PREVIOUS VERSION CONTEXT ===\n"
            + json.dumps(version_context, ensure_ascii=True, indent=2)
        )
    if preproc_facts:
        parts.append(
            "=== PRE-ANALYSIS FACTS (computed deterministically — verified, do not recalculate) ===\n"
            + json.dumps(preproc_facts, ensure_ascii=True, indent=2)
        )
    if comparison:
        parts.append(
            "=== LIVE VS BACKTEST COMPARISON ===\n"
            + json.dumps(comparison, ensure_ascii=True, indent=2)
        )
    parts.append(f"Files:\n{files_text}")
    return "\n\n".join(parts)


def build_critic_user(analysis, preproc_facts):
    parts = [
        "Initial analysis:\n" + json.dumps(
            {"m1": analysis.get("m1"), "m2": analysis.get("m2"),
             "m3": analysis.get("m3"), "m4": analysis.get("m4")},
            ensure_ascii=True, indent=2
        )
    ]
    if preproc_facts:
        compact = [
            {k: v for k, v in r.items()
             if k in ("filename", "lines", "functions", "complexity", "magic_numbers", "syntax_error", "pnl_stats", "rows")}
            for r in preproc_facts
        ]
        parts.append(
            "Pre-analysis facts:\n" + json.dumps(compact, ensure_ascii=True, indent=2)
        )
    return "\n\n".join(parts)


def build_gpt4o_user(analysis, py_files_text):
    existing_titles = [
        c["title"] for c in analysis.get("m2", []) + analysis.get("m4", [])
    ]
    return (
        f"Already covered — do NOT repeat these:\n{json.dumps(existing_titles, ensure_ascii=True)}\n\n"
        f"Code to review (JSON output required):\n{py_files_text}"
    )


def _find_related_groups(current_group, all_groups):
    """Find groups sharing the same folder_id as current_group."""
    folder_id = current_group.get("folder_id")
    if not folder_id:
        return []
    return [
        g for g in all_groups
        if g.get("folder_id") == folder_id and g["badge"] != current_group["badge"]
    ]


def _build_comparison_block(current_group, all_groups, preproc_facts):
    """
    When the current group is a backtest (has CSVs), find related live bot groups
    and build a comparison block with expected live performance ranges.
    """
    has_csv = any(f["name"].endswith(".csv") for f in current_group.get("files", []))
    if not has_csv or not all_groups:
        return None

    related = _find_related_groups(current_group, all_groups)
    live_groups = [
        g for g in related
        if any(f["name"].endswith(".py") for f in g.get("files", []))
        and not any(f["name"].endswith(".csv") for f in g.get("files", []))
    ]
    if not live_groups:
        return None

    # Aggregate backtest WR and PF across all CSV files
    wrs, pfs = [], []
    per_instrument = []
    for fact in (preproc_facts or []):
        ps = fact.get("pnl_stats", {})
        if not ps:
            continue
        entry = {"file": fact["filename"], "n_trades": ps.get("total_trades")}
        if ps.get("win_rate_pct"):
            entry["win_rate_pct"]  = ps["win_rate_pct"]
            wrs.append(ps["win_rate_pct"])
        if ps.get("profit_factor"):
            entry["profit_factor"] = ps["profit_factor"]
            pfs.append(ps["profit_factor"])
        wf = ps.get("walk_forward")
        if wf:
            entry["walk_forward_verdict"] = wf["verdict"]
        per_instrument.append(entry)

    if not per_instrument:
        return None

    avg_wr = round(sum(wrs) / len(wrs), 1) if wrs else None
    avg_pf = round(sum(pfs) / len(pfs), 2) if pfs else None

    # Expected live range: empirical 20-40% WR degradation, 40-60% PF degradation
    expected = {}
    if avg_wr:
        expected["win_rate_pct"]   = {"low": round(avg_wr * 0.60, 1), "high": round(avg_wr * 0.80, 1)}
    if avg_pf:
        expected["profit_factor"]  = {"low": round(avg_pf * 0.40, 2), "high": round(avg_pf * 0.60, 2)}

    live_has_data = any(
        any(f["name"].endswith(".csv") for f in g.get("files", []))
        for g in live_groups
    )

    print(f"    Comparison: backtest {current_group['badge']} vs live "
          f"{[g['badge'] for g in live_groups]} — live_data={live_has_data}")

    return {
        "backtest_group":      current_group["badge"],
        "live_bot_groups":     [g["badge"] for g in live_groups],
        "live_data_available": live_has_data,
        "per_instrument":      per_instrument,
        "backtest_averages":   {"win_rate_pct": avg_wr, "profit_factor": avg_pf},
        "expected_live_range": expected,
        "note": (
            "Live bots running but no real trade CSV uploaded yet. "
            "Upload MT5 trade export to enable actual live-vs-backtest comparison."
        ) if not live_has_data else
        "Live trade data available — compare metrics above against backtest.",
    }


def _build_version_context(group, all_groups):
    """
    If this group is a new version of an existing bot, return the previous version's
    final analysis summary so Claude can build on it instead of starting from scratch.
    """
    version_of = group.get("version_of")
    if not version_of or not all_groups:
        return None

    prev = next((g for g in all_groups if g.get("badge") == version_of), None)
    if not prev or prev.get("status") not in ("activo", "en_revision"):
        return None

    ctx = {
        "previous_badge": version_of,
        "previous_name":  prev.get("name", ""),
    }

    m1 = prev.get("m1", {})
    if isinstance(m1, dict) and m1.get("type") == "quality":
        ctx["previous_score"] = m1.get("score", {}).get("valor")

    for module in ("m2", "m3", "m4"):
        by_estado = {"implementado": [], "descartado": [], "pendiente": []}
        for c in prev.get(module, []):
            estado = c.get("estado", "pendiente")
            if estado in by_estado:
                by_estado[estado].append({"id": c["id"], "title": c["title"]})
        ctx[f"{module}_estados"] = by_estado

    implemented = sum(len(ctx[f"{m}_estados"]["implementado"]) for m in ("m2","m3","m4"))
    discarded   = sum(len(ctx[f"{m}_estados"]["descartado"])   for m in ("m2","m3","m4"))
    print(f"    Version context: {version_of} — {implemented} implemented, {discarded} discarded by trader")
    return ctx


def _update_trader_profile(data, group):
    """
    After each pendiente_final cycle, update root-level trader_profile with stats
    extracted from the correction fields and card estados — BEFORE they are cleared.
    """
    profile = data.get("trader_profile") or {
        "cycles":         0,
        "m2_by_tipo":     {},   # tipo  -> {corrected, discarded, total}
        "m4_by_categoria":{},   # cat   -> {corrected, total}
        "correction_rates": [],
    }

    profile["cycles"] = profile.get("cycles", 0) + 1
    total = corrected = 0

    for card in group.get("m2", []):
        tipo  = card.get("tipo", "other")
        has_c = bool((card.get("correction") or "").strip())
        total += 1
        if has_c: corrected += 1
        b = profile["m2_by_tipo"].setdefault(tipo, {"corrected": 0, "discarded": 0, "total": 0})
        b["total"] += 1
        if has_c:                          b["corrected"] += 1
        if card.get("estado") == "descartado": b["discarded"] += 1

    for card in group.get("m3", []):
        total += 1
        if (card.get("correction") or "").strip(): corrected += 1

    for card in group.get("m4", []):
        cat   = card.get("categoria", "other")
        has_c = bool((card.get("correction") or "").strip())
        total += 1
        if has_c: corrected += 1
        b = profile["m4_by_categoria"].setdefault(cat, {"corrected": 0, "total": 0})
        b["total"] += 1
        if has_c: b["corrected"] += 1

    if total > 0:
        profile["correction_rates"].append(round(corrected / total, 2))

    profile["last_updated"] = date.today().isoformat()
    data["trader_profile"]  = profile
    print(f"    Profile updated: cycle {profile['cycles']}, "
          f"correction rate {round(corrected/total*100) if total else 0}%")


def _build_profile_hint(data):
    """
    Convert the raw trader_profile stats into a compact hint string for Pass 1.
    Returns None if there are fewer than 2 cycles (not enough signal yet).
    """
    profile = data.get("trader_profile")
    if not profile or profile.get("cycles", 0) < 2:
        return None

    lines = [f"Trader correction history ({profile['cycles']} completed cycles):"]

    # M2 tipos with high discard rate — Claude is generating noise here
    high_discard = []
    for tipo, s in profile.get("m2_by_tipo", {}).items():
        if s["total"] >= 3 and s["discarded"] / s["total"] >= 0.5:
            high_discard.append(f"{tipo} ({round(s['discarded']/s['total']*100)}% discarded)")
    if high_discard:
        lines.append(f"- M2 tipos with high discard rate: {', '.join(high_discard)} "
                     f"— reduce or only include when very strong evidence")

    # M4 categories frequently corrected — depth is insufficient here
    weak_cats = []
    for cat, s in profile.get("m4_by_categoria", {}).items():
        if s["total"] >= 3 and s["corrected"] / s["total"] >= 0.6:
            weak_cats.append(cat)
    if weak_cats:
        lines.append(f"- M4 categories frequently corrected (improve depth): {', '.join(weak_cats)}")

    # M4 categories rarely corrected — Claude's analysis is landing well here
    accurate_cats = []
    for cat, s in profile.get("m4_by_categoria", {}).items():
        if s["total"] >= 3 and s["corrected"] / s["total"] <= 0.15:
            accurate_cats.append(cat)
    if accurate_cats:
        lines.append(f"- M4 categories trader rarely corrects (keep this approach): {', '.join(accurate_cats)}")

    # Overall correction rate as calibration signal
    rates = profile.get("correction_rates", [])
    if rates:
        avg = round(sum(rates) / len(rates) * 100)
        if avg >= 50:
            lines.append(f"- High correction rate ({avg}%) — analyses are missing trader expectations; "
                         f"be more thorough and specific")
        elif avg <= 15:
            lines.append(f"- Low correction rate ({avg}%) — analyses are well-calibrated to trader expectations")

    return "\n".join(lines) if len(lines) > 1 else None


def build_finalize_user(group):
    """Build a minimal payload with ONLY the corrected cards — not the full analysis."""
    trader_notes = (group.get("trader_notes") or "").strip()
    m1 = group.get("m1", {})
    m1_correction = (m1.get("correction") or "").strip()

    payload = {}
    if m1_correction:
        payload["m1"] = m1

    for module in ("m2", "m3", "m4"):
        corrected = [c for c in group.get(module, []) if (c.get("correction") or "").strip()]
        if corrected:
            payload[module] = corrected

    n = sum(len(v) for v in payload.values() if isinstance(v, list)) + bool(m1_correction)
    print(f"    Sending {n} corrected card(s) to Claude (of "
          f"{sum(len(group.get(m,[])) for m in ('m2','m3','m4'))} total)")

    return (
        f"Trader general notes: \"{trader_notes or 'None'}\"\n\n"
        f"Cards to update ({n} corrected — only these are shown):\n"
        + json.dumps(payload, ensure_ascii=True, indent=2)
    )


# ── Claude API ────────────────────────────────────────────────────────────────

def call_claude(system_blocks, user_content):
    msg = client.beta.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        system=system_blocks,
        messages=[{"role": "user", "content": user_content}],
        betas=["prompt-caching-2024-07-31"],
    )
    text = msg.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return json.loads(text)


# ── GPT-4o API ────────────────────────────────────────────────────────────────

def call_gpt4o(system_text, user_content):
    from openai import OpenAI
    oa = OpenAI()
    resp = oa.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system_text},
            {"role": "user",   "content": user_content},
        ],
        response_format={"type": "json_object"},
        max_tokens=4096,
    )
    return json.loads(resp.choices[0].message.content)


# ── Merge helpers ─────────────────────────────────────────────────────────────

def merge_additional(analysis, extra, modules=("m2", "m3", "m4")):
    """Append additional_mX cards from extra dict into analysis."""
    added = {m: 0 for m in modules}
    for module in modules:
        cards = extra.get(f"additional_{module}") or []
        if cards:
            analysis[module] = analysis.get(module, []) + cards
            added[module] = len(cards)
    return added


# ── Processing ────────────────────────────────────────────────────────────────

MAX_CHARS_PER_FILE = 20_000
CSV_SAMPLE_ROWS    = 30

def _prepare_file_block(fname, content):
    """Return a prompt-safe block for a file, applying type-specific truncation."""
    ext = fname.rsplit(".", 1)[-1].lower()

    if ext in SKIP_EXTENSIONS:
        print(f"    [{fname}] {ext.upper()}: unsupported format — skipped")
        return None

    if ext == "html":
        # Raw HTML is JavaScript-heavy — send only a placeholder.
        # All useful stats are extracted by preprocess_html into PRE-ANALYSIS FACTS.
        print(f"    [{fname}] HTML: placeholder only — stats extracted via pre-analysis")
        return f"=== {fname} ===\n[MT5 HTML backtest report — statistics extracted in PRE-ANALYSIS FACTS]"

    if ext == "csv":
        # Raw CSV rows are redundant: preprocess_csv already computes all metrics.
        # Include only a small sample so Claude can see the schema.
        lines = content.splitlines()
        header = lines[0] if lines else ""
        sample = lines[1 : CSV_SAMPLE_ROWS + 1]
        omitted = len(lines) - 1 - len(sample)
        body = "\n".join([header] + sample)
        if omitted > 0:
            body += f"\n... {omitted} more rows omitted — full stats in PRE-ANALYSIS FACTS"
        print(f"    [{fname}] CSV truncated to {len(sample)} sample rows (of {len(lines)-1})")
        return f"=== {fname} ===\n{body}"

    # Python files: no cap — cost scales linearly with size, bots rarely exceed 200 KB
    if ext != "py" and len(content) > MAX_CHARS_PER_FILE:
        content = content[:MAX_CHARS_PER_FILE] + f"\n... [truncated at {MAX_CHARS_PER_FILE} chars]"
        print(f"    [{fname}] truncated to {MAX_CHARS_PER_FILE} chars")
    return f"=== {fname} ===\n{content}"


def process_pending(group, all_groups=None, data=None):
    print(f"  Generating analysis for {group['badge']} — {group['name']}")

    parts            = []
    py_parts         = []
    obfuscated_files = []

    for f in group.get("files", []):
        fname = f["name"]
        ext   = fname.rsplit(".", 1)[-1].lower()
        if ext in SKIP_EXTENSIONS:
            print(f"    [{fname}] {ext.upper()}: unsupported format — skipped")
            continue
        content = read_file(group["folder"], fname)
        if not content:
            continue
        if ext == "py":
            reason = _detect_obfuscation(fname, content)
            if reason:
                print(f"    [{fname}] OBFUSCATED: {reason}")
                obfuscated_files.append((fname, reason))
                continue
        block = _prepare_file_block(fname, content)
        if block:
            parts.append(block)
            if fname.endswith(".py"):
                py_parts.append(block)

    if obfuscated_files:
        names = ", ".join(fn for fn, _ in obfuscated_files)
        group["status"]             = "activo"
        group["category"]           = "Archivo ofuscado - no analizable"
        group["summary"]            = (
            f"El archivo {names} parece estar ofuscado o protegido. "
            f"No es posible analizar codigo ofuscado. "
            f"Sube el codigo fuente original sin ofuscacion para obtener el analisis completo."
        )
        group["m1"] = {
            "type": "empty",
            "last_updated": date.today().isoformat() + "T00:00:00Z",
            "last_updated_meta": names,
            "empty_title": "Archivo ofuscado",
            "empty_desc": (
                f"No es posible analizar este archivo porque parece estar ofuscado o protegido. "
                f"Sube el codigo fuente legible para obtener el analisis."
            ),
            "empty_trigger": names,
        }
        group["m2"]                 = []
        group["m3"]                 = []
        group["m4"]                 = []
        group["trader_notes"]       = ""
        group["revision_submitted"] = False
        group["rereview_requested"] = False
        group["rereview_notes"]     = ""
        print(f"  -> OBFUSCATED — marked activo with error message")
        return True

    if not parts:
        print("  No readable files found — skipping")
        return False

    print("  Running pre-analysis...")
    preproc = preprocess_files(group.get("files", []), group["folder"])
    if preproc:
        for r in preproc:
            if "pnl_stats" in r:
                print(f"    {r['filename']}: {r['pnl_stats'].get('total_trades')} trades, "
                      f"WR {r['pnl_stats'].get('win_rate_pct')}%, "
                      f"PF {r['pnl_stats'].get('profit_factor')}")
            elif "syntax_error" in r:
                print(f"    {r['filename']}: SYNTAX ERROR — {r['syntax_error']}")
            else:
                print(f"    {r['filename']}: {r.get('lines', '?')} lines, "
                      f"{len(r.get('functions', []))} functions")

    # Live vs backtest comparison block
    comparison = _build_comparison_block(group, all_groups, preproc)

    # Version continuity + trader profile
    version_ctx  = _build_version_context(group, all_groups)
    profile_hint = _build_profile_hint(data) if data else None

    # Pass 1 — Claude initial analysis
    print("  Pass 1 — Claude initial analysis...")
    analysis = call_claude(
        ANALYSIS_SYSTEM,
        build_analysis_user(
            "\n\n".join(parts), date.today().isoformat(),
            preproc, comparison,
            version_context=version_ctx,
            profile_hint=profile_hint,
        ),
    )
    print(f"    -> {len(analysis.get('m2',[]))} recs | {len(analysis.get('m3',[]))} obs | {len(analysis.get('m4',[]))} findings")

    # Pass 2 — Claude critic
    print("  Pass 2 — Claude critic...")
    try:
        critic_out = call_claude(CRITIC_SYSTEM, build_critic_user(analysis, preproc))
        added = merge_additional(analysis, critic_out)
        total = sum(added.values())
        print(f"    -> critic added {added['m2']} recs, {added['m3']} obs, {added['m4']} findings"
              if total else "    -> critic: no gaps found")
    except Exception as e:
        print(f"    [warn] Critic pass failed: {e}")

    # Pass 3 — GPT-4o (optional)
    if os.environ.get("OPENAI_API_KEY") and py_parts:
        print("  Pass 3 — GPT-4o code review...")
        try:
            gpt_out = call_gpt4o(GPT4O_SYSTEM, build_gpt4o_user(analysis, "\n\n".join(py_parts)))
            added_g = merge_additional(analysis, gpt_out, modules=("m2", "m4"))
            total_g = sum(added_g.values())
            print(f"    -> GPT-4o added {added_g['m2']} recs, {added_g['m4']} findings"
                  if total_g else "    -> GPT-4o: no additional findings")
        except Exception as e:
            print(f"    [warn] GPT-4o pass failed: {e}")
    else:
        reason = "no Python files" if not py_parts else "OPENAI_API_KEY not set"
        print(f"  Pass 3 — GPT-4o skipped ({reason})")

    group["status"]             = "en_revision"
    group["category"]           = analysis.get("category", "")
    group["summary"]            = analysis.get("summary", "")
    group["m1"]                 = analysis.get("m1", group.get("m1", {}))
    group["m2"]                 = analysis.get("m2", [])
    group["m3"]                 = analysis.get("m3", [])
    group["m4"]                 = analysis.get("m4", [])
    group["trader_notes"]       = ""
    group["revision_submitted"] = False
    group["rereview_requested"] = False
    group["rereview_notes"]     = ""

    for card in group["m2"]:
        if not card.get("tipo_label"):
            card["tipo_label"] = TIPO_MAP.get(card.get("tipo", ""), card.get("tipo", ""))

    print(f"  -> FINAL: {len(group['m2'])} recs | {len(group['m3'])} obs | {len(group['m4'])} findings")

    if _M5_AVAILABLE:
        print("  Pass 4 — M5 market context...")
        try:
            macro = build_macro_snapshot()
            group["m5"] = _generate_m5(group, macro)
            print(f"  -> M5: {len(group['m5'].get('cards', []))} cards")
        except Exception as e:
            print(f"  [warn] M5 generation failed: {e}")

    return True


def process_pendiente_final(group, data=None):
    print(f"  Finalizing {group['badge']} — {group['name']}")

    trader_notes  = (group.get("trader_notes") or "").strip()
    m1_correction = (group.get("m1", {}).get("correction") or "").strip()
    has_corrections = m1_correction or any(
        (card.get("correction") or "").strip()
        for card in group.get("m2", []) + group.get("m3", []) + group.get("m4", [])
    )

    # Update trader profile BEFORE corrections are cleared — this is where the signal lives
    if data:
        try:
            _update_trader_profile(data, group)
        except Exception as e:
            print(f"    [warn] Profile update failed: {e}")

    if trader_notes or has_corrections:
        updated = call_claude(FINALIZE_SYSTEM, build_finalize_user(group))
        # Merge by ID: only replace cards that Claude actually rewrote
        if "m1" in updated:
            group["m1"] = updated["m1"]
        for module in ("m2", "m3", "m4"):
            if module in updated:
                updated_by_id = {c["id"]: c for c in updated[module]}
                group[module] = [
                    updated_by_id.get(card["id"], card)
                    for card in group.get(module, [])
                ]
                print(f"    {module}: updated {len(updated_by_id)} card(s)")
    else:
        print("  No corrections — approving as-is")

    group["status"]             = "activo"
    group["trader_notes"]       = ""
    group["revision_submitted"] = False
    group["rereview_requested"] = False
    group["rereview_notes"]     = ""

    for card in group.get("m2", []) + group.get("m3", []) + group.get("m4", []):
        card.pop("correction", None)
    if isinstance(group.get("m1"), dict):
        group["m1"].pop("correction", None)

    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== Trading Bot Analyzer ===")

    data   = get_data()
    groups = data.get("groups", [])

    pending         = [g for g in groups if g.get("status") == "pending"]
    pendiente_final = [g for g in groups if g.get("status") == "pendiente_final"]

    if not pending and not pendiente_final:
        print("Nothing to process.")
        return

    changed   = False
    had_error = False

    for g in pending:
        print(f"\n[PENDING] {g['badge']} — {g['name']}")
        try:
            if process_pending(g, groups, data):
                changed = True
                print(f"  -> {g['status']}")
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            had_error = True

    for g in pendiente_final:
        print(f"\n[PENDIENTE_FINAL] {g['badge']} — {g['name']}")
        try:
            if process_pendiente_final(g, data):
                changed = True
                print("  -> activo")
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
