"""
Options executor — selects contracts, sizes positions, places option orders.

Hybrid mode: Strategy B (breakouts) and Catalyst trades use call options for
leverage on a $5k account. Strategy A (pullbacks) stays as stocks because
those setups need time to develop and theta would eat the position.

Options advantages on small accounts:
  - Control 100 shares for $300-800 instead of $15,000+
  - Max loss = premium paid (no stop-loss order needed)
  - 3-10x leverage on directional moves

Options risks managed here:
  - 50% premium stop (sell if option loses half its value)
  - Exit before expiry week (gamma/theta acceleration)
  - Target-based exits (don't hold for home runs — take 80-100% gains)
  - Fallback to stocks if no liquid contract found
"""

import math
import logging
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    GetOptionContractsRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, AssetStatus

import config as cfg

log = logging.getLogger(__name__)


# ─── Contract selection ──────────────────────────────────────────────────────

def select_contract(symbol: str, entry: float, strategy: str,
                    trade_client: TradingClient) -> dict | None:
    """
    Find the best call option contract for a signal.

    For breakouts (Strategy B): ATM-to-slightly-OTM, 14-28 DTE, delta ~0.60
    For catalyst:               ATM, 5-10 DTE, delta ~0.70

    Returns dict with contract details or None if no suitable contract found.
    """
    is_catalyst = strategy.upper() == "CATALYST"

    if is_catalyst:
        min_dte = cfg.OPTIONS_CATALYST_EXPIRY_MIN
        max_dte = cfg.OPTIONS_CATALYST_EXPIRY_MAX
        target_delta = cfg.OPTIONS_CATALYST_DELTA
    else:
        min_dte = cfg.OPTIONS_EXPIRY_MIN_DAYS
        max_dte = cfg.OPTIONS_EXPIRY_MAX_DAYS
        target_delta = cfg.OPTIONS_TARGET_DELTA

    today = datetime.utcnow().date()
    exp_min = today + timedelta(days=min_dte)
    exp_max = today + timedelta(days=max_dte)

    # Strike range: OTM preferred for affordability on small accounts.
    # For stocks >$100, go further OTM to find affordable premiums.
    # Max risk per option: account_value * OPTIONS_RISK_PCT = ~$250 on $5k.
    # Premium needs to be ≤ $2.50/share for 1 contract within that budget.
    if entry > 200:
        # Expensive stocks: 3-10% OTM to find affordable premiums
        strike_min = round(entry * 1.03, 2)
        strike_max = round(entry * 1.10, 2)
    elif entry > 100:
        # Mid-price: 2-7% OTM
        strike_min = round(entry * 1.02, 2)
        strike_max = round(entry * 1.07, 2)
    else:
        # Cheap stocks: ATM to slightly OTM (can afford ATM calls)
        strike_min = round(entry * 0.98, 2)
        strike_max = round(entry * 1.05, 2)

    try:
        contracts_resp = trade_client.get_option_contracts(
            GetOptionContractsRequest(
                underlying_symbols=[symbol],
                type="call",
                status=AssetStatus.ACTIVE,
                expiration_date_gte=exp_min.isoformat(),
                expiration_date_lte=exp_max.isoformat(),
                strike_price_gte=str(strike_min),
                strike_price_lte=str(strike_max),
            )
        )
    except Exception as e:
        log.warning(f"[OPTIONS] Failed to fetch contracts for {symbol}: {e}")
        return None

    # Extract contract list from response
    if contracts_resp is None:
        return None
    contracts = []
    if hasattr(contracts_resp, 'option_contracts'):
        contracts = contracts_resp.option_contracts or []
    elif isinstance(contracts_resp, list):
        contracts = contracts_resp
    elif hasattr(contracts_resp, '__iter__'):
        contracts = list(contracts_resp)

    if not contracts:
        log.info(f"[OPTIONS] No contracts found for {symbol} "
                 f"(strikes {strike_min}-{strike_max}, DTE {min_dte}-{max_dte})")
        return None

    # Score each contract: prefer delta closest to target, good OI, tight spread
    best = None
    best_score = -999

    for c in contracts:
        try:
            strike     = float(c.strike_price) if hasattr(c, 'strike_price') else 0
            oi         = int(c.open_interest or 0) if hasattr(c, 'open_interest') else 0
            expiry_str = str(c.expiration_date) if hasattr(c, 'expiration_date') else ""
            occ_symbol = c.symbol if hasattr(c, 'symbol') else ""
            tradable   = getattr(c, 'tradable', True)

            # Skip non-tradable or illiquid contracts
            if not tradable:
                continue
            if oi < cfg.OPTIONS_MIN_OPEN_INTEREST:
                continue

            # Calculate DTE
            try:
                exp_date = datetime.strptime(expiry_str[:10], "%Y-%m-%d").date()
                dte = (exp_date - today).days
            except Exception:
                continue

            # Score: proximity to ATM (prefer slightly OTM), good OI, right DTE
            # ATM strike is closest to entry price
            moneyness = abs(strike - entry) / entry  # 0 = ATM
            dte_ideal = (min_dte + max_dte) / 2
            dte_score = -abs(dte - dte_ideal) / dte_ideal  # 0 = perfect

            score = (
                -moneyness * 10        # penalize far from ATM
                + min(oi / 1000, 2)    # reward OI up to 2 points
                + dte_score            # reward ideal DTE
            )

            if score > best_score:
                best_score = score
                best = {
                    "occ_symbol"  : occ_symbol,
                    "underlying"  : symbol,
                    "strike"      : strike,
                    "expiry"      : expiry_str[:10],
                    "dte"         : dte,
                    "open_interest": oi,
                    "type"        : "call",
                    "strategy"    : strategy,
                }
        except Exception:
            continue

    if best:
        log.info(f"[OPTIONS] Selected {best['occ_symbol']} for {symbol}: "
                 f"strike={best['strike']}, DTE={best['dte']}, OI={best['open_interest']}")
    return best


# ─── Options position sizing ────────────────────────────────────────────────

def options_position_size(account_value: float, premium_ask: float,
                          cfg_mod=None, risk_mult: float = 1.0) -> dict:
    """
    Size an options position. Max loss = premium paid.

    premium_ask : per-share ask price of the option (multiply by 100 for cost)
    risk_mult   : conviction multiplier (from conviction tiers)

    Returns {"contracts": int, "total_premium": float, "max_loss": float}
    """
    if cfg_mod is None:
        cfg_mod = cfg

    # Options use a higher risk % because max loss = premium paid
    # (no gap risk, no stop slippage — you can't lose more than the premium)
    options_risk_pct = getattr(cfg_mod, 'OPTIONS_RISK_PCT', 0.05)
    max_risk = account_value * options_risk_pct * risk_mult
    cost_per_contract = premium_ask * 100  # options = 100 shares per contract

    if cost_per_contract <= 0:
        return {"contracts": 0, "total_premium": 0, "max_loss": 0}

    raw_contracts = max_risk / cost_per_contract
    contracts = min(
        math.floor(raw_contracts),
        cfg_mod.OPTIONS_MAX_CONTRACTS  # hard cap
    )
    contracts = max(contracts, 0)

    return {
        "contracts"    : contracts,
        "total_premium": round(contracts * cost_per_contract, 2),
        "max_loss"     : round(contracts * cost_per_contract, 2),  # max loss = premium
    }


# ─── Get option quote ───────────────────────────────────────────────────────

def get_option_quote(occ_symbol: str, trade_client: TradingClient) -> dict | None:
    """
    Get current bid/ask/last for an option contract.
    Uses the latest trade or snapshot endpoint.
    """
    try:
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.requests import OptionLatestQuoteRequest
        import os

        opt_client = OptionHistoricalDataClient(
            os.environ.get("ALPACA_API_KEY", ""),
            os.environ.get("ALPACA_API_SECRET", ""),
        )
        req = OptionLatestQuoteRequest(symbol_or_symbols=occ_symbol)
        quote = opt_client.get_option_latest_quote(req)

        if isinstance(quote, dict):
            q = quote.get(occ_symbol)
        else:
            q = quote

        if q:
            bid = float(getattr(q, 'bid_price', 0) or 0)
            ask = float(getattr(q, 'ask_price', 0) or 0)
            mid = (bid + ask) / 2 if bid and ask else 0
            return {"bid": bid, "ask": ask, "mid": mid}
    except Exception as e:
        log.warning(f"[OPTIONS] Quote fetch failed for {occ_symbol}: {e}")
    return None


# ─── Execute option order ───────────────────────────────────────────────────

def execute_option(contract: dict, size: dict,
                   trade_client: TradingClient,
                   remaining_bp: float | None = None,
                   client_order_id: str | None = None) -> dict:
    """
    Buy-to-open a call option contract.

    contract : output from select_contract()
    size     : output from options_position_size()

    Returns {status, order_id?, occ_symbol, contracts, premium, ...}
    """
    occ_symbol = contract["occ_symbol"]
    contracts  = size["contracts"]
    premium    = size["total_premium"]

    if contracts <= 0:
        return {"status": "SKIPPED", "reason": "0 contracts after sizing",
                "occ_symbol": occ_symbol, "premium": 0}

    if remaining_bp is not None and premium > remaining_bp:
        return {"status": "SKIPPED",
                "reason": f"insufficient BP — need ${premium:,.2f}, have ${remaining_bp:,.2f}",
                "occ_symbol": occ_symbol, "premium": premium}

    try:
        order_kwargs = dict(
            symbol        = occ_symbol,
            qty           = contracts,
            side          = OrderSide.BUY,
            time_in_force = TimeInForce.DAY,
            # No order_class — simple buy, no attached stop (max loss = premium)
        )
        if client_order_id:
            order_kwargs["client_order_id"] = client_order_id

        # Use market order for liquid options, limit for less liquid
        order_req = MarketOrderRequest(**order_kwargs)
        order = trade_client.submit_order(order_req)

        return {
            "status"     : "PLACED",
            "order_id"   : str(order.id),
            "occ_symbol" : occ_symbol,
            "underlying"  : contract["underlying"],
            "strike"     : contract["strike"],
            "expiry"     : contract["expiry"],
            "contracts"  : contracts,
            "premium"    : premium,
            "type"       : "OPTION",
        }
    except Exception as e:
        return {"status": "ERROR", "reason": str(e),
                "occ_symbol": occ_symbol, "premium": premium}


# ─── Options position management ────────────────────────────────────────────

def should_exit_option(current_value: float, entry_premium: float,
                       strategy: str, days_held: int,
                       days_to_expiry: int) -> dict:
    """
    Check if an options position should be closed.

    current_value  : current market value of the position (per contract × 100)
    entry_premium  : what we paid (per contract × 100)
    strategy       : "B", "CATALYST", etc.
    days_held      : trading days since entry
    days_to_expiry : calendar days until expiration

    Returns {"exit": bool, "reason": str}
    """
    if entry_premium <= 0:
        return {"exit": False, "reason": "invalid entry premium"}

    pct_change = (current_value - entry_premium) / entry_premium

    # 1. Premium stop — cut losses at 50%
    if pct_change <= -cfg.OPTIONS_PREMIUM_STOP_PCT:
        return {"exit": True, "reason": f"premium stop ({pct_change:+.0%} loss)"}

    # 2. Expiry week risk — exit before gamma acceleration
    if days_to_expiry <= cfg.OPTIONS_EXPIRY_WARN_DAYS:
        return {"exit": True, "reason": f"expiry in {days_to_expiry} days — closing"}

    # 3. Target-based exits (strategy-specific)
    is_catalyst = strategy.upper() == "CATALYST"

    if is_catalyst:
        if pct_change >= cfg.OPTIONS_CATALYST_TARGET_PCT:
            return {"exit": True, "reason": f"catalyst target hit ({pct_change:+.0%} gain)"}
    else:
        if pct_change >= cfg.OPTIONS_BREAKOUT_TARGET_PCT:
            return {"exit": True, "reason": f"breakout target hit ({pct_change:+.0%} gain)"}

    # 4. Time stop — catalyst trades max 2 days, breakouts max 10 days
    max_days = 2 if is_catalyst else 10
    if days_held >= max_days:
        return {"exit": True, "reason": f"time stop ({days_held} days held)"}

    return {"exit": False, "reason": "holding"}


def manage_option_positions(trade_client: TradingClient,
                            option_state: dict) -> dict:
    """
    Review all open option positions and exit as needed.

    option_state : {occ_symbol: {"entry_premium": float, "entry_date": str,
                                  "expiry": str, "strategy": str}}

    Returns summary dict.
    """
    summary = {
        "reviewed": 0, "exited": 0, "held": 0, "errors": 0, "details": [],
    }

    try:
        positions = trade_client.get_all_positions()
    except Exception as e:
        summary["errors"] += 1
        summary["error_msg"] = str(e)
        return summary

    today = datetime.utcnow().date()

    for pos in positions:
        symbol = pos.symbol

        # Only manage option positions (OCC symbols are long, like NVDA250530C00210000)
        if symbol not in option_state:
            continue

        state = option_state[symbol]
        summary["reviewed"] += 1

        try:
            current_value = abs(float(pos.market_value or 0))
            entry_premium = state["entry_premium"]
            strategy      = state.get("strategy", "B")

            # Calculate days held
            try:
                entry_date = datetime.strptime(state["entry_date"][:10], "%Y-%m-%d").date()
                days_held = (today - entry_date).days
            except Exception:
                days_held = 0

            # Calculate days to expiry
            try:
                expiry_date = datetime.strptime(state["expiry"][:10], "%Y-%m-%d").date()
                days_to_expiry = (expiry_date - today).days
            except Exception:
                days_to_expiry = 30  # assume safe if unknown

            decision = should_exit_option(
                current_value, entry_premium, strategy,
                days_held, days_to_expiry
            )

            if decision["exit"]:
                try:
                    trade_client.close_position(symbol)
                    summary["exited"] += 1
                    summary["details"].append({
                        "symbol": symbol, "action": "closed",
                        "reason": decision["reason"],
                        "pnl_pct": f"{(current_value/entry_premium - 1)*100:+.1f}%",
                    })
                    # Remove from state
                    del option_state[symbol]
                except Exception as e:
                    summary["errors"] += 1
                    summary["details"].append({
                        "symbol": symbol, "action": "close_failed",
                        "error": str(e),
                    })
            else:
                summary["held"] += 1
                summary["details"].append({
                    "symbol": symbol, "action": "hold",
                    "reason": decision["reason"],
                    "pnl_pct": f"{(current_value/entry_premium - 1)*100:+.1f}%",
                    "dte": days_to_expiry,
                })

        except Exception as e:
            summary["errors"] += 1
            summary["details"].append({"symbol": symbol, "action": "error", "error": str(e)})

    return summary


# ─── Utility: check if a symbol is an options contract ──────────────────────

def is_option_symbol(symbol: str) -> bool:
    """OCC option symbols are 21 chars: SYMBOL + YYMMDD + C/P + 8-digit strike."""
    return len(symbol) >= 15 and any(c in symbol for c in ["C", "P"]) and symbol[-8:].isdigit()
