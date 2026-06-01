"""
Free text shopping list parser — no API call needed.
Handles inputs like:
  "Add Eggoz eggs, Amul milk x2, atta 5kg"
  "eggs, milk, bread x3"
  "Eggoz 30 egg tray - 1, Amul milk - 2"
"""
import re

def parse_shopping_list(text: str) -> list:
    """
    Returns list of {name, qty} dicts.
    Falls back to the full item text as name with qty=1 if no quantity found.
    """
    # Remove common filler phrases
    text = re.sub(
        r"(?i)^(add|please add|order|i need|get me|buy)\s+", "", text.strip()
    )
    text = re.sub(r"(?i)\s+to\s+(my\s+)?(list|cart|watchlist).*$", "", text)

    # Split on commas or newlines
    raw_items = re.split(r"[,\n]+", text)

    results = []
    for item in raw_items:
        item = item.strip(" -•*")
        if not item:
            continue

        # Extract quantity — patterns: x2, x 2, ×2, - 2, (2), qty 2, 2 units
        qty = 1
        qty_match = re.search(
            r"(?:x\s*|×\s*|-\s*|qty\s*|quantity\s*|\()\s*(\d+)\s*(?:units?|packs?|pcs?|pieces?)?\)?",
            item, re.IGNORECASE
        )
        if qty_match:
            qty = int(qty_match.group(1))
            # Remove the quantity part from the name
            item = item[:qty_match.start()].strip(" -x×(")

        if item:
            results.append({"name": item.strip(), "qty": qty})

    return results


if __name__ == "__main__":
    tests = [
        "Add Eggoz 30 egg tray, Amul milk 1L x2, Aashirvaad atta 5kg",
        "eggs x3, milk, bread - 2",
        "Eggoz eggs",
        "please add tomatoes x4 to my list",
        "Amul butter\nAmul milk x2\nEggoz eggs x1",
    ]
    for t in tests:
        print(f"Input:  {t}")
        print(f"Output: {parse_shopping_list(t)}")
        print()
