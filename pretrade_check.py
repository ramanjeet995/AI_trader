"""
Pre-trade checks done at the moment of order placement:
  1. Bid-ask spread filter — skip illiquid quotes that bleed P&L on entry/exit
  2. Intraday confirmation — make sure today's price is still above the
     breakout/pullback level we signaled on yesterday's daily bar

Both use Alpaca's free quote/trade endpoints.
"""

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest


def check_spread(symbol: str, data_client: StockHistoricalDataClient,
                 max_spread_pct: float) -> tuple[bool, str, dict]:
    """
    Returns (ok, reason, info). ok=True means spread is acceptable.
    Unknown/missing quote = ok (don't block on data hiccup).
    """
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
    Confirms today's current price hasn't gapped meaningfully below the level
    the signal was generated at. Defends against gap-down opens that invalidate
    yesterday's setup.

    ok = today's price >= signal_entry * (1 - tolerance_pct/100)
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

        floor = signal_entry * (1 - tolerance_pct / 100.0)
        gap_pct = (current - signal_entry) / signal_entry * 100
        info    = {"current": current, "signal_entry": signal_entry,
                   "gap_pct": round(gap_pct, 2)}

        if current < floor:
            return False, (f"price ${current:.2f} dropped >{tolerance_pct}% "
                           f"from signal ${signal_entry:.2f} ({gap_pct:+.1f}%)"), info
        return True, f"price ${current:.2f} ({gap_pct:+.1f}% from signal)", info
    except Exception as e:
        return True, f"intraday check failed: {e}", {}
