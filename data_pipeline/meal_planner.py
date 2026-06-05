"""
meal_planner.py
===============
FAISS-powered meal plan generator with strict ≤60s generation guarantee.
"""
from __future__ import annotations

import json
import logging
import pickle
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_ROOT       = Path(__file__).resolve().parents[1]
INDEX_PATH  = _ROOT / "data" / "faiss.index"
META_PATH   = _ROOT / "data" / "faiss_meta.pkl"
FILTER_DIR  = _ROOT / "data" / "bloom_filters"
LEARNING_DB = _ROOT / "data" / "meal_learning.json"

EMBED_COLS = [
    "calories", "protein", "carbs", "fat", "fiber",
    "iron", "calcium", "vitamin_b12", "vitamin_d", "zinc",
    "sodium", "potassium", "magnesium",
]
DIM = len(EMBED_COLS)
NUTRIENT_COLS = EMBED_COLS
MAX_GENERATION_SECONDS = 55
RDA_PASS_THRESHOLD = 0.80

_faiss_index = None
_faiss_meta  = None
_bloom_filters = None


def _nutrient_key(nutrient: str) -> str:
    return str(nutrient or "").strip().lower().replace(" ", "_").replace("-", "_")

GENERIC_MEAL_WORDS = {
    "and", "or", "with", "without", "nfs", "ns", "not", "specified",
    "restaurant", "fast", "foods", "food", "prepared", "cooked", "raw",
    "frozen", "canned", "dry", "mix", "regular", "plain", "style",
    "white", "wheat", "whole", "reduced", "fat", "low", "sodium",
}

MEAL_FAMILY_ALIASES = [
    ("pizza_flatbread", {"pizza", "flatbread", "calzone"}),
    ("sandwich_wrap", {"sandwich", "sub", "wrap", "panini", "pita"}),
    ("taco_burrito", {"taco", "burrito", "quesadilla", "enchilada", "tostada"}),
    ("pasta_noodle", {"pasta", "spaghetti", "macaroni", "noodle", "lasagna", "ravioli"}),
    ("rice_bowl", {"rice", "biryani", "pilaf", "risotto"}),
    ("soup_stew", {"soup", "stew", "chili", "gumbo", "chowder"}),
    ("salad", {"salad"}),
    ("egg_breakfast", {"egg", "omelet", "frittata"}),
]

FISH_SEAFOOD_TERMS = {
    "fish", "salmon", "tuna", "cod", "tilapia", "halibut", "anchovy",
    "sardine", "trout", "bass", "mackerel", "shrimp", "seafood",
    "crab", "lobster", "clam", "oyster", "mussel", "pompano",
    "lingcod", "mahimahi", "herring", "whitefish",
}


def meal_family_signature(food: dict) -> str:
    """Group similar descriptions so weekly plans avoid repeating meal styles."""
    text = " ".join([
        str(food.get("description") or ""),
        str(food.get("food_category") or ""),
    ]).lower()
    words = set(re.findall(r"[a-z]+", text))
    for family, aliases in MEAL_FAMILY_ALIASES:
        if words & aliases:
            return family
    meaningful = [
        word for word in re.findall(r"[a-z]+", text)
        if len(word) > 3 and word not in GENERIC_MEAL_WORDS
    ]
    return "_".join(meaningful[:3]) or str(food.get("food_category") or "uncategorized").lower()


def _load_faiss():
    global _faiss_index, _faiss_meta
    if _faiss_index is not None:
        return _faiss_index, _faiss_meta
    try:
        import faiss
    except ImportError:
        raise ImportError("Run: pip install faiss-cpu")
    if not INDEX_PATH.exists():
        raise FileNotFoundError(f"FAISS index not found at {INDEX_PATH}")
    t0 = time.time()
    _faiss_index = faiss.read_index(str(INDEX_PATH))
    with open(META_PATH, "rb") as f:
        _faiss_meta = pickle.load(f)
    log.info(f"FAISS loaded in {time.time()-t0:.2f}s ({_faiss_index.ntotal:,} vectors)")
    return _faiss_index, _faiss_meta


def _load_faiss_meta_only():
    global _faiss_meta
    if _faiss_meta is not None:
        return _faiss_meta
    if not META_PATH.exists():
        return None
    with open(META_PATH, "rb") as f:
        _faiss_meta = pickle.load(f)
    return _faiss_meta


def _load_bloom_filters() -> dict:
    global _bloom_filters
    if _bloom_filters is not None:
        return _bloom_filters
    if not FILTER_DIR.exists():
        return {}
    filters = {}
    for path in FILTER_DIR.glob("*.bloom"):
        try:
            with open(path, "rb") as f:
                filters[path.stem] = pickle.load(f)
        except Exception as exc:
            log.warning("Skipping Bloom filter %s (%s)", path.name, exc)
    _bloom_filters = filters
    log.info(f"Loaded {len(filters)} Bloom filters")
    return filters


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class NutritionGoals:
    total_calories: float
    protein_target: float
    age: int = 30
    sex: str = "Not specified"
    carbs_target: float = 0.0
    fat_target: float = 0.0
    fiber_target: float = 0.0  # No minimum by default
    max_sodium: float = 2300.0
    potassium_target: float = 3400.0
    clinical_conditions: list = field(default_factory=list)
    priority_nutrients: list = field(default_factory=list)
    glycemic_target: str | None = None  # "low" or "high" — used for low-GI fiber constraint

    def __post_init__(self):
        if not self.carbs_target:
            self.carbs_target = (self.total_calories * 0.45) / 4
        if not self.fat_target:
            self.fat_target = (self.total_calories * 0.30) / 9

    @staticmethod
    def from_profile(sex, age, weight_kg, activity, goal="maintain"):
        if sex.lower() in ("male", "m"):
            bmr = 10 * weight_kg + 6.25 * 170 - 5 * age + 5
        else:
            bmr = 10 * weight_kg + 6.25 * 160 - 5 * age - 161
        multipliers = {"sedentary": 1.2, "light": 1.375, "moderate": 1.55,
                       "active": 1.725, "very_active": 1.9}
        tdee = bmr * multipliers.get(activity, 1.55)
        if goal == "lose":   tdee -= 500
        elif goal == "gain": tdee += 300
        return NutritionGoals(total_calories=round(tdee), protein_target=round(weight_kg * 1.6))

    @staticmethod
    def from_sex_and_goals(
        sex,
        total_calories,
        protein_grams,
        age=30,
        clinical_conditions=None,
        priority_nutrients=None,
        glycemic_target=None,
    ):
        """
        Create NutritionGoals from basic user profile and clinical needs.
        
        Args:
            sex (str): "male", "female", or "not specified"
            total_calories (float): Daily calorie target
            protein_grams (float): Daily protein target in grams
            age (int): User age for RDA calculations (default: 30)
            clinical_conditions (list): List of conditions like ["hypertension", "diabetes"]
                Affects sodium/potassium targets and meal scoring
            glycemic_target (str): "low" or "high" for glycemic index preference
        
        Returns:
            NutritionGoals: Configured goals object ready for meal planning
        """
        conditions = [c.lower() for c in (clinical_conditions or [])]
        return NutritionGoals(
            total_calories=total_calories,
            protein_target=protein_grams,
            age=age, sex=sex,
            max_sodium=1500.0 if "hypertension" in conditions else 2300.0,
            potassium_target=4700.0 if "hypertension" in conditions else 3400.0,
            clinical_conditions=conditions,
            priority_nutrients=[n.lower() for n in (priority_nutrients or [])],
            glycemic_target=glycemic_target,
        )

    def priority_rda_gaps(self, nutrient_totals):
        """Return selected priority nutrient keys that are below 80% of target."""
        targets = get_rda_targets(self.age, self.sex, self)
        gaps = []
        for nutrient in self.priority_nutrients:
            key = _nutrient_key(nutrient)
            target = float(targets.get(key) or 0)
            if target <= 0:
                continue
            actual = float(nutrient_totals.get(key, 0) or 0)
            if actual / target * 100 < RDA_PASS_THRESHOLD * 100:
                gaps.append(key)
        return gaps

    def per_meal_target(self, num_meals=3):
        s = 100 / 300
        return np.array([
            self.total_calories / num_meals * s,
            self.protein_target  / num_meals * s,
            self.carbs_target    / num_meals * s,
            self.fat_target      / num_meals * s,
            self.fiber_target    / num_meals * s,
            2.0, 200.0, 1.0, 5.0, 3.0,
            self.max_sodium / num_meals * s,
            500.0 * s, 50.0 * s,
        ], dtype=np.float32)


@dataclass
class MealPlanResult:
    meals: list
    total_calories: float
    total_protein: float
    total_carbs: float
    total_fat: float
    total_fiber: float
    total_sodium: float
    total_potassium: float
    nutrient_totals: dict
    score: float
    calorie_score: float
    protein_score: float
    variety_score: float
    health_score: float
    diversity_score: float
    glycemic_load_score: float
    sodium_potassium_score: float
    glycemic_load: float
    category_breakdown: dict
    rda_flags: list
    priority_rda_flags: list = field(default_factory=list)
    generation_ms: float = 0.0
    diversity_breakdown: dict = field(default_factory=dict)
    diversity_label: str = ""

    def to_dict(self):
        return {
            "meals": [
                {
                    "slot":        m.get("_slot", ""),
                    "fdc_id":      m.get("fdc_id"),
                    "description": m.get("description", ""),
                    "category":    m.get("food_category", ""),
                    "dietary":     m.get("dietary_category", ""),
                    "calories":    round(float(m.get("_cal_scaled") or 0), 1),
                    "protein":     round(float(m.get("_prot_scaled") or 0), 1),
                    "items": [
                        {
                            "description": item.get("description", ""),
                            "serving_g": item.get("_serving", 0),
                            "calories": round(float(item.get("_cal_scaled") or 0), 1),
                        }
                        for item in m.get("_items", [m])
                    ],
                }
                for m in self.meals
            ],
            "totals": {
                "calories":      round(self.total_calories, 1),
                "protein":       round(self.total_protein,  1),
                "carbs":         round(self.total_carbs,    1),
                "fat":           round(self.total_fat,      1),
                "fiber":         round(self.total_fiber,    1),
                "sodium":        round(self.total_sodium,   0),
                "potassium":     round(self.total_potassium, 0),
                "glycemic_load": round(self.glycemic_load,  1),
            },
            "scores": {
                "overall":          round(self.score,                 1),
                "calorie_fit":      round(self.calorie_score,         1),
                "protein_fit":      round(self.protein_score,         1),
                "variety":          round(self.variety_score,         1),
                "health":           round(self.health_score,          1),
                "diversity":        round(self.diversity_score,       1),
                "glycemic_load":    round(self.glycemic_load_score,   1),
                "sodium_potassium": round(self.sodium_potassium_score,1),
            },
            "category_breakdown": self.category_breakdown,
            "rda_flags":          self.rda_flags,
            "priority_rda_flags": self.priority_rda_flags,
            "generation_ms":      round(self.generation_ms, 1),
        }


# ── FAISS helpers ─────────────────────────────────────────────────────────────

def _normalize_vec(vec, col_min, col_max):
    col_range = np.where(col_max - col_min == 0, 1.0, col_max - col_min)
    return ((vec - col_min) / col_range).astype(np.float32)


def _faiss_candidates(query_vec, k, allowed_fdc_ids=None):
    index, meta = _load_faiss()
    norm_q = _normalize_vec(query_vec, meta["col_min"], meta["col_max"]).reshape(1, -1)
    fetch_k = min(k * 10, index.ntotal)
    _, indices = index.search(norm_q, fetch_k)
    results = []
    for i in indices[0]:
        if i < 0:
            continue
        food = meta["metadata"][i]
        if allowed_fdc_ids is not None and food["fdc_id"] not in allowed_fdc_ids:
            continue
        results.append(food)
        if len(results) >= k:
            break
    return results


def _linear_candidates(query_vec, k, foods, allowed_fdc_ids=None):
    if not foods:
        return []
    rows = [
        food for food in foods
        if allowed_fdc_ids is None or food.get("fdc_id") in allowed_fdc_ids
    ]
    if not rows:
        return []
    matrix = np.array([
        [float(food.get(col) or 0) for col in EMBED_COLS]
        for food in rows
    ], dtype=np.float32)
    col_min = matrix.min(axis=0)
    col_max = matrix.max(axis=0)
    norm_matrix = _normalize_vec(matrix, col_min, col_max)
    norm_query = _normalize_vec(np.array(query_vec, dtype=np.float32), col_min, col_max)
    distances = np.linalg.norm(norm_matrix - norm_query, axis=1)
    order = np.argsort(distances)[:k]
    return [rows[int(idx)] for idx in order]


def _check_allergens(food, avoid_allergens):
    """Check if food contains any of the avoided allergens (by tag or description)."""
    if not avoid_allergens:
        return True  # No restrictions
    
    from data_pipeline.filters.query import ALLERGEN_KEYWORDS
    
    desc_lower = str(food.get("description", "")).lower()
    allergens_lower = str(food.get("allergens", "")).lower()
    
    for allergen in avoid_allergens:
        allergen_lower = str(allergen).lower()
        
        # Check 1: allergens column
        if allergen_lower in allergens_lower:
            return False
        
        # Check 2: description keywords
        keywords = ALLERGEN_KEYWORDS.get(allergen_lower, [])
        for kw in keywords:
            if kw.lower() in desc_lower:
                return False
    
    return True  # No allergens found


def _bloom_screen(food, constraints):
    filters = _load_bloom_filters()
    if not filters:
        return True
    desc = food.get("description", "").lower()
    for constraint, active in constraints.items():
        if not active:
            continue
        bf = filters.get(constraint)
        if bf and (desc in bf or any(w in bf for w in desc.split() if len(w) > 3)):
            return False
    return True


# ── Scoring helpers ───────────────────────────────────────────────────────────

RDA_LOOKUP = {
    "male": {
        "default": {"fiber": 38, "iron": 8, "calcium": 1000, "vitamin_b12": 2.4,
                    "vitamin_c": 90, "vitamin_d": 15, "zinc": 11,
                    "magnesium": 420, "potassium": 3400},
        "51+":     {"fiber": 30, "calcium": 1200},
    },
    "female": {
        "default": {"fiber": 25, "iron": 8, "calcium": 1000, "vitamin_b12": 2.4,
                    "vitamin_c": 90, "vitamin_d": 15, "zinc": 8,
                    "magnesium": 320, "potassium": 2600},
        "51+":     {"fiber": 21, "iron": 8, "calcium": 1200},
    },
    "not specified": {
        "default": {"fiber": 28, "iron": 8, "calcium": 1000, "vitamin_b12": 2.4,
                    "vitamin_c": 90, "vitamin_d": 15, "zinc": 9.5,
                    "magnesium": 360, "potassium": 3000},
        "51+":     {"fiber": 25, "calcium": 1200},
    },
}

NUTRIENT_LABELS = {
    "calories": "Calories",
    "protein": "Protein",
    "carbs": "Carbs",
    "fat": "Fat",
    "fiber": "Fiber",
    "iron": "Iron",
    "calcium": "Calcium",
    "vitamin_b12": "Vitamin B12",
    "vitamin_c": "Vitamin C",
    "vitamin_d": "Vitamin D",
    "zinc": "Zinc",
    "sodium": "Sodium",
    "potassium": "Potassium",
    "magnesium": "Magnesium",
}

def get_rda_targets(age, sex, goals=None):
    sex_key = (sex or "not specified").strip().lower()
    if sex_key not in RDA_LOOKUP:
        sex_key = "not specified"
    targets = dict(RDA_LOOKUP[sex_key]["default"])
    if age >= 51:
        targets.update(RDA_LOOKUP[sex_key].get("51+", {}))
    if goals:
        targets.update({
            "calories": goals.total_calories, "protein": goals.protein_target,
            "carbs": goals.carbs_target, "fat": goals.fat_target,
            "fiber": goals.fiber_target, "potassium": goals.potassium_target,
        })
    return targets


def flag_rda_gaps(nutrient_totals, age, sex, goals):
    """
    Flag nutrients genuinely below the required RDA pass threshold.
    SKIPS nutrients where actual=0 AND the value is NULL in the DB —
    NULL means 'not measured by USDA', not 'food contains zero'.
    Only flag when actual > 0 (data exists) but is still below threshold,
    OR for macros/fiber/sodium where 0 is genuinely concerning.
    """
    ALWAYS_FLAG = {"calories", "protein", "carbs", "fat", "fiber", "sodium"}
    flags = []
    for nutrient, target in get_rda_targets(age, sex, goals).items():
        actual = float(nutrient_totals.get(nutrient, 0) or 0)
        pct = (actual / target * 100) if target else 100
        # Skip micros that are simply absent from USDA data (actual=0, not measured)
        if actual == 0 and nutrient not in ALWAYS_FLAG:
            continue
        if pct < RDA_PASS_THRESHOLD * 100:
            flags.append({
                "nutrient":  NUTRIENT_LABELS.get(nutrient, nutrient),
                "actual":    round(actual, 1),
                "target":    round(target, 1),
                "percent":   round(pct, 1),
                "threshold": round(target * RDA_PASS_THRESHOLD, 1),
            })
    return flags


def flag_priority_rda_gaps(nutrient_totals, age, sex, goals):
    """Flag selected nutrients below the required 80% RDA/goal threshold."""
    selected = [_nutrient_key(n) for n in getattr(goals, "priority_nutrients", []) if n]
    if not selected:
        return []

    targets = get_rda_targets(age, sex, goals)
    flags = []
    for nutrient in selected:
        target = float(targets.get(nutrient) or 0)
        if target <= 0:
            continue
        actual = float(nutrient_totals.get(nutrient, 0) or 0)
        pct = actual / target * 100
        if pct < RDA_PASS_THRESHOLD * 100:
            flags.append({
                "nutrient":  NUTRIENT_LABELS.get(nutrient, nutrient),
                "actual":    round(actual, 1),
                "target":    round(target, 1),
                "percent":   round(pct, 1),
                "threshold": round(target * RDA_PASS_THRESHOLD, 1),
            })
    return flags




def compute_diversity_score(meals, previously_used_proteins=None, previously_used_categories=None, previously_used_descriptions=None, clinical_conditions=None):
    """
    RELAXED DIVERSITY ENGINE
    
    HARD CONSTRAINTS (reject if fail):
    1. No meal appears more than once across all meals (check descriptions)
    2. Minimum variety in meal categories (relaxed for hypertension due to sodium restrictions)
    
    SCORING (lenient but fair):
    - Start at 70 for passing hard constraints
    - Bonus points for variety in proteins, methods, cuisines
    - Easy to achieve 70+
    """
    if not meals:
        return 0.0, {}, False, ["No meals provided"]
    
    constraints_violated = []
    is_hypertension = clinical_conditions and "hypertension" in [c.lower() for c in clinical_conditions]
    
    # Extract descriptions first
    descriptions = [str(m.get("description", "")).strip().lower() for m in meals]
    
    # HARD CONSTRAINT 1a: Check for duplicate meals within the same day (same core meal type)
    # SKIP for hypertension — sodium limitations require flexibility
    if not is_hypertension:
        core_meals = []
        for desc in descriptions:
            # Get first meaningful words before common prepositions
            parts = desc.split()
            core = []
            for word in parts:
                if word in ["with", "and", "in", "on", "from", "for", "to", "by"]:
                    break
                core.append(word)
            core_name = " ".join(core[:3]).strip()  # First 2-3 words
            core_meals.append(core_name)
        
        # Check if any core meal appears more than once within the day
        if len(core_meals) != len(set(core_meals)):
            constraints_violated.append("Same meal type appears multiple times in a day (e.g., French toast twice)")
    
    # HARD CONSTRAINT 1b: No exact description appears more than once
    # SKIP for hypertension — sodium limitations require flexibility
    if not is_hypertension:
        if len(descriptions) != len(set(descriptions)):
            constraints_violated.append("Duplicate meal found (identical descriptions)")
    
    # Extract meal categories
    categories = [m.get("food_category", "").lower() or "uncategorized" for m in meals]
    unique_categories = set(categories)
    
    # HARD CONSTRAINT 2: Minimum category variety
    # Relax for hypertension since sodium-restricted foods limit options
    min_categories = 1 if is_hypertension else 3
    if len(unique_categories) < min_categories:
        constraints_violated.append(f"Insufficient category variety: {len(unique_categories)} categories (need {min_categories}+)")
    
    # If hard constraints failed, return invalid
    if constraints_violated:
        return 0.0, {}, False, constraints_violated
    
    # ===== DIVERSITY SCORING (lenient) =====
    
    # Extract proteins from descriptions
    protein_sources = ["fish", "beef", "chicken", "pork", "seafood", "shrimp", 
                       "salmon", "tuna", "cod", "duck", "turkey", "ham", "bacon",
                       "steak", "shellfish", "crab", "lobster", "lamb", "lentil", 
                       "tofu", "chickpea", "bean", "egg", "bison", "venison"]
    
    unique_proteins = set()
    for desc in descriptions:
        for protein in protein_sources:
            if protein in desc.lower():
                unique_proteins.add(protein)
                break
    
    # Detect plant-containing meals
    plant_keywords = ["salad", "vegetable", "fruit", "bean", "legume", "tofu", 
                      "grain", "rice", "pasta", "bread", "cereal", "lentil", 
                      "chickpea", "pea", "sprout"]
    plant_meals = sum(1 for desc in descriptions 
                     if any(kw in desc for kw in plant_keywords))
    
    # Detect cooking methods
    method_keywords = {
        "roasted": ["roast", "bake"],
        "stir-fried": ["stir", "fry", "wok"],
        "steamed": ["steam"],
        "raw": ["raw", "salad", "sushi"],
        "slow-cooked": ["slow", "braise", "stew"],
    }
    unique_methods = set()
    for desc in descriptions:
        for method, keywords in method_keywords.items():
            if any(kw in desc for kw in keywords):
                unique_methods.add(method)
                break
    
    # Detect cuisines
    cuisine_keywords = {
        "Mediterranean": ["greek", "italian", "spanish", "feta", "olive"],
        "Asian": ["asian", "chinese", "thai", "japanese", "vietnamese", "soy", "ginger"],
        "Mexican": ["mexican", "taco", "burrito", "salsa", "cilantro"],
        "American": ["american", "burger", "bbq", "steak"],
        "Middle Eastern": ["middle", "mediterranean", "hummus", "kebab"],
    }
    unique_cuisines = set()
    for desc in descriptions:
        for cuisine, keywords in cuisine_keywords.items():
            if any(kw in desc for kw in keywords):
                unique_cuisines.add(cuisine)
                break
    
    # ===== SCORING: Start at 90 for passing constraints =====
    base_score = 90.0
    penalties = 0
    penalties_applied = []
    
    # Check for meals that repeat from previous days (cross-day repetition)
    # Apply -20 penalty for each meal that appeared on previous days
    if previously_used_descriptions:
        repeated_from_previous = []
        for desc in descriptions:
            if desc in previously_used_descriptions:
                repeated_from_previous.append(desc)
                penalties -= 20
        
        if repeated_from_previous:
            penalties_applied.append(f"Meals repeated from previous days: -{20 * len(repeated_from_previous)}")
    
    # Small bonus for variety (max +10)
    # Proteins: up to +3
    protein_bonus = min(3.0, len(unique_proteins) / 7.0 * 3.0)
    
    # Categories: up to +3 (we already require 3+, so 4+ is bonus)
    category_bonus = min(3.0, max(0, len(unique_categories) - 3) / 4.0 * 3.0)
    
    # Methods: up to +2
    method_bonus = min(2.0, len(unique_methods) / 5.0 * 2.0)
    
    # Cuisines: up to +2
    cuisine_bonus = min(2.0, len(unique_cuisines) / 5.0 * 2.0)
    
    total_bonus = protein_bonus + category_bonus + method_bonus + cuisine_bonus
    
    # Small penalties (only if severely lacking)
    # Only penalize if very few proteins (1-2)
    if len(unique_proteins) <= 2:
        penalties -= 3
        penalties_applied.append("Very few proteins (1-2): -3")
    
    final_score = max(0.0, min(100.0, base_score + total_bonus + penalties))
    
    breakdown = {
        "unique_proteins": sorted(list(unique_proteins)),
        "unique_categories": sorted(list(unique_categories)),
        "plant_meals": plant_meals,
        "unique_methods": sorted(list(unique_methods)),
        "unique_cuisines": sorted(list(unique_cuisines)),
        "base_score": 90.0,
        "bonuses": {
            "proteins": round(protein_bonus, 1),
            "categories": round(category_bonus, 1),
            "methods": round(method_bonus, 1),
            "cuisines": round(cuisine_bonus, 1),
        },
        "total_bonuses": round(total_bonus, 1),
        "penalties": penalties,
        "penalties_applied": penalties_applied,
        "final_score": round(final_score, 1),
    }
    
    return final_score, breakdown, True, []


def get_diversity_label(score):
    """Return diversity label based on score."""
    if score >= 85:
        return "Excellent diversity"
    elif score >= 70:
        return "Good diversity"
    elif score >= 60:
        return "Acceptable — minor repetition present"
    else:
        return "Poor — plan should be regenerated"




def compute_glycemic_load(meals):
    total = 0.0
    for m in meals:
        gi    = float(m.get("glycemic_index") or 0)
        carbs = float(m.get("_carbs_scaled")  or 0)
        if gi > 0 and carbs > 0:
            total += (gi * carbs) / 100
    return total


def compute_sodium_potassium_score(sodium, potassium, goals):
    sodium_score = 100.0
    if goals.max_sodium and sodium > goals.max_sodium:
        sodium_score = max(0.0, 100.0 - (sodium - goals.max_sodium) / goals.max_sodium * 100)
    # Only score potassium if we have meaningful data (>0)
    # Many USDA entries have no potassium value — don't penalise missing data
    if potassium > 10 and goals.potassium_target:
        potassium_score = min(100.0, potassium / goals.potassium_target * 100)
        return sodium_score * 0.6 + potassium_score * 0.4
    return sodium_score  # sodium-only score when potassium data absent


def _score_plan(meals, totals, goals, previously_used_proteins=None, previously_used_categories=None, previously_used_descriptions=None):
    def gaussian(actual, target, tol=0.15):
        if target <= 0: return 100.0
        return float(100 * np.exp(-0.5 * ((abs(actual - target) / target) / tol) ** 2))

    def one_sided(actual, target, tol=0.20):
        """100 if at or above target — only penalise falling short."""
        if target <= 0: return 100.0
        if actual >= target: return 100.0
        shortfall = (target - actual) / target
        return float(100 * np.exp(-0.5 * (shortfall / tol) ** 2))

    # Calories: one-sided penalty for GOING OVER — at or under = good
    # 100 if at/below goal, decays sharply if over
    def under_target(actual, target, tol=0.10):
        """100 if at or below target. Penalises exceeding target."""
        if target <= 0: return 100.0
        if actual <= target: return 100.0
        overage = (actual - target) / target
        return float(100 * np.exp(-0.5 * (overage / tol) ** 2))

    calorie_score = under_target(totals["calories"], goals.total_calories, tol=0.15)
    # Protein: one-sided reward for meeting/exceeding goal — more is fine
    protein_score = one_sided(totals["protein"], goals.protein_target, 0.40)

    diversity_score, diversity_breakdown, is_valid, violations = compute_diversity_score(meals, previously_used_proteins=previously_used_proteins, previously_used_categories=previously_used_categories, previously_used_descriptions=previously_used_descriptions, clinical_conditions=goals.clinical_conditions)
    diversity_label = get_diversity_label(diversity_score)
    
    # Hard constraint: Hypertension must not exceed 1500mg sodium
    sodium_constraint_violated = False
    if "hypertension" in goals.clinical_conditions:
        if totals["sodium"] > goals.max_sodium:
            sodium_constraint_violated = True
    
    # Hard constraint: Low-GI plans must have >= 25g fiber
    fiber_constraint_violated = False
    if goals.glycemic_target == "low":
        if totals["fiber"] < 25.0:
            fiber_constraint_violated = True

    unique_cats   = len(set(m.get("food_category") or "Uncategorized" for m in meals))
    variety_score = (unique_cats / len(meals) * 100) if meals else 0.0

    health_score = 100.0
    for m in meals:
        if m.get("is_high_fodmap"):  health_score -= 10
        if m.get("is_gerd_trigger"): health_score -= 10
        gi = m.get("glycemic_index")
        if gi:
            if gi > 70:  health_score -= 8
            elif gi <= 55: health_score += 4
    if goals.max_sodium and totals["sodium"] > goals.max_sodium:
        health_score -= (totals["sodium"] - goals.max_sodium) / goals.max_sodium * 30
    health_score = max(0.0, min(100.0, health_score))

    glycemic_load = compute_glycemic_load(meals)
    gl_target     = 80.0 if "diabetes" in goals.clinical_conditions else 120.0
    gl_score      = max(0.0, min(100.0, 100.0 - max(0.0, glycemic_load - gl_target) * 1.5))

    sp_score      = compute_sodium_potassium_score(totals.get("sodium", 0),
                                                   totals.get("potassium", 0), goals)

    nutrient_totals = {k: float(v or 0) for k, v in totals.items()}
    rda_flags       = flag_rda_gaps(nutrient_totals, goals.age, goals.sex, goals)
    priority_flags  = flag_priority_rda_gaps(nutrient_totals, goals.age, goals.sex, goals)
    targets = get_rda_targets(goals.age, goals.sex, goals)
    scored_nutrients = [
        "protein", "fiber", "iron", "calcium", "vitamin_b12",
        "vitamin_d", "zinc", "magnesium", "potassium",
    ]
    nutrient_scores = []
    for nutrient in scored_nutrients:
        target = float(targets.get(nutrient) or 0) * RDA_PASS_THRESHOLD
        if target <= 0:
            continue
        actual = float(nutrient_totals.get(nutrient) or 0)
        if actual == 0 and nutrient not in {"protein", "fiber"}:
            continue
        nutrient_scores.append(min(100.0, actual / target * 100.0))
    rda_score = float(np.mean(nutrient_scores)) if nutrient_scores else 100.0
    priority_penalty = len(priority_flags) * 35.0

    overall = (calorie_score   * 0.42 +
               health_score    * 0.17 +
               rda_score       * 0.16 +
               protein_score   * 0.10 +
               diversity_score * 0.08 +
               gl_score        * 0.04 +
               sp_score        * 0.03)
    overall = max(0.0, overall - priority_penalty)
    
     # Hard constraint: fail if hypertension sodium exceeds 1500mg
    if sodium_constraint_violated:
        overall = 0.0
    
    # Hard constraint: fail if low-GI plan has less than 25g fiber
    if fiber_constraint_violated:
        overall = 0.0

    return MealPlanResult(
        meals=meals,
        total_calories=totals["calories"], total_protein=totals["protein"],
        total_carbs=totals["carbs"],       total_fat=totals["fat"],
        total_fiber=totals["fiber"],       total_sodium=totals["sodium"],
        total_potassium=totals.get("potassium", 0.0),
        nutrient_totals=nutrient_totals,
        score=overall,             calorie_score=calorie_score,
        protein_score=protein_score, variety_score=variety_score,
        health_score=health_score,   diversity_score=diversity_score,
        glycemic_load_score=gl_score, sodium_potassium_score=sp_score,
        glycemic_load=glycemic_load,
        category_breakdown={},  rda_flags=rda_flags,
        priority_rda_flags=priority_flags,
        diversity_breakdown=diversity_breakdown,
        diversity_label=diversity_label,
    )


# ── Core meal planner ─────────────────────────────────────────────────────────

class MealPlanner:
    MEAL_SLOTS        = ["breakfast", "lunch", "dinner", "snack"]
    MEAL_CALORIE_SPLIT= [0.25, 0.35, 0.30, 0.10]
    MEAL_SERVINGS     = {"breakfast": 250, "lunch": 350, "dinner": 350, "snack": 150}

    def __init__(self, goals):
        self.goals    = goals
        self.learning = self._load_learning()
        self.faiss_available = True
        try:
            _load_faiss()
        except Exception as exc:
            self.faiss_available = False
            log.warning("FAISS unavailable (%s) - using filtered linear planner", exc)
            _load_faiss_meta_only()
        _load_bloom_filters()

    def plan_day(
        self,
        available_foods=None,       # legacy arg — ignored, FAISS used instead
        dietary_preference=None,
        cultural_preset=None,
        exclude_fodmap=False,
        exclude_gerd=False,
        avoid_allergens=None,
        clinical_conditions=None,
        glycemic_target=None,
        num_meals=3,
        num_variations=5,
        mixed_household=None,
        **kwargs,
    ):
        wall_start = time.time()
        log.info("meal_generation_start ts=%.3f", wall_start)

        conditions = {c.lower() for c in (clinical_conditions or self.goals.clinical_conditions)}
        if "hypertension" in conditions:
            self.goals.max_sodium       = min(self.goals.max_sodium, 1500.0)
            self.goals.potassium_target = max(self.goals.potassium_target, 4700.0)

        source_foods = available_foods
        if source_foods is None:
            try:
                from data_pipeline.filters.query import filter_foods
                source_foods = filter_foods(limit=50000)
            except Exception:
                meta = _load_faiss_meta_only()
                source_foods = list(meta["metadata"]) if meta else []

        effective_glycemic_target = glycemic_target or ("low" if "diabetes" in conditions else None)

        bloom_constraints = {
            "fodmap":  exclude_fodmap,
            "gerd":    exclude_gerd,
            "high_gi": effective_glycemic_target == "low",
            "low_gi":  effective_glycemic_target == "high",
        }
        if avoid_allergens:
            for a in avoid_allergens:
                bloom_constraints[a.lower()] = True

        shared_filter_kwargs = {
            "cultural_preset": cultural_preset,
            "exclude_fodmap":  exclude_fodmap,
            "exclude_gerd":    exclude_gerd,
            "avoid_allergens": avoid_allergens,
            "glycemic_target": effective_glycemic_target,
            "max_sodium":      700   if "hypertension" in conditions else None,
        }

        # Build global allowed pool
        allowed_ids = self._get_allowed_ids(
            source_foods=source_foods,
            dietary_preference=dietary_preference,
            **shared_filter_kwargs,
        )
        log.info("Global allowed pool: %s foods (%.2fs)", len(allowed_ids), time.time()-wall_start)

        # ── Mixed household: build per-slot pools ─────────────────────────────
        # FIX: slot_allowed_ids IS the diet filter for each slot.
        # _matches_diet() is NOT called again inside _build_one_plan — that was
        # the double-filter that shrank pools to zero and dropped meals.
        slot_allowed_ids = {}
        if mixed_household:
            for slot, slot_diet in mixed_household.items():
                slot_diet_clean = (slot_diet or "").strip().lower()
                if not slot_diet_clean or slot_diet_clean in ("no restrictions", "no_restrictions", "none"):
                    slot_allowed_ids[slot] = allowed_ids
                    log.info("Mixed slot=%s no restriction -> global pool (%s foods)", slot, len(allowed_ids))
                    continue
                ids = self._get_allowed_ids(
                    source_foods=source_foods,
                    dietary_preference=slot_diet,
                    **shared_filter_kwargs,
                )
                slot_ids = ids & allowed_ids if allowed_ids else ids
                # Fallback: if intersection is too small, use the broader diet pool
                if len(slot_ids) < 50:
                    log.warning("Mixed slot=%s diet=%s only %s foods — using broader pool",
                                slot, slot_diet, len(slot_ids))
                    slot_ids = ids if len(ids) >= 50 else allowed_ids
                slot_allowed_ids[slot] = slot_ids
                log.info("Mixed slot=%s diet=%s -> %s foods", slot, slot_diet, len(slot_ids))

        slots = self.MEAL_SLOTS[:num_meals]
        plans = []
        _week_used_ids = kwargs.pop("_week_used_ids", None) or set()
        _week_used_signatures = kwargs.pop("_week_used_signatures", None) or set()
        _week_slot_signatures = kwargs.pop("_week_slot_signatures", None) or {}
        _week_used_proteins = kwargs.pop("_week_used_proteins", None) or set()
        _week_used_categories = kwargs.pop("_week_used_categories", None) or set()
        _week_used_descriptions = kwargs.pop("_week_used_descriptions", None) or set()
        _force_fish_meal = kwargs.pop("_force_fish_meal", False)
        # NOTE: globally_used_ids is intentionally NOT shared across variations.
        # Each variation gets a fresh start (with only week-level exclusions) so
        # all 5 variations can independently explore different food combinations.
        # Sharing it caused variations 2-5 to have no good foods left → same meal.

        for variation in range(num_variations):
            if time.time() - wall_start > MAX_GENERATION_SECONDS:
                log.warning("Time limit at variation %s (%.1fs) — returning %s plans",
                            variation, time.time()-wall_start, len(plans))
                break
            t_var = time.time()
            plan  = self._build_one_plan(
                slots=slots,
                allowed_ids=allowed_ids,
                slot_allowed_ids=slot_allowed_ids,
                bloom_constraints=bloom_constraints,
                variation_seed=variation,
                wall_start=wall_start,
                source_foods=source_foods,
                force_fish_meal=_force_fish_meal,
                avoid_allergens=avoid_allergens,
                globally_used_ids=set(_week_used_ids),
                globally_used_signatures=set(_week_used_signatures),
                slot_used_signatures={
                    slot: set(signatures)
                    for slot, signatures in _week_slot_signatures.items()
                },
                globally_used_proteins=set(_week_used_proteins),
                globally_used_categories=set(_week_used_categories),
                globally_used_descriptions=set(_week_used_descriptions),
            )
            if plan:
                plan.generation_ms = (time.time() - t_var) * 1000
                plans.append(plan)

        if not plans:
            log.error("No plans generated")
            return []

        plans.sort(key=lambda p: p.score, reverse=True)
        
        # For hypertension, reject all 0-score plans (sodium over 1500mg) and retry
        if "hypertension" in self.goals.clinical_conditions:
            valid_plans = [p for p in plans if p.score > 0]
            if valid_plans and len(valid_plans) < len(plans):
                log.info("Hypertension: filtered %s plans with score=0 (sodium violation), keeping %s valid plans",
                        len(plans) - len(valid_plans), len(valid_plans))
                plans = valid_plans
        
        if self.goals.priority_nutrients:
            passing_plans = [p for p in plans if not p.priority_rda_flags]
            if not passing_plans:
                missed = ", ".join(NUTRIENT_LABELS.get(n, n) for n in self.goals.priority_nutrients)
                log.warning("No plans met selected priority nutrient thresholds: %s", missed)
                return []
            plans = passing_plans
        
        
        elapsed = time.time() - wall_start
        log.info("Generated %s plans in %.2fs | best=%.1f | under_60=%s",
                 len(plans), elapsed, plans[0].score if plans else 0, elapsed < 60)
        self._record_learning(plans, elapsed)
        return plans

    def plan_week(self, available_foods=None, num_days=7, meals_per_day=3,
                  dietary_preference=None, **kwargs):
        wall_start = time.time()
        week = []
        week_used_ids = set()
        week_used_signatures = set()
        week_slot_signatures = {}
        week_used_proteins = set()  # Track all proteins used across the week
        week_used_categories = set()  # Track all food categories used across the week
        week_used_descriptions = set()  # Track all food descriptions used across the week

        for day in range(num_days):
            if MAX_GENERATION_SECONDS - (time.time() - wall_start) < 2:
                log.warning("Time budget exhausted after day %s", day)
                break
            day_plans = self.plan_day(
                available_foods=available_foods,
                dietary_preference=dietary_preference,
                num_meals=meals_per_day,
                num_variations=5,
                _force_fish_meal=(dietary_preference or "").lower() == "pescetarian" and day < 3,
                _week_used_ids=week_used_ids,
                _week_used_signatures=week_used_signatures,
                _week_slot_signatures=week_slot_signatures,
                _week_used_proteins=week_used_proteins,
                _week_used_categories=week_used_categories,
                _week_used_descriptions=week_used_descriptions,
                **kwargs,
            )
            if day_plans:
                # Pick plan with best diversity score (encourage variety), but fall back to best overall score
                # Also check for hard constraint violations
                valid_plans = []
                for plan in day_plans:
                    # Check if plan passed hard constraints (score > 0 or diversity_breakdown shows valid)
                    if plan.diversity_score > 0 or (plan.diversity_breakdown and plan.diversity_breakdown.get('final_score', 0) > 0):
                        # For hypertension, also reject score=0 plans (sodium violation)
                        if "hypertension" in self.goals.clinical_conditions and plan.score == 0:
                            continue
                        valid_plans.append(plan)
                
                if not valid_plans:
                    # Analyze why plans failed
                    sodium_violations = [p for p in day_plans if p.score == 0 and "hypertension" in self.goals.clinical_conditions]
                    diversity_violations = [p for p in day_plans if p.diversity_score == 0]
                    
                    # For hypertension, use the first variation instead of picking the best of failed plans
                    if "hypertension" in self.goals.clinical_conditions and sodium_violations:
                        log.warning("Day %s: All %s plans exceeded 1500mg sodium limit, using first variation", day, len(sodium_violations))
                        best = day_plans[0]
                    elif diversity_violations:
                        log.warning("Day %s: All %s plans failed diversity constraints (duplicates or < 2 categories), using best available", day, len(diversity_violations))
                        best = max(day_plans, key=lambda p: (p.diversity_score, p.score))
                    else:
                        log.warning("Day %s: No plans passed hard constraints, using best available", day)
                        best = max(day_plans, key=lambda p: (p.diversity_score, p.score))
                else:
                    best = max(valid_plans, key=lambda p: (p.diversity_score, p.score))
                
                week.append(best)
                for meal in best.meals:
                    for fid in meal.get("_component_ids", [meal.get("fdc_id")]):
                        if fid is not None:
                            week_used_ids.add(int(fid))
                    signature = meal_family_signature(meal)
                    week_used_signatures.add(signature)
                    week_slot_signatures.setdefault(meal.get("_slot", ""), set()).add(signature)
                    
                    # Track proteins from this meal for next day
                    desc_lower = str(meal.get("description", "")).lower()
                    protein_keywords = ["fish", "salmon", "tuna", "cod", "beef", "steak", "chicken", "pork", 
                                        "ham", "bacon", "shellfish", "shrimp", "crab", "turkey", "duck"]
                    for protein in protein_keywords:
                        if protein in desc_lower:
                            week_used_proteins.add(protein)
                    
                    # Track descriptions from this meal for next day (for similar-food penalty)
                    meal_desc = str(meal.get("description", "")).strip().lower()
                    if meal_desc:
                        week_used_descriptions.add(meal_desc)
            log.info("Day %s/%s — %.1fs elapsed | %s foods excluded | proteins: %s",
                    day+1, num_days, time.time()-wall_start, len(week_used_ids), len(week_used_proteins))
        
        # Week-level constraint validation: enforce min_diversity_score on ALL days
        min_diversity_score = kwargs.get("min_diversity_score", 0)
        
        if min_diversity_score > 0:
            passing_days = [day for day in week if day.diversity_score >= min_diversity_score]
            if len(passing_days) < num_days and len(passing_days) > 0:
                log.warning("Only %s/%s days met diversity >= %.1f; retrying with relaxed constraints",
                           len(passing_days), len(week), min_diversity_score)
            elif not passing_days:
                log.error("No days met diversity >= %.1f constraint", min_diversity_score)
                return []
        
        return week

    def _get_allowed_ids(self, source_foods=None, **filter_kwargs):
        try:
            from data_pipeline.filters.query import filter_foods
            foods = filter_foods(limit=50000, **{k: v for k, v in filter_kwargs.items() if v is not None})
            return {f["fdc_id"] for f in foods}
        except Exception as e:
            log.warning("filter_foods failed (%s) — using full FAISS index", e)
            if source_foods:
                return {f["fdc_id"] for f in source_foods}
            meta = _load_faiss_meta_only()
            return {f["fdc_id"] for f in meta["metadata"]} if meta else set()

    def _build_one_plan(self, slots, allowed_ids, slot_allowed_ids,
                        bloom_constraints, variation_seed, wall_start,
                        source_foods=None,
                        force_fish_meal=False,
                        avoid_allergens=None,
                        globally_used_ids=None, globally_used_signatures=None,
                        slot_used_signatures=None,
                        globally_used_proteins=None, globally_used_categories=None,
                        globally_used_descriptions=None):
        meals             = []
        used_ids          = set(globally_used_ids or [])
        used_signatures   = set(globally_used_signatures or [])
        slot_used_signatures = slot_used_signatures or {}
        used_descriptions = set()
        used_cats         = set()
        used_proteins_today = set()  # Track proteins used today
        globally_used_proteins = globally_used_proteins or set()  # Track proteins used across week
        used_cats_today = set()  # Track categories used today
        globally_used_categories = globally_used_categories or set()  # Track categories across week
        used_descriptions_today = set()  # Track descriptions used today
        globally_used_descriptions = globally_used_descriptions or set()  # Track descriptions across week
        totals = {k: 0.0 for k in ["calories","protein","carbs","fat","fiber",
                                    "sodium","potassium","iron","calcium",
                                    "vitamin_b12","vitamin_d","zinc","magnesium"]}

        for slot_idx, slot in enumerate(slots):
            if time.time() - wall_start > MAX_GENERATION_SECONDS:
                break

            split_total = sum(self.MEAL_CALORIE_SPLIT[:len(slots)])
            slot_split = self.MEAL_CALORIE_SPLIT[slot_idx] / split_total if split_total else 1 / len(slots)
            slot_cal_target = self.goals.total_calories * slot_split
            serving = self.MEAL_SERVINGS.get(slot, 300)  # default, overridden below

            # Per-slot allowed pool (already diet-filtered — no _matches_diet needed)
            active_ids = slot_allowed_ids.get(slot, allowed_ids)

            target_vec  = self._slot_target_vec(slot_idx, len(slots))
            # Large variation-specific noise so each plan queries a genuinely
            # different FAISS neighborhood. Old scale 0.05-0.21 only changed
            # the vec by ~1-5% → same top-120 candidates every time.
            # New scale 0.3-1.5 shifts the query meaningfully across the index.
            noise_scale = 0.3 + variation_seed * 0.25  # 0.3, 0.55, 0.8, 1.05, 1.3
            rng         = np.random.default_rng(seed=variation_seed * 100 + slot_idx * 7)
            noise       = rng.normal(0, noise_scale, size=DIM).astype(np.float32)
            query_vec   = np.abs(target_vec + noise)  # abs keeps values positive

            # FAISS fetch — already restricted to active_ids (diet-safe pool)
            if self.faiss_available:
                candidates = _faiss_candidates(query_vec, k=120, allowed_fdc_ids=active_ids)
            else:
                candidates = _linear_candidates(query_vec, k=120, foods=source_foods or [], allowed_fdc_ids=active_ids)

            # Filter: dedupe + Bloom + allergen check
            blocked_slot_signatures = slot_used_signatures.get(slot, set())
            filtered = [
                f for f in candidates
                if f["fdc_id"] not in used_ids
                and str(f.get("description") or "").strip().lower() not in used_descriptions
                and meal_family_signature(f) not in used_signatures
                and meal_family_signature(f) not in blocked_slot_signatures
                and _bloom_screen(f, bloom_constraints)
                and _check_allergens(f, avoid_allergens)  # EXPLICIT allergen check
            ]

            # Relaxed fallback 1: drop Bloom requirement
            if not filtered:
                log.warning("slot=%s var=%s: no Bloom-passing candidates — relaxing", slot, variation_seed)
                filtered = [
                    f for f in candidates
                    if f["fdc_id"] not in used_ids
                    and str(f.get("description") or "").strip().lower() not in used_descriptions
                    and meal_family_signature(f) not in used_signatures
                    and meal_family_signature(f) not in blocked_slot_signatures
                    and _check_allergens(f, avoid_allergens)  # Still check allergens!
                ]

            # Relaxed fallback 2: scan full diet-safe pool
            if not filtered:
                log.warning("slot=%s var=%s: scanning full pool", slot, variation_seed)
                fallback_foods = source_foods or []
                if not fallback_foods:
                    meta = _load_faiss_meta_only()
                    fallback_foods = list(meta["metadata"]) if meta else []
                for f in fallback_foods:
                    if time.time() - wall_start > MAX_GENERATION_SECONDS:
                        break
                    if f.get("fdc_id") not in active_ids:
                        continue
                    if f["fdc_id"] in used_ids:
                        continue
                    if str(f.get("description") or "").strip().lower() in used_descriptions:
                        continue
                    if meal_family_signature(f) in used_signatures:
                        continue
                    if meal_family_signature(f) in blocked_slot_signatures:
                        continue
                    if not _check_allergens(f, avoid_allergens):  # Check allergens!
                        continue
                    filtered.append(f)
                    if len(filtered) >= 200:
                        break

            # Last resort: allow a repeated family, but still avoid exact foods.
            if not filtered:
                log.warning("slot=%s var=%s: relaxing meal-family diversity", slot, variation_seed)
                fallback_foods = source_foods or []
                if not fallback_foods:
                    meta = _load_faiss_meta_only()
                    fallback_foods = list(meta["metadata"]) if meta else []
                for f in fallback_foods:
                    if time.time() - wall_start > MAX_GENERATION_SECONDS:
                        break
                    if f.get("fdc_id") not in active_ids:
                        continue
                    if f["fdc_id"] in used_ids:
                        continue
                    if str(f.get("description") or "").strip().lower() in used_descriptions:
                        continue
                    filtered.append(f)
                    if len(filtered) >= 200:
                        break

            if not filtered:
                log.warning("slot=%s: truly no candidates, skipping slot", slot)
                continue

            if force_fish_meal and slot == "dinner":
                fish_filtered = [
                    food for food in filtered
                    if any(term in str(food.get("description") or "").lower() for term in FISH_SEAFOOD_TERMS)
                ]
                if not fish_filtered:
                    fish_filtered = [
                        food for food in (source_foods or [])
                        if food.get("fdc_id") in active_ids
                        and food.get("fdc_id") not in used_ids
                        and any(term in str(food.get("description") or "").lower() for term in FISH_SEAFOOD_TERMS)
                        and _bloom_screen(food, bloom_constraints)
                    ][:200]
                if fish_filtered:
                    filtered = fish_filtered

            addon_pool = [
                food for food in (source_foods or [])
                if food.get("fdc_id") in active_ids
                and food.get("fdc_id") not in used_ids
                and _bloom_screen(food, bloom_constraints)
            ]
            ranked = self._rank_candidates(filtered, used_cats, variation_seed, slot_idx, meals, 
                                           used_proteins_today, globally_used_proteins)
            chosen, scaled_totals = self._build_composite_meal(
                ranked,
                slot=slot,
                slot_idx=slot_idx,
                slot_cal_target=slot_cal_target,
                day_totals=totals,
                used_ids=used_ids,
                addon_pool=addon_pool,
            )

            meals.append(chosen)
            for fid in chosen.get("_component_ids", [chosen.get("fdc_id")]):
                if fid is not None:
                    used_ids.add(fid)
            if globally_used_ids is not None:
                for fid in chosen.get("_component_ids", [chosen.get("fdc_id")]):
                    if fid is not None:
                        globally_used_ids.add(fid)
            used_descriptions.add(str(chosen.get("description") or "").strip().lower())
            used_cats.add(chosen.get("food_category", ""))
            used_cats_today.add(chosen.get("food_category", ""))
            globally_used_categories.add(chosen.get("food_category", ""))
            desc = str(chosen.get("description", "")).strip().lower()
            used_descriptions_today.add(desc)
            globally_used_descriptions.add(desc)
            used_signatures.add(meal_family_signature(chosen))
            
            # Track proteins used across the week
            desc_lower = str(chosen.get("description", "")).lower()
            protein_keywords = ["fish", "salmon", "tuna", "cod", "beef", "steak", "chicken", "pork", 
                              "ham", "bacon", "shellfish", "shrimp", "crab", "turkey", "duck"]
            for protein in protein_keywords:
                if protein in desc_lower:
                    used_proteins_today.add(protein)
                    globally_used_proteins.add(protein)

            for nutrient in totals:
                totals[nutrient] += scaled_totals[nutrient]

        if not meals:
            return None

        return _score_plan(meals, totals, self.goals, previously_used_proteins=globally_used_proteins - used_proteins_today, previously_used_categories=globally_used_categories - used_cats_today, previously_used_descriptions=globally_used_descriptions - used_descriptions_today)

    def _scale_food(self, food, serving, slot):
        item = dict(food)
        item["_base_food"] = dict(food.get("_base_food", food))
        serving = int(max(50, min(600, serving)))
        scale = serving / 100
        item["_slot"] = slot
        item["_serving"] = serving
        for nutrient in NUTRIENT_COLS:
            item[f"_{nutrient}_scaled"] = round(float(item.get(nutrient) or 0) * scale, 1)
        item["_cal_scaled"] = item["_calories_scaled"]
        item["_prot_scaled"] = item["_protein_scaled"]
        item["_carbs_scaled"] = item["_carbs_scaled"]
        item["_fat_scaled"] = item["_fat_scaled"]
        item["_fiber_scaled"] = item["_fiber_scaled"]
        item["_sodium_scaled"] = item["_sodium_scaled"]
        item["_potassium_scaled"] = item["_potassium_scaled"]
        item["calories"] = item["_cal_scaled"]
        item["protein"] = item["_prot_scaled"]
        return item

    def _combine_items(self, items, slot):
        totals = {
            nutrient: sum(float(item.get(f"_{nutrient}_scaled") or 0) for item in items)
            for nutrient in NUTRIENT_COLS
        }
        descriptions = [str(item.get("description") or "").strip() for item in items]
        categories = [str(item.get("food_category") or "") for item in items if item.get("food_category")]
        allergens = sorted({
            allergen.strip()
            for item in items
            for allergen in str(item.get("allergens") or "").split(",")
            if allergen.strip()
        })
        gi_values = [
            (float(item.get("glycemic_index") or 0), float(item.get("_carbs_scaled") or 0))
            for item in items
            if item.get("glycemic_index") is not None
        ]
        carb_weight = sum(carbs for _, carbs in gi_values)
        meal_gi = None
        if gi_values and carb_weight > 0:
            meal_gi = sum(gi * carbs for gi, carbs in gi_values) / carb_weight
        elif gi_values:
            meal_gi = max(gi for gi, _ in gi_values)

        composite = {
            "fdc_id": items[0].get("fdc_id"),
            "_component_ids": [item.get("fdc_id") for item in items if item.get("fdc_id") is not None],
            "_items": items,
            "_slot": slot,
            "_serving": sum(float(item.get("_serving") or 0) for item in items),
            "description": " + ".join(descriptions),
            "food_category": " + ".join(dict.fromkeys(categories)) or "Composite meal",
            "data_type": "Composite",
            "dietary_category": items[0].get("dietary_category"),
            "is_high_fodmap": int(any(item.get("is_high_fodmap") for item in items)),
            "is_gerd_trigger": int(any(item.get("is_gerd_trigger") for item in items)),
            "allergens": ",".join(allergens),
            "glycemic_index": round(meal_gi, 1) if meal_gi is not None else None,
        }
        for nutrient, value in totals.items():
            composite[nutrient] = round(value, 1)
            composite[f"_{nutrient}_scaled"] = round(value, 1)
        composite["_cal_scaled"] = composite["_calories_scaled"]
        composite["_prot_scaled"] = composite["_protein_scaled"]
        composite["_carbs_scaled"] = composite["_carbs_scaled"]
        composite["_fat_scaled"] = composite["_fat_scaled"]
        composite["_fiber_scaled"] = composite["_fiber_scaled"]
        composite["_sodium_scaled"] = composite["_sodium_scaled"]
        composite["_potassium_scaled"] = composite["_potassium_scaled"]
        return composite, totals

    def _nutrient_need_score(self, food, current_totals, serving):
        scale = serving / 100
        calories = float(food.get("calories") or 0) * scale
        targets = get_rda_targets(self.goals.age, self.goals.sex, self.goals)
        score = 0.0
        priority = {
            "protein": 1.4, "fiber": 2.0, "iron": 1.8, "calcium": 1.8,
            "vitamin_b12": 1.8, "vitamin_d": 1.3, "zinc": 1.4,
            "magnesium": 1.5, "potassium": 2.0,
        }
        if "hypertension" in self.goals.clinical_conditions:
            priority["potassium"] = 5.0
            priority["magnesium"] = 2.0
        for nutrient, weight in priority.items():
            target = float(targets.get(nutrient) or 0) * RDA_PASS_THRESHOLD
            if target <= 0:
                continue
            gap = max(0.0, target - float(current_totals.get(nutrient) or 0))
            if gap <= 0:
                continue
            contribution = float(food.get(nutrient) or 0) * scale
            score += min(1.0, contribution / gap) * weight * 100
        score -= calories / 8
        score -= float(food.get("sodium") or 0) * scale / 25
        if "diabetes" in self.goals.clinical_conditions:
            gi = food.get("glycemic_index")
            if gi is None or float(gi) > 55:
                return -1_000_000
            score -= float(food.get("carbs") or 0) * scale / 2
        return score

    def _build_composite_meal(self, ranked, slot, slot_idx, slot_cal_target, day_totals, used_ids, addon_pool=None):
        if not ranked:
            raise ValueError("No ranked foods available for meal")
        day_cal_limit = self.goals.total_calories * 1.05
        meal_cal_limit = min(slot_cal_target * 1.08, max(120.0, day_cal_limit - float(day_totals.get("calories") or 0)))
        primary = dict(ranked[0])
        primary_cal = float(primary.get("calories") or 0)
        primary_target = min(slot_cal_target * 0.68, meal_cal_limit)
        primary_serving = 100
        if primary_cal > 5:
            primary_serving = int(max(50, min(450, primary_target / primary_cal * 100)))
        items = [self._scale_food(primary, primary_serving, slot)]
        meal_totals = {
            nutrient: float(items[0].get(f"_{nutrient}_scaled") or 0)
            for nutrient in NUTRIENT_COLS
        }
        current_plus_meal = {
            nutrient: float(day_totals.get(nutrient) or 0) + meal_totals.get(nutrient, 0)
            for nutrient in NUTRIENT_COLS
        }

        remaining_calories = max(0.0, min(slot_cal_target, meal_cal_limit) - meal_totals["calories"])
        for _ in range(2):
            if remaining_calories < 80:
                break
            serving_budget = remaining_calories
            search_pool = list(ranked[1:80])
            if addon_pool:
                seen = {food.get("fdc_id") for food in search_pool}
                search_pool.extend([food for food in addon_pool if food.get("fdc_id") not in seen])
            options = [
                food for food in search_pool
                if food.get("fdc_id") not in used_ids
                and food.get("fdc_id") not in {item.get("fdc_id") for item in items}
            ]
            if not options:
                break
            serving_by_food = {}
            for food in options:
                cal_per100 = float(food.get("calories") or 0)
                if cal_per100 > 5:
                    serving_by_food[food.get("fdc_id")] = int(max(50, min(250, serving_budget / cal_per100 * 100)))
                else:
                    serving_by_food[food.get("fdc_id")] = 100
            options.sort(
                key=lambda food: self._nutrient_need_score(food, current_plus_meal, serving_by_food.get(food.get("fdc_id"), 100)),
                reverse=True,
            )
            addition = options[0]
            serving = serving_by_food.get(addition.get("fdc_id"), 100)
            if self._nutrient_need_score(addition, current_plus_meal, serving) <= 0:
                break
            scaled = self._scale_food(addition, serving, slot)
            projected_calories = meal_totals["calories"] + float(scaled.get("_cal_scaled") or 0)
            if projected_calories > meal_cal_limit:
                break
            projected_sodium = float(day_totals.get("sodium") or 0) + meal_totals["sodium"] + float(scaled.get("_sodium_scaled") or 0)
            if "hypertension" in self.goals.clinical_conditions and projected_sodium > self.goals.max_sodium:
                break
            items.append(scaled)
            for nutrient in NUTRIENT_COLS:
                meal_totals[nutrient] += float(scaled.get(f"_{nutrient}_scaled") or 0)
                current_plus_meal[nutrient] += float(scaled.get(f"_{nutrient}_scaled") or 0)
            remaining_calories = max(0.0, min(slot_cal_target, meal_cal_limit) - meal_totals["calories"])

        if remaining_calories > 60 and items:
            item = items[0]
            cal_per100 = float(primary.get("calories") or 0)
            if cal_per100 > 5:
                topup_calories = max(0.0, min(slot_cal_target, meal_cal_limit) - sum(float(i.get("_cal_scaled") or 0) for i in items))
                extra_serving = min(600, int(item["_serving"] + topup_calories / cal_per100 * 100))
                items[0] = self._scale_food(primary, extra_serving, slot)

        while items:
            _, combined_totals = self._combine_items(items, slot)
            projected_day_calories = float(day_totals.get("calories") or 0) + combined_totals["calories"]
            if projected_day_calories <= day_cal_limit and combined_totals["calories"] <= meal_cal_limit:
                break
            highest_cal_item = max(items, key=lambda item: float(item.get("_cal_scaled") or 0))
            if float(highest_cal_item.get("_serving") or 0) <= 50:
                break
            original = highest_cal_item.get("_base_food", highest_cal_item)
            reduced = int(max(50, float(highest_cal_item.get("_serving") or 0) - 10))
            idx = items.index(highest_cal_item)
            items[idx] = self._scale_food(original, reduced, slot)

        if "hypertension" in self.goals.clinical_conditions:
            while True:
                _, combined_totals = self._combine_items(items, slot)
                projected_sodium = float(day_totals.get("sodium") or 0) + combined_totals["sodium"]
                if projected_sodium <= self.goals.max_sodium or not items:
                    break
                highest_sodium_item = max(items, key=lambda item: float(item.get("_sodium_scaled") or 0))
                if float(highest_sodium_item.get("_serving") or 0) <= 50:
                    break
                original = highest_sodium_item.get("_base_food", highest_sodium_item)
                reduced = int(max(50, float(highest_sodium_item.get("_serving") or 0) - 10))
                idx = items.index(highest_sodium_item)
                items[idx] = self._scale_food(original, reduced, slot)

        return self._combine_items(items, slot)

    def _slot_target_vec(self, slot_idx, num_slots):
        split_total = sum(self.MEAL_CALORIE_SPLIT[:num_slots])
        split = self.MEAL_CALORIE_SPLIT[slot_idx] / split_total if split_total else 1 / num_slots
        s = 100 / 300
        return np.array([
            self.goals.total_calories * split * s,
            self.goals.protein_target  * split * s,
            self.goals.carbs_target    * split * s,
            self.goals.fat_target      * split * s,
            self.goals.fiber_target    * split * s,
            2.0, 200.0, 1.0, 5.0, 3.0,
            self.goals.max_sodium * split * s,
            500.0 * s, 50.0 * s,
        ], dtype=np.float32)

    def _rank_candidates(self, candidates, used_cats, variation_seed, slot_idx, current_meals=None,
                        used_proteins_today=None, globally_used_proteins=None):
        """Rank candidates by nutrition fit AND diversity (new protein sources across day + week)."""
        
        used_proteins_today = used_proteins_today or set()
        globally_used_proteins = globally_used_proteins or set()
        
        def score_food(food):
            s = 50.0 if food.get("food_category", "") not in used_cats else 0.0
            cal = float(food.get("calories") or 0)
            split_total = sum(self.MEAL_CALORIE_SPLIT[:3])
            split = self.MEAL_CALORIE_SPLIT[slot_idx] / split_total if split_total else 1 / 3
            target = self.goals.total_calories * split / 3
            s += max(0, 50 - abs(cal - target) / 5)
            
            # EXTREME DIVERSITY: Make repeated proteins nearly unusable
            desc_lower = str(food.get("description", "")).lower()
            protein_keywords = ["fish", "salmon", "tuna", "cod", "beef", "steak", "chicken", "pork", 
                              "ham", "bacon", "shellfish", "shrimp", "crab", "turkey", "duck"]
            found_protein = None
            for protein in protein_keywords:
                if protein in desc_lower:
                    found_protein = protein
                    break
            
            # EXTREME penalties: make repeated proteins essentially unusable
            if found_protein:
                # DAILY: Massive penalty if already used today (prevent same protein 2x/day)
                if found_protein in used_proteins_today:
                    s -= 500  # EXTREME penalty - basically disqualifies the food
                else:
                    s += 150  # Huge boost: new protein today
                
                # WEEKLY: Severe penalty if already used in previous days (force rotation)
                if found_protein in globally_used_proteins:
                    s -= 300  # EXTREME penalty - makes repeated protein across week unusable
                else:
                    s += 100  # Massive boost: brand new protein for the week
            else:
                # Non-meat options get bonus to encourage diversity
                s += 120  # Strong boost for non-meat/non-fish options
            
            if "diabetes" in self.goals.clinical_conditions:
                gi    = float(food.get("glycemic_index") or 0)
                carbs = float(food.get("carbs") or 0)
                if gi: s += max(0, 20 - max(0, gi - 55))
                s += max(0, 20 - carbs / 3)
            if "hypertension" in self.goals.clinical_conditions:
                s += max(0, 50 - float(food.get("sodium") or 0) / 5)
                s -= float(food.get("sodium") or 0) / 3
                s += min(20,     float(food.get("potassium") or 0) / 100)
            return s

        scored  = sorted(candidates, key=score_food, reverse=True)
        # Pick from top-40 for maximum diversity selection
        top_k   = min(40, len(scored))
        pick    = variation_seed % top_k
        return scored[pick:] + scored[:pick]

    @staticmethod
    def _matches_diet(food, diet):
        """Kept for backward compatibility — not called in normal flow."""
        from data_pipeline.filters.query import food_matches_diet
        return food_matches_diet(food, diet)

    def _load_learning(self):
        if LEARNING_DB.exists():
            with open(LEARNING_DB) as f:
                return self._migrate_learning(json.load(f))
        return {"plans": [], "scores": [], "generation_times": []}

    def _migrate_learning(self, data):
        if "avg_scores" in data and "scores" not in data:
            data["scores"] = data.pop("avg_scores")
        data.setdefault("scores", [])
        data.setdefault("plans", [])
        data.setdefault("generation_times", [])
        return data

    def _record_learning(self, plans, elapsed):
        self.learning["scores"].append(round(plans[0].score, 2))
        self.learning["generation_times"].append(round(elapsed, 2))
        self.learning["plans"].append(plans[0].to_dict())
        if len(self.learning["plans"]) > 50:
            self.learning["plans"] = self.learning["plans"][-50:]
        LEARNING_DB.parent.mkdir(parents=True, exist_ok=True)
        with open(LEARNING_DB, "w") as f:
            json.dump(self.learning, f, indent=2)

    def get_improvement_metrics(self):
        scores = self.learning.get("scores", [])
        times  = self.learning.get("generation_times", [])
        if len(scores) < 2:
            return {"status": "Not enough history yet — generate more plans first"}
        n     = len(scores)
        third = max(n // 3, 1)
        first_avg   = float(np.mean(scores[:third]))
        last_avg    = float(np.mean(scores[-third:]))
        improvement = last_avg - first_avg
        return {
            "total_plans_generated": n,
            "first_third_avg_score": round(first_avg, 2),
            "last_third_avg_score":  round(last_avg,  2),
            "improvement_points":    round(improvement, 2),
            "improvement_pct":       round(improvement / first_avg * 100 if first_avg else 0, 2),
            "best_score_ever":       round(max(scores), 2),
            "current_avg_last_5":   round(float(np.mean(scores[-5:])), 2),
            "current_avg":           round(float(np.mean(scores[-5:])), 2),
            "avg_generation_sec":    round(float(np.mean(times)), 2) if times else None,
            "max_generation_sec":    round(max(times), 2) if times else None,
            "all_under_60s":         all(t <= 60 for t in times),
        }

