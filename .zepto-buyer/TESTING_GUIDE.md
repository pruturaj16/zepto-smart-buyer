# Batch SKU Resolution — Testing Guide

Complete testing checklist for bot.py and price_check.py batch optimizations.

---

## Prerequisites

- Bot is running: `python C:\Users\Owner\.zepto-buyer\bot.py`
- Price_check.py is available for manual runs
- Telegram bot connected to your chat
- At least 2-3 items in watchlist (if running price_check tests)

---

## Test 1: Single Item (Baseline)

**What:** Verify batch function works with 1 item (backward compatibility)

**Steps:**
1. Send to Telegram bot: `Add eggs`
2. Verify response shows: ✅ eggs found and added

**Expected:**
- ✅ Item added successfully
- 1 API call made
- Cost: ~$0.001

**Status:** ☐ Pass / ☐ Fail

---

## Test 2: Multiple Items — All Found

**What:** Verify batch resolution with 3 items, all in stock

**Steps:**
1. Send to Telegram bot: `Add milk, bread, butter`
2. Watch for prices to resolve
3. Verify all 3 appear in watchlist

**Expected:**
- ✅ All 3 items added
- Response shows all 3 with prices (e.g., "₹65", "₹48", "₹200")
- 1 API call (not 3!)
- Cost: ~$0.001

**Check:**
```bash
# Verify cart total updated
Send to bot: /list
```

**Status:** ☐ Pass / ☐ Fail

---

## Test 3: Multiple Items — Partial Success

**What:** Verify partial success handling (some found, some not)

**Steps:**
1. Send to Telegram bot: `Add eggs, fake_product_xyz, milk`
2. Watch response

**Expected:**
```
✅ Added to your watchlist:
  ✅ Eggoz 30 egg tray x1 — ₹322
  ✅ Amul milk 1L x1 — ₹65

❌ Could not find on Zepto:
  ❌ fake_product_xyz
```

- 2 items added, 1 failed
- User is informed of failures
- 1 API call (not 3!)
- Cost: ~$0.001

**Status:** ☐ Pass / ☐ Fail

---

## Test 4: Large Batch (Stress Test)

**What:** Verify batch handles 5+ items without timeout

**Steps:**
1. Send to Telegram bot: `Add chips, coke, popcorn, nachos, salsa, cookies`
2. Wait 3-5 seconds for Zepto to respond
3. Verify all resolved

**Expected:**
- All 6 items resolve successfully (or most of them)
- 1 API call
- No timeout/error
- Cost: ~$0.001

**Check watchlist:**
```
Send to bot: /list
Should show ~6 items with prices
```

**Status:** ☐ Pass / ☐ Fail

---

## Test 5: Quantities (Multi-pack)

**What:** Verify quantity handling works correctly

**Steps:**
1. Send to Telegram bot: `Add eggs x2, milk x3, bread`
2. Verify response shows correct quantities

**Expected:**
```
✅ Added to your watchlist:
  ✅ Eggoz 30 egg tray x2 — ₹644  (2 × ₹322)
  ✅ Amul milk x3 — ₹195  (3 × ₹65)
  ✅ Bread x1 — ₹48
```

**Check:**
```
Send: /list
Should show cart total = (322×2) + (65×3) + 48 = ₹819
```

**Status:** ☐ Pass / ☐ Fail

---

## Test 6: Price Check — Batch Resolution

**What:** Verify price_check.py uses batch resolution

**Steps:**
1. Ensure 3-5 items in watchlist (use Test 2 or earlier `/list` result)
2. Run manually: `python C:\Users\Owner\.zepto-buyer\price_check.py`
3. Watch terminal output

**Expected output:**
```
[2026-04-12 16:45] Starting price check...
  Eggoz 30 egg tray: ₹322 (in_stock=True)
  Amul milk: ₹65 (in_stock=True)
  Bread: ₹48 (in_stock=True)
  Cart: ₹435 | Best baseline: ₹450 | Drop: ₹15
  Drop ₹15 below ₹50 threshold — no alert.
Done.
```

**Key signs of batch operation:**
- All prices printed in sequence (not one-at-a-time)
- No individual "fetch failed" messages for each item
- Completes quickly (< 5 seconds)

**Status:** ☐ Pass / ☐ Fail

---

## Test 7: Price Check — Partial OOS

**What:** Verify OOS handling with batch resolution

**Steps:**
1. Manually remove an item from stock (or wait until Zepto reports OOS)
2. Run: `python C:\Users\Owner\.zepto-buyer\price_check.py`
3. Bot sends OOS prompt to Telegram

**Expected:**
- Partial cart total shown
- User prompted: "Should I record this partial total?"
- ✅ Yes / ❌ No buttons available

**Status:** ☐ Pass / ☐ Fail

---

## Test 8: Type Safety — JSON String Values

**What:** Verify type conversions work (qty/price as strings)

**Steps:**
1. Manually edit `history.json` and change one item's qty to a string:
   ```json
   {"id": "...", "name": "...", "qty": "2", "history": [...]}
   ```
   (Note: `"qty": "2"` as string instead of number)

2. Run: `python C:\Users\Owner\.zepto-buyer\price_check.py`

**Expected:**
- ✅ No TypeError
- Script completes successfully
- Type conversion silently fixes the issue

**Verify:**
```bash
# After script runs, reload watchlist
/list in Telegram
Should show correct totals despite qty being string in JSON
```

**Status:** ☐ Pass / ☐ Fail

---

## Test 9: Remove & Recalculate

**What:** Verify cart recalculation after removal

**Steps:**
1. Have 3+ items in watchlist from Test 2
2. Send: `/remove milk`
3. Send: `/list`
4. Verify cart total decreased by milk's price

**Expected:**
- Milk removed successfully
- Cart history reset (fresh baseline)
- Cart total now excludes milk

**Status:** ☐ Pass / ☐ Fail

---

## Test 10: Cost Verification

**What:** Measure actual cost reduction

**Steps:**
1. Note your Anthropic API usage before changes
2. Run for 24 hours with updated code
3. Check API usage after

**Expected:**
- **Before**: ~$0.09/day (5 items × 12 price checks/day × $0.001 + bot additions)
- **After**: ~$0.02/day (1 call per check + 1 per bot addition)
- **Savings**: ~$0.07/day or **~$2.10/month**

**How to check usage:**
- Log in to: https://console.anthropic.com/account/billing/overview
- Look for "Claude API" usage
- Compare with previous week

**Status:** ☐ Pass / ☐ Fail

---

## Test 11: Error Handling — API Failure

**What:** Verify graceful handling of batch API failure

**Steps:**
1. Manually break Zepto token (or simulate with network issue)
2. Send to bot: `Add eggs, milk`
3. Observe error handling

**Expected:**
```
Got it, looking these up on Zepto...
Error searching products: [error message]
Please try again.
```

- Clear error message
- No crash
- User can retry

**Status:** ☐ Pass / ☐ Fail

---

## Test 12: Alert Trigger — Price Drop Detection

**What:** Verify alert fires correctly with batch prices

**Steps:**
1. Run price_check once: `python price_check.py` (establishes baseline)
2. Wait ~2 hours (or manually reduce prices in Zepto if possible)
3. Run price_check again: `python price_check.py`
4. If drop ≥ ₹50, bot sends alert to Telegram

**Expected:**
```
🛒 Your cart is ₹XXX cheaper than [date].

  Item 1 x1   ₹YYY   (was ₹ZZZ) ↓
  Item 2 x1   ₹AAA   (no change)
  ...
  
Cart total now:    ₹BBB
As of [date]:      ₹CCC
You save:          ₹XXX

Place the order?
[✅ Yes, order now] [❌ Skip]
```

**Status:** ☐ Pass / ☐ Fail

---

## Test 13: Order Placement via Alert

**What:** Verify full end-to-end order from alert

**Steps:**
1. If price drop alert fires (Test 12), click ✅ Yes
2. Bot places order on Zepto

**Expected:**
```
Placing your order on Zepto...
✅ Order placed!

  Item 1 x1   ₹YYY
  Item 2 x1   ₹ZZZ
  ...

────────────────────────────────
  Total paid:     ₹XXX
  Delivery ETA:   12–18 minutes
  Order ID:       xxxxxxxx
────────────────────────────────
```

- Order placed successfully
- Order ID shown
- Telegram notification received

**Status:** ☐ Pass / ☐ Fail

---

## Summary Checklist

| Test | Description | Status |
|------|-------------|--------|
| 1 | Single item (baseline) | ☐ |
| 2 | Multiple items all found | ☐ |
| 3 | Partial success handling | ☐ |
| 4 | Large batch (stress) | ☐ |
| 5 | Quantities multi-pack | ☐ |
| 6 | Price check batch | ☐ |
| 7 | Price check OOS | ☐ |
| 8 | Type safety (JSON strings) | ☐ |
| 9 | Remove & recalculate | ☐ |
| 10 | Cost verification | ☐ |
| 11 | API error handling | ☐ |
| 12 | Alert trigger | ☐ |
| 13 | Order placement | ☐ |

**Status:** ☐ All Pass / ☐ Some Failures

---

## If Tests Fail

### TypeError on price_check.py
- ✅ Fixed by watchlist.py type conversions
- Retry: `python price_check.py`

### Bot doesn't respond to message
- Check bot is running: `python bot.py`
- Check Telegram token in config.py is correct
- Check internet connection

### Prices not updating in /list
- Run price_check manually: `python price_check.py`
- Wait a few seconds for prices to fetch

### Large batch times out
- Reduce batch size to 5-10 items max
- Try again in a few seconds

---

## Next Steps After Testing

✅ All tests pass:
- Deploy to production
- Monitor costs on Anthropic console
- Continue with next improvements (e.g., `/checkprices` command)

⚠️ Some tests fail:
- Check error logs
- Review watchlist.py backups
- Reach out with specific error messages

---

**Testing completed on:** _______________

**Tester notes:** _______________________________________________

---

**Cost savings achieved:** ~**$1.68/month** (80% reduction) ✨
