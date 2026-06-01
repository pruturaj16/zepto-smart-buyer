# ZEPTO SMART BUYER
## Product Requirements Document (PRD)
**v1.0 | May 2026**

---

## Executive Summary

Zepto Smart Buyer is an AI-powered price monitoring bot that helps Gen Z users save money on grocery shopping at Zepto (India's leading quick commerce platform). Users set price thresholds for items they frequently buy, and the bot automatically monitors prices, sends smart recommendations, and alerts them when to buy.

**Key Value Prop:** Save time, money, and make confident purchase decisions with AI-driven price insights delivered via Telegram.

---

## 1. Product Overview

### What It Does

- **Maintains a personal grocery watchlist** on Zepto
- **Automatically checks prices** every 2 hours
- **Compares against your price threshold** and historical lows
- **Sends actionable recommendations** via Telegram
- **Tracks price history** for insights

### Why It Matters

Grocery prices fluctuate frequently. Smart Buyer eliminates the guesswork by delivering personalized price alerts and historical context, helping users stretch their budgets further.

---

## 2. Target Users & Personas

### Primary Audience: Gen Z

**Ages 18–25 | Tech-savvy | Budget-conscious | Active on messaging apps**

Characteristics: Values convenience, deals, and digital-first experiences. Uses apps like Telegram, WhatsApp, and Instagram as daily tools.

### User Personas

| Persona | Profile | Pain Point |
|---------|---------|------------|
| **The Saver** | College student, strict budget, buys 5-10 staples weekly | Manually checking prices is tedious; misses deals |
| **The Multi-tasker** | Young professional, busy schedule, values time | No time to track prices; wants alerts sent to phone |
| **The Deal Junkie** | Loves finding the best price, enjoys seeing patterns | Wants historical data and trends to predict drops |

---

## 3. Key Features

### Core Features

#### 3.1 Watchlist Management
- **Add items:** Users send `"Add milk, eggs, bread"` in Telegram
- **Set price threshold:** Specify max price they're willing to pay
- **View watchlist:** `/list` shows all tracked items with current prices
- **Remove items:** Delete from watchlist when no longer interested

#### 3.2 Smart Price Recommendations
- **"ORDER NOW"** – Price is at or below threshold AND historical low
- **"GOOD PRICE"** – Price is below threshold but not the best ever
- **"WAIT"** – Price is above threshold; better deals expected

#### 3.3 Automatic Price Checks
- Runs every 2 hours (configurable)
- Batched API calls for efficiency
- Only alerts if price changes or item goes out of stock

#### 3.4 Price History Tracking
- Stores price data with timestamps
- Tracks in-stock/out-of-stock status

#### 3.5 Out-of-Stock Detection
- Alerts when an item is no longer available on Zepto

---

## 4. User Stories & Use Cases

### Story 1: Bulk Add Items
**As a college student, I want to add multiple grocery items to my watchlist so that I can track prices without manually checking the app every day.**

*Acceptance:* User can add 3+ items in one message. Bot confirms each item found or notes if not available.

### Story 2: Smart Alerts
**As a busy professional, I want to receive an "ORDER NOW" alert only when the price is at its best, so I don't miss out and don't buy too early.**

*Acceptance:* Bot compares current price to historical low AND user's threshold. Only sends alert if both conditions met.

### Story 3: Price History
**As a deal hunter, I want to see the price history for each item so I can understand trends and predict when prices will drop.**

*Acceptance:* `/history [item]` shows last 10 prices with dates and "in stock" status.

### Story 4: Telegram Integration
**As a Gen Z user, I want the bot to integrate with Telegram so I get messages without downloading another app.**

*Acceptance:* Bot sends all alerts and updates as Telegram messages. No web dashboard required.

---

## 5. Technical Requirements

### System Requirements
- Python 3.9+
- Linux/Mac/Windows machine (server or local)
- Internet connection (for Zepto API & Telegram)
- Minimal compute: runs efficiently on a $5/month server

### API Dependencies
- **Zepto API:** Search products, get prices
- **Telegram Bot API:** Send/receive messages
- **Claude AI (Anthropic):** Parse user commands, generate recommendations

### Key Dependencies
- `python-telegram-bot` (v21.0+)
- `anthropic` (Claude SDK, v0.40.0+)
- `requests` (HTTP calls, v2.31.0+)
- `google-generativeai` (optional, for fallback AI)

---

## 6. Setup & Getting Started

### Prerequisites

1. **Telegram Bot Token:** Create a bot via @BotFather on Telegram
2. **Claude API Key:** Get from https://console.anthropic.com
3. **Zepto Account:** Any Indian Zepto account; API credentials extracted via bot

### Installation Steps

1. Clone the repo or copy files to your server
2. Install dependencies: `pip install -r requirements.txt`
3. Configure `config.py` with your API keys
4. Start the bot: `python bot.py`
5. Message the bot on Telegram with `/start`

---

## 7. Success Metrics

### User Engagement
- **Active Users:** % of users checking prices weekly
- **Watchlist Size:** Average items per user (target: 5+)
- **Alert Response Rate:** % of "ORDER NOW" alerts acted on

### User Value
- **Average Savings:** ₹ saved per user per month
- **Purchase Timing:** Avg days from alert to purchase
- **Satisfaction:** User ratings/feedback (target: 4.5+/5)

### System Health
- **Uptime:** Target 99.5%
- **API Reliability:** Successful price checks % (target 99%+)
- **Cost Efficiency:** API cost per check (target <₹0.50)

---

## 8. Future Roadmap

### Phase 2 (Q3 2026)
- Multi-store support (Blinkit, Dunzo)
- Price comparison across platforms
- Weekly digest with savings summary

### Phase 3 (Q4 2026)
- Predictive price alerts (ML-based)
- Group shopping & shared wishlists
- Integration with delivery partners

### Phase 4 (2027)
- Web dashboard with analytics
- Mobile app (iOS/Android)
- Subscriptions & premium features

---

## 9. Support & Feedback

For setup help, issues, or feature requests:

- **Email:** p.ruturaj16@gmail.com
- **GitHub:** Report issues & contribute
- **Telegram:** Discuss directly with the bot

---

## 10. Glossary & Key Terms

| Term | Definition |
|------|-----------|
| **Watchlist** | Personal list of products user wants to monitor |
| **Price Threshold** | Maximum price the user is willing to pay |
| **Historical Low** | Lowest price recorded for an item (considering in-stock only) |
| **SKU** | Stock Keeping Unit; unique product identifier |
| **Batch API Call** | Checking multiple items in one API request (efficient) |
| **MCP** | Model Context Protocol; enables bot to access tools and APIs |

---

**Document Version:** 1.0 | **Last Updated:** May 2026
