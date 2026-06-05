"""
filters/tag.py
--------------
Tags every food in the DB with clinical and dietary constraint flags.
 
Columns set:
  - is_high_fodmap     (IBS)
  - is_gerd_trigger    (GERD)
  - allergens          (comma-separated allergen labels)
  - glycemic_index     (where known)
  - dietary_category   (vegan | vegetarian | pescetarian | non-vegetarian)
 
Run after dedupe:
    python -m data_pipeline.filters.tag
"""
 
import logging
import sqlite3
from pathlib import Path
 
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
 
DB_PATH = Path(__file__).resolve().parents[2] / "data" / "foods.db"
 
# ── FODMAP trigger keywords ──────────────────────────────────────────────────
HIGH_FODMAP_KEYWORDS = [
    "garlic", "onion", "leek", "shallot", "scallion",
    "wheat", "rye", "barley", "spelt",
    "apple", "pear", "watermelon", "mango", "cherry", "peach", "plum",
    "honey", "high fructose", "agave",
    "milk", "yogurt", "cottage cheese", "ricotta", "ice cream",
    "kidney bean", "black bean", "chickpea", "lentil",
    "cashew", "pistachio",
    "cauliflower", "mushroom", "asparagus", "artichoke",
]
 
# ── GERD trigger keywords ────────────────────────────────────────────────────
GERD_TRIGGER_KEYWORDS = [
    "tomato", "ketchup", "marinara", "pizza sauce",
    "orange", "lemon", "lime", "grapefruit", "pineapple", "citrus",
    "coffee", "espresso", "caffeine",
    "chocolate", "cocoa",
    "fried", "deep fried", "french fry", "chips",
    "peppermint", "spearmint",
    "alcohol", "wine", "beer", "spirits",
    "spicy", "hot sauce", "chili pepper", "jalapeño", "sriracha", "salsa",
]
 
# ── Allergen keyword → label ─────────────────────────────────────────────────
ALLERGEN_KEYWORDS: dict[str, list[str]] = {
    "gluten":    ["wheat", "rye", "barley", "spelt", "semolina", "farro",
                  "bread", "pasta", "flour tortilla", "cracker", "pretzel",
                  "beer", "malt"],
    "dairy":     ["milk", "cheese", "butter", "cream", "yogurt", "whey",
                  "casein", "lactose", "ghee", "ice cream", "custard", "eggnog", "quiche", "frittata", "souffle", "pizza", "mcflurry", "milkshake"],
    "tree_nuts": ["almond", "cashew", "walnut", "pecan", "pistachio",
                  "hazelnut", "macadamia", "brazil nut", "pine nut"],
    "peanuts":   ["peanut", "groundnut"],
    "shellfish": ["shrimp", "prawn", "crab", "lobster", "crayfish",
                  "scallop", "clam", "oyster", "mussel"],
    "soy":       ["tofu", "tempeh", "edamame", "miso", "natto",
                  "soy milk", "soy sauce", "tamari"],
    "eggs":      ["egg", "mayonnaise", "meringue", "albumin", "eggnog", "quiche", "frittata", "souffle", "pizza", "mcflurry", "milkshake",
                  "deviled egg", "egg salad", "egg sandwich", "egg roll", "egg drop soup",
                  "egg foo young", "scotch egg", "eggs benedict", "shakshuka", "huevos rancheros",
                  "egg curry", "egg biryani", "egg fried rice", "egg noodles", "egg tart",
                  "egg custard", "egg pudding", "egg wash", "egg glaze", "egg white", "egg yolk", "cakecake", "brownie", "cookie", "pancake", "waffle"
                  ,"bread", "pastry", "batter", "dough", "biscuit", "croissant", "danish", "muffin", "scone", "bagel", "doughnut", "donut", "brioche", "roll", "bun", "breadstick",
                  "pizza", "quiche", "frittata", "souffle", "mcflurry", "milkshake"],
    "fish":      ["salmon", "tuna", "cod", "tilapia", "halibut", "anchovy",
                  "sardine", "trout", "bass", "mackerel"],
}
 
# ── GI overrides ─────────────────────────────────────────────────────────────
GI_OVERRIDES: list[tuple[str, float]] = [
    ("white rice", 72), ("white bread", 75), ("baguette", 95),
    ("corn flakes", 81), ("instant oats", 79), ("oat", 57),
    ("whole wheat", 69), ("brown rice", 66), ("quinoa", 53),
    ("lentil", 32), ("chickpea", 33), ("kidney bean", 24),
    ("sweet potato", 63), ("potato", 82), ("carrot", 39),
    ("apple", 39), ("banana", 52), ("watermelon", 76),
    ("orange", 40), ("grape", 59),
    ("milk", 39), ("yogurt", 35), ("ice cream", 62),
    ("chocolate", 45), ("honey", 61), ("sugar", 65),
]
 
# ── Dietary category keywords ─────────────────────────────────────────────────
# Order matters: check non-veg first, then pescetarian, then vegetarian, then vegan
# A food is assigned the MOST RESTRICTIVE category it fits into
 
MEAT_KEYWORDS = [
    "beef", "pork", "lamb", "veal", "venison", "bison", "mutton", "goat",
    "chicken", "turkey", "duck", "goose", "quail", "pheasant",
    "bacon", "ham", "prosciutto", "sausage", "pepperoni", "salami",
    "pastrami", "chorizo", "bratwurst", "hot dog", "bologna",
    "burger", "meatball", "meatloaf", "jerky", "ribs", "steak",
    "pork chop", "chicken breast", "ground beef", "ground turkey",
    "poultry", "game meat", "organ meat", "liver", "kidney meat",
    "lard", "suet", "tallow", "bone broth", "gelatin",
]
 
FISH_SEAFOOD_KEYWORDS = [
    "salmon", "tuna", "cod", "tilapia", "halibut", "anchovy",
    "sardine", "trout", "bass", "mackerel", "herring", "catfish",
    "snapper", "mahi", "swordfish", "flounder", "perch",
    "shrimp", "prawn", "crab", "lobster", "crayfish",
    "scallop", "clam", "oyster", "mussel", "squid", "octopus",
    "fish", "seafood", "shellfish",
]
 
DAIRY_EGG_KEYWORDS = [
    "milk", "cheese", "butter", "cream", "yogurt", "whey",
    "casein", "lactose", "ghee", "ice cream", "custard",
    "egg", "mayonnaise", "meringue", "albumin",
]
 
HONEY_KEYWORDS = ["honey", "beeswax", "royal jelly"]
 
 
def keyword_match(description: str, keywords: list[str]) -> bool:
    desc_lower = description.lower()
    return any(kw in desc_lower for kw in keywords)
 
 
# ── USDA food_category → dietary_category mapping ────────────────────────────
# Checked FIRST — more reliable than keyword matching on coded descriptions
# like "CHOP,PK,BNLS,GM,ET12" which contain no readable meat keywords
 
NON_VEG_CATEGORIES = {
    "meat/poultry/other animals",
    "meat/poultry",
    "poultry products",
    "beef products",
    "pork products",
    "lamb, veal, and game products",
    "sausages and luncheon meats",
    "prepared/processed",        
}
 
FISH_CATEGORIES = {
    "finfish and shellfish products",
    "fish and shellfish",
    "seafood",
}
 
DAIRY_EGG_CATEGORIES = {
    "dairy and egg products",
    "dairy products",
    "egg products",
    "cheese",
}
 
VEGAN_CATEGORIES = {
    "fruits and fruit juices",
    "vegetables and vegetable products",
    "legumes and legume products",
    "nut and seed products",
    "grains and pasta",
    "cereal grains and pasta",
    "breakfast cereals",
    "baked products",
    "fats and oils",
    "beverages",
    "spices and herbs",
}
 
 
def detect_dietary_category(description: str, food_category: str = "") -> str:
    """
    Returns one of: vegan | vegetarian | pescetarian | non-vegetarian
 
    Checks USDA food_category first (catches coded names like CHOP,PK,BNLS),
    then falls back to keyword matching on the description.
    """
    cat_lower = food_category.lower().strip()
 
    # Category-based detection (most reliable)
    if cat_lower:
        # Non-veg category match — also catches "Meat/Poultry Prepared/Processed"
        if any(nv in cat_lower for nv in NON_VEG_CATEGORIES):
            return "non-vegetarian"
        if any(fc in cat_lower for fc in FISH_CATEGORIES):
            return "pescetarian"
        if any(dc in cat_lower for dc in DAIRY_EGG_CATEGORIES):
            return "vegetarian"
        if any(vc in cat_lower for vc in VEGAN_CATEGORIES):
            return "vegan"
 
    # Fallback: keyword match on description
    has_meat      = keyword_match(description, MEAT_KEYWORDS)
    has_fish      = keyword_match(description, FISH_SEAFOOD_KEYWORDS)
    has_dairy_egg = keyword_match(description, DAIRY_EGG_KEYWORDS)
    has_honey     = keyword_match(description, HONEY_KEYWORDS)
 
    if has_meat:
        return "non-vegetarian"
    if has_fish:
        return "pescetarian"
    if has_dairy_egg or has_honey:
        return "vegetarian"
    return "vegan"
 
 
def detect_allergens(description: str) -> str:
    found = [
        allergen for allergen, keywords in ALLERGEN_KEYWORDS.items()
        if keyword_match(description, keywords)
    ]
    return ",".join(found)
 
 
def estimate_gi(description: str) -> float | None:
    desc_lower = description.lower()
    for keyword, gi in GI_OVERRIDES:
        if keyword in desc_lower:
            return gi
    return None
 
 
def add_dietary_column(conn: sqlite3.Connection) -> None:
    """Add dietary_category column if it doesn't exist yet."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(foods)").fetchall()]
    if "dietary_category" not in cols:
        conn.execute("ALTER TABLE foods ADD COLUMN dietary_category TEXT DEFAULT 'vegan'")
        conn.commit()
        log.info("Added dietary_category column to foods table")
 
 
def run_tagging() -> None:
    conn = sqlite3.connect(DB_PATH)
    add_dietary_column(conn)
 
    rows = conn.execute("SELECT fdc_id, description, food_category FROM foods").fetchall()
 
    counts = {"vegan": 0, "vegetarian": 0, "pescetarian": 0, "non-vegetarian": 0}
    fodmap_count = gerd_count = allergen_count = gi_count = 0
 
    for fdc_id, description, food_category in rows:
        is_fodmap  = int(keyword_match(description, HIGH_FODMAP_KEYWORDS))
        is_gerd    = int(keyword_match(description, GERD_TRIGGER_KEYWORDS))
        allergens  = detect_allergens(description)
        gi         = estimate_gi(description)
        diet_cat   = detect_dietary_category(description, food_category or "")
 
        conn.execute("""
            UPDATE foods SET
                is_high_fodmap   = ?,
                is_gerd_trigger  = ?,
                allergens        = ?,
                glycemic_index   = ?,
                dietary_category = ?
            WHERE fdc_id = ?
        """, (is_fodmap, is_gerd, allergens, gi, diet_cat, fdc_id))
 
        counts[diet_cat] += 1
        if is_fodmap:   fodmap_count  += 1
        if is_gerd:     gerd_count    += 1
        if allergens:   allergen_count += 1
        if gi:          gi_count      += 1
 
    conn.commit()
    conn.close()
 
    total = len(rows)
    log.info(f"Tagged {total:,} foods:")
    log.info(f"  Vegan:            {counts['vegan']:,}  ({counts['vegan']/total:.1%})")
    log.info(f"  Vegetarian:       {counts['vegetarian']:,}  ({counts['vegetarian']/total:.1%})")
    log.info(f"  Pescetarian:      {counts['pescetarian']:,}  ({counts['pescetarian']/total:.1%})")
    log.info(f"  Non-vegetarian:   {counts['non-vegetarian']:,}  ({counts['non-vegetarian']/total:.1%})")
    log.info(f"  FODMAP flagged:   {fodmap_count:,}")
    log.info(f"  GERD flagged:     {gerd_count:,}")
    log.info(f"  Allergen tagged:  {allergen_count:,}")
    log.info(f"  GI estimated:     {gi_count:,}")
 
 
if __name__ == "__main__":
    run_tagging()
 