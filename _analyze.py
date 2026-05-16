"""Temporary analysis script — run after backtest to understand weaknesses."""
import json

trades = json.load(open("backtest_results.json"))["trades"]
losses = [t for t in trades if t["pnl"] <= 0]
wins   = [t for t in trades if t["pnl"] > 0]

print(f"=== LOSS ANALYSIS (222 trades) ===\n")

# R-multiple distribution
print("R-multiple distribution:")
buckets = {"full loss (-1R)":0, "partial loss":0, "tiny win (0-1R)":0,
           "good win (1-2R)":0, "great (2-5R)":0, "home run (5R+)":0}
for t in trades:
    r = t["r_multiple"]
    if   r <= -0.9:  buckets["full loss (-1R)"] += 1
    elif r < 0:      buckets["partial loss"] += 1
    elif r < 1:      buckets["tiny win (0-1R)"] += 1
    elif r < 2:      buckets["good win (1-2R)"] += 1
    elif r < 5:      buckets["great (2-5R)"] += 1
    else:            buckets["home run (5R+)"] += 1
for k, v in buckets.items():
    print(f"  {k:<28} {v:>4}  ({100*v/len(trades):.0f}%)")

# Hold time for losses
print("\nLosing trades by hold time:")
for label, lo, hi in [("0-1 days (whipsaw)",0,1), ("2-5 days",2,5),
                       ("6-15 days",6,15), ("16+ days",16,999)]:
    sub = [t for t in losses if lo <= t["hold_days"] <= hi]
    pnl = sum(t["pnl"] for t in sub)
    print(f"  {label:<22} {len(sub):>3} trades  total P&L: ${pnl:+,.0f}")

# Winning trades by hold time
print("\nWinning trades by hold time:")
for label, lo, hi in [("0-1 days",0,1), ("2-5 days",2,5), ("6-15 days",6,15),
                       ("16-30 days",16,30), ("30+ days",31,999)]:
    sub = [t for t in wins if lo <= t["hold_days"] <= hi]
    pnl = sum(t["pnl"] for t in sub)
    avg_r = sum(t["r_multiple"] for t in sub) / max(len(sub), 1)
    print(f"  {label:<22} {len(sub):>3} trades  total P&L: ${pnl:+,.0f}  avg R: {avg_r:.1f}")

# Strategy breakdown
print("\nStrategy breakdown:")
for s in ["A", "B", "C"]:
    st = [t for t in trades if t["strategy"] == s]
    if not st: continue
    sw  = [t for t in st if t["pnl"] > 0]
    pnl = sum(t["pnl"] for t in st)
    wr  = 100 * len(sw) / len(st)
    avg_r = sum(t["r_multiple"] for t in st) / len(st)
    print(f"  Strategy {s}: {len(st):>3} trades, {wr:.0f}% WR, "
          f"avg R={avg_r:+.2f}, total ${pnl:+,.0f}")

# Loss streaks
print("\nLoss streaks:")
streaks = []
cur = 0
for t in trades:
    if t["pnl"] <= 0:
        cur += 1
    else:
        if cur > 0: streaks.append(cur)
        cur = 0
if cur > 0: streaks.append(cur)
print(f"  Max: {max(streaks)}, avg: {sum(streaks)/len(streaks):.1f}, "
      f"count of 5+: {sum(1 for s in streaks if s >= 5)}")

# Regime bear exits
bear   = [t for t in trades if t["exit_reason"] == "regime_bear"]
bear_w = [t for t in bear if t["pnl"] > 0]
bear_l = [t for t in bear if t["pnl"] <= 0]
print(f"\nRegime-bear exits: {len(bear)} "
      f"({len(bear_w)} profitable, {len(bear_l)} losing)")
print(f"  Bear exit total P&L: ${sum(t['pnl'] for t in bear):+,.0f}")

# Per-year
print("\nPer-year stats:")
by_year = {}
for t in trades:
    y = t["entry_date"][:4]
    by_year.setdefault(y, []).append(t)
for y in sorted(by_year):
    yt   = by_year[y]
    pnl  = sum(t["pnl"] for t in yt)
    wr   = 100 * sum(1 for t in yt if t["pnl"] > 0) / len(yt)
    whips = sum(1 for t in yt if t["hold_days"] <= 1 and t["pnl"] < 0)
    mx = 0; cur = 0
    for t in yt:
        if t["pnl"] <= 0: cur += 1; mx = max(mx, cur)
        else: cur = 0
    print(f"  {y}: {len(yt):>3} trades, WR={wr:.0f}%, "
          f"P&L=${pnl:+,.0f}, whipsaws={whips}, max_streak={mx}")

# Biggest single-trade losses
print("\nTop 10 worst trades:")
worst = sorted(trades, key=lambda t: t["pnl"])[:10]
for t in worst:
    print(f"  {t['entry_date']} {t['symbol']:<6} Strat-{t['strategy']} "
          f"held {t['hold_days']:>2}d  R={t['r_multiple']:+.2f}  "
          f"${t['pnl']:+,.0f}  exit={t['exit_reason']}")
