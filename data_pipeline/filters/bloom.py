"""
filters/bloom.py
----------------
Builds and persists Bloom filters for fast pre-filtering at query time.
One filter per constraint type — loaded into memory once at app startup.
 
Install dependency:
    pip install pybloom-live
 
Usage:
    python -m filters.bloom          # build all filters from DB
    
Then in your app:
    from filters.bloom import load_filters
    bf = load_filters()
    if bf["fodmap"].check(food_name_lower):
        # skip this food for IBS users
"""
 
import logging
import pickle
import sqlite3
from pathlib import Path
 
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
 
try:
    from pybloom_live import BloomFilter
except ImportError:
    raise ImportError("Run: pip install pybloom-live")
 
DB_PATH = Path(__file__).resolve().parents[2] / "data" / "foods.db"
FILTER_DIR = Path(__file__).resolve().parents[2] / "data" / "bloom_filters"
FILTER_DIR.mkdir(parents=True, exist_ok=True)
 
# Each filter: (filename, SQL WHERE clause to select foods for that set)
FILTER_SPECS: dict[str, str] = {
    "fodmap":     "is_high_fodmap = 1",
    "gerd":       "is_gerd_trigger = 1",
    "gluten":     "allergens LIKE '%gluten%'",
    "dairy":      "allergens LIKE '%dairy%'",
    "tree_nuts":  "allergens LIKE '%tree_nuts%'",
    "peanuts":    "allergens LIKE '%peanuts%'",
    "shellfish":  "allergens LIKE '%shellfish%'",
    "soy":        "allergens LIKE '%soy%'",
    "eggs":       "allergens LIKE '%eggs%'",
    "fish":       "allergens LIKE '%fish%'",
    "low_gi":     "glycemic_index IS NOT NULL AND glycemic_index <= 55",
    "high_gi":    "glycemic_index IS NOT NULL AND glycemic_index > 55",
}
 
 
def build_filter(conn: sqlite3.Connection, where_clause: str, capacity: int = 20000) -> "BloomFilter":
    bf = BloomFilter(capacity=capacity, error_rate=0.001)
    rows = conn.execute(
        f"SELECT LOWER(description) FROM foods WHERE {where_clause}"
    ).fetchall()
    for (desc,) in rows:
        # Add full description and individual words for flexible matching
        bf.add(desc)
        for word in desc.split():
            if len(word) > 3:  # skip short noise words
                bf.add(word)
    log.info(f"  Built filter for [{where_clause[:50]}...] — {len(rows):,} foods indexed")
    return bf
 
 
def build_all_filters() -> None:
    conn = sqlite3.connect(DB_PATH)
    filters = {}
 
    for name, where in FILTER_SPECS.items():
        log.info(f"Building Bloom filter: {name}")
        bf = build_filter(conn, where)
        filters[name] = bf
        path = FILTER_DIR / f"{name}.bloom"
        with open(path, "wb") as f:
            pickle.dump(bf, f)
 
    conn.close()
    log.info(f"All {len(filters)} Bloom filters saved to {FILTER_DIR}/")
 
 
def load_filters() -> dict[str, "BloomFilter"]:
    """Load all persisted Bloom filters into memory. Call once at app startup."""
    filters = {}
    for path in FILTER_DIR.glob("*.bloom"):
        with open(path, "rb") as f:
            filters[path.stem] = pickle.load(f)
    log.info(f"Loaded {len(filters)} Bloom filters")
    return filters
 
 
def check_food(filters: dict, food_description: str, constraint: str) -> bool:
    """
    Returns True if the food MIGHT match the constraint (could be in the flagged set).
    False positives are possible (~0.1% rate) — always verify against DB for hard safety.
    This is a fast pre-filter, not a guarantee.
    
    Example:
        if check_food(bf, "whole milk", "dairy"):
            skip_food()
    """
    if constraint not in filters:
        raise KeyError(f"No Bloom filter for constraint: {constraint}")
    desc_lower = food_description.lower()
    bf = filters[constraint]
    # Check full description OR any significant word
    if desc_lower in bf:
        return True
    return any(word in bf for word in desc_lower.split() if len(word) > 3)
 
 
if __name__ == "__main__":
    build_all_filters()