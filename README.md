# Cart Watch: Autonomous Grocery Order Agent

An intelligent commerce automation system that monitors Zepto grocery prices in real-time and autonomously places orders when your cart total drops below a user-defined threshold. Never miss a deal again.

## The Problem

Grocery prices fluctuate constantly on ecommerce platforms. You might add items to your cart hoping to catch a price drop, but:
- Manual price checking is tedious and error-prone
- You can't monitor prices 24/7
- Price drops happen at unpredictable times
- By the time you check, the deal is already gone
- Managing watchlists across multiple carts is chaotic

## The Solution

Cart Watch is an **agentic automation system** that:
- **Monitors** your Zepto cart in real-time (every 2 hours, configurable)
- **Detects** price changes and calculates total cart value
- **Alerts** you via Telegram notifications
- **Autonomously places orders** when your cart total drops ≥ your threshold
- **Tracks** order history and price trends

Set it once, let it run. Never manually check prices again.

## How It Works

### Architecture

```
┌─────────────────────────────────────────────────────┐
│          Zepto Smart Buyer Agent                    │
└─────────────────────────────────────────────────────┘
                        ↓
         ┌──────────────────────────────┐
         │   Zepto API Integration      │
         │  (Price checking, Ordering)  │
         └──────────────────────────────┘
                ↓              ↓
        ┌───────────┐   ┌────────────┐
        │   Search  │   │  Cart Info │
        │   Items   │   │  & Orders  │
        └───────────┘   └────────────┘
                ↓
        ┌──────────────────────┐
        │  Price Monitoring    │
        │  & Analysis          │
        └──────────────────────┘
                ↓
        ┌──────────────────────────────┐
        │  Telegram Notifications      │
        │  & Autonomous Ordering       │
        └──────────────────────────────┘
```

### Core Components

1. **Zepto API Client** (`zepto_mcp.py`)
   - Authenticates with Zepto using cached session tokens
   - Searches products, fetches cart details, places orders
   - Handles rate limiting and session refresh

2. **Watchlist Manager** (`watchlist.py`)
   - Tracks items you want to monitor
   - Stores price history and trends
   - Manages multiple carts simultaneously

3. **Price Monitor** (`price_check.py`)
   - Periodic polling service (configurable interval)
   - Calculates total cart value
   - Detects price drops against threshold

4. **Order Agent** (`bot.py`)
   - Decision engine: should we order now?
   - Validates cart state before ordering
   - Executes autonomous order placement
   - Logs all actions for auditing

5. **Notification System** (`notify.py`)
   - Real-time Telegram alerts
   - Order confirmations
   - Price trend updates

### How Autonomous Ordering Works

```python
1. Monitor cart total every 2 hours
   ↓
2. Compare against DROP_THRESHOLD (e.g., 10% of original price)
   ↓
3. If drop detected:
   - Fetch latest cart data
   - Validate items still available
   - Verify price hasn't changed
   ↓
4. Auto-place order
   ↓
5. Send Telegram notification with order ID
   ↓
6. Log transaction to history
```

## Features

✨ **Core Capabilities**
- Real-time price monitoring across Zepto catalog
- Configurable price drop threshold (absolute or percentage)
- Autonomous order placement without user intervention
- Multi-cart support
- Price history tracking and trend analysis
- Telegram notifications for all actions
- Order history logging and auditing

## Quick Start

### Prerequisites
- Python 3.8+
- Zepto account (India only)
- Telegram bot token
- Anthropic API key (for Claude integration)
- Google Gemini API key (optional, for price intelligence)

### Installation

```bash
# Clone the repo
git clone https://github.com/pruturaj16/zepto-smart-buyer.git
cd zepto-smart-buyer

# Install dependencies
pip install -r requirements.txt

# Configure credentials
cp config.example.py config.py
# Edit config.py with your API keys and chat ID
```

See [SETUP.md](SETUP.md) for detailed setup instructions.

### Running CartWatch

```bash
# Start the monitoring agent
python bot.py

# Or run specific modules
python price_check.py      # One-time price check
python watchlist.py        # Manage your watchlist
python zepto_mcp.py        # Direct Zepto API testing
```

## Configuration

Edit `config.py`:

```python
TELEGRAM_TOKEN    = "your-bot-token"      # Get from BotFather
CHAT_ID           = "your-chat-id"        # Your Telegram user ID
ANTHROPIC_API_KEY = "your-claude-key"     # For AI analysis
GEMINI_API_KEY    = "your-gemini-key"     # For price intelligence

DROP_THRESHOLD       = 50  # Order when cart drops by ₹50
CHECK_INTERVAL_HOURS = 2   # Check every 2 hours
```

## Example Workflow

```
1. You add ₹500 worth of items to your Zepto cart
2. Cart Watch starts monitoring (every 2 hours)
3. Price drops to ₹475 (not below threshold yet)
4. Notification: "Cart down to ₹475, waiting for ₹450"
5. Price drops to ₹435 (below threshold!)
6. Notification: "🎯 Threshold hit! Placing order..."
7. Order placed automatically
8. Notification: "✅ Order placed! Order ID: #12345"
9. Agent waits for next monitoring cycle
```

## Tech Stack

- **Language**: Python 3.8+
- **API Integration**: Zepto REST API, Anthropic Claude, Google Gemini
- **Notifications**: Telegram Bot API
- **Storage**: Local JSON (watchlist, history)
- **Architecture**: Async/Await for concurrent operations

## Author

[Ruturaj](https://github.com/pruturaj16)

---

**Questions?** Open an issue or contact via Telegram (configure in `config.py`)

**Enjoying CartWatch?** ⭐ Star this repo to show your support!
