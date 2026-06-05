with open('data_pipeline/meal_planner.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find the "return week" line and insert week validation before it
for idx, line in enumerate(lines):
    if line.strip() == "return week" and idx > 900:  # Make sure it's the plan_week return
        # Found it - insert validation code before this line
        indent = " " * 8
        validation_code = [
            indent + "# Week-level validation: if any day failed or constraints not met, return empty to signal failure\n",
            indent + "if len(week) < num_days:\n",
            indent + "    log.warning('Generated incomplete week: only %s of %s days', len(week), num_days)\n",
            indent + "    return []\n",
            indent + "\n",
            indent + "# Check all-days diversity constraint if specified\n",
            indent + "min_diversity = kwargs.get('min_diversity_score', 0)\n",
            indent + "max_sodium = kwargs.get('max_sodium_mg', None)\n",
            indent + "\n",
            indent + "if min_diversity > 0:\n",
            indent + "    for day_idx, day_plan in enumerate(week):\n",
            indent + "        if day_plan.diversity_score < min_diversity:\n",
            indent + "            log.warning('Day %s failed diversity: %.1f < %.1f threshold',\n",
            indent + "                       day_idx+1, day_plan.diversity_score, min_diversity)\n",
            indent + "            return []  # Retry entire week\n",
            indent + "\n",
            indent + "if max_sodium and max_sodium > 0:\n",
            indent + "    for day_idx, day_plan in enumerate(week):\n",
            indent + "        if day_plan.total_sodium >= max_sodium:\n",
            indent + "            log.warning('Day %s failed sodium: %.0f >= %.0fmg threshold',\n",
            indent + "                       day_idx+1, day_plan.total_sodium, max_sodium)\n",
            indent + "            return []  # Retry entire week\n",
            indent + "\n",
            indent + "log.info('Week passed all constraints: diversity >= %.1f, sodium < %.0fmg',\n",
            indent + "        min_diversity, max_sodium or 5000)\n",
        ]
        lines = lines[:idx] + validation_code + [lines[idx]]
        break

with open('data_pipeline/meal_planner.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)

print("* Added week-level constraint checking")
