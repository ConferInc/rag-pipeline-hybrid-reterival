# Intent & Entity Extraction

Single source of truth for NLU (Natural Language Understanding) in the RAG pipeline.  
All intent constants live in `rag_pipeline/nlu/intents.py`.

---

## 1. Expected Intents (Enum)

All NLU output (keyword extractor, LLM extractor, chatbot rules) must produce an intent from `VALID_INTENTS`.

### Recipe search & discovery
| Intent | Description |
|--------|-------------|
| `find_recipe` | General recipe search (diet, course, allergens) |
| `find_recipe_by_pantry` | Recipes using specific pantry ingredients |
| `similar_recipes` | Recipes like a reference dish |
| `recipes_for_cuisine` | Recipes by cuisine (e.g. Italian, Mexican) |
| `recipes_by_nutrient` | Recipes by nutrient (high-protein, low-sodium) |
| `rank_results` | Rank/sort recipes by criterion |
| `ingredient_in_recipes` | Recipes containing an ingredient |
| `cuisine_recipes` | Alias for recipes_for_cuisine |

### Ingredient & nutrition
| Intent | Description |
|--------|-------------|
| `get_nutritional_info` | Nutrition of an ingredient/food |
| `nutrient_in_foods` | Foods high/rich in a nutrient |
| `nutrient_category` | Types of vitamins/minerals/macronutrients |
| `compare_foods` | Compare nutrition of two+ foods |
| `check_diet_compliance` | Is X vegan/keto/etc.? |
| `ingredient_nutrients` | Nutrients in an ingredient |

### Substitution
| Intent | Description |
|--------|-------------|
| `check_substitution` | Can I substitute X with Y? |
| `get_substitution_suggestion` | Alternatives to X |
| `similar_ingredients` | Ingredients like X |

### Product
| Intent | Description |
|--------|-------------|
| `find_product` | Find products (e.g. gluten-free bread) |
| `product_nutrients` | Nutrition of a product |

### Cuisine & allergens
| Intent | Description |
|--------|-------------|
| `cuisine_hierarchy` | Cuisine types/taxonomy |
| `cross_reactive_allergens` | Cross-reactive allergens for an allergen |

### General
| Intent | Description |
|--------|-------------|
| `general_nutrition` | What is X? (nutrition concepts) |
| `out_of_scope` | Query outside food/nutrition domain |

### Conversational (chatbot)
| Intent | Description |
|--------|-------------|
| `greeting` | Hi, hello, etc. |
| `help` | What can you do? |
| `farewell` | Bye, thanks, etc. |

### Chatbot data intents
| Intent | Description |
|--------|-------------|
| `plan_meals` | Create/generate meal plan |
| `show_meal_plan` | Show current meal plan |
| `log_meal` | Log a meal |
| `meal_history` | What did I eat? |
| `nutrition_summary` | Nutrition summary (week/today) |
| `swap_meal` | Swap a meal in plan |
| `grocery_list` | Grocery list |
| `set_preference` | Set dietary preference |
| `dietary_advice` | General dietary advice |

### Fallback
| Intent | Description |
|--------|-------------|
| `unclear` | Empty or unparseable input |

---

## 2. Intent Subsets

| Constant | Purpose |
|----------|---------|
| `VALID_INTENTS` | All valid intents — used by sanity_check |
| `RECIPE_INTENTS` | Intents that return recipes → hard constraints (allergens, diets, calories) applied |
| `DATA_INTENTS_NEEDING_RETRIEVAL` | Intents that run semantic + structural + Cypher retrieval + LLM generation |
| `CHATBOT_DATA_INTENTS` | Deterministic chatbot intents (show_meal_plan, meal_history, nutrition_summary) — fixed Cypher, no retrieval |

---

## 3. Usage

```python
from rag_pipeline.nlu.intents import (
    VALID_INTENTS,
    RECIPE_INTENTS,
    DATA_INTENTS_NEEDING_RETRIEVAL,
    CHATBOT_DATA_INTENTS,
)

# Validate extracted intent
if intent not in VALID_INTENTS:
    raise ValueError(f"Unknown intent: {intent}")

# Apply hard constraints only for recipe-returning intents
if intent in RECIPE_INTENTS:
    fused = apply_hard_constraints(fused, entities, intent, ...)
```

---

## 4. Adding a New Intent

1. Add to `VALID_INTENTS` in `rag_pipeline/nlu/intents.py`
2. Add to the appropriate subset(s): `RECIPE_INTENTS`, `DATA_INTENTS_NEEDING_RETRIEVAL`, or `CHATBOT_DATA_INTENTS`
3. Update `extractor_classifier.py` SYSTEM_PROMPT and keyword patterns
4. Add to `embedding_config.yaml` `intent_semantic_labels` and `intent_structural` if needed
5. Add Cypher handler in `cypher_query_generator.py` if data intent
6. Update this doc
