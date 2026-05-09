"""
VIX-based volatility gate. The current SPY ATR% gate is a backward-looking
proxy; VIX is the forward-looking fear gauge from the options market.

Rules (configurable in config.py):
  - VIX > MAX_VIX             : block all new entries
  - VIX > VIX_HALVE_THRESHOLD : halve position size

Free from yfinance ticker ^VIX. Returns None on failure → caller treats as
"no extra constraint" so the pipeline never breaks on a yfinance hiccup.
"""

from datetime import datetime, timedelta

try:
    import yfinance as yf
    YF_AVAILABLE = True
except ImportError:
    YF_AVAILABLE = False


def get_vix() -> float | None:
    if not YF_AVAILABLE:
        return None
    try:
        end   = datetime.utcnow()
        start = end - timedelta(days=7)
        df    = yf.download("^VIX", start=start.strftime("%Y-%m-%d"),
                            end=end.strftime("%Y-%m-%d"), progress=False,
                            auto_adjust=False)
        if df is None or df.empty:
            return None
        # yfinance returns multi-index columns even for a single ticker — flatten
        close = df["Close"]
        if hasattr(close, "iloc"):
            last = close.iloc[-1]
            # If multi-column (multi-index), take the first column scalar
            if hasattr(last, "iloc"):
                last = last.iloc[0]
            return float(last)
        return None
    except Exception:
        return None


def assess(vix: float | None, cfg) -> dict:
    """
    Returns:
      {
        "vix"          : float or None,
        "block"        : bool   (block all new entries),
        "size_factor"  : float  (multiplier on position sizes; 1.0 = normal),
        "reason"       : str
      }
    """
    if vix is None:
        return {"vix": None, "block": False, "size_factor": 1.0,
                "reason": "VIX unavailable — no constraint"}

    max_vix     = getattr(cfg, "MAX_VIX", 30.0)
    halve_above = getattr(cfg, "VIX_HALVE_THRESHOLD", 20.0)

    if vix > max_vix:
        return {"vix": vix, "block": True, "size_factor": 0.0,
                "reason": f"VIX {vix:.1f} > {max_vix} — no new entries"}
    if vix > halve_above:
        return {"vix": vix, "block": False, "size_factor": 0.5,
                "reason": f"VIX {vix:.1f} > {halve_above} — half size"}
    return {"vix": vix, "block": False, "size_factor": 1.0,
            "reason": f"VIX {vix:.1f} — normal"}
