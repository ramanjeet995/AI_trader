"""
News sentiment scoring.

Two backends:
  1. FinBERT (default if cfg.USE_FINBERT and the model loads) — finance-trained
     BERT, scores each headline as positive/neutral/negative with confidence.
     Catches nuances like "tepid guidance disappoints despite revenue beat"
     that pure keyword matching misses.
  2. Keyword regex with negation handling — fallback when FinBERT unavailable
     or disabled (faster, no model download).

Final score = sum across headlines, clamped to -3 / +3.
"""

import os
import re
from datetime import datetime, timedelta

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

import config as cfg
from analyst_ratings import analyst_score as _analyst_score


# ── FinBERT backend (lazy-loaded) ─────────────────────────────────────────────

_FINBERT_PIPELINE = None
_FINBERT_TRIED    = False

def _get_finbert():
    """Lazy-load FinBERT. Returns None if unavailable (caller falls back)."""
    global _FINBERT_PIPELINE, _FINBERT_TRIED
    if _FINBERT_TRIED:
        return _FINBERT_PIPELINE
    _FINBERT_TRIED = True

    if not getattr(cfg, "USE_FINBERT", False):
        return None
    try:
        from transformers import pipeline
        _FINBERT_PIPELINE = pipeline(
            "sentiment-analysis",
            model="ProsusAI/finbert",
            tokenizer="ProsusAI/finbert",
            device=-1,   # CPU
        )
        print("  [finbert] model loaded")
    except Exception as e:
        print(f"  [finbert] unavailable, using keyword fallback: {e}")
        _FINBERT_PIPELINE = None
    return _FINBERT_PIPELINE


def _score_with_finbert(headline: str, pipe) -> int:
    """
    FinBERT returns label in {positive, neutral, negative} with score (confidence).
    Map to +1 / 0 / -1, but require confidence > 0.6 to count (avoid noise).
    """
    try:
        result = pipe(headline[:512])  # FinBERT max ~512 tokens
        if not result:
            return 0
        item       = result[0]
        label      = item.get("label", "neutral").lower()
        confidence = float(item.get("score", 0.0))
        if confidence < 0.6:
            return 0
        if label == "positive":
            return 1
        if label == "negative":
            return -1
        return 0
    except Exception:
        return 0


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

    Score has two components:
      1. Base sentiment (FinBERT or keyword): clamped -3 to +3
      2. Analyst rating bonus (upgrades/downgrades weighted by firm tier): -5 to +5

    Combined score clamped to -5 / +5.
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

    base_total = 0
    details    = []
    raw_headlines = []
    pipe       = _get_finbert()   # None if disabled or load failed

    for article in articles:
        headline = article.get("headline", "") if isinstance(article, dict) else (article.headline or "")
        if not headline:
            continue
        raw_headlines.append(headline)
        score      = _score_with_finbert(headline, pipe) if pipe else _score_text(headline)
        base_total += score
        icon        = "+" if score > 0 else ("-" if score < 0 else "~")
        details.append(f"[{icon}] {headline[:80]}")

    base_clamped = max(-3, min(3, base_total))

    # Analyst rating bonus — upgrades/downgrades from reputable firms
    analyst_bonus, analyst_details = _analyst_score(raw_headlines)
    if analyst_details:
        for ad in analyst_details:
            details.append(f"[ANALYST] {ad}")

    combined = max(-5, min(5, base_clamped + analyst_bonus))
    return combined, details


def sentiment_label(score: int) -> str:
    if score >= 4:
        return "STRONG_POSITIVE"
    if score >= 2:
        return "POSITIVE"
    if score <= -4:
        return "STRONG_NEGATIVE"
    if score <= -2:
        return "NEGATIVE"
    return "NEUTRAL"
