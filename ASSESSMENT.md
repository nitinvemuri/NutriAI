# NutriAI Progress Assessment
## What's Built vs. What's Missing

---

## CAPABILITY-BY-CAPABILITY BREAKDOWN

### ✅ **Capability 1: Clinical Condition Filtering** — 70% Complete

**What You Have:**
- ✅ IBS (FODMAP) detection with keyword-based tagging (tag.py)
  - HIGH_FODMAP_KEYWORDS list covers garlic, onion, wheat, apples, milk, beans, etc.
  - SQLite column `is_high_fodmap` flags these foods
  - Bloom filter for O(1) lookup at query time
  
- ✅ GERD detection with keyword-based tagging
  - GERD_TRIGGER_KEYWORDS list (tomato, citrus, coffee, chocolate, fried, alcohol, spicy)
  - SQLite column `is_gerd_trigger` flags these foods
  - Bloom filter for O(1) lookup

- ✅ Low Glycemic Index constraints
  - GI_OVERRIDES table in tag.py with ~30 specific foods and GI values
  - Query supports filtering by GI ≤ 55 or > 55
  - Bloom filters for low_gi and high_gi

**What's Missing:**
- ❌ **Only 2 conditions out of 4 required** — need at least 2 more
  - Suggested additions:
    - **Diabetes**: Glycemic Load filtering (GI × carbs), HbA1c considerations
    - **Hypertension**: Sodium limits, potassium/magnesium targets
    - Celiac: More rigorous gluten detection beyond keywords
    - Cardiovascular: Cholesterol/saturated fat limits
  
- ❌ **Missing RDA flagging**: No warnings when vitamins/minerals fall below RDA standards
- ❌ **No quantitative low-FODMAP rules**: Just keyword matching, not FODMAP score per food
- ❌ **No GI quantification for full meals**: Only individual food GI shown, not meal GI index

---

### ✅ **Capability 2: Allergy Detection & Exclusion** — 65% Complete

**What You Have:**
- ✅ Allergen list support (8 allergens in ALLOWED_ALLERGENS):
  - gluten, dairy, tree_nuts, peanuts, shellfish, soy, eggs, fish, meat
  
- ✅ Keyword-based allergen tagging (ALLERGEN_KEYWORDS in tag.py)
  - Each allergen has 5-15 keywords for detection
  - SQLite `allergens` column stores comma-separated labels
  
- ✅ Query filtering with zero-allergen guarantee
  - `filter_foods()` avoids foods if any requested allergen is in `allergens` field
  - Combined with dietary preference allergens

**What's Missing:**
- ❌ **Cross-contamination flagging**: No tracking of shared equipment, batch numbers, or manufacturing warnings
- ❌ **Traces vs. presence distinction**: Can't differentiate "may contain" vs. "contains"
- ❌ **Allergen confidence scoring**: No indicator whether detection is keyword-based vs. USDA certified
- ❌ **Tree nut specificity**: Treats all tree nuts as one allergen (doesn't distinguish hazelnut, walnut, etc.)
- ❌ **Severity levels**: All allergies treated equally (no anaphylaxis vs. intolerance distinction)

---

### ✅ **Capability 3: Dietary Preference Handling** — 85% Complete

**What You Have:**
- ✅ Vegan mode (DIETARY_ALLOWED hierarchy)
  - Excludes all animal products: meat, dairy, eggs, fish, shellfish
  
- ✅ Vegetarian mode
  - Excludes meat but allows dairy, eggs
  
- ✅ Non-vegetarian mode
  - Full access to all foods
  
- ✅ Pescetarian mode
  - Vegetarian + fish/seafood
  
- ✅ Cultural/religious constraints
  - Halal: avoids pork, alcohol, gelatin
  - Kosher: avoids pork, shellfish
  - Hindu: avoids beef
  - Jain: full vegan + avoids root vegetables (onion, garlic, potato, carrot, beet, etc.)
  
- ✅ Mixed household support
  - Can call `filter_foods()` separately per meal with different dietary_preference

**What's Missing:**
- ❌ **No meal-level mixed support in app.py**: Current Streamlit app forces same diet for entire plan
- ❌ **No texture preferences**: Pureed diets, soft foods for elderly/dysphagia not supported
- ❌ **Limited religious constraints**: Only 4 presets; no SDA (Seventh-day Adventist), Muslim specifics, etc.
- ❌ **No cost preferences**: No budget filtering or price per serving calculations
- ❌ **No pregnancy/nursing goals**: Different macro targets for pregnant users

---

### ❌ **Capability 4: Diversity Engine** — 30% Complete

**What You Have:**
- ✅ Category diversity tracking (FoodRanker.selected_categories)
  - Penalizes foods from already-selected categories within a day
  - diversity_score = 100 if unique category, 50 if repeat
  
- ✅ No meal repetition within 7-day plan
  - plan_week() tracks used_ids and never selects same food twice
  - Requires unique FDC IDs across all 21 meals (7 days × 3 meals)

**What's Missing:**
- ❌ **No diversity score metric**: FoodRanker computes score but no overall daily/weekly diversity metric reported to user
- ❌ **No category distribution reporting**: Doesn't show "You have 4 salads, 3 soups, 2 mains this week"
- ❌ **Only ingredient diversity checked**: Not meal diversity (e.g., all "grain bowls" with different ingredients)
- ❌ **No flavor profile tracking**: Could have monotonous meals (all savory, no sweet, all spicy, etc.)
- ❌ **No texture variety**: All soft foods or all crunchy — not balanced

---

### ✅ **Capability 5: Macro & Micronutrient Analysis** — 75% Complete

**What You Have:**
- ✅ Macronutrients tracked per meal and daily:
  - Calories ✅
  - Protein ✅
  - Carbs ✅
  - Fat ✅
  - Fiber ✅
  
- ✅ 5 micronutrients in database (from query.py DEFAULT_COLUMNS):
  - Iron ✅
  - Calcium ✅
  - Vitamin B12 ✅
  - Vitamin D ✅
  - Zinc ✅
  - (+ Sodium, Potassium, Magnesium tracked but not prominently displayed)
  
- ✅ Displayed in app.py meal plan view
  - Per-meal totals shown
  - Daily totals vs. goals displayed as metrics

**What's Missing:**
- ❌ **No RDA comparison**: Displays totals but no "vs. RDA target" flags
  - Should show: "Calcium: 800mg (goal: 1000mg) ⚠️ -20% below target"
  
- ❌ **No gap flagging**: No warnings if critical nutrients are deficient
  - E.g., B12 for vegan diets, Iron for vegetarian, Vitamin D for all
  
- ❌ **Limited micronutrient selection**: Only 5 required, but missing:
  - Vitamin A, Vitamin C, Folate, Magnesium (micronutrient list exists but not full coverage)
  
- ❌ **No bioavailability notes**: Doesn't account for nutrient absorption
  - E.g., plant-based iron (heme vs. non-heme) absorption differences
  
- ❌ **No meal-level micronutrient display**: Only daily totals shown, not breakdown by meal
  
- ❌ **No supplementation recommendations**: If B12 is low for vegan, doesn't suggest supplement

---

### ❌ **Capability 6: Sub-60-Second Generation** — 40% Complete

**What You Have:**
- ✅ Partial BAX-423 technique #1: **Bloom filters (Sketching)**
  - O(1) pre-filtering for FODMAP, GERD, allergens, GI
  - Reduces query time from linear scan to constant-time checks
  - Implemented in bloom.py and integrated in query.py
  
- ✅ Partial BAX-423 technique #2: **FAISS embeddings (embeddings)**
  - build_index.py creates FAISS nutritional embedding index
  - However, **NOT actively used in meal planning** 
  - Index built but never loaded/searched in meal_planner.py or app.py

- ❌ **No end-to-end timing measurement**
  - App doesn't log total generation time
  - No performance metrics displayed to user
  
- ❌ **No optimization for <60 seconds**
  - Meal planner does 5 random iterations per plan
  - For 7-day plan: 7 days × 3 meals × 5 iterations = 105 operations
  - No early stopping, caching, or parallelization
  
- ❌ **FAISS index not leveraged**
  - Could use similarity search to find nutritionally balanced meals 10-100x faster
  - Currently just ranking all foods linearly

**Performance Issues:**
- Potential bottleneck: filter_foods() does full database scan for every query
- No caching of filtered food lists between plan generations
- No parallelization of meal selection across days

---

## SUMMARY SCORECARD

| Capability | Score | Status |
|-----------|-------|--------|
| 1. Clinical Condition Filtering | 70% | ⚠️ Only 2/4 conditions |
| 2. Allergy Detection & Exclusion | 65% | ⚠️ No cross-contamination |
| 3. Dietary Preference Handling | 85% | ✅ Strong, missing some edge cases |
| 4. Diversity Engine | 30% | ❌ **Critical gap** — no metric/reporting |
| 5. Macro & Micronutrient Analysis | 75% | ⚠️ No RDA comparison, no gap flagging |
| 6. Sub-60-Second Generation | 40% | ❌ **Critical gap** — no timing, FAISS unused |
| **Overall** | **61%** | ⚠️ **Below 60% threshold** |

---

## PRIORITY ACTION ITEMS

### **Tier 1: Must-Fix (Blocking)**
To reach 60/100 minimum, fix these:

1. **Add 2 more clinical conditions** (Capability 1)
   - [ ] Diabetes: Glycemic Load (GI × carbs/100) calculations
   - [ ] Hypertension: Sodium per meal, potassium/magnesium targets
   - [ ] Implementation: Add tagging columns, update meal_planner scoring

2. **Implement Diversity Engine reporting** (Capability 4)
   - [ ] Add `compute_diversity_score()` function
   - [ ] Show category breakdown per day/week
   - [ ] Display in Streamlit as "Diversity Report"

3. **Add RDA comparison and gap flagging** (Capability 5)
   - [ ] Create RDA lookup table (age/sex dependent)
   - [ ] Flag nutrients < 80% of RDA
   - [ ] Display warnings in meal plan output

4. **Implement end-to-end timing & leverage FAISS** (Capability 6)
   - [ ] Load FAISS index in meal_planner
   - [ ] Use similarity search instead of linear ranking
   - [ ] Add `time.time()` logging around plan generation
   - [ ] Ensure <60s execution

---

### **Tier 2: Nice-to-Have (Polish)**
- [ ] Cross-contamination flagging (Capability 2)
- [ ] Meal-level micronutrient breakdown (Capability 5)
- [ ] Supplementation recommendations (Capability 5)
- [ ] Texture preferences (Capability 3)
- [ ] Mixed household UI (Capability 3)

---

## TECHNICAL BRIEF: BAX-423 TECHNIQUES USED

### **Technique 1: Bloom Filters (Sketching)** ✅
- **Purpose**: O(1) constant-time pre-filtering for allergens, FODMAP, GERD
- **Implementation**: pybloom-live library (bloom.py)
- **Benefit**: Reduces database scan from O(n) to ~5 O(1) checks per food
- **Trade-off**: Small false positive rate (0.1%) acceptable for food safety (false negatives = failure)

### **Technique 2: FAISS Embeddings** ⚠️ Built but Unused
- **Purpose**: Fast similarity search for nutritionally balanced meal combinations
- **Implementation**: Numpy embeddings → FAISS index (build_index.py)
- **Status**: Index built, but never loaded/used in meal_planner.py
- **Fix needed**: Integrate into plan_week() to replace linear ranking with similarity search
- **Expected speedup**: 10-100x faster for large food databases

### **Missing: Reinforcement Learning (RL)** 
- Currently uses basic heuristic scoring (meal_learning.json tracks past plans)
- Could implement RL to optimize for user satisfaction over multiple generations
- Low priority unless planning iterations increase

---

## FILES REQUIRING CHANGES

```
data_pipeline/
├── filters/
│   ├── tag.py              [ADD 2 more clinical conditions]
│   └── query.py            [Add RDA comparison logic]
├── meal_planner.py         [ADD timing, integrate FAISS, compute diversity]
└── pipeline/
    └── build_index.py      [Ensure FAISS index loads]

app.py                       [ADD diversity report, RDA warnings, generation timing]
```

---

## NEXT STEPS

1. **Review this assessment** with your project requirements
2. **Prioritize Tier 1 fixes** (20-30 hours of work to reach 60+)
3. **Set up timing instrumentation** (start here — fastest win)
4. **Implement Diabetes/Hypertension conditions**
5. **Add diversity score metric**
6. **Integrate FAISS for performance**
7. **Test sub-60s generation** on typical profiles
