import time

import streamlit as st
from data_pipeline.filters.query import (
    ALLOWED_ALLERGENS,
    CULTURAL_PRESETS,
    filter_foods,
    get_diet_exclude_keywords,
    get_diet_violations,
)
from data_pipeline.meal_planner import MealPlanner, NutritionGoals, get_rda_targets

st.set_page_config(page_title="NutriAI: Food Filtering & Meal Planning", layout="wide")

st.title("🥗 NutriAI: Food Filtering & Meal Planning")
st.markdown(
    "Use the clinical tags and nutrition data from `data/foods.db` to filter foods "
    "for FODMAP, GERD, allergens, glycemic index, and generate personalized meal plans."
)

# Create tabs for different features
tab1, tab2, tab3 = st.tabs(["Food Search", "Meal Planner", "Filtered Foods"])

if "filtered_food_report" not in st.session_state:
    st.session_state.filtered_food_report = []
if "filtered_food_context" not in st.session_state:
    st.session_state.filtered_food_context = "No Meal Planner filter report has been generated yet."

PRIORITY_NUTRIENT_OPTIONS = {
    "Protein": "protein",
    "Fiber": "fiber",
    "Iron": "iron",
    "Calcium": "calcium",
    "Vitamin B12": "vitamin_b12",
    "Vitamin D": "vitamin_d",
    "Zinc": "zinc",
    "Magnesium": "magnesium",
    "Potassium": "potassium",
}

# ═══════════════════════════════════════════════════════════════════════════════
# SHARED FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def get_allergens_for_diet(diet: str) -> list[str]:
    """Return allergens to avoid based on dietary preference."""
    allergen_exclusions = {
        "Vegetarian": ["meat"],
        "Vegan": ["meat", "dairy", "eggs", "fish", "shellfish"],
        "Pescetarian": ["meat"],
        "Red meat only": ["poultry", "fish", "shellfish"],
        "No restrictions": [],
    }
    return allergen_exclusions.get(diet, [])


def get_diet_keywords(diet: str) -> list[str]:
    """Return description keywords to exclude for dietary restrictions."""
    normalized = {
        "No restrictions": None,
        "Vegetarian": "vegetarian",
        "Vegan": "vegan",
        "Pescetarian": "pescetarian",
        "Red meat only": "non-vegetarian",
    }.get(diet, None)
    return get_diet_exclude_keywords(normalized)


def normalize_dietary_preference(diet: str) -> str | None:
    """Convert display labels into query-safe dietary preference values."""
    return {
        "No restrictions": None,
        "Vegetarian": "vegetarian",
        "Vegan": "vegan",
        "Pescetarian": "pescetarian",
        "Red meat only": "non-vegetarian",
    }.get(diet, None)


def normalize_cultural_preset(preset: str) -> str | None:
    """Convert display labels into query-safe cultural preset values."""
    return {
        "None": None,
        "Halal": "halal",
        "Kosher": "kosher",
        "Hindu": "hindu",
        "Jain": "jain",
    }.get(preset, None)


def build_restriction_profile(
    selected_restrictions: list[str],
    cultural_preset_label: str,
) -> tuple[list[str], str | None, list[str]]:
    """Return allergen filters plus cultural preset after special restrictions."""
    normalized = [item.lower().replace(" ", "_") for item in selected_restrictions]
    avoid_allergens = [
        item for item in selected_restrictions
        if item.lower().replace(" ", "_") != "pork"
    ]
    cultural_preference = normalize_cultural_preset(cultural_preset_label)
    notes = []

    if "pork" in normalized:
        if cultural_preference is None:
            cultural_preference = "halal"
            notes.append("pork avoided through Halal preset")
        else:
            notes.append("pork avoided through cultural preset")

    return avoid_allergens, cultural_preference, notes


def combine_dietary_preferences(
    dietary_preference: str | None,
    preset_dietary_preference: str | None,
) -> str | None:
    """Return the stricter diet when both profile and cultural preset provide one."""
    strictness = {
        "vegan": 0,
        "vegetarian": 1,
        "pescetarian": 2,
        "non-vegetarian": 3,
    }
    if not dietary_preference:
        return preset_dietary_preference
    if not preset_dietary_preference:
        return dietary_preference
    user_diet = dietary_preference.lower().replace(" ", "-")
    preset_diet = preset_dietary_preference.lower().replace(" ", "-")
    if user_diet not in strictness or preset_diet not in strictness:
        return dietary_preference
    return user_diet if strictness[user_diet] <= strictness[preset_diet] else preset_diet


CROSS_CONTAMINATION_PHRASES = [
    "may contain",
    "shared equipment",
    "same equipment",
    "same facility",
    "processed in a facility",
    "manufactured in a facility",
    "traces of",
    "cross contamination",
    "cross-contamination",
    "cross contact",
    "cross-contact",
]


def get_cross_contamination_risks(food: dict, avoid_allergens: list[str] | None = None) -> list[str]:
    """Return possible may-contain/shared-equipment risks from available food text."""
    if not avoid_allergens:
        return []

    text = " ".join([
        str(food.get("description") or ""),
        str(food.get("allergens") or ""),
        str(food.get("food_category") or ""),
    ]).lower()
    normalized_allergens = [a.lower().replace(" ", "_") for a in avoid_allergens]
    risks = []

    for allergen in normalized_allergens:
        allergen_text = allergen.replace("_", " ")
        direct_tag = allergen in str(food.get("allergens") or "").lower()
        may_contain = any(phrase in text for phrase in CROSS_CONTAMINATION_PHRASES) and (
            allergen in text or allergen_text in text
        )
        if direct_tag or may_contain:
            label = allergen.replace("_", " ")
            risks.append(f"May contain/shared-equipment risk for {label}.")

    return risks


# ═══════════════════════════════════════════════════════════════════════════════
def explain_food_violations(
    food: dict,
    *,
    dietary_preference: str | None = None,
    cultural_preset: str | None = None,
    exclude_fodmap: bool = False,
    exclude_gerd: bool = False,
    avoid_allergens: list[str] | None = None,
    glycemic_target: str | None = None,
    max_sodium: float | None = None,
    diet_keywords: list[str] | None = None,
    mixed_household: dict[str, str | None] | None = None,
) -> list[str]:
    """Return every meal-planner constraint this food violates."""
    reasons = []
    desc_lower = str(food.get("description") or "").lower()

    effective_diet = dietary_preference
    cultural_keywords = []
    if cultural_preset:
        preset = CULTURAL_PRESETS.get(cultural_preset.lower())
        if preset:
            effective_diet = combine_dietary_preferences(
                effective_diet,
                preset.get("dietary_preference"),
            )
            cultural_keywords = preset.get("avoid_keywords", [])

    if mixed_household:
        blocked_slots = []
        eligible_slots = []
        slot_reasons = []
        for slot, slot_diet in mixed_household.items():
            if not slot_diet:
                eligible_slots.append(slot.title())
                continue
            diet_violations = get_diet_violations(food, slot_diet)
            if diet_violations:
                blocked_slots.append(slot.title())
                slot_reasons.extend(diet_violations)
            else:
                eligible_slots.append(slot.title())
        if blocked_slots and not eligible_slots:
            reasons.append(
                f"Not allowed for any mixed-household meal slot ({', '.join(blocked_slots)})."
            )
        elif blocked_slots:
            reasons.append(
                f"Mixed-household slot restriction: not eligible for {', '.join(blocked_slots)}."
            )
        for slot_reason in dict.fromkeys(slot_reasons):
            reasons.append(slot_reason)
    elif effective_diet:
        reasons.extend(get_diet_violations(food, effective_diet))

    matched_cultural = [kw for kw in cultural_keywords if kw in desc_lower]
    if matched_cultural:
        reasons.append(f"Cultural preset blocks keyword(s): {', '.join(matched_cultural[:5])}.")

    matched_diet_keywords = [kw for kw in (diet_keywords or []) if kw in desc_lower]
    if matched_diet_keywords:
        reasons.append(f"Diet keyword exclusion matched: {', '.join(matched_diet_keywords[:5])}.")

    if exclude_fodmap and bool(food.get("is_high_fodmap")):
        reasons.append("High-FODMAP for IBS.")

    if exclude_gerd and bool(food.get("is_gerd_trigger")):
        reasons.append("GERD trigger.")

    food_allergens = str(food.get("allergens") or "").lower()
    matched_allergens = [
        allergen for allergen in (avoid_allergens or [])
        if allergen.lower().replace(" ", "_") in food_allergens
    ]
    if matched_allergens:
        allergen_text = ", ".join(matched_allergens)
        if "gluten" in [a.lower() for a in matched_allergens]:
            reasons.append(f"Contains avoided allergen/intolerance tag(s): {allergen_text}; cross-contamination risk for Celiac/gluten intolerance.")
        else:
            reasons.append(f"Contains avoided allergen/intolerance tag(s): {allergen_text}.")

    cross_contamination_risks = get_cross_contamination_risks(food, avoid_allergens)
    for risk in cross_contamination_risks:
        reasons.append(f"Cross-contamination flag: {risk}")

    gi = food.get("glycemic_index")
    if glycemic_target == "low":
        if gi is None:
            reasons.append("Missing glycemic index, so it cannot satisfy the low-GI diabetes constraint.")
        elif float(gi) > 55:
            reasons.append(f"Glycemic index {float(gi):.0f} is above the low-GI limit of 55.")
    elif glycemic_target == "high":
        if gi is None:
            reasons.append("Missing glycemic index, so it cannot satisfy the high-GI filter.")
        elif float(gi) <= 55:
            reasons.append(f"Glycemic index {float(gi):.0f} is not above 55.")

    sodium = food.get("sodium")
    if max_sodium is not None and sodium is not None and float(sodium) > max_sodium:
        reasons.append(f"Sodium {float(sodium):.0f}mg exceeds the per-food limit of {max_sodium:.0f}mg.")

    return reasons


def build_filtered_food_report(all_foods: list[dict], **constraint_kwargs) -> list[dict]:
    rows = []
    for food in all_foods:
        reasons = explain_food_violations(food, **constraint_kwargs)
        if reasons:
            description = food.get("description") or "Food"
            cross_contamination_risks = get_cross_contamination_risks(
                food,
                constraint_kwargs.get("avoid_allergens"),
            )
            rows.append({
                "FDC ID": food.get("fdc_id"),
                "Description": description,
                "Category": food.get("food_category"),
                "Calories": food.get("calories"),
                "Protein": food.get("protein"),
                "Sodium": food.get("sodium"),
                "GI": food.get("glycemic_index"),
                "Dietary Category": food.get("dietary_category"),
                "Cross-Contamination Risk": "Yes" if cross_contamination_risks else "No",
                "Cross-Contamination Detail": " ".join(cross_contamination_risks),
                "Why Filtered": f"{description} excluded: {' '.join(reasons)}",
            })
    return rows


def nutrient_comparison_rows(plan, goals: NutritionGoals) -> list[dict]:
    rows = []
    for nutrient, target in get_rda_targets(goals.age, goals.sex, goals).items():
        actual = float(plan.nutrient_totals.get(nutrient, 0) or 0)
        percent = (actual / target * 100) if target else 100.0
        rows.append({
            "Nutrient": nutrient.replace("_", " ").title(),
            "Daily Total": round(actual, 1),
            "RDA/Goal": round(float(target), 1),
            "% of Target": round(percent, 1),
            "80% Threshold": round(float(target) * 0.80, 1),
            "Status": "Below 80%" if percent < 80 else "OK",
        })
    return rows


def meal_nutrient_rows(plan) -> list[dict]:
    rows = []
    for meal_idx, meal in enumerate(plan.meals, 1):
        nutrients = meal.get("_nutrient_scaled") or {}
        rows.append({
            "Meal": meal_idx,
            "Food": meal.get("description"),
            "Calories": nutrients.get("calories", meal.get("_cal_scaled", 0)),
            "Protein": nutrients.get("protein", meal.get("_prot_scaled", 0)),
            "Carbs": nutrients.get("carbs", meal.get("_carbs_scaled", 0)),
            "Fat": nutrients.get("fat", meal.get("_fat_scaled", 0)),
            "Fiber": nutrients.get("fiber", meal.get("_fiber_scaled", 0)),
            "Iron": nutrients.get("iron", 0),
            "Calcium": nutrients.get("calcium", 0),
            "Vitamin B12": nutrients.get("vitamin_b12", 0),
            "Vitamin D": nutrients.get("vitamin_d", 0),
            "Zinc": nutrients.get("zinc", 0),
            "Sodium": nutrients.get("sodium", meal.get("_sodium_scaled", 0)),
            "Potassium": nutrients.get("potassium", meal.get("_potassium_scaled", 0)),
            "Magnesium": nutrients.get("magnesium", 0),
        })
    return rows


def cross_contamination_rows(foods: list[dict], avoid_allergens: list[str] | None) -> list[dict]:
    rows = []
    for food in foods:
        risks = get_cross_contamination_risks(food, avoid_allergens)
        if risks:
            rows.append({
                "Food": food.get("description"),
                "Meal Slot": str(food.get("_slot") or "").title(),
                "Risk": " ".join(risks),
            })
    return rows


def show_nutrition_analysis(plan, goals: NutritionGoals) -> None:
    st.subheader("Per-Meal Macro & Micronutrients")
    st.dataframe(meal_nutrient_rows(plan), use_container_width=True)

    st.subheader("Daily RDA / Goal Comparison")
    comparison = nutrient_comparison_rows(plan, goals)
    st.dataframe(comparison, use_container_width=True)
    below = [row for row in comparison if row["Status"] == "Below 80%"]
    if below:
        st.warning("This day falls below 80% for one or more macro/micronutrient targets.")
        st.table(below)
    else:
        st.success("This day meets at least 80% of all tracked macro/micronutrient targets.")


# TAB 1: FOOD FILTERS
# ═══════════════════════════════════════════════════════════════════════════════

with tab1:
    st.header("👤 Your Profile")

    col1, col2, col3 = st.columns(3)
    with col1:
        age = st.number_input("Age", min_value=1, max_value=120, value=30, step=1, key="age_tab1")
    with col2:
        sex_tab1 = st.selectbox("Sex", ["Not specified", "Male", "Female", "Other"], key="sex_tab1")
    with col3:
        dietary_restriction = st.selectbox(
            "Dietary Preference",
            [
                "No restrictions",
                "Vegetarian",
                "Vegan",
                "Pescetarian",
                "Red meat only",
            ],
            key="diet_tab1"
        )

    allergies = st.multiselect(
        "Known allergies/intolerances",
        sorted(ALLOWED_ALLERGENS | {"pork"}),
        default=[],
        key="allergies_tab1"
    )

    st.markdown("---")

    # ─── Dietary Filters Section ────────────────────────────────────────────────
    st.header("🍽️ Food Filters")

    col_filter1, col_filter2 = st.columns(2)
    with col_filter1:
        query = st.text_input("Search food description", value="", key="search_food_tab1", placeholder="e.g., chicken, rice, salad")
    with col_filter2:
        glycemic_target = st.radio(
            "Glycemic index",
            ["any", "low", "high"],
            index=0,
            help="Low = GI <= 55, High = GI > 55",
            key="gi_tab1",
            horizontal=True
        )

    col_filter3, col_filter4, col_filter5 = st.columns(3)
    with col_filter3:
        exclude_fodmap = st.checkbox("Exclude high-FODMAP foods", value=False, key="fodmap_tab1")
    with col_filter4:
        exclude_gerd = st.checkbox("Exclude GERD triggers", value=False, key="gerd_tab1")
    with col_filter5:
        cultural_preset = st.selectbox(
            "Religious / cultural preference",
            ["None", "Halal", "Kosher", "Hindu", "Jain"],
            index=0,
            help="Apply additional cultural filtering via keywords and dietary presets.",
            key="cultural_tab1"
        )

    col_btn1, col_btn2 = st.columns([1, 4])
    with col_btn1:
        clear_query = st.button("🔄 Clear", key="clear_query_tab1")
        if clear_query:
            query = ""
    with col_btn2:
        pass


    if st.button("🔍 Find Foods", key="search_button_tab1"):
        try:
            # Combine user allergies with dietary preference allergens
            dietary_allergens = get_allergens_for_diet(dietary_restriction)
            combined_restrictions = list(set(allergies + dietary_allergens))
            diet_keywords = get_diet_keywords(dietary_restriction)
            dietary_preference = normalize_dietary_preference(dietary_restriction)
            combined_allergens, cultural_preference, special_notes = build_restriction_profile(
                combined_restrictions,
                cultural_preset,
            )
            
            foods = filter_foods(
                exclude_fodmap=exclude_fodmap,
                exclude_gerd=exclude_gerd,
                avoid_allergens=combined_allergens or None,
                glycemic_target=glycemic_target if glycemic_target != "any" else None,
                search_term=query or None,
                dietary_preference=dietary_preference,
                cultural_preset=cultural_preference,
            )

            # Apply dietary keyword exclusions to catch foods not tagged with the allergen label yet.
            filtered_foods = []
            for food in foods:
                desc_lower = food["description"].lower()
                if not any(keyword in desc_lower for keyword in diet_keywords):
                    filtered_foods.append(food)

            st.success(f"Found {len(filtered_foods)} food(s) matching your profile")
            
            if filtered_foods:
                profile_details = f"**Profile:** {age} years old, {sex_tab1}, {dietary_restriction}"
                if cultural_preset and cultural_preset != "None":
                    profile_details += f", {cultural_preset}"
                st.write(profile_details)
                if allergies or dietary_allergens:
                    combined = combined_allergens
                    st.write(f"**Allergies/Intolerances:** {', '.join(combined)}")
                if special_notes:
                    st.write(f"**Special restrictions:** {', '.join(special_notes)}")
                
                df = [
                    {
                        "FDC ID": f["fdc_id"],
                        "Description": f["description"],
                        "Category": f["food_category"],
                        "Calories": f["calories"],
                        "Protein": f["protein"],
                        "Carbs": f["carbs"],
                        "Fat": f["fat"],
                        "Fiber": f["fiber"],
                        "Allergens": f["allergens"],
                        "GI": f["glycemic_index"],
                        "High FODMAP": bool(f["is_high_fodmap"]),
                        "GERD Trigger": bool(f["is_gerd_trigger"]),
                    }
                    for f in filtered_foods
                ]
                st.dataframe(df, use_container_width=True)
            else:
                st.warning("No foods matched your profile and filter settings.")
        except Exception as exc:
            st.error(f"Unable to query foods.db: {exc}")
            st.stop()
    else:
        st.info("Fill in your profile above, choose additional filters on the left, then click **Find Foods**.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2: MEAL PLANNER
# ═══════════════════════════════════════════════════════════════════════════════

with tab2:
    st.header("👥 Daily Nutrition Goals")

    col_a, col_b, col_c, col_g = st.columns(4)
    with col_a:
        sex_tab2 = st.selectbox("Sex", ["Not specified", "Male", "Female", "Other"], key="sex_tab2")
    with col_b:
        age_tab2 = st.number_input("Age", min_value=1, max_value=120, value=30, step=1, key="age_tab2")
    with col_c:
        daily_calories = st.number_input(
            "Daily calorie target",
            min_value=1000,
            max_value=5000,
            value=2000,
            step=50,
            help="Recommended: 2000 (Female), 2500 (Male)",
            key="cal_tab2"
        )
    with col_g:
        daily_protein = st.number_input(
            "Daily protein target (grams)",
            min_value=30,
            max_value=300,
            value=75,
            step=5,
            help="Recommended: 0.8-1g per lb of body weight",
            key="prot_tab2"
        )

    st.markdown("---")
    st.header("🍽️ Filter Options")

    col_d, col_e, col_f, col_g, col_h = st.columns(5)
    with col_d:
        dietary_pref_tab2 = st.selectbox(
            "Dietary Preference",
            [
                "No restrictions",
                "Vegetarian",
                "Vegan",
                "Pescetarian",
                "Red meat only",
            ],
            key="diet_tab2"
        )
    with col_e:
        allergies_tab2 = st.multiselect(
            "Allergies/intolerances",
            sorted(ALLOWED_ALLERGENS | {"pork"}),
            default=[],
            key="allergies_tab2"
        )
    with col_f:
        exclude_fodmap_tab2 = st.checkbox("Exclude high-FODMAP", value=False, key="fodmap_tab2")
    with col_g:
        glycemic_target_tab2 = st.radio(
            "Glycemic index",
            ["any", "low", "high"],
            index=0,
            help="Low = GI <= 55. Diabetes automatically uses low GI unless you choose a stricter filter.",
            key="gi_tab2",
        )
    with col_h:
        cultural_preset_tab2 = st.selectbox(
            "Religious / cultural preference",
            ["None", "Halal", "Kosher", "Hindu", "Jain"],
            index=0,
            help="Applies religious/cultural keyword exclusions and any matching dietary preset.",
            key="cultural_tab2",
        )

    mixed_household_enabled = st.checkbox(
        "Mixed household: set different diets by meal",
        value=False,
        help="Use this when breakfast, lunch, and dinner need different diet rules.",
        key="mixed_household_tab2",
    )
    mixed_household_tab2 = None
    if mixed_household_enabled:
        diet_choices_tab2 = [
            "No restrictions",
            "Vegetarian",
            "Vegan",
            "Pescetarian",
            "Red meat only",
        ]
        mix_cols = st.columns(3)
        mixed_household_tab2 = {}
        for slot, label, col in [
            ("breakfast", "Breakfast diet", mix_cols[0]),
            ("lunch", "Lunch diet", mix_cols[1]),
            ("dinner", "Dinner diet", mix_cols[2]),
        ]:
            with col:
                slot_choice = st.selectbox(
                    label,
                    diet_choices_tab2,
                    index=diet_choices_tab2.index(dietary_pref_tab2),
                    key=f"{slot}_diet_tab2",
                )
                mixed_household_tab2[slot] = normalize_dietary_preference(slot_choice)

    exclude_gerd_tab2 = st.checkbox("Exclude GERD triggers", value=False, key="gerd_tab2")
    clinical_conditions_tab2 = st.multiselect(
        "Clinical conditions",
        ["IBS (FODMAP)", "GERD", "Diabetes", "Hypertension"],
        default=[],
        help="Adds condition-specific scoring and targets. IBS/GERD also enable the matching exclusions.",
        key="clinical_conditions_tab2",
    )
    exclude_fodmap_tab2 = exclude_fodmap_tab2 or "IBS (FODMAP)" in clinical_conditions_tab2
    exclude_gerd_tab2 = exclude_gerd_tab2 or "GERD" in clinical_conditions_tab2

    priority_nutrient_labels_tab2 = st.multiselect(
        "Priority nutrients that must reach at least 80% of RDA/goal",
        list(PRIORITY_NUTRIENT_OPTIONS.keys()),
        default=[],
        help="Generated plans must meet the 80% threshold for every selected nutrient.",
        key="priority_nutrients_tab2",
    )
    priority_nutrients_tab2 = [
        PRIORITY_NUTRIENT_OPTIONS[label]
        for label in priority_nutrient_labels_tab2
    ]
    
    min_diversity_score = st.slider(
        "Minimum diversity score (strict for ALL 7 days)",
        min_value=0,
        max_value=100,
        value=0,
        step=5,
        help="All meals across all 7 days must have a diversity score at or above this value. Set to 0 for no restriction.",
        key="min_diversity_tab2"
    )
    
    max_sodium_mg = st.slider(
        "Maximum daily sodium (mg) - informational only",
        min_value=0,
        max_value=5000,
        value=2300,
        step=100,
        help="For reference (FDA daily limit is 2300mg, hypertension target is 1500mg). This slider is informational.",
        key="max_sodium_tab2"
    )

    plan_mode = st.radio(
        "Plan horizon",
        ["Daily", "7-Day Weekly"],
        index=1,
        help="Choose whether to generate a single-day plan or a full 7-day plan with unique meals."
    )

    st.markdown("---")

    if st.button("📊 Generate Meal Plans", key="generate_plans"):
        try:
            # Get filtered foods for meal planning
            dietary_allergens_tab2 = [] if mixed_household_tab2 else get_allergens_for_diet(dietary_pref_tab2)
            combined_restrictions_tab2 = list(set(allergies_tab2 + dietary_allergens_tab2))
            dietary_preference_tab2 = normalize_dietary_preference(dietary_pref_tab2)
            combined_allergens_tab2, cultural_preference_tab2, special_notes_tab2 = build_restriction_profile(
                combined_restrictions_tab2,
                cultural_preset_tab2,
            )
            diet_keywords_tab2 = [] if mixed_household_tab2 else get_diet_keywords(dietary_pref_tab2)
            planner_dietary_preference = None if mixed_household_tab2 else dietary_preference_tab2
            scoring_conditions = [
                condition.lower()
                for condition in clinical_conditions_tab2
                if condition in {"Diabetes", "Hypertension"}
            ]
            planner_glycemic_target = glycemic_target_tab2 if glycemic_target_tab2 != "any" else None
            if "Diabetes" in clinical_conditions_tab2 and planner_glycemic_target is None:
                planner_glycemic_target = "low"
            planner_max_sodium = None  # Daily limit of 1500mg enforced in NutritionGoals, not per-food

            report_pool = filter_foods(limit=None)
            st.session_state.filtered_food_report = build_filtered_food_report(
                report_pool,
                dietary_preference=planner_dietary_preference,
                cultural_preset=cultural_preference_tab2,
                exclude_fodmap=exclude_fodmap_tab2,
                exclude_gerd=exclude_gerd_tab2,
                avoid_allergens=combined_allergens_tab2 or None,
                glycemic_target=planner_glycemic_target,
                max_sodium=planner_max_sodium,
                diet_keywords=diet_keywords_tab2,
                mixed_household=mixed_household_tab2,
            )
            active_filters = []
            if mixed_household_tab2:
                slot_filters = [
                    f"{slot.title()}: {diet or 'no restrictions'}"
                    for slot, diet in mixed_household_tab2.items()
                ]
                active_filters.append("mixed household (" + "; ".join(slot_filters) + ")")
            elif dietary_pref_tab2 != "No restrictions":
                active_filters.append(dietary_pref_tab2)
            if cultural_preset_tab2 != "None":
                active_filters.append(cultural_preset_tab2)
            if exclude_fodmap_tab2:
                active_filters.append("high-FODMAP foods removed")
            if exclude_gerd_tab2:
                active_filters.append("GERD triggers removed")
            if combined_allergens_tab2:
                active_filters.append(f"allergens avoided: {', '.join(combined_allergens_tab2)}")
            active_filters.extend(special_notes_tab2)
            if planner_glycemic_target:
                active_filters.append(f"{planner_glycemic_target}-GI foods")
            if "Hypertension" in clinical_conditions_tab2:
                active_filters.append("sodium limit for hypertension")
            if priority_nutrient_labels_tab2:
                active_filters.append(
                    "priority nutrients at 80%+: " + ", ".join(priority_nutrient_labels_tab2)
                )
            st.session_state.filtered_food_context = (
                "Meal Planner filters: " + (", ".join(active_filters) if active_filters else "none")
            )

            available_foods = filter_foods(
                exclude_fodmap=exclude_fodmap_tab2,
                exclude_gerd=exclude_gerd_tab2,
                avoid_allergens=combined_allergens_tab2 or None,
                glycemic_target=planner_glycemic_target,
                dietary_preference=planner_dietary_preference,
                cultural_preset=cultural_preference_tab2,
                max_sodium=planner_max_sodium,
                limit=None,  # Get all foods for meal planning
            )

            # Filter out foods by keywords
            available_foods = [
                f for f in available_foods
                if not any(kw in f["description"].lower() for kw in diet_keywords_tab2)
            ]

            if not available_foods:
                st.error("No foods available matching your constraints. Adjust filters and try again.")
                st.stop()

            st.info(f"Generating meal plans from {len(available_foods)} available foods")

            # Create nutrition goals and planner
            goals = NutritionGoals.from_sex_and_goals(
                sex=sex_tab2,
                total_calories=daily_calories,
                protein_grams=daily_protein,
                age=age_tab2,
                clinical_conditions=scoring_conditions,
                priority_nutrients=priority_nutrients_tab2 or None,
                glycemic_target=planner_glycemic_target,
            )

            planner = MealPlanner(goals)
            generation_start = time.time()
            if plan_mode == "7-Day Weekly":
                meal_plans = planner.plan_week(
                    available_foods,
                    num_days=7,
                    meals_per_day=3,
                    dietary_preference=planner_dietary_preference,
                    cultural_preset=cultural_preference_tab2,
                    exclude_fodmap=exclude_fodmap_tab2,
                    exclude_gerd=exclude_gerd_tab2,
                    avoid_allergens=combined_allergens_tab2 or None,
                    clinical_conditions=scoring_conditions,
                    glycemic_target=planner_glycemic_target,
                    mixed_household=mixed_household_tab2,
                    min_diversity_score=min_diversity_score,
                )
            else:
                meal_plans = planner.plan_day(
                    available_foods,
                    dietary_preference=planner_dietary_preference,
                    cultural_preset=cultural_preference_tab2,
                    exclude_fodmap=exclude_fodmap_tab2,
                    exclude_gerd=exclude_gerd_tab2,
                    avoid_allergens=combined_allergens_tab2 or None,
                    clinical_conditions=scoring_conditions,
                    glycemic_target=planner_glycemic_target,
                    mixed_household=mixed_household_tab2,
                    num_meals=3,
                )
            generation_elapsed = time.time() - generation_start

            if not meal_plans:
                if priority_nutrient_labels_tab2:
                    st.error(
                        "No meal plan met the selected priority nutrient thresholds. "
                        f"Required at 80%+: {', '.join(priority_nutrient_labels_tab2)}. "
                        "Try selecting fewer priority nutrients or loosening other filters."
                    )
                else:
                    st.error("No meal plans could be generated with the current filters.")
                st.stop()

            # Display results
            if plan_mode == "7-Day Weekly":
                st.success(f"Generated a 7-day meal plan with 3 unique meals per day in {generation_elapsed:.2f} seconds.")
            else:
                st.success(f"Generated 5 meal plan options in {generation_elapsed:.2f} seconds.")
            timing_cols = st.columns(3)
            with timing_cols[0]:
                st.metric("Generation Time", f"{generation_elapsed:.2f}s")
            with timing_cols[1]:
                st.metric("Under 60 Seconds", "Yes" if generation_elapsed < 60 else "No")
            with timing_cols[2]:
                st.metric("Food Pool", f"{len(available_foods):,}")

            

            # Show improvement metrics
            st.subheader("📈 Learning & Optimization Progress")
            metrics = planner.get_improvement_metrics()
            if "status" not in metrics:
                col_m1, col_m2, col_m3, col_m4 = st.columns(4)
                with col_m1:
                    st.metric("Plans Generated", metrics["total_plans_generated"])
                with col_m2:
                    st.metric("Current Avg Score", f"{metrics['current_avg']}/100")
                with col_m3:
                    st.metric("Best Ever", f"{metrics['best_score_ever']}/100")
                with col_m4:
                    improvement_text = f"+{metrics['improvement_pct']}%" if metrics['improvement_pct'] > 0 else f"{metrics['improvement_pct']}%"
                    st.metric("Improvement", improvement_text)
            else:
                st.info(metrics["status"])

            st.markdown("---")

            # Display each meal plan option
            if plan_mode == "7-Day Weekly":
                for day_idx, plan in enumerate(meal_plans, 1):
                    with st.expander(f"Day {day_idx} - Overall Score: {plan.score:.1f}/100", expanded=(day_idx == 1)):
                        st.subheader(f"Meals")
                        for meal_idx, meal in enumerate(plan.meals, 1):
                            st.write(f"**Meal {meal_idx}:** {meal['description']}")
                            st.caption(f"Category: {meal['food_category']} | Cal: {meal.get('_cal_scaled', meal.get('calories', 0)):.0f} | Protein: {meal.get('_prot_scaled', meal.get('protein', 0)):.1f}g | GI: {meal.get('glycemic_index', 'n/a')}")
                            if meal.get("_items"):
                                st.table([
                                    {
                                        "Item": item.get("description", ""),
                                        "Serving (g)": item.get("_serving", 0),
                                        "Calories": item.get("_cal_scaled", 0),
                                        "GI": item.get("glycemic_index"),
                                    }
                                    for item in meal["_items"]
                                ])
                        contamination_rows = cross_contamination_rows(plan.meals, combined_allergens_tab2)
                        if contamination_rows:
                            st.warning("Potential may-contain/shared-equipment risk found in selected meals.")
                            st.table(contamination_rows)
                        
                        st.markdown("---")
                        st.subheader("Daily Totals vs. Goals")
                        metrics_cols = st.columns(3)
                        with metrics_cols[0]:
                            st.metric(
                                "Calories",
                                f"{plan.total_calories:.0f}",
                                f"{plan.total_calories - daily_calories:+.0f}",
                                delta_color="off"
                            )
                        with metrics_cols[1]:
                            st.metric(
                                "Protein",
                                f"{plan.total_protein:.1f}g",
                                f"{plan.total_protein - daily_protein:+.1f}g",
                                delta_color="off"
                            )
                        
                        st.subheader("Ranking Scores")
                        score_cols = st.columns(6)
                        with score_cols[0]:
                            st.metric("Calorie Fit", f"{plan.calorie_score:.1f}/100")
                        with score_cols[1]:
                            st.metric("Protein Fit", f"{plan.protein_score:.1f}/100")
                        with score_cols[2]:
                            st.metric("Variety", f"{plan.variety_score:.1f}/100")
                        with score_cols[3]:
                            st.metric("Health", f"{plan.health_score:.2f}/100")
                        with score_cols[4]:
                            st.metric("Diversity", f"{plan.diversity_score:.2f}/100")
                        with score_cols[5]:
                            st.metric("Glycemic Load", f"{plan.glycemic_load_score:.1f}/100")

                        # Display Diversity Breakdown
                        if plan.diversity_breakdown:
                            st.subheader(f"🎯 Diversity Analysis: {plan.diversity_label}")
                            bd = plan.diversity_breakdown
                            div_cols = st.columns(5)
                            with div_cols[0]:
                                st.metric("Unique Proteins", len(bd.get("unique_proteins", [])))
                            with div_cols[1]:
                                st.metric("Unique Categories", len(bd.get("unique_categories", [])))
                            with div_cols[2]:
                                st.metric("Plant Meals", bd.get("plant_meals", 0))
                            with div_cols[3]:
                                st.metric("Unique Methods", len(bd.get("unique_methods", [])))
                            with div_cols[4]:
                                st.metric("Unique Cuisines", len(bd.get("unique_cuisines", [])))
                            
                            st.caption(f"**Proteins:** {', '.join(bd.get('unique_proteins', []))}")
                            st.caption(f"**Categories:** {', '.join(bd.get('unique_categories', []))}")
                            
                            if bd.get("penalties_applied"):
                                st.warning("**Penalties applied:** " + "; ".join(bd.get("penalties_applied", [])))


                        st.subheader("Clinical Targets")
                        clinical_cols = st.columns(3)
                        with clinical_cols[0]:
                            st.metric("Glycemic Load", f"{plan.glycemic_load:.1f}")
                        with clinical_cols[1]:
                            st.metric("Sodium", f"{plan.total_sodium:.0f}mg", f"{plan.total_sodium - goals.max_sodium:+.0f}mg", delta_color="inverse")
                        with clinical_cols[2]:
                            st.metric("Potassium", f"{plan.total_potassium:.0f}mg", f"{plan.total_potassium - goals.potassium_target:+.0f}mg", delta_color="off")

                        st.subheader("Category Breakdown")
                        st.table([
                            {"Category": category, "Meals": count}
                            for category, count in plan.category_breakdown.items()
                        ])
                        if plan.rda_flags:
                            st.subheader("RDA Flags")
                            st.warning("Nutrients below 80% of target")
                            st.table(plan.rda_flags)
                        else:
                            st.success("All nutrients are at 80%+ RDA")
                        if priority_nutrient_labels_tab2:
                            st.success(
                                "Priority nutrient filter passed: "
                                + ", ".join(priority_nutrient_labels_tab2)
                            )
            else:
                for idx, plan in enumerate(meal_plans, 1):
                    with st.expander(f"Option {idx} - Overall Score: {plan.score:.1f}/100", expanded=(idx == 1)):
                        st.subheader(f"Meals")
                        for meal_idx, meal in enumerate(plan.meals, 1):
                            st.write(f"**Meal {meal_idx}:** {meal['description']}")
                            st.caption(f"Category: {meal['food_category']} | Cal: {meal.get('_cal_scaled', meal.get('calories', 0)):.0f} | Protein: {meal.get('_prot_scaled', meal.get('protein', 0)):.1f}g | GI: {meal.get('glycemic_index', 'n/a')}")
                            if meal.get("_items"):
                                st.table([
                                    {
                                        "Item": item.get("description", ""),
                                        "Serving (g)": item.get("_serving", 0),
                                        "Calories": item.get("_cal_scaled", 0),
                                        "GI": item.get("glycemic_index"),
                                    }
                                    for item in meal["_items"]
                                ])
                        contamination_rows = cross_contamination_rows(plan.meals, combined_allergens_tab2)
                        if contamination_rows:
                            st.warning("Potential may-contain/shared-equipment risk found in selected meals.")
                            st.table(contamination_rows)
                        
                        st.markdown("---")
                        st.subheader("Daily Totals vs. Goals")
                        metrics_cols = st.columns(3)
                        with metrics_cols[0]:
                            st.metric(
                                "Calories",
                                f"{plan.total_calories:.0f}",
                                f"{plan.total_calories - daily_calories:+.0f}",
                                delta_color="off"
                            )
                        with metrics_cols[1]:
                            st.metric(
                                "Protein",
                                f"{plan.total_protein:.1f}g",
                                f"{plan.total_protein - daily_protein:+.1f}g",
                                delta_color="off"
                            )

                        st.subheader("Ranking Scores")
                        score_cols = st.columns(6)
                        with score_cols[0]:
                            st.metric("Calorie Fit", f"{plan.calorie_score:.1f}/100")
                        with score_cols[1]:
                            st.metric("Protein Fit", f"{plan.protein_score:.1f}/100")
                        with score_cols[2]:
                            st.metric("Variety", f"{plan.variety_score:.1f}/100")
                        with score_cols[3]:
                            st.metric("Health", f"{plan.health_score:.1f}/100")
                        with score_cols[4]:
                            st.metric("Diversity", f"{plan.diversity_score:.1f}/100")
                        with score_cols[5]:
                            st.metric("Glycemic Load", f"{plan.glycemic_load_score:.1f}/100")

                        # Display Diversity Breakdown
                        if plan.diversity_breakdown:
                            st.subheader(f"🎯 Diversity Analysis: {plan.diversity_label}")
                            bd = plan.diversity_breakdown
                            div_cols = st.columns(5)
                            with div_cols[0]:
                                st.metric("Unique Proteins", len(bd.get("unique_proteins", [])))
                            with div_cols[1]:
                                st.metric("Unique Categories", len(bd.get("unique_categories", [])))
                            with div_cols[2]:
                                st.metric("Plant Meals", bd.get("plant_meals", 0))
                            with div_cols[3]:
                                st.metric("Unique Methods", len(bd.get("unique_methods", [])))
                            with div_cols[4]:
                                st.metric("Unique Cuisines", len(bd.get("unique_cuisines", [])))
                            
                            st.caption(f"**Proteins:** {', '.join(bd.get('unique_proteins', []))}")
                            st.caption(f"**Categories:** {', '.join(bd.get('unique_categories', []))}")
                            
                            if bd.get("penalties_applied"):
                                st.warning("**Penalties applied:** " + "; ".join(bd.get("penalties_applied", [])))


                        st.subheader("Clinical Targets")
                        clinical_cols = st.columns(3)
                        with clinical_cols[0]:
                            st.metric("Glycemic Load", f"{plan.glycemic_load:.1f}")
                        with clinical_cols[1]:
                            st.metric("Sodium", f"{plan.total_sodium:.0f}mg", f"{plan.total_sodium - goals.max_sodium:+.0f}mg", delta_color="inverse")
                        with clinical_cols[2]:
                            st.metric("Potassium", f"{plan.total_potassium:.0f}mg", f"{plan.total_potassium - goals.potassium_target:+.0f}mg", delta_color="off")

                        st.subheader("Category Breakdown")
                        st.table([
                            {"Category": category, "Meals": count}
                            for category, count in plan.category_breakdown.items()
                        ])
                        if plan.rda_flags:
                            st.subheader("RDA Flags")
                            st.warning("Nutrients below 80% of target")
                            st.table(plan.rda_flags)
                        else:
                            st.success("All nutrients are at 80%+ RDA")
                        if priority_nutrient_labels_tab2:
                            st.success(
                                "Priority nutrient filter passed: "
                                + ", ".join(priority_nutrient_labels_tab2)
                            )

        except Exception as exc:
            st.error(f"Error generating meal plans: {exc}")
            import traceback
            st.write(traceback.format_exc())


# TAB 3: FILTERED FOOD EXPLANATIONS
# ═══════════════════════════════════════════════════════════════════════════════

with tab3:
    st.header("Filtered Food Explanations")
    st.caption(st.session_state.filtered_food_context)

    latest_report = st.session_state.filtered_food_report
    if latest_report:
        st.success(f"{len(latest_report)} food(s) were removed by the latest Meal Planner filters.")
        st.dataframe(latest_report, use_container_width=True)
    else:
        st.info("Generate a meal plan first, then this tab will show what the planner filtered out and why.")




