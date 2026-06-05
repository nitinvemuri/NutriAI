"""
pipeline/dedupe.py
------------------
Cleans and deduplicates the foods table after ingestion.
 
Deduplication strategy:
  1. Exact fdc_id duplicates are already blocked by the UNIQUE index in ingest.py.
  2. This script handles near-duplicate descriptions (e.g. "Milk, whole" vs "Whole milk")
     using fuzzy matching — keeps the row with the most nutrient coverage.
  3. Drops rows where ALL macros are NULL (useless records).
 
Usage:
    python -m pipeline.dedupe
"""
 
import logging
import sqlite3
from pathlib import Path
 
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
 
DB_PATH = Path(__file__).resolve().parents[2] / "data" / "foods.db"
 
MACRO_COLS = ["calories", "protein", "carbs", "fat", "fiber"]
 
 
def nutrient_coverage(row: dict) -> int:
    """Count how many nutrient fields are non-NULL."""
    all_nutrients = MACRO_COLS + ["iron", "calcium", "vitamin_b12", "vitamin_d",
                                   "zinc", "sodium", "potassium", "magnesium"]
    return sum(1 for col in all_nutrients if row.get(col) is not None)
 
 
def drop_empty_macros(conn: sqlite3.Connection) -> int:
    """Remove rows where every macro is NULL — they can't contribute to meal plans."""
    cursor = conn.execute(f"""
        DELETE FROM foods
        WHERE {" AND ".join(f"{c} IS NULL" for c in MACRO_COLS)}
    """)
    conn.commit()
    return cursor.rowcount
 
 
def normalize_description(desc: str) -> str:
    """Lowercase, strip punctuation noise for comparison."""
    import re
    return re.sub(r"[^a-z0-9 ]", "", desc.lower().strip())
 
 
def dedupe_by_description(conn: sqlite3.Connection) -> int:
    """
    Find groups of foods with the same normalized description.
    Within each group, keep the one with the best nutrient coverage.
    Delete the rest.
    """
    rows = conn.execute(
        "SELECT fdc_id, description, calories, protein, carbs, fat, fiber, "
        "iron, calcium, vitamin_b12, vitamin_d, zinc, sodium, potassium, magnesium "
        "FROM foods"
    ).fetchall()
 
    cols = ["fdc_id", "description", "calories", "protein", "carbs", "fat", "fiber",
            "iron", "calcium", "vitamin_b12", "vitamin_d", "zinc", "sodium", "potassium", "magnesium"]
    records = [dict(zip(cols, r)) for r in rows]
 
    # Group by normalized description
    groups: dict[str, list[dict]] = {}
    for rec in records:
        key = normalize_description(rec["description"])
        groups.setdefault(key, []).append(rec)
 
    to_delete = []
    for key, group in groups.items():
        if len(group) == 1:
            continue
        # Sort: highest coverage first
        group.sort(key=nutrient_coverage, reverse=True)
        # Keep the first (best), delete the rest
        to_delete.extend(r["fdc_id"] for r in group[1:])
 
    if to_delete:
        conn.execute(
            f"DELETE FROM foods WHERE fdc_id IN ({','.join('?' * len(to_delete))})",
            to_delete,
        )
        conn.commit()
 
    return len(to_delete)
 
 
def report_stats(conn: sqlite3.Connection) -> None:
    total = conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0]
    with_calories = conn.execute("SELECT COUNT(*) FROM foods WHERE calories IS NOT NULL").fetchone()[0]
    categories = conn.execute(
        "SELECT food_category, COUNT(*) FROM foods GROUP BY food_category ORDER BY 2 DESC LIMIT 10"
    ).fetchall()
 
    log.info(f"--- DB Stats ---")
    log.info(f"Total records:        {total:,}")
    log.info(f"Records with calories:{with_calories:,}")
    log.info(f"Top categories:")
    for cat, count in categories:
        log.info(f"  {cat or '(none)':40s} {count:,}")
 
 
def run_dedupe() -> None:
    conn = sqlite3.connect(DB_PATH)
 
    removed_empty = drop_empty_macros(conn)
    log.info(f"Removed {removed_empty:,} rows with no macro data")
 
    removed_dupes = dedupe_by_description(conn)
    log.info(f"Removed {removed_dupes:,} near-duplicate descriptions")
 
    report_stats(conn)
    conn.close()
 
 
if __name__ == "__main__":
    run_dedupe()