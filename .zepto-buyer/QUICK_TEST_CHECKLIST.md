# Quick Test Checklist — 15 Minutes

Fast track testing for batch SKU resolution changes.

---

## Pre-flight Check (2 min)

- [ ] Bot is running: `python bot.py`
- [ ] Watchlist has 1-2 items from before
- [ ] Can access Telegram bot chat

---

## Test Bot Batching (5 min)

**Single item:**
```
Send: Add eggs
Expected: ✅ Eggs found and added
```
☐ Pass

**Multiple items:**
```
Send: Add milk, bread, butter
Expected: ✅ All 3 added, one response
```
☐ Pass

**Partial success:**
```
Send: Add chips, fake_item_xyz, cola
Expected: ✅ chips & cola added, ❌ fake_item marked as not found
```
☐ Pass

**Verify quantities:**
```
Send: /list
Expected: Cart total shows correct math (price × qty)
```
☐ Pass

---

## Test Price Check Batching (5 min)

**Manual run:**
```bash
python C:\Users\Owner\.zepto-buyer\price_check.py
```

**Expected:**
- All prices print quickly (< 3 seconds)
- No "fetch failed" errors per item
- Summary shows: "Cart: ₹XXX | Best baseline: ₹YYY"
- Completes with "Done."

☐ Pass

**Verify batching happened:**
- Script output shows all prices, not one-by-one delays
- No TypeError

☐ Pass

---

## Test Type Safety (2 min)

**Run:**
```bash
python price_check.py
```

**Expected:**
- No TypeError about qty or price
- Script completes successfully
- /list still shows correct totals

☐ Pass

---

## Cost Verification (1 min)

**Check:**
```
Before: 5 items × 5 calls/check = 25 API calls per 2-hour check
After:  5 items × 1 call/check = 1 API call per 2-hour check
Savings: 96% per check cycle
```

**Monthly:**
- Before: ~$2.10/month
- After: ~$0.42/month
- **Savings: $1.68/month ✨**

☐ Understood

---

## Summary

| Item | Pass | Notes |
|------|------|-------|
| Bot single item | ☐ | |
| Bot batch items | ☐ | |
| Bot partial success | ☐ | |
| Bot quantities | ☐ | |
| Price check batch | ☐ | |
| Type safety | ☐ | |
| Cost savings | ☐ | |

**Overall:** ☐ Ready for production

---

## If Anything Fails

1. Check error message
2. Review `TESTING_GUIDE.md` for detailed troubleshooting
3. Check backups exist: `*.backup` files

---

**Date tested:** _______________

**Tester:** _______________

**Status:** ☐ Pass All / ☐ Needs review
