#!/bin/bash
# Raspberry Pi setup for AI Trader
# Run: bash setup_pi.sh
# Assumes: Pi is logged in, has internet, fresh Raspberry Pi OS.

set -e

REPO_URL="https://github.com/ramanjeet995/AI_trader.git"
INSTALL_DIR="$HOME/AI_trader"

echo "==> AI Trader — Raspberry Pi setup"
echo

# 1. System packages
echo "==> Installing system packages..."
sudo apt update
sudo apt install -y git python3 python3-pip python3-venv

# 2. Set timezone to Eastern (handles EDT/EST automatically)
echo "==> Setting timezone to America/New_York (ET)..."
sudo timedatectl set-timezone America/New_York

# 3. Clone repo (or pull if already exists)
if [ -d "$INSTALL_DIR" ]; then
    echo "==> Repo exists, pulling latest..."
    cd "$INSTALL_DIR" && git pull
else
    echo "==> Cloning repo..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# 4. Python virtual env (avoids ARM pip headaches)
echo "==> Setting up Python venv..."
cd "$INSTALL_DIR"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip

# 5. Install deps. Torch is HEAVY on ARM — make it optional.
echo "==> Installing Python dependencies..."
pip install alpaca-py python-dotenv pandas yfinance
# Torch is optional — only needed for FinBERT sentiment. Falls back gracefully.
echo "==> Attempting torch + transformers install (optional, slow on Pi)..."
pip install transformers torch --extra-index-url https://download.pytorch.org/whl/cpu \
    || echo "    [warn] torch install failed — system will use keyword sentiment (works fine)"

# 6. Create .env template if not present
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "==> Creating .env template..."
    cat > "$INSTALL_DIR/.env" << 'EOF'
# Edit these with your Alpaca paper trading API keys:
ALPACA_API_KEY=PKUBTHDHWPQZ75UC3B3A26S2OP
ALPACA_API_SECRET=CgM1jPBHQPkQqqAHn9hRaA2i2Yit4Rk2km2y8VWnAFp5
ALPACA_PAPER=true
EOF
    echo "    [!] EDIT $INSTALL_DIR/.env with your real Alpaca keys before running"
fi

# 7. Create log directory
mkdir -p "$INSTALL_DIR/logs"

# 8. Install cron jobs
echo "==> Installing cron jobs (scheduled scans)..."
CRON_FILE=$(mktemp)
crontab -l 2>/dev/null > "$CRON_FILE" || true

# Remove any existing AI Trader entries
sed -i '/# AI_TRADER_/d' "$CRON_FILE"

# Add new entries. PATH ensures Python from venv is used.
cat >> "$CRON_FILE" << EOF

# AI_TRADER_BEGIN
# All times in America/New_York (ET). Auto-handles EDT/EST.
33  9 * * 1-5  cd $INSTALL_DIR && SKIP_TIME_GUARD=true MODE=FULL     ./venv/bin/python main.py >> logs/scan.log 2>&1; ./push_log.sh
53 10 * * 1-5  cd $INSTALL_DIR && SKIP_TIME_GUARD=true MODE=CATALYST ./venv/bin/python main.py >> logs/scan.log 2>&1; ./push_log.sh
53 10 * * 1-5  cd $INSTALL_DIR && SKIP_TIME_GUARD=true MODE=NEWS     ./venv/bin/python main.py >> logs/scan.log 2>&1; ./push_log.sh
53 12 * * 1-5  cd $INSTALL_DIR && SKIP_TIME_GUARD=true MODE=NEWS     ./venv/bin/python main.py >> logs/scan.log 2>&1; ./push_log.sh
53 14 * * 1-5  cd $INSTALL_DIR && SKIP_TIME_GUARD=true MODE=NEWS     ./venv/bin/python main.py >> logs/scan.log 2>&1; ./push_log.sh
23 16 * * 1-5  cd $INSTALL_DIR && SKIP_TIME_GUARD=true MODE=FULL     ./venv/bin/python main.py >> logs/scan.log 2>&1; ./push_log.sh
# Daily: pull latest code each morning at 6 AM ET
0   6 * * *    cd $INSTALL_DIR && git pull >> logs/git.log 2>&1
# AI_TRADER_END
EOF

crontab "$CRON_FILE"
rm "$CRON_FILE"

# 9. Push helper script
echo "==> Creating push_log.sh..."
cat > "$INSTALL_DIR/push_log.sh" << 'EOF'
#!/bin/bash
# Pushes scan log updates to GitHub after each run.
cd "$(dirname "$0")"
git add scan_log.json scan_log.md earnings_cache.json position_state.json discovered_watchlist.json 2>/dev/null || true
git diff --staged --quiet 2>/dev/null && exit 0
git -c user.name="AI Trader Pi" -c user.email="pi@local" commit -m "scan log update [skip ci]" >/dev/null 2>&1
git push >/dev/null 2>&1
EOF
chmod +x "$INSTALL_DIR/push_log.sh"

echo
echo "==> Setup complete!"
echo
echo "Next steps:"
echo "  1. Edit $INSTALL_DIR/.env with your Alpaca API keys"
echo "  2. Set up git push auth — either:"
echo "       a) git config --global credential.helper store, then git pull once"
echo "       b) Set up SSH key for GitHub"
echo "  3. Test manually: cd $INSTALL_DIR && SKIP_TIME_GUARD=true ./venv/bin/python main.py"
echo "  4. View installed cron: crontab -l"
echo "  5. Watch logs:  tail -f $INSTALL_DIR/logs/scan.log"
echo
echo "Scheduled scans will fire weekdays at:"
echo "  9:33 AM ET  - Main scan"
echo "  10:53 AM ET - Catalyst + News check"
echo "  12:53 PM ET - News check"
echo "  2:53 PM ET  - News check"
echo "  4:23 PM ET  - Main scan (pre-close)"
echo "  6:00 AM ET  - Daily git pull for code updates"
