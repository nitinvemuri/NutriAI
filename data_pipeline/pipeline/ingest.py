"""
pipeline/ingest.py
------------------
Pulls food records from USDA FoodData Central API and stores them in SQLite.
Target: >= 10,000 deduplicated structured records.
 
Usage:
    python -m pipeline.ingest --api-key YOUR_KEY --limit 12000
"""
 
import argparse
import logging
import sqlite3
import time
from pathlib import Path
 
import requests
 
API_BASE = "https://api.nal.usda.gov/fdc/v1"
DB_PATH = Path(__file__).resolve().parents[2] / "data" / "foods.db"
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
 
# Data types to pull from USDA — Branded added last so it only runs if we're short
DATA_TYPES = ["Foundation", "SR Legacy", "Survey (FNDDS)", "Branded"]
 
NUTRIENT_IDS = {
    # Macros
    "calories":  1008,
    "protein":   1003,
    "carbs":     1005,
    "fat":       1004,
    "fiber":     1079,
    # Micros
    "iron":      1089,
    "calcium":   1087,
    "vitamin_b12": 1178,
    "vitamin_d": 1114,
    "zinc":      1095,
    "sodium":    1093,
    "potassium": 1092,
    "magnesium": 1090,
}
 
 
def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS foods (
            fdc_id          INTEGER PRIMARY KEY,
            description     TEXT NOT NULL,
            data_type       TEXT,
            food_category   TEXT,
            -- Macros (per 100g)
            calories        REAL,
            protein         REAL,
            carbs           REAL,
            fat             REAL,
            fiber           REAL,
            -- Micros (per 100g)
            iron            REAL,
            calcium         REAL,
            vitamin_b12     REAL,
            vitamin_d       REAL,
            zinc            REAL,
            sodium          REAL,
            potassium       REAL,
            magnesium       REAL,
            -- Flags (set by filter layer)
            is_high_fodmap  INTEGER DEFAULT 0,
            is_gerd_trigger INTEGER DEFAULT 0,
            allergens       TEXT DEFAULT '',   -- comma-separated
            glycemic_index  REAL,
            inserted_at     TEXT DEFAULT (datetime('now'))
        );
 
        CREATE TABLE IF NOT EXISTS ingest_log (
            run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  TEXT DEFAULT (datetime('now')),
            records_in  INTEGER,
            records_new INTEGER,
            status      TEXT
        );
 
        CREATE UNIQUE INDEX IF NOT EXISTS ux_foods_fdc ON foods(fdc_id);
        CREATE INDEX IF NOT EXISTS ix_foods_category ON foods(food_category);
    """)
    conn.commit()
 
 
def fetch_page(api_key: str, data_type: str, page: int, page_size: int = 200) -> list[dict]:
    """Fetch one page of search results from USDA FoodData Central."""
    url = f"{API_BASE}/foods/search"
    params = {
        "api_key":    api_key,
        "query":      "*",
        "dataType":   data_type,
        "pageSize":   page_size,
        "pageNumber": page,
    }
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r.json().get("foods", [])
        except requests.RequestException as e:
            log.warning(f"Page {page} attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    return []
 
 
def parse_nutrients(food: dict) -> dict:
    """Extract the nutrients we care about from a raw USDA food record."""
    nutrient_map = {n["nutrientId"]: n.get("value", 0.0) for n in food.get("foodNutrients", [])}
    return {col: nutrient_map.get(nid, None) for col, nid in NUTRIENT_IDS.items()}
 
 
def upsert_food(conn: sqlite3.Connection, food: dict) -> bool:
    """Insert a food record; returns True if it was a new row."""
    nutrients = parse_nutrients(food)
    row = {
        "fdc_id":       food["fdcId"],
        "description":  food.get("description", ""),
        "data_type":    food.get("dataType", ""),
        "food_category": food.get("foodCategory", ""),
        **nutrients,
    }
    cursor = conn.execute("""
        INSERT OR IGNORE INTO foods
            (fdc_id, description, data_type, food_category,
             calories, protein, carbs, fat, fiber,
             iron, calcium, vitamin_b12, vitamin_d, zinc,
             sodium, potassium, magnesium)
        VALUES
            (:fdc_id, :description, :data_type, :food_category,
             :calories, :protein, :carbs, :fat, :fiber,
             :iron, :calcium, :vitamin_b12, :vitamin_d, :zinc,
             :sodium, :potassium, :magnesium)
    """, row)
    return cursor.rowcount == 1
 
 
def run_ingest(api_key: str, target: int = 12000) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    create_schema(conn)
 
    # Resume: count existing rows so we don't re-pull what we already have
    existing = conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0]
    log.info(f"Existing records in DB: {existing:,} — target: {target:,}")
    if existing >= target:
        log.info("Target already reached. Skipping ingest.")
        conn.close()
        return
 
    total_in = 0
    total_new = existing  # start from current count so we stop at the right place
 
    for data_type in DATA_TYPES:
        page = 1
        log.info(f"Starting ingest for data_type={data_type}")
        while total_new < target:
            foods = fetch_page(api_key, data_type, page)
            if not foods:
                log.info(f"  No more pages for {data_type} at page {page}")
                break
            for food in foods:
                total_in += 1
                if upsert_food(conn, food):
                    total_new += 1
            conn.commit()
            log.info(f"  [{data_type}] page={page} fetched={len(foods)} new={total_new}/{target}")
            page += 1
            time.sleep(0.25)  # stay well under USDA rate limit (1000 req/hr)
 
    conn.execute(
        "INSERT INTO ingest_log (records_in, records_new, status) VALUES (?,?,?)",
        (total_in, total_new, "complete"),
    )
    conn.commit()
    conn.close()
    log.info(f"Ingest complete. Total fetched={total_in}, new records={total_new}")
 
 
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--limit", type=int, default=12000)
    args = parser.parse_args()
    run_ingest(args.api_key, args.limit)