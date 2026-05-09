"""
Volume-only override using yfinance.

Why: Alpaca's free IEX feed reports only ~3% of the consolidated tape, so its
volume column is unreliable for breakout/surge detection. yfinance pulls daily
bars from Yahoo, which uses full SIP-consolidated volume. We use Alpaca for
everything else (price, news, trading) and patch in yfinance volume per symbol.

If yfinance fails (rate limit, network, ticker not found), we fall back to
Alpaca's volume silently — the rest of the pipeline keeps running.
"""

from datetime import datetime, timedelta
import pandas as pd

try:
    import yfinance as yf
    YF_AVAILABLE = True
except ImportError:
    YF_AVAILABLE = False


def fetch_volumes(symbols: list[str], lookback_days: int) -> dict[str, pd.Series]:
    """
    Fetch daily SIP-consolidated volume from yfinance for each symbol.
    Returns dict of symbol -> Series indexed by date (date only, no time).
    Symbols that fail are silently skipped — caller falls back to existing volume.
    """
    if not YF_AVAILABLE or not symbols:
        return {}

    end   = datetime.utcnow()
    start = end - timedelta(days=lookback_days)
    out   = {}

    try:
        # Batch download is much faster than per-symbol
        df = yf.download(
            tickers=symbols,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
    except Exception as e:
        print(f"  [yfinance] batch fetch failed: {e}")
        return {}

    if df is None or df.empty:
        return {}

    # yfinance returns different shapes depending on # of tickers
    if len(symbols) == 1:
        sym = symbols[0]
        if "Volume" in df.columns:
            vol = df["Volume"].dropna()
            vol.index = pd.to_datetime(vol.index).normalize()
            out[sym] = vol
        return out

    for sym in symbols:
        try:
            sub = df[sym] if sym in df.columns.get_level_values(0) else None
            if sub is None or "Volume" not in sub.columns:
                continue
            vol = sub["Volume"].dropna()
            if vol.empty:
                continue
            vol.index = pd.to_datetime(vol.index).normalize()
            out[sym] = vol
        except Exception:
            continue
    return out


def patch_volume(bars: dict[str, pd.DataFrame], lookback_days: int) -> int:
    """
    For each symbol's DataFrame, overwrite its 'volume' column using yfinance
    where dates align. Mutates bars in place. Returns count of patched symbols.
    """
    volumes = fetch_volumes(list(bars.keys()), lookback_days)
    patched = 0
    for sym, df in bars.items():
        # Default: mark as NOT patched. Strategy B will apply the IEX penalty.
        df.attrs["volume_patched"] = False
        yf_vol = volumes.get(sym)
        if yf_vol is None or yf_vol.empty:
            continue
        # Align by date only (Alpaca bars are timestamped, yfinance is date-only)
        df_dates = pd.to_datetime(df.index).normalize()
        replacement = yf_vol.reindex(df_dates)
        # Only overwrite where yfinance has a real value; preserve Alpaca volume otherwise
        mask = replacement.notna().values
        if mask.any():
            new_volume = df["volume"].astype(float).values
            new_volume[mask] = replacement.values[mask]
            df["volume"] = new_volume
            df.attrs["volume_patched"] = True
            patched += 1
    return patched
