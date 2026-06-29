#!/usr/bin/env python3
"""
Backtest — Relative Strength Scanner
Período: últimos 365 días en datos IEX (Alpaca paper)
"""

import datetime
import statistics
import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

API_KEY    = "PKAUNSXX2KN5TGHXMK7P3CFCKN"
API_SECRET = "ET3LUrxB9SN1sKtG6K5AbvqjYyH41H8AjciocFrMNLcY"

# ── Selección de tickers del S&P 500 ─────────────────────────────────────────
# Criterios: alta liquidez, distintos sectores, representativos del índice
TICKERS = {
    # Tecnología
    "AAPL":  "Tecnología",
    "MSFT":  "Tecnología",
    "NVDA":  "Semiconductores",
    "AMD":   "Semiconductores",
    "QCOM":  "Semiconductores",
    "TXN":   "Semiconductores",
    "AMAT":  "Semiconductores",
    "INTC":  "Semiconductores",
    # Comunicaciones / Internet
    "META":  "Comunicaciones",
    "GOOGL": "Comunicaciones",
    "NFLX":  "Comunicaciones",
    "DIS":   "Entretenimiento",
    # Consumo discrecional
    "AMZN":  "Consumo Discr.",
    "TSLA":  "Automóviles",
    "HD":    "Retail",
    "MCD":   "Restaurantes",
    "COST":  "Retail",
    "NKE":   "Deportes",
    "SBUX":  "Restaurantes",
    "TGT":   "Retail",
    # Consumo básico
    "PG":    "Consumo Básico",
    "KO":    "Consumo Básico",
    "PEP":   "Consumo Básico",
    "WMT":   "Retail",
    # Finanzas
    "JPM":   "Financiero",
    "GS":    "Financiero",
    "MS":    "Financiero",
    "BAC":   "Financiero",
    "V":     "Pagos",
    "MA":    "Pagos",
    "AXP":   "Pagos",
    "BLK":   "Asset Mgmt",
    # Energía
    "XOM":   "Energía",
    "CVX":   "Energía",
    "COP":   "Energía",
    "SLB":   "Energía",
    # Salud
    "UNH":   "Salud",
    "LLY":   "Farmacéutica",
    "ABT":   "Salud",
    "JNJ":   "Salud",
    "MRK":   "Farmacéutica",
    # Industriales
    "CAT":   "Industriales",
    "HON":   "Industriales",
    "GE":    "Industriales",
    "DE":    "Industriales",
    "RTX":   "Defensa",
    # Software / Cloud
    "CRM":   "Software",
    "NOW":   "Software",
    "PANW":  "Ciberseguridad",
    "ADBE":  "Software",
    # Utilities / Real Estate
    "NEE":   "Utilities",
    "AMT":   "Real Estate",
}
SPX_PROXY = "SPY"

# ── Parámetros de estrategia (mismo que el bot) ───────────────────────────────
RS_LOOKBACK        = 20
KEY_LEVEL_LOOKBACK = 60
MIN_TOUCHES        = 2
RS_THRESHOLD       = 0.0   # solo fuerza relativa real
NEAR_PCT           = 0.02
ENTRY_BUFFER       = 0.005
STOP_BUFFER        = 0.01
MIN_RR             = 3.0
MIN_TARGET_PCT     = 0.03
TOLERANCE          = 0.015

# ── Período del backtest ──────────────────────────────────────────────────────
BT_DAYS   = 365          # días de historia para backtest
WARMUP    = KEY_LEVEL_LOOKBACK + RS_LOOKBACK + 5   # días de warmup sin operar

client = StockHistoricalDataClient(API_KEY, API_SECRET)


# ── Datos ─────────────────────────────────────────────────────────────────────

def fetch(ticker: str, days: int) -> pd.DataFrame:
    end   = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(days=days + 20)
    req = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    df = client.get_stock_bars(req).df
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(ticker, level=0)
    df = df.sort_index()
    # Normalizar índice a fecha (sin tz)
    df.index = pd.to_datetime(df.index).normalize().tz_localize(None)
    return df.tail(days)


# ── Funciones de análisis (replicadas del bot) ────────────────────────────────

def find_resistance_levels(df: pd.DataFrame, lookback: int, min_touches: int):
    data   = df.tail(lookback)
    prices = list(data["high"]) + list(data["low"])
    clusters: list[list[float]] = []
    for p in sorted(prices):
        placed = False
        for c in clusters:
            if abs(p - statistics.mean(c)) / statistics.mean(c) <= TOLERANCE:
                c.append(p)
                placed = True
                break
        if not placed:
            clusters.append([p])
    levels = []
    for c in clusters:
        if len(c) >= min_touches:
            levels.append((statistics.mean(c), len(c)))
    return levels   # lista de (precio, toques)


def calc_rs(stock_df, spx_df, date_idx, lookback):
    stock = stock_df["close"].pct_change()
    spx   = spx_df["close"].pct_change()
    common = stock.index.intersection(spx.index)
    stock, spx = stock.loc[common], spx.loc[common]

    window_end = date_idx
    window = stock.index[stock.index <= window_end][-lookback:]
    if len(window) < 5:
        return 1.0

    s_w = stock.loc[window]
    p_w = spx.loc[window]
    down = p_w[p_w < 0].index
    if len(down) == 0:
        return 1.0

    sm = s_w.loc[down].mean()
    pm = p_w.loc[down].mean()
    if pm == 0:
        return 1.0
    return float(sm / pm)


def spx_reboting_on(spx_df, date_idx):
    loc = spx_df.index.get_loc(date_idx) if date_idx in spx_df.index else None
    if loc is None or loc == 0:
        return False
    return bool(spx_df["close"].iloc[loc] > spx_df["close"].iloc[loc - 1])


def level_held(stock_df, date_idx, level_price, lookback=5):
    pos = stock_df.index.get_loc(date_idx) if date_idx in stock_df.index else None
    if pos is None or pos < lookback:
        return False
    recent = stock_df["close"].iloc[pos - lookback: pos + 1]
    floor  = level_price * (1 - STOP_BUFFER)
    return bool((recent >= floor).all())


def find_next_res(levels, entry, stop):
    above = sorted([p for p, _ in levels if p > entry])
    risk = entry - stop
    min_by_rr  = entry + MIN_RR * risk
    min_by_pct = entry * (1 + MIN_TARGET_PCT)
    min_target = max(min_by_rr, min_by_pct)
    for p in above:
        if p >= min_target:
            return p
    return min_target


# ── Motor de backtest ─────────────────────────────────────────────────────────

COOLDOWN_DAYS     = 10    # mínimo días entre trades del mismo ticker
TREND_FILTER      = True  # activar/desactivar filtro de tendencia
TREND_MAX_DD      = 0.10  # no operar si precio > 10% bajo el máximo de 60 días


def is_in_downtrend(hist: pd.DataFrame, lookback: int = 60, max_dd: float = 0.10) -> bool:
    """True si el precio actual está más de max_dd% por debajo del máximo de lookback días."""
    window   = hist.tail(lookback)
    high_60  = float(window["high"].max())
    current  = float(hist["close"].iloc[-1])
    drawdown = (high_60 - current) / high_60
    return drawdown > max_dd


def backtest_ticker(ticker: str, stock_df: pd.DataFrame, spx_df: pd.DataFrame,
                    trend_filter: bool = False):
    trades = []
    dates  = stock_df.index[WARMUP:]
    last_trade_date = None

    for i, date in enumerate(dates):
        # Cooldown: no operar el mismo ticker en menos de COOLDOWN_DAYS
        if last_trade_date is not None:
            if (date - last_trade_date).days < COOLDOWN_DAYS:
                continue

        pos_in_df = stock_df.index.get_loc(date)
        hist      = stock_df.iloc[:pos_in_df + 1]

        current_price = float(hist["close"].iloc[-1])

        # Filtro de tendencia: no operar si está en caída >10% desde máximo 60d
        if trend_filter and is_in_downtrend(hist, lookback=60, max_dd=TREND_MAX_DD):
            continue

        # Niveles en ventana KEY_LEVEL_LOOKBACK
        levels = find_resistance_levels(hist, KEY_LEVEL_LOOKBACK, MIN_TOUCHES)
        if not levels:
            continue

        # Soporte: nivel DEBAJO del precio actual (precio lo superó y sigue encima)
        res_candidates = [(p, t) for p, t in levels if p <= current_price]
        if not res_candidates:
            continue
        key_price = max(res_candidates, key=lambda x: x[0])[0]  # soporte más cercano debajo

        # Condiciones
        rs = calc_rs(stock_df, spx_df, date, RS_LOOKBACK)
        # Precio debe estar encima del nivel y dentro del 2%
        near  = (current_price >= key_price) and (current_price - key_price) / key_price <= NEAR_PCT
        spx_r = spx_reboting_on(spx_df, date)
        l_h   = level_held(stock_df, date, key_price * 0.99)
        rs_ok = rs < RS_THRESHOLD

        if not (spx_r and l_h and near and rs_ok):
            continue

        # Setup válido → calcular trade
        entry  = key_price * (1 + ENTRY_BUFFER)
        stop   = key_price * (1 - STOP_BUFFER)
        target = find_next_res(levels, entry, stop)
        risk   = entry - stop
        reward = target - entry
        if risk <= 0:
            continue
        rr = reward / risk
        if rr < MIN_RR:
            continue

        # Simular resultado: buscar en los siguientes 10 días si toca target o stop
        future = stock_df.iloc[pos_in_df + 1: pos_in_df + 11]
        result = "OPEN"
        exit_price = None
        exit_date  = None

        for _, row in future.iterrows():
            if row["low"] <= stop:
                result     = "LOSS"
                exit_price = stop
                exit_date  = row.name
                break
            if row["high"] >= target:
                result     = "WIN"
                exit_price = target
                exit_date  = row.name
                break

        if result == "OPEN" and not future.empty:
            exit_price = float(future["close"].iloc[-1])
            exit_date  = future.index[-1]
            result     = "WIN" if exit_price >= entry else "LOSS"

        if exit_price is None:
            continue

        pnl_pct = (exit_price - entry) / entry * 100

        trades.append({
            "ticker":      ticker,
            "entry_date":  date,
            "exit_date":   exit_date,
            "entry":       round(entry, 2),
            "target":      round(target, 2),
            "stop":        round(stop, 2),
            "key_level":   round(key_price, 2),
            "exit_price":  round(exit_price, 2),
            "rr":          round(rr, 2),
            "rs_score":    round(rs, 3),
            "result":      result,
            "pnl_pct":     round(pnl_pct, 2),
        })
        last_trade_date = date

    return trades


# ── Reporte ───────────────────────────────────────────────────────────────────

def print_results(all_trades: list[dict]):
    if not all_trades:
        print("\n❌ Sin trades en el período analizado.")
        return

    df = pd.DataFrame(all_trades)

    wins   = df[df["result"] == "WIN"]
    losses = df[df["result"] == "LOSS"]
    total  = len(df)
    win_r  = len(wins) / total * 100 if total else 0
    avg_win  = wins["pnl_pct"].mean()   if len(wins)   else 0
    avg_loss = losses["pnl_pct"].mean() if len(losses) else 0
    expectancy = (win_r/100 * avg_win) + ((1 - win_r/100) * avg_loss)
    total_pnl  = df["pnl_pct"].sum()

    print("\n" + "═" * 60)
    print("  📊 RESULTADOS DEL BACKTEST — RS Scanner")
    print(f"  Período: últimos {BT_DAYS} días  |  Tickers: {len(TICKERS)}")
    print("═" * 60)
    print(f"\n  Total trades:      {total}")
    print(f"  Win rate:          {win_r:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Avg ganancia:     +{avg_win:.2f}%")
    print(f"  Avg pérdida:       {avg_loss:.2f}%")
    print(f"  Expectancy:        {expectancy:.2f}% por trade")
    print(f"  PnL total acum:   {total_pnl:+.2f}%  (suma de todos los trades)")
    print(f"  Avg RS Score:      {df['rs_score'].mean():.3f}")
    print(f"  Avg R:R logrado:   {df['rr'].mean():.2f}")

    # Por ticker
    print("\n" + "─" * 60)
    print(f"  {'Ticker':<8} {'Sector':<22} {'Trades':>6} {'Win%':>6} {'PnL%':>8} {'AvgRS':>7}")
    print("─" * 60)

    for ticker in sorted(TICKERS.keys()):
        t = df[df["ticker"] == ticker]
        if t.empty:
            sector = TICKERS[ticker]
            print(f"  {ticker:<8} {sector:<22} {'—':>6} {'—':>6} {'—':>8} {'—':>7}")
            continue
        w  = len(t[t["result"] == "WIN"])
        wr = w / len(t) * 100
        pnl = t["pnl_pct"].sum()
        rs  = t["rs_score"].mean()
        sector = TICKERS[ticker]
        marker = "🔥" if wr >= 60 else ("✅" if wr >= 50 else "❌")
        print(f"  {ticker:<8} {sector:<22} {len(t):>6} {wr:>5.0f}% {pnl:>+7.1f}% {rs:>6.3f}  {marker}")

    # Top 5 mejores trades
    print("\n" + "─" * 60)
    print("  🏆 TOP 5 MEJORES TRADES")
    print("─" * 60)
    top = df.nlargest(5, "pnl_pct")[["ticker", "entry_date", "entry", "exit_price", "pnl_pct", "rr", "rs_score"]]
    for _, r in top.iterrows():
        print(f"  {r['ticker']:<6} {str(r['entry_date'])[:10]}  "
              f"${r['entry']:.2f}→${r['exit_price']:.2f}  "
              f"{r['pnl_pct']:+.2f}%  RR={r['rr']:.1f}  RS={r['rs_score']:.3f}")

    # 5 peores
    print("\n  💀 5 PEORES TRADES")
    print("─" * 60)
    bot5 = df.nsmallest(5, "pnl_pct")[["ticker", "entry_date", "entry", "exit_price", "pnl_pct", "rr", "rs_score"]]
    for _, r in bot5.iterrows():
        print(f"  {r['ticker']:<6} {str(r['entry_date'])[:10]}  "
              f"${r['entry']:.2f}→${r['exit_price']:.2f}  "
              f"{r['pnl_pct']:+.2f}%  RR={r['rr']:.1f}  RS={r['rs_score']:.3f}")

    # Distribución mensual
    print("\n" + "─" * 60)
    print("  📅 PnL POR MES")
    print("─" * 60)
    df["month"] = pd.to_datetime(df["entry_date"]).dt.to_period("M")
    monthly = df.groupby("month").agg(trades=("pnl_pct","count"), pnl=("pnl_pct","sum")).reset_index()
    for _, r in monthly.iterrows():
        bar = "█" * max(0, int(r["pnl"] / 2)) if r["pnl"] > 0 else "░" * max(0, int(abs(r["pnl"]) / 2))
        print(f"  {r['month']}  {r['trades']:>3} trades  {r['pnl']:>+6.1f}%  {bar}")

    print("\n" + "═" * 60)

    # Guardar CSV
    df.to_csv("/Users/mac/Documents/Relative-Strengh/backtest_results.csv", index=False)
    print("  💾 Resultados guardados en backtest_results.csv")
    print("═" * 60 + "\n")

    return df


def print_key_zones(df: pd.DataFrame, stock_data: dict, top_n: int = 4):
    """Muestra las zonas clave detectadas para los mejores tickers."""
    # Seleccionar top N tickers por PnL acumulado positivo
    by_pnl = df.groupby("ticker")["pnl_pct"].sum().sort_values(ascending=False)
    best = by_pnl[by_pnl > 0].head(top_n).index.tolist()

    print("\n" + "═" * 60)
    print("  🗺️  ZONAS CLAVE DETECTADAS — Mejores tickers")
    print("═" * 60)

    for ticker in best:
        t     = df[df["ticker"] == ticker].copy()
        t     = t.sort_values("entry_date")
        stock = stock_data.get(ticker)
        wins  = len(t[t["result"] == "WIN"])
        pnl   = t["pnl_pct"].sum()

        print(f"\n  {'─'*56}")
        print(f"  📈 {ticker}  ({TICKERS[ticker]})  "
              f"PnL={pnl:+.1f}%  {wins}W/{len(t)-wins}L")
        print(f"  {'─'*56}")

        # Niveles únicos detectados (agrupados por proximidad 1.5%)
        raw_levels = sorted(t["key_level"].unique())
        clusters: list[list[float]] = []
        for p in raw_levels:
            placed = False
            for c in clusters:
                if abs(p - statistics.mean(c)) / statistics.mean(c) <= 0.015:
                    c.append(p); placed = True; break
            if not placed:
                clusters.append([p])

        zones = [(statistics.mean(c), len(c)) for c in clusters]
        zones.sort(key=lambda x: x[0])

        print(f"\n  Zonas de nivel clave detectadas ({len(zones)} zonas):")
        for zone_price, hits in zones:
            zone_trades = t[abs(t["key_level"] - zone_price) / zone_price <= 0.015]
            zone_wins   = len(zone_trades[zone_trades["result"] == "WIN"])
            zone_pnl    = zone_trades["pnl_pct"].sum()
            bar = "█" * hits
            icon = "🟢" if zone_pnl > 0 else "🔴"
            print(f"    {icon} ${zone_price:>8.2f}  │ {bar:<5}  "
                  f"{hits} trades  {zone_wins}W/{hits-zone_wins}L  {zone_pnl:+.2f}%")

        # Tabla de trades individuales
        print(f"\n  Trades individuales:")
        print(f"  {'Fecha':<12} {'Nivel':>8} {'Entrada':>8} {'Salida':>8} {'PnL%':>7} {'RS':>6}  Res")
        print(f"  {'-'*56}")
        for _, r in t.iterrows():
            icon = "✅" if r["result"] == "WIN" else "❌"
            print(f"  {str(r['entry_date'])[:10]:<12} "
                  f"${r['key_level']:>7.2f} "
                  f"${r['entry']:>7.2f} "
                  f"${r['exit_price']:>7.2f} "
                  f"{r['pnl_pct']:>+6.2f}% "
                  f"{r['rs_score']:>6.3f}  {icon}")

        # Mini gráfico ASCII del precio con los niveles marcados
        if stock is not None:
            _print_ascii_chart(ticker, stock, t)

    print("\n" + "═" * 60 + "\n")


def _print_ascii_chart(ticker: str, stock: pd.DataFrame, trades: pd.DataFrame):
    """Gráfico ASCII del precio con zonas y entradas marcadas."""
    # Últimos 60 días de precio
    recent = stock.tail(60).copy()
    prices = recent["close"].values
    highs  = recent["high"].values
    lows   = recent["low"].values

    p_min  = float(lows.min())
    p_max  = float(highs.max())
    rows   = 12
    cols   = min(60, len(recent))

    step = (p_max - p_min) / rows if p_max != p_min else 1

    # Niveles del ticker en el período
    levels_in_range = trades["key_level"].unique()

    print(f"\n  Precio (últimos 60 días) — {ticker}")
    print(f"  ${p_max:>8.2f} ┐")

    chart = []
    for row in range(rows, -1, -1):
        level_price = p_min + row * step
        line = []
        for col in range(cols):
            # Usar sample proporcional si hay más barras que cols
            idx = int(col * len(recent) / cols)
            c = float(recent["close"].iloc[idx])
            h = float(recent["high"].iloc[idx])
            l = float(recent["low"].iloc[idx])
            in_range = l <= level_price <= h

            # ¿Hay un trade entry cerca de este nivel y columna?
            col_date = recent.index[idx]
            trade_here = trades[
                (pd.to_datetime(trades["entry_date"]).dt.normalize() == col_date) &
                (abs(trades["key_level"] - level_price) / level_price <= 0.015)
            ]

            if not trade_here.empty:
                r = trade_here.iloc[0]
                ch = "▲" if r["result"] == "WIN" else "▼"
            elif in_range:
                ch = "│"
            else:
                ch = " "
            line.append(ch)

        # Marcar si hay un nivel clave en esta fila
        is_level = any(abs(level_price - lp) / lp <= 0.008 for lp in levels_in_range)
        level_marker = f"◀ ${level_price:.2f}" if is_level else ""
        print(f"  {' '.join(line)}  {level_marker}")

    print(f"  ${p_min:>8.2f} ┘")
    print(f"  {'▲=WIN entrada  ▼=LOSS entrada  │=precio en zona':^60}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "═" * 60)
    print("  🔄 BACKTEST — Relative Strength Scanner")
    print(f"  {len(TICKERS)} tickers  |  {BT_DAYS} días de historia")
    print("═" * 60)

    total_days = BT_DAYS + WARMUP + 20
    print(f"\n  Descargando SPY ({total_days} días)...", end=" ", flush=True)
    spx_df = fetch(SPX_PROXY, total_days)
    print(f"✅ {len(spx_df)} barras")

    stock_cache = {}
    print(f"\n  Descargando {len(TICKERS)} tickers...\n")

    for ticker, sector in TICKERS.items():
        print(f"  {ticker:<6} ({sector})...", end=" ", flush=True)
        try:
            stock_df = fetch(ticker, total_days)
            stock_cache[ticker] = stock_df
            print(f"✅")
        except Exception as e:
            print(f"❌ {e}")

    # ── Pasada 1: SIN filtro de tendencia ─────────────────────────────────────
    print(f"\n{'═'*60}")
    print(f"  PASADA 1 — SIN filtro de tendencia (base)")
    print(f"{'═'*60}")
    trades_base = []
    for ticker, sector in TICKERS.items():
        if ticker not in stock_cache: continue
        t = backtest_ticker(ticker, stock_cache[ticker], spx_df, trend_filter=False)
        trades_base.extend(t)
    df_base = print_results(trades_base)

    # ── Pasada 2: CON filtro de tendencia ─────────────────────────────────────
    print(f"\n{'═'*60}")
    print(f"  PASADA 2 — CON filtro tendencia (no opera si >10% bajo máximo 60d)")
    print(f"{'═'*60}")
    trades_filt = []
    for ticker, sector in TICKERS.items():
        if ticker not in stock_cache: continue
        t = backtest_ticker(ticker, stock_cache[ticker], spx_df, trend_filter=True)
        trades_filt.extend(t)
    df_filt = print_results(trades_filt)

    # ── Comparación final ─────────────────────────────────────────────────────
    import pandas as pd
    b_wr  = len([x for x in trades_base if x["result"]=="WIN"]) / len(trades_base) * 100 if trades_base else 0
    f_wr  = len([x for x in trades_filt if x["result"]=="WIN"]) / len(trades_filt) * 100 if trades_filt else 0
    b_exp = sum(x["pnl_pct"] for x in trades_base) / len(trades_base) if trades_base else 0
    f_exp = sum(x["pnl_pct"] for x in trades_filt) / len(trades_filt) if trades_filt else 0

    print(f"\n{'═'*60}")
    print(f"  📊 COMPARACIÓN DIRECTA")
    print(f"{'═'*60}")
    print(f"  {'Métrica':<25} {'Sin filtro':>12} {'Con filtro':>12}  {'Δ':>8}")
    print(f"  {'─'*57}")
    print(f"  {'Trades totales':<25} {len(trades_base):>12} {len(trades_filt):>12}  {len(trades_filt)-len(trades_base):>+8}")
    print(f"  {'Win Rate':<25} {b_wr:>11.1f}% {f_wr:>11.1f}%  {f_wr-b_wr:>+7.1f}%")
    print(f"  {'Expectancy /trade':<25} {b_exp:>11.2f}% {f_exp:>11.2f}%  {f_exp-b_exp:>+7.2f}%")
    print(f"  {'PnL total':<25} {sum(x['pnl_pct'] for x in trades_base):>11.1f}% {sum(x['pnl_pct'] for x in trades_filt):>11.1f}%  {sum(x['pnl_pct'] for x in trades_filt)-sum(x['pnl_pct'] for x in trades_base):>+7.1f}%")

    winner = "CON FILTRO ✅" if f_wr > b_wr else "SIN FILTRO"
    print(f"\n  → Gana: {winner}")

    # Tickers positivos con filtro → actualizar watchlist
    if df_filt is not None and not df_filt.empty:
        by_pnl = df_filt.groupby("ticker").agg(
            trades=("pnl_pct","count"),
            pnl=("pnl_pct","sum"),
            wins=("result", lambda x: (x=="WIN").sum())
        )
        by_pnl["wr"] = by_pnl["wins"] / by_pnl["trades"] * 100
        positives = by_pnl[by_pnl["pnl"] > 0].sort_values("wr", ascending=False)
        print(f"\n  🏆 WATCHLIST RECOMENDADA (tickers positivos con filtro, por WR):")
        print(f"  {'Ticker':<8} {'Trades':>6} {'WR%':>6} {'PnL%':>8}")
        print(f"  {'─'*32}")
        for tk, row in positives.iterrows():
            print(f"  {tk:<8} {int(row['trades']):>6} {row['wr']:>5.0f}% {row['pnl']:>+7.1f}%")

    print(f"\n{'═'*60}\n")


if __name__ == "__main__":
    main()
