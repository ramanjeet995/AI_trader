"""
News sentiment scoring using Alpaca's built-in News API.
No AI/LLM calls — pure keyword matching with word boundaries
(so "beat" doesn't match "beaten down", "risk" doesn't match macro headlines).

Phrases (with spaces) are matched as substrings. Single words use \\b boundaries.

Score per headline: +1 (bullish), -1 (bearish), 0 (neutral)
Headlines with NEGATION ("not strong", "no upgrade") are inverted.
Final score = sum across headlines, clamped to -3 / +3.
"""

import re
from datetime import datetime, timedelta

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest


BULLISH_TERMS = {
    "beat", "beats", "record", "upgrade", "upgraded", "raised", "raises",
    "growth", "outperform", "outperforms", "surge", "surges", "rally", "rallies",
    "profit", "profits", "earnings beat", "above expectations", "positive",
    "bullish", "partnership", "approved", "launch", "launches", "expands",
    "dividend", "buyback", "guidance raised", "upbeat", "soar", "soars",
    "jumps", "tops", "exceeds", "strong results", "strong earnings",
}

BEARISH_TERMS = {
    "miss", "misses", "downgrade", "downgraded", "cut", "cuts", "loss",
    "losses", "recall", "fraud", "lawsuit", "investigation", "layoff",
    "layoffs", "below expectations", "negative", "bearish", "decline",
    "declines", "warning", "guidance cut", "disappoints", "disappointing",
    "bankruptcy", "fine", "penalty", "probe", "subpoena", "plunge", "plunges",
    "crashes", "tumbles", "slumps", "delisting", "halt",
}

# Negators that flip the next 3 words' meaning
NEGATORS = {"not", "no", "never", "without", "fails", "failed", "fail"}


def _build_pattern(term: str) -> re.Pattern:
    """Single word → \\b...\\b boundary. Phrase (has space) → escaped substring."""
    if " " in term:
        return re.compile(re.escape(term), re.IGNORECASE)
    return re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)


_BULL_PATTERNS = [(t, _build_pattern(t)) for t in BULLISH_TERMS]
_BEAR_PATTERNS = [(t, _build_pattern(t)) for t in BEARISH_TERMS]


def _is_negated(text: str, match_start: int) -> bool:
    """Check if any negator appears within ~4 words before the match."""
    prefix      = text[:match_start].lower()
    prev_words  = re.findall(r"\b\w+\b", prefix)[-4:]
    return any(w in NEGATORS for w in prev_words)


def _score_text(text: str) -> int:
    bull = 0
    bear = 0

    for term, pat in _BULL_PATTERNS:
        for m in pat.finditer(text):
            if _is_negated(text, m.start()):
                bear += 1
            else:
                bull += 1

    for term, pat in _BEAR_PATTERNS:
        for m in pat.finditer(text):
            if _is_negated(text, m.start()):
                bull += 1
            else:
                bear += 1

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
        if not headline:
            continue
        score    = _score_text(headline)
        total   += score
        icon     = "+" if score > 0 else ("-" if score < 0 else "~")
        details.append(f"[{icon}] {headline[:80]}")

    clamped = max(-3, min(3, total))
    return clamped, details


def sentiment_label(score: int) -> str:
    if score >= 2:
        return "POSITIVE"
    if score <= -2:
        return "NEGATIVE"
    return "NEUTRAL"
