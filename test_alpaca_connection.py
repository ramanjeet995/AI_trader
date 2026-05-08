"""
Alpaca API connectivity test. Credentials are loaded from .env
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

load_dotenv(Path(__file__).parent / ".env")

API_KEY    = os.environ["ALPACA_API_KEY"]
API_SECRET = os.environ["ALPACA_API_SECRET"]
PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"


def test_trading_connection():
    print("--- Trading API ---")
    client  = TradingClient(API_KEY, API_SECRET, paper=PAPER)
    account = client.get_account()
    print(f"  Account status : {account.status}")
    print(f"  Buying power   : ${float(account.buying_power):,.2f}")
    print(f"  Portfolio value: ${float(account.portfolio_value):,.2f}")
    print(f"  Currency       : {account.currency}")


def test_market_data_connection():
    print("--- Market Data API ---")
    client = StockHistoricalDataClient(API_KEY, API_SECRET)
    req    = StockLatestQuoteRequest(symbol_or_symbols=["AAPL", "MSFT"])
    quotes = client.get_stock_latest_quote(req)
    for symbol, quote in quotes.items():
        print(f"  {symbol}: ask=${quote.ask_price}  bid=${quote.bid_price}")


if __name__ == "__main__":
    try:
        test_trading_connection()
        test_market_data_connection()
        print("\nAll connections OK.")
    except Exception as e:
        print(f"\nConnection FAILED: {e}")
        raise
