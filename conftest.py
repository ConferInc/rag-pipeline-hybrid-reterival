# Root-level conftest.py — collected before any subdirectory conftest.
#
# collect_ignore: TDD test files for features not yet implemented.
# pytest skips collection on these so the suite doesn't error out.
# Remove each entry once the corresponding source function is implemented.
collect_ignore = [
    "tests/api/test_goal_calorie_adjustment.py",          # pending: _GOAL_CALORIE_ADJUSTMENT, _apply_goal_calorie_adjustment
    "tests/api/test_joint_calorie_usda_selection.py",     # pending: _combo_joint_score, _fg_coverage, _get_joint_weights
    "tests/api/test_meals_per_day_contract.py",           # pending: _normalize_meals_per_day
    "tests/api/test_slot_aware_calorie_planning.py",      # pending: _compute_slot_targets
    "tests/api/test_food_group_transparency_message.py",  # pending: _build_food_group_selection_explanations
    "tests/orchestrator/test_constraint_filter_pr2.py",   # pending: _FDA_ALLERGEN_SYNONYMS
    "tests/orchestrator/test_constraint_filter_tier1.py", # pending: AllergenFilterUnavailable
    "tests/orchestrator/test_variety_rerank.py",          # pending: infer_protein_source
]
