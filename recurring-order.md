# PRD: Pantry Mode — Recurring Orders for Cart-Watch

**Author:** Ruturaj  
**Status:** Implemented  
**Last updated:** 2026-08-10

---

## 1. Problem

Cart-Watch currently operates in a reactive mode: the user manually types a shopping list, the bot finds the SKUs, and it watches prices. Every week when the user wants to restock groceries, they start from scratch — retyping the same 15 items, re-confirming SKUs, and rebuilding context the bot already had.

This is the same friction Zepto itself doesn't solve. There is no recurring/standing order on Zepto. Power users who buy the same basket weekly either keep a notes app list and retype it, or forget items. The result is lower order frequency and higher churn to BigBasket (which does support subscriptions).

**Pantry Mode** turns Cart-Watch into a habit engine: the user defines a standing weekly grocery list once, and the bot handles the rest — nudging on schedule, checking stock, suggesting substitutes for OOS items, and presenting a ready-to-confirm cart.

---

## 2. Goals

- Allow users to define one or more named pantry lists (e.g., "Weekly Groceries", "Monthly Staples")
- Send a scheduled nudge (Telegram message) on a user-chosen day + time in IST
- At nudge time, show current prices, flag OOS items, and offer pre-approved substitutes
- Let the user confirm, edit, or skip — one tap to load the cart into Zepto
- Track whether nudges result in orders (conversion), and surface this as a metric

## Non-Goals (v1)

- Automatic order placement without user confirmation (always requires a tap)
- Multiple delivery addresses per pantry list
- Price-optimized reorder timing within the week ("order Thursday, it's cheaper") — this can come in v2 using existing price history
- Multi-user support — Cart-Watch is single-user today, Pantry Mode stays that way

---

## 3. Success Metrics

**Primary**
- Nudge-to-order conversion rate: % of nudges that result in the user placing a Zepto order within 4 hours
- Week-4 recurring order rate: % of users who placed a pantry order in week 1 who are still doing so in week 4 (target: ≥50%)

**Secondary**
- Average items per pantry list (proxy for depth of adoption; target: ≥10)
- OOS rate at nudge time (target: <20% of items OOS)
- Substitute acceptance rate when OOS items are present
- Edit rate before confirming (high = cart is stale / wrong SKUs)

**Guardrails**
- Nudge skip rate (user taps Skip 2+ weeks in a row → list is paused automatically)
- SKU drift rate: how often a pantry SKU returns invalid/OOS at 3+ consecutive nudge times (triggers re-discovery prompt)

---

## 4. User Stories

| As a...               | I want to...                                          | So that...                                              |
|-----------------------|-------------------------------------------------------|---------------------------------------------------------|
| Weekly grocery buyer  | Set up a standing list once                           | I don't retype the same 15 items every Sunday           |
| Busy user             | Get a nudge on Sunday morning                         | I remember to order before the week starts              |
| User with substitution anxiety | Pre-approve a backup for my usual eggs      | I don't get a random brand delivered                    |
| User who skips a week | Snooze the nudge without deleting the list            | The list is ready again next week                       |
| User whose pantry item is discontinued | Get told the SKU is stale and pick a replacement | My list doesn't silently fail               |

---

## 5. Feature Specification

### 5.1 New Telegram Commands

| Command                   | Behaviour                                                                                      |
|---------------------------|-----------------------------------------------------------------------------------------------|
| `/pantry`                 | Show all pantry lists with name, item count, next nudge time, last ordered date               |
| `/pantry new`             | Start the create flow (name → cadence → day/time)                                             |
| `/pantry edit <name>`     | Enter edit mode for a list: add/remove items, change cadence                                  |
| `/pantry delete <name>`   | Delete a list (with confirm prompt)                                                            |
| `/pantry pause <name>`    | Pause nudges for this list indefinitely                                                        |
| `/pantry resume <name>`   | Resume a paused list                                                                           |

Creating or editing a list also accepts free-text messages in the same natural-language format as the existing watchlist flow. Example: *"Add Eggoz 30 egg tray x1, Amul Taaza 1L x3 to my weekly list"* — the existing `zepto_agent.plan/execute/analyze` pipeline resolves the SKUs exactly as it does today.

### 5.2 Nudge Message Format

Sent automatically on schedule by the pantry checker:

```
🛒 *Weekly Grocery — time to restock!*

✅ Amul Taaza 1L × 3 — ₹58 each
✅ Eggoz 30 egg tray × 1 — ₹239
⚠️ Britannia Brown Bread — OUT OF STOCK
   → Suggested: Modern Brown Bread 400g — ₹45 (tap to approve)
✅ Aashirvaad Atta 5kg × 1 — ₹280

Total (excl. OOS): ₹669

[✅ Load cart & order]  [✏️ Edit list]  [⏭ Skip this week]
```

- **Load cart & order** — pushes all in-stock items to the live Zepto cart via `update_cart`, then sends: *"Cart loaded! Open Zepto to check out."*
- **Edit list** — enters a back-and-forth edit session, same flow as `/pantry edit`
- **Skip this week** — records skip, schedules next nudge for next cadence cycle

### 5.3 OOS Handling at Nudge Time

1. At nudge time, `fetch_all_prices()` runs for all pantry SKUs (reuses `price_check.py` — free, direct MCP).
2. OOS items are flagged in the nudge message.
3. If the user previously approved a substitute, it is used automatically and loaded to cart on "Load cart".
4. If no substitute exists, a **"🔍 Substitute for X"** button appears. When tapped:
   - Claude Haiku extracts a category search query from the item name (e.g., "Eggoz 30 egg tray" → "eggs")
   - `search_products_batch` fetches up to 10 results
   - Results are ranked: **previously ordered items first** (matched against `order_context.txt`), then others sorted by price ascending
   - Note: Zepto MCP does not expose product ratings — price is used as the secondary ranking signal
   - A **"🔎 Type my own search"** button lets the user type a custom query (e.g., "organic eggs under ₹200") with optional price ceiling filtering
5. When a substitute is selected, the user is asked: "Save as permanent backup?" — yes saves to `approved_substitute` in the pantry item.

### 5.4 Smart Nudge Timing (Price-Aware)

The nudge fires within a 24-hour window around the user's chosen day/time, but only when the pantry cart total is at or near its historical low for that week:

- Every 2-hour price check run also calls `pantry_check.run()` (piggybacked — no extra Cloud Scheduler job needed)
- Each run appends a price sample to `items[].price_history[]`
- At nudge time, `is_good_time_to_nudge()` compares the current total against the per-item minimum price over the last `cadence_days` of samples
- If current total ≤ (historical min × 1.03) → nudge now
- If prices are elevated → wait; retry on the next 2-hour check
- Hard deadline: after 90% of the cadence period has passed since the last nudge, nudge regardless of price

### 5.5 Cadence Logic

- Cadence is stored as `cadence_days` (7 = weekly, 14 = biweekly, 30 = monthly)
- `nudge_day_of_week` (0=Monday … 6=Sunday) + `nudge_time_ist` ("09:00") define the window start
- `pantry_check.run()` is called from `price_check.run()` — no separate cron needed
- `last_nudged_at` is updated on every sent nudge to prevent double-firing within the same cycle

Auto-pause rule: if a user skips 3 consecutive nudges, the list is automatically paused and the user is notified: *"Paused Weekly Grocery — you've skipped 3 times. Resume with /pantry resume."*

---

## 6. Data Model

Add a `pantry_lists` key to `history.json` alongside the existing top-level keys. No existing schema changes — fully backwards-compatible.

```json
{
  "skus": [],
  "cart_total_history": [],
  "pantry_lists": [
    {
      "id": "a1b2c3d4",
      "name": "Weekly Grocery",
      "cadence_days": 7,
      "nudge_day_of_week": 6,
      "nudge_time_ist": "09:00",
      "paused": false,
      "created_at": "2026-08-10T10:00:00+05:30",
      "last_nudged_at": null,
      "last_ordered_at": null,
      "consecutive_skips": 0,
      "items": [
        {
          "sku_id": "uuid-from-zepto",
          "name": "Amul Taaza Toned Milk 1L",
          "qty": 3,
          "store_product_id": "zepto-store-product-id",
          "approved_substitute": {
            "sku_id": "uuid-of-substitute",
            "store_product_id": "...",
            "name": "Amul Gold Full Cream Milk 1L"
          }
        }
      ],
      "nudge_history": [
        {
          "nudged_at": "2026-08-03T09:02:11+05:30",
          "outcome": "ordered",
          "resolved_at": "2026-08-03T09:14:00+05:30",
          "oos_items": []
        }
      ]
    }
  ]
}
```

`nudge_history` keeps the last 12 nudge records per list — enough for 3 months of weekly data. This is the raw material for the Week-4 retention metric and nudge conversion rate.

---

## 7. Technical Design

### New file: `pantry.py`

Owns all pantry list logic. Keeps it fully separate from the existing `watchlist.py` to avoid coupling.

```
pantry.py
  load_pantry_lists()                                    → list[dict]
  save_pantry_lists(lists)
  add_pantry_list(name, cadence_days, nudge_day, nudge_time) → dict
  delete_pantry_list(list_id)
  add_item_to_list(list_id, sku_id, name, qty, store_product_id)
  remove_item_from_list(list_id, sku_id)
  set_substitute(list_id, sku_id, substitute_dict)
  get_lists_due_for_nudge(now_ist)                       → list[dict]
  record_nudge_sent(list_id, oos_items)
  record_nudge_outcome(list_id, outcome)                 # "ordered" | "skipped" | "edited"
  maybe_autopause(list_id)                               → bool
```

### New file: `pantry_check.py`

The scheduler entry point — runs every 30 minutes via cron, analogous to `price_check.py`.

```
pantry_check.py
  run()
    pull_state()
    lists = get_lists_due_for_nudge(now_ist)
    for each list:
      prices = fetch_all_prices(list["items"])   # reuse from price_check.py
      oos, in_stock = partition items by stock
      send_pantry_nudge(list, in_stock, oos)
      record_nudge_sent(list["id"], oos_names)
```

### Changes to `bot.py`

- Register new command handlers: `/pantry`, `/pantry new`, `/pantry edit`, etc.
- Add a new callback branch in `handle_callback` for `pantry:*` prefixed callback data:
  - `pantry:load:<list_id>` → push in-stock items to Zepto cart via `update_cart`, record outcome `"ordered"`
  - `pantry:skip:<list_id>` → record outcome `"skipped"`, increment `consecutive_skips`
  - `pantry:edit:<list_id>` → enter edit session
  - `pantry:sub:<list_id>:<sku_id>:<candidate_idx>` → approve a substitute, save to `approved_substitute`

### Changes to `config.py`

None — pantry check is driven by the existing `CHECK_INTERVAL_HOURS` via `price_check.py`.

### Changes to `gcs_sync.py`

None — `pantry_lists` lives inside `history.json` so GCS sync is automatic.

### Changes to `price_check.py`

`run()` calls `pantry_check.run()` in its `finally` block (alongside `metrics.write_metrics()`), so pantry nudges are evaluated on every 2-hour price check with no new infrastructure.

### Cron entries

No new entries — pantry check piggybacks on the existing Cloud Scheduler job.
```
# Existing (unchanged): triggers both price check and pantry check
0 */2 * * * python price_check.py
```

---

## 8. User Flows

### Setup (first time)

```
User: /pantry new
Bot:  What do you want to call this list? (e.g. "Weekly Grocery")
User: Weekly Grocery
Bot:  How often should I remind you?
      [Every week]  [Every 2 weeks]  [Every month]
User: [Every week]
Bot:  Which day and time?
      [Sunday 9 AM]  [Sunday 7 PM]  [Monday 9 AM]  [Custom]
User: [Sunday 9 AM]
Bot:  Got it! Now add your items — just type them naturally.
      e.g. "Amul milk 1L x3, Eggoz 30 egg tray, Aashirvaad atta 5kg"
User: Amul Taaza 1L x3, Eggoz 30 egg tray, bread
Bot:  [shows product picker for each item using existing _send_picker flow]
      ... user confirms each item ...
Bot:  ✅ Weekly Grocery is set up with 3 items.
      Next nudge: Sunday, 17 Aug at 9:00 AM IST
```

### Nudge → Order (happy path, Sunday 9 AM)

```
Bot:  🛒 Weekly Grocery — time to restock!
      [lists items with prices]
      [✅ Load cart & order]  [✏️ Edit list]  [⏭ Skip this week]

User: [taps Load cart & order]
Bot:  ✅ 3 items loaded into your Zepto cart.
      Open the app to check out!
      (Next nudge: Sun 24 Aug at 9:00 AM)
```

### Nudge → OOS item

```
Bot:  🛒 Weekly Grocery — time to restock!
      ✅ Amul Taaza 1L × 3 — ₹58
      ⚠️ Eggoz 30 egg tray — OUT OF STOCK
         → Find substitute?
      ✅ Aashirvaad Atta 5kg — ₹280

      [✅ Load available items]  [🔍 Find substitute for Eggoz]  [⏭ Skip]

User: [taps Find substitute for Eggoz]
Bot:  [shows 2-3 egg product candidates with photos, price, picker buttons]
User: [selects option 2]
Bot:  Got it — using Nandus Farm Eggs 30pk ₹229 as substitute.
      Save this as permanent backup for Eggoz? [Yes] [No, just this time]
```

---

## 9. Edge Cases

| Edge case | Handling |
|---|---|
| User removes a SKU from the main watchlist that is also in a pantry list | Remove from pantry list silently; notify user at next `/pantry` view |
| All items in a pantry list are OOS at nudge time | Send nudge with full OOS warning; offer Skip or "check back in 4 hours" snooze |
| Zepto `update_cart` fails when loading cart from nudge | Show error, let user retry the same button; do not mark as "ordered" |
| User taps "Load cart" on a stale nudge message (>12 hours old) | Re-fetch prices before loading; warn if total has changed by >₹50 |
| Two pantry lists are due in the same 30-minute window | Send both nudges sequentially, 60 seconds apart, to avoid Telegram rate limits |
| Pantry item's `sku_id` returns 404 from Zepto (SKU discontinued) | Flag as stale; prompt user to re-pick; do not load silently to cart |

---

## 10. Out of Scope (v1)

- Price-optimized nudge timing ("prices are ₹40 cheaper than usual right now — order today instead of Sunday")
- Quantity auto-adjustment based on purchase history
- Multi-user / household sharing
- Pantry analytics dashboard (spend per list, savings over time)
- Integration with Zepto Café items or non-grocery categories

These are natural v2 additions once the core nudge → order loop is validated with alpha users.

---

## 11. Open Questions

1. **Single file vs. separate file for pantry state** — `pantry_lists` lives inside `history.json` for now (simplest, one GCS sync). If the list grows large with nudge history, consider splitting into `pantry.json`.
2. **Nudge staleness UX** — if the user misses Sunday's nudge and opens it on Monday, should we re-fetch prices before showing the "Load cart" button? Recommended: yes, if nudge is >6 hours old.
3. **Cadence flexibility** — "every 7 days from last order" vs. "fixed day of week" are meaningfully different. Fixed day of week (Sunday) is simpler and more habit-forming; implement that first.
