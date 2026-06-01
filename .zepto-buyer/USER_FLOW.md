# Zepto Smart Buyer - User Flow Documentation

---

## 1. ONBOARDING FLOW

```
User opens Telegram
        ↓
Searches for / adds Zepto Smart Buyer bot
        ↓
Sends /start command
        ↓
Bot responds with welcome message:
"👋 Welcome to Zepto Smart Buyer!
I help you save money by tracking prices on Zepto.
Here's what you can do:
- Add items to watch: 'Add milk, bread, eggs'
- View your watchlist: /list
- Check price history: /history [item]
- Remove an item: /remove [item]"
        ↓
User ready to add items
```

---

## 2. ADD ITEMS TO WATCHLIST

### Scenario 2A: Single Item

```
USER: "Add milk"
        ↓
BOT (Processing):
  1. Search Zepto API for "milk"
  2. Find product: Amul Milk 500ml - ₹35
  3. Add to watchlist (default threshold: ₹40)
        ↓
BOT RESPONSE:
"✅ Added to watchlist!
📦 Amul Milk 500ml
💰 Current price: ₹35
🎯 Your threshold: ₹40
💡 Set a different price? Reply: 'Milk ₹30'"
```

### Scenario 2B: Multiple Items (Batch)

```
USER: "Add eggs, bread, butter, paneer"
        ↓
BOT (Processing):
  1. Batch search Zepto API (1 call for all 4 items)
  2. Find products:
     - Eggs (30 count) - ₹180
     - Bread - ₹40
     - Butter 100g - ₹85
     - Paneer 200g - ₹95
  3. Add all to watchlist
        ↓
BOT RESPONSE:
"✅ Added 4 items to your watchlist!
🥚 Eggs (30 count) - ₹180 | Threshold: ₹200
🍞 Bread - ₹40 | Threshold: ₹50
🧈 Butter - ₹85 | Threshold: ₹95
🧀 Paneer - ₹95 | Threshold: ₹110"
```

### Scenario 2C: Item Not Found

```
USER: "Add fake_product_xyz"
        ↓
BOT (Processing):
  1. Search Zepto API
  2. Product not found
        ↓
BOT RESPONSE:
"❌ Could not find 'fake_product_xyz' on Zepto
Try being more specific:
- 'Add almond milk' instead of 'milk'
- 'Add amul butter' for a specific brand"
```

### Scenario 2D: Set Custom Price Threshold

```
USER: "Milk ₹30"  (after seeing current threshold is ₹40)
        ↓
BOT (Processing):
  1. Update milk threshold from ₹40 to ₹30
  2. Store in watchlist
        ↓
BOT RESPONSE:
"✅ Updated!
🎯 Milk threshold changed: ₹40 → ₹30
Current price: ₹35 (above your new target)"
```

---

## 3. VIEW WATCHLIST

```
USER: "/list"
        ↓
BOT (Processing):
  1. Fetch all items from watchlist
  2. Get current prices from Zepto
  3. Calculate cart total & best baseline
  4. Compare each to threshold
        ↓
BOT RESPONSE:
"📋 Your Watchlist (5 items)
━━━━━━━━━━━━━━━━━━━━━━━━━

🥚 Eggs (30 count)
   💰 Current: ₹180 | Threshold: ₹200
   Status: ✅ IN STOCK

🍞 Bread
   💰 Current: ₹40 | Threshold: ₹50
   Status: ✅ IN STOCK

🧈 Butter 100g
   💰 Current: ₹85 | Threshold: ₹95
   Status: ✅ IN STOCK

🧀 Paneer 200g
   💰 Current: ₹95 | Threshold: ₹110
   Status: ✅ IN STOCK

🥛 Milk 500ml
   💰 Current: ₹35 | Threshold: ₹30
   Status: ✅ IN STOCK

━━━━━━━━━━━━━━━━━━━━━━━━━
💳 Cart Total: ₹435
📊 Best Baseline: ₹420
⏰ Last checked: 2 hours ago"
```

---

## 4. AUTOMATIC PRICE CHECK (Every 2 Hours)

### Scenario 4A: Price Below Threshold AND Historical Low

```
⏰ 2:00 PM - Automatic check triggers
        ↓
BOT (Processing):
  1. Fetch current prices for all items (batch call)
  2. Compare to thresholds
  3. Check historical lows
  4. Detect in-stock status
        ↓
CHANGE DETECTED:
  Butter price: ₹85 → ₹75 (NEW LOW!)
        ↓
BOT SENDS ALERT:
"🎉 ORDER NOW!
🧈 Butter 100g
💰 Current Price: ₹75
🎯 Your Threshold: ₹95 ✅ (below)
📉 Historical Low: ₹82 → ₹75 ✅ (new best!)
⏱️ This is the BEST time to buy!

[BUY NOW] [View History] [Skip]"
```

### Scenario 4B: Price Below Threshold (But NOT Historical Low)

```
⏰ 2:00 PM - Automatic check triggers
        ↓
CHANGE DETECTED:
  Eggs price: ₹180 → ₹170
        ↓
BOT SENDS ALERT:
"💡 GOOD PRICE
🥚 Eggs (30 count)
💰 Current Price: ₹170
🎯 Your Threshold: ₹200 ✅ (below)
📉 Historical Low: ₹165 (not the best)

ℹ️ Good deal, but wait 2-3 days for potential ₹165

[BUY ANYWAY] [WAIT] [View History]"
```

### Scenario 4C: Price Above Threshold

```
⏰ 2:00 PM - Automatic check triggers
        ↓
CHANGE DETECTED:
  Paneer price: ₹95 → ₹110
        ↓
BOT SENDS ALERT:
"⏸️ WAIT
🧀 Paneer 200g
💰 Current Price: ₹110
🎯 Your Threshold: ₹110 → ABOVE TARGET
📈 Price up from: ₹95

Prices typically drop on weekends.

[Check Tomorrow] [View History] [Remove Item]"
```

### Scenario 4D: Out of Stock

```
⏰ 2:00 PM - Automatic check triggers
        ↓
CHANGE DETECTED:
  Milk status: IN STOCK → OUT OF STOCK
        ↓
BOT SENDS ALERT:
"⚠️ OUT OF STOCK
🥛 Milk 500ml
❌ Currently unavailable on Zepto

We'll notify you when it's back in stock.
Last available: ₹35 (on May 15)"
```

### Scenario 4E: No Changes

```
⏰ 2:00 PM - Automatic check triggers
        ↓
NO CHANGES DETECTED:
  All prices same as last check
        ↓
BOT: (Silent - no alert sent)
  Just updates timestamp in history
```

---

## 5. VIEW PRICE HISTORY

```
USER: "/history butter"
        ↓
BOT (Processing):
  1. Fetch all price records for "butter"
  2. Format with dates and status
  3. Calculate trend
        ↓
BOT RESPONSE:
"📊 Price History: Butter 100g
━━━━━━━━━━━━━━━━━━━━━━━━━

Date       | Price | Status    | Notes
May 19     | ₹75   | ✅ IN ST  | NEW LOW!
May 19     | ₹85   | ✅ IN ST  |
May 18     | ₹90   | ✅ IN ST  |
May 18     | ₹82   | ✅ IN ST  | Previous low
May 17     | ₹88   | ✅ IN ST  |
May 16     | ₹85   | ✅ IN ST  |
May 15     | ₹92   | ✅ IN ST  |
May 14     | ₹80   | ✅ IN ST  |
May 13     | ₹95   | ✅ IN ST  |
May 12     | ₹85   | ❌ OUT ST | (not checked)

━━━━━━━━━━━━━━━━━━━━━━━━━
📈 Lowest: ₹75 (May 19)
📊 Average (7 days): ₹86
🎯 Your Threshold: ₹95
💡 Trend: Slightly declining"
```

---

## 6. REMOVE ITEM FROM WATCHLIST

```
USER: "/remove milk"  (or click [Remove Item] in alert)
        ↓
BOT (Processing):
  1. Find "milk" in watchlist
  2. Remove it
  3. Stop monitoring
        ↓
BOT RESPONSE:
"✅ Removed!
🥛 Milk is no longer on your watchlist.
Last price was ₹35
Total watched: 4 items"
```

---

## 7. DAILY/WEEKLY DIGEST (Future Feature)

```
⏰ 9:00 AM Daily Summary
        ↓
BOT SENDS:
"📊 Your Zepto Smart Buyer Summary (May 19)
━━━━━━━━━━━━━━━━━━━━━━━━━

💰 Potential Savings Today: ₹45
  • Butter: ₹10 off (₹75 vs ₹95 threshold)
  • Eggs: ₹10 off (₹170 vs ₹200 threshold)
  • Paneer: ₹5 off (₹105 vs ₹110 threshold)

📈 Best Deals:
  1. Butter 100g ⭐⭐⭐ (NEW LOW!)
  2. Eggs 30 count ⭐⭐ (Good price)

⚠️ Out of Stock:
  • Milk (was ₹35)

📋 Total Watched: 5 items
💳 Cart Value: ₹435 (vs normal ₹480)

[BUY NOW] [View Watchlist] [Settings]"
```

---

## 8. COMPLETE JOURNEY MAP

```
START
  ↓
[/start] → Welcome message
  ↓
Add items → "Add milk, eggs, bread"
  ↓
Receive confirmation → Item added with threshold
  ↓
View watchlist → /list (shows all items + prices)
  ↓
Set thresholds → "Milk ₹30" (custom price)
  ↓
Wait for automatic check → Every 2 hours
  ↓
[Decision Point] Price changes?
  ├─ YES → Send alert with recommendation
  │  ├─ ORDER NOW (price ≤ threshold AND historical low)
  │  ├─ GOOD PRICE (price ≤ threshold but not lowest)
  │  ├─ WAIT (price > threshold)
  │  └─ OUT OF STOCK (unavailable)
  │
  └─ NO → Silent (no alert, just log update)
  ↓
User takes action → BUY / WAIT / SKIP / VIEW HISTORY
  ↓
View price history → /history [item]
  ↓
Remove items → /remove [item] (when done buying)
  ↓
Continue monitoring → Back to automatic checks
  ↓
END (or keep monitoring indefinitely)
```

---

## 9. USER INTERACTION EXAMPLES

### Example 1: College Student (Budget-Conscious)

```
Day 1:
USER: "Add milk, eggs, bread, butter, rice"
BOT: ✅ Added 5 items

Day 2 (2:00 PM):
BOT: "🎉 ORDER NOW! Milk ₹28 (was ₹35)"
USER: "[BUY NOW]"
USER: "Just bought milk for ₹28! 🎉"

Day 3 (4:00 PM):
BOT: "💡 GOOD PRICE - Eggs ₹170 (below ₹200)"
USER: "[WAIT] - buying this weekend"

Day 4 (10:00 AM):
BOT: "⏸️ WAIT - Butter ₹95 (above ₹85)"
USER: "[Skip]"

Day 5 (9:00 AM):
BOT: "📊 Weekly Summary: Saved ₹45 this week!"
USER: Views summary, feels good about savings
```

### Example 2: Busy Professional (Time-Conscious)

```
Monday:
USER: "Add everything I buy regularly: milk, paneer, bread"
BOT: ✅ Added 3 items

Tuesday-Thursday:
BOT: (Sends 2-3 alerts automatically)
USER: Glances at alerts, saves relevant ones

Friday (9:00 AM):
USER: Checks /list before grocery run
BOT: Shows all prices + recommendations
USER: Takes screenshot, goes to Zepto app, buys based on alerts

Result: Saved time by not checking prices manually
```

### Example 3: Deal Hunter (Data-Curious)

```
Week 1:
USER: "Add chocolate, coffee, snacks (10 items)"
BOT: ✅ Added 10 items

Daily:
BOT: Sends 3-5 alerts
USER: Views /history for each item after a few days

Week 2:
USER: "Show me items with biggest price drops"
USER: Analyzes patterns, sets strategic thresholds
USER: Buys when prices hit patterns they discovered

Result: Found that chocolate drops every Monday, coffee drops mid-week
```

---

## 10. ERROR HANDLING & EDGE CASES

### Case 1: Item Name Ambiguity

```
USER: "Add milk"
BOT: "Which milk? 
  1. Amul Full Cream 500ml - ₹35
  2. Amul Toned 500ml - ₹32
  3. Lactose-free milk 500ml - ₹65
  
Reply: 'Milk 1' or 'Amul Full Cream'"
```

### Case 2: Zepto API Temporarily Down

```
USER: "/list"
BOT: "⚠️ Prices couldn't be fetched right now
Using last cached prices from 30 min ago:

🥚 Eggs - ₹180 (🕐 cached)
🍞 Bread - ₹40 (🕐 cached)

Try again in a moment. We'll auto-refresh."
```

### Case 3: User Sets Unrealistic Threshold

```
USER: "Milk ₹10"
BOT: "⚠️ That's very low!
Current price: ₹35
Historical low: ₹28

Setting ₹10 - you might wait forever.
Suggest ₹25-28 for more realistic alerts?
[Accept ₹10] [Change to ₹25] [Show History]"
```

### Case 4: Item Goes Out of Stock & Back In

```
Day 1: Milk OUT OF STOCK
BOT: "⚠️ Milk unavailable"

Day 2: Milk BACK IN STOCK at ₹40
BOT: "🎉 Milk is back!
Price: ₹40 | Your threshold: ₹30
(Above target but available again)"
```

---

## 11. COMMAND REFERENCE

| Command | Example | Action |
|---------|---------|--------|
| `/start` | `/start` | Show welcome & help |
| Add items | `Add milk, bread, eggs` | Add multiple items |
| Set threshold | `Milk ₹30` | Update price threshold |
| View watchlist | `/list` | Show all items + prices |
| Price history | `/history milk` | Show last 10 price records |
| Remove item | `/remove milk` | Stop monitoring |
| Help | `/help` | Show all commands |
| Settings | `/settings` | Configure notification frequency |

---

## 12. NOTIFICATION STRATEGY

### When to Send Alerts

✅ **SEND alerts when:**
- Price goes below threshold AND hits historical low → "ORDER NOW"
- Price goes below threshold (but not lowest) → "GOOD PRICE"
- Item goes out of stock → "OUT OF STOCK"
- Item comes back in stock → "BACK IN STOCK"

❌ **DON'T send alerts when:**
- Price stays the same
- Price goes above previous level (unless was out of stock)
- Only 1-2 rupee fluctuation

### Notification Frequency
- **Immediate:** Major price drops, out of stock
- **Batched:** 2-hour check cycles
- **Daily:** Summary at 9:00 AM (optional)
- **Weekly:** Sunday recap (future feature)

---

## Summary

The user flow is designed to be **minimal friction**:
1. Add items in one message
2. Get automatic alerts
3. Make quick decisions
4. Check history when curious

No forms, no clicking, no extra steps. Just natural conversation with AI.
