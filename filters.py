"""
Per-stock filters — both pre-scan (liquidity, ATR, RS) and pre-trade
(spread, intraday gap) checks.

Pre-scan: applied to all watchlist symbols early to discard illiquid/
flat stocks. Uses indicator columns already computed.

Pre-trade: applied at the moment of order placement. Live market data
(latest quote/trade) used to confirm conditions haven't changed since
the signal was generated.
"""

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest


# ── Pre-scan: liquidity + movement filters ───────────────────────────────────

def passes_filters(df: pd.DataFrame, cfg) -> tuple[bool, str]:
    """
    Liquidity + ATR% gate. Adapts volume threshold to data-feed reality
    (IEX shows ~3% of consolidated tape — scale threshold if not yfinance-patched).
    """
    last        = df.iloc[-1]
    avg_vol     = last["vol_ma20"]
    is_patched  = bool(df.attrs.get("volume_patched", False))
    feed_is_iex = getattr(cfg, "DATA_FEED", "iex").lower() == "iex"
    effective_min = (cfg.MIN_AVG_VOLUME / 33.0) if (feed_is_iex and not is_patched) \
                                                else cfg.MIN_AVG_VOLUME

    if pd.isna(avg_vol) or avg_vol < effective_min:
        return False, f"volume {avg_vol:,.0f} < {effective_min:,.0f}"

    atr_pct = last["atr_pct"]
    if pd.isna(atr_pct) or atr_pct < cfg.MIN_ATR_PCT:
        return False, f"ATR% {atr_pct:.2f} < {cfg.MIN_ATR_PCT}"

    return True, "OK"


def relative_strength(df: pd.DataFrame, benchmark_df: pd.DataFrame, period: int = 20) -> float:
    """
    RS = (1 + stock_return) / (1 + benchmark_return).
    > 1.0 → outperforming, < 1.0 → underperforming.
    """
    if len(df) < period + 1 or len(benchmark_df) < period + 1:
        return 1.0
    stock_ret = df["close"].iloc[-1] / df["close"].iloc[-period] - 1
    bench_ret = benchmark_df["close"].iloc[-1] / benchmark_df["close"].iloc[-period] - 1
    return (1 + stock_ret) / (1 + bench_ret)


# ── Pre-trade: bid-ask spread + intraday confirmation ────────────────────────

def check_spread(symbol: str, data_client: StockHistoricalDataClient,
                 max_spread_pct: float) -> tuple[bool, str, dict]:
    """Skip illiquid quotes that bleed P&L. Returns (ok, reason, info)."""
    try:
        req   = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        resp  = data_client.get_stock_latest_quote(req)
        quote = resp.get(symbol) if isinstance(resp, dict) else resp
        if quote is None:
            return True, "no quote available", {}
        bid = float(getattr(quote, "bid_price", 0) or 0)
        ask = float(getattr(quote, "ask_price", 0) or 0)
        if bid <= 0 or ask <= 0 or ask <= bid:
            return True, "stale/invalid quote", {"bid": bid, "ask": ask}
        mid        = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100
        info       = {"bid": bid, "ask": ask, "spread_pct": round(spread_pct, 3)}
        if spread_pct > max_spread_pct:
            return False, f"spread {spread_pct:.2f}% > {max_spread_pct}%", info
        return True, f"spread {spread_pct:.2f}%", info
    except Exception as e:
        return True, f"spread check failed: {e}", {}


def check_intraday_confirmation(symbol: str, data_client: StockHistoricalDataClient,
                                yesterday_close: float, signal_entry: float,
                                tolerance_pct: float = 1.0) -> tuple[bool, str, dict]:
    """
    Bidirectional gap check: rejects if today's price has drifted MORE than
    tolerance_pct from the signal entry in either direction. Protects against
    stale crons and overnight gaps invalidating the trade levels.
    """
    try:
        req   = StockLatestTradeRequest(symbol_or_symbols=symbol)
        resp  = data_client.get_stock_latest_trade(req)
        trade = resp.get(symbol) if isinstance(resp, dict) else resp
        if trade is None:
            return True, "no trade available", {}
        current = float(getattr(trade, "price", 0) or 0)
        if current <= 0:
            return True, "stale price", {}
        gap_pct = (current - signal_entry) / signal_entry * 100
        info    = {"current": current, "signal_entry": signal_entry,
                   "gap_pct": round(gap_pct, 2)}
        if abs(gap_pct) > tolerance_pct:
            direction = "above" if gap_pct > 0 else "below"
            return False, (f"price ${current:.2f} is {abs(gap_pct):.1f}% {direction} "
                           f"signal ${signal_entry:.2f} (max {tolerance_pct}%) — "
                           f"trade levels no longer valid"), info
        return True, f"price ${current:.2f} ({gap_pct:+.1f}% from signal)", info
    except Exception as e:
        return True, f"intraday check failed: {e}", {}
