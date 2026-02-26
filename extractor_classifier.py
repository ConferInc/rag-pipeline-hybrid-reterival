import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from openai import OpenAI

try:
    from rag_pipeline.llm_retry import with_retry
except ImportError:
    with_retry = None

SYSTEM_PROMPT = """You are a Culinary NLU assistant. Analyze the user request and return a JSON object with exactly two keys: "intent" and "entities". No markdown, no extra text.

INTENT — pick exactly one:
- "find_recipe"                 Search for recipes matching criteria.
- "find_recipe_by_pantry"       User lists available ingredients and asks what to cook.
- "get_nutritional_info"        Nutritional data for an ingredient.
- "compare_foods"               Compare nutrition of 2+ foods.
- "check_diet_compliance"       Is an ingredient allowed on a diet?
- "check_substitution"          Can ingredient A directly replace ingredient B?
- "get_substitution_suggestion" Suggest alternatives for an ingredient.
- "rank_results"                Rank a previous result set by a criterion.

ENTITIES — include a key only if explicitly mentioned or strongly implied:
- "include_ingredient" list[str]   Ingredients to include.
- "exclude_ingredient" list[str]   Ingredients to exclude.
- "diet"               list[str]   "Vegan"|"Vegetarian"|"Gluten-Free"|"Keto"|"Paleo"|"Dairy-Free"|"Nut-Free"|"High-Protein"|"Low-Fat"|"Low-Carb"
- "course"             str         "breakfast"|"lunch"|"dinner"|"dessert"|"appetizer"|"main_dish"|"side_dish"|"salad"|"soup"|"snack" (map e.g. "main course"→"main_dish")
- "dish"               str         Dish name or title keyword (e.g. "Moussaka").
- "cal_upper_limit"    int         Max calories per serving.
- "nutrient_threshold" obj         {nutrient, operator:"gt"|"lt", value}. Nutrient names: "Protein","Total Fat","Carbohydrate","Dietary Fiber","Total Sugars","Sodium","Energy".
- "pantry_ingredients" list[str]   Ingredients the user has (find_recipe_by_pantry).
- "ingredient"         str         Single ingredient for info/compliance/substitution queries.
- "nutrient"           str         Specific nutrient name; omit to return all macros.
- "ingredients"        list[str]   2+ foods to compare (compare_foods).
- "original_ingredient"   str      Ingredient being replaced (check_substitution).
- "substitute_ingredient" str      Proposed replacement (check_substitution).
- "criterion"          str         "protein_to_calorie_ratio"|"lowest_fat"|"lowest_calories"
- "target"             list        IDs to rank (rank_results).

EXAMPLES (one per intent):
User: "Vegan lunch under 600 cal with chicken and broccoli, no dairy."
{"intent":"find_recipe","entities":{"course":"lunch","diet":["Vegan","Dairy-Free"],"include_ingredient":["chicken","broccoli"],"cal_upper_limit":600}}

User: "Breakfast with at least 20g protein and a nutrient threshold for fat under 15g."
{"intent":"find_recipe","entities":{"course":"breakfast","nutrient_threshold":{"nutrient":"Protein","operator":"gt","value":20}}}

User: "I have chicken, tomato, and onion. What can I cook?"
{"intent":"find_recipe_by_pantry","entities":{"pantry_ingredients":["chicken","tomato","onion"]}}

User: "How much protein is in quinoa?"
{"intent":"get_nutritional_info","entities":{"ingredient":"quinoa","nutrient":"Protein"}}

User: "Which has more carbs, rice or pasta?"
{"intent":"compare_foods","entities":{"ingredients":["rice","pasta"],"nutrient":"Carbohydrate"}}

User: "Is honey suitable for a vegan diet?"
{"intent":"check_diet_compliance","entities":{"ingredient":"honey","diet":["Vegan"]}}

User: "Can I substitute wheat flour with rice flour?"
{"intent":"check_substitution","entities":{"original_ingredient":"wheat flour","substitute_ingredient":"rice flour"}}

User: "What can I replace butter with in a vegan diet?"
{"intent":"get_substitution_suggestion","entities":{"ingredient":"butter","diet":["Vegan"]}}"""


def _get_client() -> OpenAI:
    return OpenAI(
        base_url=os.environ.get("OPENAI_BASE_URL"),
        api_key=os.environ.get("OPENAI_API_KEY"),
    )


def _load_llm_retry_config(config_path: str | Path = "embedding_config.yaml") -> dict:
    path = Path(config_path)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
        return raw.get("llm_retry", {}) or {}
    except Exception:
        return {}


def extract_intent(
    text: str,
    *,
    model: str | None = None,
    config_path: str | Path = "embedding_config.yaml",
) -> str:
    """
    Extract intent + entities from free-text user query using LLM via LiteLLM/OpenAI.

    Args:
        text: User query
        model: LLM model name (defaults to INTENT_MODEL env var or gpt-4o-mini)
        config_path: Path to embedding_config.yaml for llm_retry settings

    Returns:
        Raw JSON string from LLM
    """
    client = _get_client()
    model = model or os.environ.get("INTENT_MODEL", "gpt-4o-mini")

    def _call():
        return client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0,
        )

    retry_cfg = _load_llm_retry_config(config_path)
    if with_retry and retry_cfg:
        response = with_retry(
            _call,
            max_attempts=retry_cfg.get("max_attempts", 3),
            initial_delay_ms=float(retry_cfg.get("initial_delay_ms", 1000)),
            max_delay_ms=float(retry_cfg.get("max_delay_ms", 30000)),
            jitter=retry_cfg.get("jitter", True),
        )
    else:
        response = _call()

    return response.choices[0].message.content


RETRY_USER_MESSAGE = "Return only valid JSON with keys 'intent' and 'entities'. No markdown."


def parse_extractor_output(raw: str) -> dict[str, Any] | None:
    """
    Parse LLM extractor output. Tries json.loads first, then repair (strip markdown, fix trailing commas).

    Returns:
        Parsed dict or None if unparseable
    """
    text = raw.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try repairing common issues
    try:
        fixed = re.sub(r",\s*}", "}", text)
        fixed = re.sub(r",\s*]", "]", fixed)
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass
    return None


def extract_intent_with_retry(
    text: str,
    *,
    model: str | None = None,
    max_retries: int = 1,
    retry_message: str = RETRY_USER_MESSAGE,
    config_path: str | Path = "embedding_config.yaml",
) -> tuple[str, bool]:
    """
    Extract intent + entities with optional retry on parse failure.

    Args:
        text: User query
        model: LLM model name
        max_retries: Max extra calls after first parse failure
        retry_message: Message to add on retry when output was invalid JSON

    Returns:
        (raw_json_string, was_parse_successful)
    """
    raw = extract_intent(text, model=model, config_path=config_path)
    parsed = parse_extractor_output(raw)
    if parsed is not None:
        return raw, True
    for _ in range(max_retries):
        client = _get_client()
        m = model or os.environ.get("INTENT_MODEL", "gpt-4o-mini")
        def _retry_call():
            return client.chat.completions.create(
                model=m,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{retry_message}"},
                    {"role": "user", "content": text},
                ],
                temperature=0,
            )
        retry_cfg = _load_llm_retry_config(config_path)
        if with_retry and retry_cfg:
            response = with_retry(
                _retry_call,
                max_attempts=retry_cfg.get("max_attempts", 3),
                initial_delay_ms=float(retry_cfg.get("initial_delay_ms", 1000)),
                max_delay_ms=float(retry_cfg.get("max_delay_ms", 30000)),
                jitter=retry_cfg.get("jitter", True),
            )
        else:
            response = _retry_call()
        raw = response.choices[0].message.content
        parsed = parse_extractor_output(raw)
        if parsed is not None:
            return raw, True
    return raw, False


def sanity_check(output_json):
    """
    Validates the structure and basic logic of the extractor output.
    Returns True on success, or (False, reason_string) on failure.
    """
    required_keys = {"intent", "entities"}

    if not isinstance(output_json, dict):
        return False, "Output is not a dictionary"

    if not required_keys.issubset(output_json.keys()):
        return False, "Missing required top-level keys: 'intent' and/or 'entities'"

    if not isinstance(output_json["intent"], str):
        return False, "Intent is not a string"

    valid_intents = {
        "find_recipe",
        "find_recipe_by_pantry",
        "get_nutritional_info",
        "compare_foods",
        "check_diet_compliance",
        "check_substitution",
        "get_substitution_suggestion",
        "rank_results",
    }
    if output_json["intent"] not in valid_intents:
        return False, f"Unknown intent: '{output_json['intent']}'. Valid: {sorted(valid_intents)}"

    if not isinstance(output_json["entities"], dict):
        return False, "Entities is not a dictionary"

    entities = output_json["entities"]

    # --- include/exclude ingredient overlap check ---
    include = entities.get("include_ingredient", [])
    exclude = entities.get("exclude_ingredient", [])
    if isinstance(include, list) and isinstance(exclude, list):
        overlap = set(i.lower() for i in include) & set(e.lower() for e in exclude)
        if overlap:
            return False, f"Ingredient(s) in both include and exclude: {list(overlap)}"

    # --- nutrient_threshold structure check ---
    threshold = entities.get("nutrient_threshold")
    if threshold is not None:
        if not isinstance(threshold, dict):
            return False, "nutrient_threshold must be a dictionary"
        for required_field in ("nutrient", "operator", "value"):
            if required_field not in threshold:
                return False, f"nutrient_threshold missing field: '{required_field}'"
        if threshold.get("operator") not in ("gt", "lt"):
            return False, "nutrient_threshold.operator must be 'gt' or 'lt'"
        if not isinstance(threshold.get("value"), (int, float)):
            return False, "nutrient_threshold.value must be a number"

    # --- compare_foods needs at least 2 ingredients ---
    if output_json["intent"] == "compare_foods":
        foods = entities.get("ingredients", [])
        if not isinstance(foods, list) or len(foods) < 2:
            return False, "compare_foods requires at least 2 items in 'ingredients'"

    return True


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    free_text = "Find me recepies that contain olive oil"
    response = extract_intent(free_text)
    try:
        response_json = json.loads(response)
    except json.JSONDecodeError:
        response_json = {"error": "Invalid JSON response", "response": response}

    print(json.dumps(response_json, indent=2))
    print(sanity_check(response_json))
