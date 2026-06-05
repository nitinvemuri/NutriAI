"""
filters/query.py
----------------
Query helpers for food filtering with full dietary preference support.

Dietary modes supported:
  - vegan           → only vegan foods
  - vegetarian      → vegan + vegetarian foods
  - pescetarian     → vegan + vegetarian + pescetarian foods
  - non-vegetarian  → all foods (no restriction)

Mixed household support:
  Call filter_foods() separately per meal with different dietary_preference values.
  e.g. breakfast=vegan, dinner=non-vegetarian

Religious/cultural constraints supported via avoid_allergens + dietary_preference.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "foods.db"

# What each dietary mode is ALLOWED to eat (inclusive hierarchy)
DIETARY_ALLOWED: dict[str, list[str]] = {
    "vegan":           ["vegan"],
    "vegetarian":      ["vegan", "vegetarian"],
    "pescetarian":     ["vegan", "vegetarian", "pescetarian"],
    "non-vegetarian":  ["vegan", "vegetarian", "pescetarian", "non-vegetarian"],
}

DIETARY_STRICTNESS = {
    "vegan": 0,
    "vegetarian": 1,
    "pescetarian": 2,
    "non-vegetarian": 3,
}

MEAT_KEYWORDS = [
    "beef", "pork", "lamb", "veal", "venison", "game", "mutton", "goat",
    "chicken", "turkey", "duck", "goose", "meat", "poultry",
    "bacon", "ham", "sausage", "pepperoni", "salami", "pastrami",
    "burger", "meatball", "meatloaf", "jerky", "ribs", "hot dog",
    "steak", "lard", "gelatin",
]

FISH_SEAFOOD_KEYWORDS = [
    "fish", "salmon", "tuna", "cod", "tilapia", "halibut", "anchovy",
    "sardine", "trout", "bass", "mackerel", "shrimp", "seafood",
    "shellfish", "crab", "lobster", "clam", "oyster", "mussel",
]

DAIRY_EGG_KEYWORDS = [
    "milk", "cheese", "yogurt", "cream", "butter", "whey", "casein",
    "lactose", "ghee", "ice cream", "custard", "egg", "mayonnaise",
    "meringue", "albumin", "Eggnog", "quiche", "frittata", "souffle", "pizza",
    "egg salad", "egg sandwich", "deviled egg", "egg roll", "egg drop soup",
    "egg foo young", "scotch egg", "eggs benedict", "shakshuka", "huevos rancheros",
    "egg curry", "egg biryani", "egg fried rice", "egg noodles", "egg tart",
    "egg custard", "egg pudding", "egg wash", "egg glaze", "egg white", "egg yolk"
]

DIETARY_EXCLUDE_KEYWORDS: dict[str, list[str]] = {
    "vegan": MEAT_KEYWORDS + FISH_SEAFOOD_KEYWORDS + DAIRY_EGG_KEYWORDS + ["honey"],
    "vegetarian": MEAT_KEYWORDS + FISH_SEAFOOD_KEYWORDS,
    "pescetarian": MEAT_KEYWORDS,
    "non-vegetarian": [],
}

# Religious/cultural presets — maps to allergen exclusions + dietary mode
CULTURAL_PRESETS: dict[str, dict] = {
    "halal": {
        "dietary_preference": "non-vegetarian",
        "avoid_keywords": ["pork", "bacon", "ham", "lard", "gelatin",
                           "prosciutto", "pancetta", "chorizo", "pepperoni",
                           "salami", "bratwurst", "pork chop", "pulled pork",
                           "pork loin", "pork shoulder", "alcohol", "wine",
                           "beer", "spirits"],
    },
    "kosher": {
        "dietary_preference": "non-vegetarian",
        "avoid_keywords": ["pork", "bacon", "ham", "lard", "shellfish",
                           "shrimp", "crab", "lobster", "clam", "oyster"],
    },
    "hindu": {
        "dietary_preference": "non-vegetarian",
        "avoid_keywords": ["beef", "veal", "steak", "pork", "burger", "ham", "sausage", "pepperoni", 
                           "salami", "pastrami","ribs", "hot dog", "meatball", "meatloaf", "bacon","quarter-pounder", "quarter pounder",
                           "big mac", "meat loaf", "meat sauce", "bolognese", "beef stew", "chili con carne", "shepherd's pie", "philly cheesesteak", "pastrami", "Big Mac", "meat sauce", "bolognese", "beef stew", "chili con carne", "shepherd's pie", "meatloaf",
                           "rib", "filet", "tenderloin", "sirloin", "Heart", "Kidney", "tongue", "toungue","oz","armadillo", "NFS + Armadillo"],
    },
    "jain": {
        "dietary_preference": "vegan",
        "avoid_keywords": ["onion", "garlic", "leek", "shallot",
                           "potato", "carrot", "beet", "radish", "turnip"],
    },
}

ALLOWED_ALLERGENS = {
    "gluten", "dairy", "tree_nuts", "peanuts",
    "shellfish", "soy", "eggs", "fish", "meat",
}

# Allergen keywords for description-based filtering
ALLERGEN_KEYWORDS: dict[str, list[str]] = {
    "eggs": [
        "egg", "eggnog", "mayonnaise", "meringue", "albumin",
        "quiche", "frittata", "souffle", "egg salad",
        "egg sandwich", "deviled egg", "egg roll", "egg drop soup",
        "egg foo young", "scotch egg", "eggs benedict", "shakshuka",
        "huevos rancheros", "egg curry", "egg biryani", "egg fried rice",
        "egg noodles", "egg tart", "egg custard", "egg pudding",
        "egg wash", "egg glaze", "egg white", "egg yolk", 
        # Foods commonly made with eggs
        "flan", "pudding", "custard", "cake", "cupcake", "brownie", "cookie",
        "pastry","pastries", "crepe", "waffle", "pancake", "mousse", "burrito",
        "soufflé", "tiramisu", "bread pudding", "french toast", "crème brûlée",
        "hollandaise", "aioli", "ceviche", "Basbousa", "Revani", "Melopita", "Galaktoboureko", "Bao Bun"
    ],
    "dairy": [
        "milk", "cheese", "yogurt", "cream", "butter", "whey", "casein", "mocha", "frappuccino", "enchilada", "lasagna", "alfredo", "parmesan", "mozzarella", "brie", "cheddar",
        "lactose", "ghee", "ice cream", "pizza", "quiche", "kefir", "pastries", "custard", "eggnog", "frittata", "souffle", "mcflurry", "milkshake",
        "alfredo", "parmesan", "mozzarella", "brie", "cheddar", "flan","pudding", "custard", "cake", "cupcake", "brownie", "cookie", "creme brulee", "pastry","pastries", "crepe", "waffle", "pancake", "mousse","tiramisu", "bread pudding", "french toast", "hollandaise", "aioli", "ceviche", "Basbousa", "Revani", "Melopita", "Galaktoboureko", "Bao Bun",
        "cream sauce", "mac and cheese", "lasagna", "risotto", "sundae","milkshake", "custard", "frosting", "cream cheese", "blue cheese", "ricotta", "cottage cheese", "gorgonzola", "provolone", "fontina",
        "mascarpone", "gruyere", "camembert", "pecorino", "manchego", "emmental", "comté", "roquefort", "stracciatella", "burrata", "paneer", "queso fresco", "queso blanco", "labneh", "clotted cream", "double cream", "single cream",
        "crème fraîche", "crema", "kibby", "boba", "cheese", "yogurt", "mcflurry", "frosting", "fudgesicle","gelato", "kulfi",  "parfait"
    ],
    "shellfish": [
        "shrimp", "prawn", "crab", "lobster", "clam", "oyster", "mussel",
        "scallop", "squid", "octopus", "seafood", "shellfish"   
    ],
    "fish": [
        "fish", "salmon", "tuna", "cod", "tilapia", "halibut", "anchovy","caviar", "roe", "sturgeon", "herring", "grouper", "mahi mahi", "NFS + Escargot", "shrimp", "shellfish", "crab", "lobster", "clam", "oyster", "mussel", 
        "sardine", "trout", "bass", "mackerel", "swordfish", "snapper","fldr", "frog", "caviar", "roe", "sturgeon", "herring", "grouper", "CORVINA", "sea bass", "flounder", "sole", "whiting", "pollock", "catfish", "perch", "barramundi", "tilefish", "lingcod", "rockfish", "orange roughy",
        "sea bass", "flounder", "sole", "whiting", "pollock", "catfish", "perch", "barramundi", "tilefish", "lingcod", "rockfish", "orange roughy",
        "bluefish", "albacore", "yellowfin", "bigeye", "skipjack", "black cod", "black sea bass", "kingfish", "spanish mackerel", "wahoo", "cobia", "butterfish", "sablefish", "monkfish", "grenadier", "capelin", "smelt", "surf clam", "geoduck"
    ],
    "meat": [
        "beef", "pork", "lamb", "veal", "venison", "game", "mutton", "goat", "Cuban Sandwich", "gyro", "shawarma", "kebab", "meat", "poultry",
        "chicken", "turkey", "duck", "goose", "meat", "poultry", "chili","corn dog","frog legs", "alligator", "snake", "insect", "cricket", "mealworm",
        "bacon", "ham", "sausage", "pepperoni", "salami", "pastrami", "Bear", "prosciutto", "pancetta", "chorizo", "bratwurst", "pork chop", "pulled pork",
        "burger", "meatball", "meatloaf", "jerky", "ribs", "hot dog", "steak","quarter pounder", "quarter-pounder", "big mac", "meat sauce", "bolognese", "beef stew", "chili con carne", "shepherd's pie", "meatloaf", "meatball sub",
        "brisket", "short rib", "corned beef", "osso buco", "lamb", "adobo", "carnitas", "barbacoa", "big-mac", "meat sauce", "bolognese", "beef stew", "chili con carne", "shepherd's pie", "meatloaf",
        "philly cheesesteak", "pastrami", "Big Mac", "meat sauce", "bolognese", "beef stew", "chili con carne", "shepherd's pie", "meatloaf", "meatball sub",
        "rib", "filet", "tenderloin", "sirloin", "Heart", "Kidney", "toungue","oz","armadillo", "NFS + Armadillo"
        "ribeye", "porterhouse", "T-bone", "steak", "chuck roast", "arm roast", "rump roast", 
        "top round", "bottom round", "eye of round", "brisket", "short ribs", "oxtail", "kibby"
    ],
    "peanuts": ["peanut", "peanuts"],
    "tree_nuts": ["nut", "nuts", "almond", "walnut", "cashew", "pecan", "pistachio"],
    "soy": ["soy", "tofu", "edamame", "tempeh", "soybean"],
    "gluten": ["wheat", "gluten", "barley", "rye", "bread", "pasta"],
}

# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE 1: ALLERGEN CONFIDENCE SCORING
# ═══════════════════════════════════════════════════════════════════════════════
ALLERGEN_CONFIDENCE = {
    "usda_certified": 100,    # From database allergen tags
    "keyword_match": 80,      # From description keyword search
}

# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE 2: TREE NUT SPECIFICITY (expanded from generic "tree_nuts")
# ═══════════════════════════════════════════════════════════════════════════════
TREE_NUTS_SPECIFIC = {
    "almond": ["almond"],
    "cashew": ["cashew"],
    "walnut": ["walnut"],
    "pistachio": ["pistachio"],
    "pecan": ["pecan"],
    "hazelnut": ["hazelnut", "filbert"],
    "macadamia": ["macadamia"],
    "brazil_nut": ["brazil nut", "brazil"],
    "pine_nut": ["pine nut", "pinon"],
}

# Expand tree_nuts in ALLERGEN_KEYWORDS to include all specific nuts
ALLERGEN_KEYWORDS["tree_nuts"] = [
    "nut", "nuts", "almond", "walnut", "cashew", "pecan", "pistachio",
    "hazelnut", "filbert", "macadamia", "brazil nut", "brazil", "pine nut", "pinon"
]

# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE 3: ALLERGEN SEVERITY LEVELS
# ═══════════════════════════════════════════════════════════════════════════════
ALLERGEN_SEVERITY = {
    "peanuts": "SEVERE",      # 🔴 High anaphylaxis risk
    "tree_nuts": "SEVERE",    # 🔴 High anaphylaxis risk
    "shellfish": "SEVERE",    # 🔴 High anaphylaxis risk
    "fish": "MODERATE",       # 🟡 Can cause reactions but typically less severe
    "dairy": "MODERATE",      # 🟡 Intolerance/allergy varies
    "eggs": "MODERATE",       # 🟡 Intolerance/allergy varies
    "soy": "MODERATE",        # 🟡 Generally manageable
    "gluten": "MODERATE",     # 🟡 Intolerance (celiac) vs. preference
    "meat": "MODERATE",       # 🟡 Dietary preference
}

GI_OPTIONS = {"low", "high", "any"}

DEFAULT_COLUMNS = [
    "fdc_id", "description", "data_type", "food_category",
    "calories", "protein", "carbs", "fat", "fiber",
    "iron", "calcium", "vitamin_b12", "vitamin_d", "zinc",
    "sodium", "potassium", "magnesium",
    "is_high_fodmap", "is_gerd_trigger", "allergens",
    "glycemic_index", "dietary_category",
]


def _get_conn() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Database not found at {DB_PATH}\n"
            f"Run the pipeline first: python 'final project 423.py' --api-key YOUR_KEY"
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _normalize_allergens(allergens: Iterable[str]) -> list[str]:
    normalized = []
    for allergen in allergens:
        if allergen is None:
            continue
        name = allergen.strip().lower().replace(" ", "_")
        if name not in ALLOWED_ALLERGENS:
            raise ValueError(f"Unsupported allergen '{allergen}'. Supported: {sorted(ALLOWED_ALLERGENS)}")
        normalized.append(name)
    return normalized


def _combine_dietary_preferences(
    dietary_preference: str | None,
    preset_dietary_preference: str | None,
) -> str | None:
    """Return the stricter diet when both user and cultural presets provide one."""
    if not dietary_preference:
        return preset_dietary_preference
    if not preset_dietary_preference:
        return dietary_preference
    user_diet = dietary_preference.lower().replace(" ", "-")
    preset_diet = preset_dietary_preference.lower().replace(" ", "-")
    if user_diet not in DIETARY_STRICTNESS or preset_diet not in DIETARY_STRICTNESS:
        return dietary_preference
    return user_diet if DIETARY_STRICTNESS[user_diet] <= DIETARY_STRICTNESS[preset_diet] else preset_diet


def get_diet_exclude_keywords(dietary_preference: str | None) -> list[str]:
    """Return keywords that make a food unsafe for a dietary preference."""
    if not dietary_preference:
        return []
    diet_lower = dietary_preference.lower().replace(" ", "-")
    return DIETARY_EXCLUDE_KEYWORDS.get(diet_lower, [])


def get_diet_violations(food: dict, dietary_preference: str | None) -> list[str]:
    """Return diet rule violations from both tags and description keywords."""
    if not dietary_preference:
        return []

    diet_lower = dietary_preference.lower().replace(" ", "-")
    allowed = DIETARY_ALLOWED.get(diet_lower)
    if not allowed:
        return []

    violations = []
    if food.get("dietary_category") not in allowed:
        violations.append(
            f"Dietary category '{food.get('dietary_category') or 'unknown'}' is not allowed for {diet_lower}."
        )

    desc_lower = str(food.get("description") or "").lower()
    matched_keywords = [
        kw for kw in get_diet_exclude_keywords(diet_lower)
        if kw in desc_lower
    ]
    if matched_keywords:
        violations.append(f"Diet keyword exclusion matched: {', '.join(matched_keywords[:5])}.")

    return violations


def food_matches_diet(food: dict, dietary_preference: str | None) -> bool:
    """True when a food passes all tag and keyword checks for a diet."""
    return not get_diet_violations(food, dietary_preference)


# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE 1: GET ALLERGEN DETECTION CONFIDENCE
# ═══════════════════════════════════════════════════════════════════════════════

def get_allergen_confidence(food: dict, allergen: str) -> dict[str, int | str]:
    """Return allergen detection method and confidence score."""
    allergen_lower = allergen.lower().replace(" ", "_")
    usda_tags = str(food.get("allergens") or "").lower()
    
    # Check USDA certified tags first
    if allergen_lower in usda_tags or allergen.lower() in usda_tags:
        return {
            "method": "USDA_CERTIFIED",
            "confidence": ALLERGEN_CONFIDENCE["usda_certified"],
            "label": "100% - USDA certified tag"
        }
    
    # Check keyword match in description
    keywords = ALLERGEN_KEYWORDS.get(allergen_lower, [])
    desc_lower = str(food.get("description") or "").lower()
    if any(kw in desc_lower for kw in keywords):
        return {
            "method": "KEYWORD_MATCH",
            "confidence": ALLERGEN_CONFIDENCE["keyword_match"],
            "label": "80% - keyword detected"
        }
    
    return {
        "method": "NONE",
        "confidence": 0,
        "label": "Not detected"
    }


# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE 2: GET ALLERGEN SEVERITY INDICATOR
# ═══════════════════════════════════════════════════════════════════════════════

def get_allergen_severity(allergen: str) -> dict[str, str]:
    """Return severity level and emoji indicator."""
    allergen_lower = allergen.lower().replace(" ", "_")
    severity = ALLERGEN_SEVERITY.get(allergen_lower, "MODERATE")
    
    emoji = "🔴" if severity == "SEVERE" else "🟡"
    
    return {
        "severity": severity,
        "emoji": emoji,
        "description": "High anaphylaxis risk" if severity == "SEVERE" else "Manageable/intolerance"
    }


# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE 3: GET SPECIFIC TREE NUT ALLERGEN
# ═══════════════════════════════════════════════════════════════════════════════

def get_tree_nuts_options() -> dict[str, list[str]]:
    """Return all specific tree nut options for granular filtering."""
    return TREE_NUTS_SPECIFIC


def is_tree_nut(allergen: str) -> bool:
    """Check if allergen is a specific tree nut."""
    allergen_lower = allergen.lower().replace(" ", "_")
    return allergen_lower in TREE_NUTS_SPECIFIC


def normalize_allergens_with_specificity(allergens: Iterable[str]) -> list[str]:
    """Normalize allergens, expanding 'tree_nuts' to specific nuts if present."""
    normalized = []
    has_generic_tree_nuts = False
    
    for allergen in allergens:
        if allergen is None:
            continue
        name = allergen.strip().lower().replace(" ", "_")
        
        # Check if it's a specific tree nut
        if name in TREE_NUTS_SPECIFIC:
            normalized.append(name)
        elif name == "tree_nuts":
            has_generic_tree_nuts = True
        elif name in ALLOWED_ALLERGENS:
            normalized.append(name)
        else:
            raise ValueError(f"Unsupported allergen '{allergen}'. Supported: {sorted(ALLOWED_ALLERGENS)} or specific tree nuts: {sorted(TREE_NUTS_SPECIFIC.keys())}")
    
    # If user selected generic "tree_nuts", expand to all specific nuts
    if has_generic_tree_nuts:
        normalized.extend(TREE_NUTS_SPECIFIC.keys())
    
    return list(set(normalized))  # Remove duplicates


def _build_where_clause(
    dietary_preference: str | None = None,
    cultural_preset: str | None = None,
    exclude_fodmap: bool = False,
    exclude_gerd: bool = False,
    avoid_allergens: Iterable[str] | None = None,
    glycemic_target: str | None = None,
    min_calories: float | None = None,
    max_calories: float | None = None,
    min_protein: float | None = None,
    max_carbs: float | None = None,
    max_sodium: float | None = None,
    food_category: str | None = None,
    search_term: str | None = None,
) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []

    # ── Dietary preference — filter on the pre-tagged dietary_category column ──
    # This is a DB column lookup, not slow keyword scanning at query time
    effective_diet = dietary_preference
    avoid_keywords: list[str] = []

    if cultural_preset:
        preset = CULTURAL_PRESETS.get(cultural_preset.lower())
        if preset:
            effective_diet = _combine_dietary_preferences(
                dietary_preference,
                preset["dietary_preference"],
            )
            avoid_keywords = preset.get("avoid_keywords", [])
        else:
            raise ValueError(f"Unknown cultural preset '{cultural_preset}'. "
                           f"Supported: {list(CULTURAL_PRESETS.keys())}")

    if effective_diet:
        diet_lower = effective_diet.lower().replace(" ", "-")
        allowed = DIETARY_ALLOWED.get(diet_lower)
        if not allowed:
            raise ValueError(
                f"Unknown dietary_preference '{effective_diet}'. "
                f"Use: {list(DIETARY_ALLOWED.keys())}"
            )
        placeholders = ",".join("?" * len(allowed))
        clauses.append(f"dietary_category IN ({placeholders})")
        params.extend(allowed)
        for kw in get_diet_exclude_keywords(diet_lower):
            clauses.append("LOWER(description) NOT LIKE ?")
            params.append(f"%{kw}%")

    # ── Cultural keyword exclusions (halal pork-free, kosher, etc.) ──────────
    for kw in avoid_keywords:
        clauses.append("LOWER(description) NOT LIKE ?")
        params.append(f"%{kw}%")

    # ── Exclude infant formula (not appropriate for general meal planning) ───────
    clauses.append("LOWER(description) NOT LIKE ?")
    params.append("%infant formula%")

    # ── Clinical filters ──────────────────────────────────────────────────────
    if exclude_fodmap:
        clauses.append("is_high_fodmap = 0")

    if exclude_gerd:
        clauses.append("is_gerd_trigger = 0")

    # ── Allergen exclusions ───────────────────────────────────────────────────
    if avoid_allergens:
        for allergen in _normalize_allergens(avoid_allergens):
            # Check both the allergens column AND description keywords
            clauses.append("allergens NOT LIKE ?")
            params.append(f"%{allergen}%")
            
            # Also exclude based on description keywords
            keywords = ALLERGEN_KEYWORDS.get(allergen, [])
            for kw in keywords:
                clauses.append("LOWER(description) NOT LIKE ?")
                params.append(f"%{kw}%")

    # ── Glycemic index ────────────────────────────────────────────────────────
    if glycemic_target:
        target = glycemic_target.strip().lower()
        if target not in GI_OPTIONS:
            raise ValueError(f"glycemic_target must be one of {sorted(GI_OPTIONS)}")
        if target == "low":
            clauses.append("glycemic_index IS NOT NULL AND glycemic_index <= 55")
        elif target == "high":
            clauses.append("glycemic_index IS NOT NULL AND glycemic_index > 55")

    # ── Nutrition range filters ───────────────────────────────────────────────
    if min_calories is not None:
        clauses.append("calories >= ?")
        params.append(min_calories)
    if max_calories is not None:
        clauses.append("calories <= ?")
        params.append(max_calories)
    if min_protein is not None:
        clauses.append("protein >= ?")
        params.append(min_protein)
    if max_carbs is not None:
        clauses.append("carbs <= ?")
        params.append(max_carbs)
    if max_sodium is not None:
        clauses.append("sodium <= ?")
        params.append(max_sodium)

    # ── Category and search ───────────────────────────────────────────────────
    if food_category:
        clauses.append("food_category = ?")
        params.append(food_category.strip())
    if search_term:
        clauses.append("LOWER(description) LIKE ?")
        params.append(f"%{search_term.strip().lower()}%")

    where_clause = " AND ".join(clauses) if clauses else "1=1"
    return where_clause, params


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def filter_foods(
    dietary_preference: str | None = None,
    cultural_preset: str | None = None,
    exclude_fodmap: bool = False,
    exclude_gerd: bool = False,
    avoid_allergens: Iterable[str] | None = None,
    glycemic_target: str | None = None,
    min_calories: float | None = None,
    max_calories: float | None = None,
    min_protein: float | None = None,
    max_carbs: float | None = None,
    max_sodium: float | None = None,
    food_category: str | None = None,
    search_term: str | None = None,
    limit: int | None = None,
    order_by: str = "description",
) -> list[dict]:
    """
    Return filtered foods. Key params:

    dietary_preference: 'vegan' | 'vegetarian' | 'pescetarian' | 'non-vegetarian'
    cultural_preset:    'halal' | 'kosher' | 'hindu' | 'jain'

    For mixed households, call this function once per meal slot with
    the appropriate dietary_preference for that meal.
    """
    if order_by not in DEFAULT_COLUMNS:
        raise ValueError(f"Invalid order_by column '{order_by}'")

    where_clause, params = _build_where_clause(
        dietary_preference=dietary_preference,
        cultural_preset=cultural_preset,
        exclude_fodmap=exclude_fodmap,
        exclude_gerd=exclude_gerd,
        avoid_allergens=avoid_allergens,
        glycemic_target=glycemic_target,
        min_calories=min_calories,
        max_calories=max_calories,
        min_protein=min_protein,
        max_carbs=max_carbs,
        max_sodium=max_sodium,
        food_category=food_category,
        search_term=search_term,
    )

    conn = _get_conn()
    if limit is None:
        sql = (
            f"SELECT {', '.join(DEFAULT_COLUMNS)} FROM foods "
            f"WHERE {where_clause} ORDER BY {order_by}"
        )
        rows = conn.execute(sql, params).fetchall()
    else:
        sql = (
            f"SELECT {', '.join(DEFAULT_COLUMNS)} FROM foods "
            f"WHERE {where_clause} ORDER BY {order_by} LIMIT ?"
        )
        rows = conn.execute(sql, [*params, limit]).fetchall()
    conn.close()

    log.info(f"filter_foods({dietary_preference=}, {cultural_preset=}) → {len(rows)} rows")
    return [_row_to_dict(row) for row in rows]


def filter_mixed_household(
    meal_preferences: dict[str, str],
    **shared_kwargs,
) -> dict[str, list[dict]]:
    """
    Filter foods for a mixed household where different meals have different diets.

    Args:
        meal_preferences: dict mapping meal name → dietary preference
            e.g. {"breakfast": "vegan", "lunch": "vegetarian", "dinner": "non-vegetarian"}
        **shared_kwargs: any other filter_foods kwargs applied to all meals
            e.g. exclude_fodmap=True, avoid_allergens=["gluten"]

    Returns:
        dict mapping meal name → list of matching foods
    """
    return {
        meal: filter_foods(dietary_preference=pref, **shared_kwargs)
        for meal, pref in meal_preferences.items()
    }


def count_foods(**kwargs) -> int:
    """Return count of foods matching the given constraints."""
    where_clause, params = _build_where_clause(**{
        k: v for k, v in kwargs.items()
        if k in _build_where_clause.__code__.co_varnames
    })
    conn = _get_conn()
    count = conn.execute(
        f"SELECT COUNT(*) FROM foods WHERE {where_clause}", params
    ).fetchone()[0]
    conn.close()
    return count


def diagnose() -> None:
    """Print DB stats to debug filtering. Run this first if results seem wrong."""
    print(f"\nDB path: {DB_PATH}")
    print(f"DB exists: {DB_PATH.exists()}")
    if not DB_PATH.exists():
        print("ERROR: DB not found. Run the pipeline first.")
        return

    conn = sqlite3.connect(DB_PATH)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(foods)").fetchall()]
    print(f"\nColumns: {cols}")

    total = conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0]
    print(f"\nTotal records: {total:,}")

    if "dietary_category" in cols:
        print("\nDietary breakdown:")
        for cat, count in conn.execute(
            "SELECT dietary_category, COUNT(*) FROM foods GROUP BY dietary_category ORDER BY 2 DESC"
        ).fetchall():
            print(f"  {cat or '(unset)':20s} {count:,}")
    else:
        print("\n⚠ dietary_category column missing — run filters/tag.py first")

    fodmap = conn.execute("SELECT COUNT(*) FROM foods WHERE is_high_fodmap=1").fetchone()[0]
    gerd   = conn.execute("SELECT COUNT(*) FROM foods WHERE is_gerd_trigger=1").fetchone()[0]
    allerg = conn.execute("SELECT COUNT(*) FROM foods WHERE allergens != ''").fetchone()[0]
    gi     = conn.execute("SELECT COUNT(*) FROM foods WHERE glycemic_index IS NOT NULL").fetchone()[0]

    print(f"\nFODMAP flagged:   {fodmap:,}")
    print(f"GERD flagged:     {gerd:,}")
    print(f"Allergen tagged:  {allerg:,}")
    print(f"GI values set:    {gi:,}")

    if fodmap == 0:
        print("\n⚠ All flags are 0 — run: python -m data_pipeline.filters.tag")

    conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--dietary-preference",
                        choices=list(DIETARY_ALLOWED.keys()), default=None)
    parser.add_argument("--cultural-preset",
                        choices=list(CULTURAL_PRESETS.keys()), default=None)
    parser.add_argument("--exclude-fodmap",  action="store_true")
    parser.add_argument("--exclude-gerd",    action="store_true")
    parser.add_argument("--avoid-allergens", nargs="*", default=None)
    parser.add_argument("--glycemic-target", choices=["low", "high", "any"], default=None)
    parser.add_argument("--min-protein",     type=float, default=None)
    parser.add_argument("--max-sodium",      type=float, default=None)
    parser.add_argument("--search",          default=None)
    parser.add_argument("--limit",           type=int, default=50)
    parser.add_argument("--diagnose",        action="store_true")
    args = parser.parse_args()

    if args.diagnose:
        diagnose()
        return

    foods = filter_foods(
        dietary_preference=args.dietary_preference,
        cultural_preset=args.cultural_preset,
        exclude_fodmap=args.exclude_fodmap,
        exclude_gerd=args.exclude_gerd,
        avoid_allergens=args.avoid_allergens,
        glycemic_target=args.glycemic_target,
        min_protein=args.min_protein,
        max_sodium=args.max_sodium,
        search_term=args.search,
        limit=args.limit,
    )

    if not foods:
        print("No foods matched. Run with --diagnose to check DB state.")
        return

    for food in foods:
        print(
            f"{food['fdc_id']} | {food['description'][:45]:45s} | "
            f"{food['dietary_category']:16s} | cal={food['calories']} | "
            f"allergens={food['allergens']}"
        )
    print(f"\nTotal: {len(foods)}")


if __name__ == "__main__":
    main()
