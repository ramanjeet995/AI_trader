"""
News sentiment scoring using Alpaca's built-in News API.
No AI/LLM calls — pure keyword matching.

Score per headline: +1 (bullish), -1 (bearish), 0 (neutral)
Final score = sum of all headlines, clamped to -3 / +3.
"""

from datetime import datetime, timedelta
import os

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest


BULLISH_WORDS = {
    "beat", "beats", "record", "upgrade", "upgraded", "raised", "raise",
    "growth", "outperform", "buy", "strong", "surge", "rally", "profit",
    "revenue", "earnings beat", "above expectations", "positive", "bullish",
    "partnership", "deal", "contract", "approved", "launch", "expands",
    "dividend", "buyback", "guidance raised", "upbeat",
}

BEARISH_WORDS = {
    "miss", "misses", "downgrade", "downgraded", "cut", "cuts", "loss",
    "losses", "recall", "fraud", "lawsuit", "investigation", "layoff",
    "layoffs", "below expectations", "negative", "bearish", "decline",
    "warning", "guidance cut", "disappoints", "weak", "concern", "risk",
    "debt", "bankruptcy", "fine", "penalty", "probe", "subpoena",
}


def _score_text(text: str) -> int:
    text_lower = text.lower()
    bull = sum(1 for w in BULLISH_WORDS if w in text_lower)
    bear = sum(1 for w in BEARISH_WORDS if w in text_lower)
    if bull > bear:
        return 1
    if bear > bull:
        return -1
    return 0


def get_sentiment(symbol: str, news_client: NewsClient, days: int = 3) -> tuple[int, list[str]]:
    """
    Fetch last `days` days of news for symbol, score each headline.
    Returns (clamped_score, list_of_headline_summaries).
    """
    start = datetime.utcnow() - timedelta(days=days)
    end   = datetime.utcnow()

    try:
        req      = NewsRequest(symbols=symbol, start=start, end=end, limit=20)
        response = news_client.get_news(req)
        # NewsSet iterates as (key, value) tuples
        # key='data' holds {'news': [list of dicts]}, key='next_page_token' is None
        articles = []
        for key, data in response:
            if key == "data" and isinstance(data, dict):
                articles.extend(data.get("news", []))
    except Exception as e:
        return 0, [f"News fetch failed: {e}"]

    if not articles:
        return 0, ["No recent news"]

    total   = 0
    details = []

    for article in articles:
        headline = article.get("headline", "") if isinstance(article, dict) else (article.headline or "")
        score    = _score_text(headline)
        total   += score
        icon     = "+" if score > 0 else ("-" if score < 0 else "~")
        details.append(f"[{icon}] {headline[:80]}")

    # Clamp to -3 / +3
    clamped = max(-3, min(3, total))
    return clamped, details


def sentiment_label(score: int) -> str:
    if score >= 2:
        return "POSITIVE"
    if score <= -2:
        return "NEGATIVE"
    return "NEUTRAL"
