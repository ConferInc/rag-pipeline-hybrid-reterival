# RAG Pipeline Architecture — Summary for Architects

## 1. Overview

The system recommends meal plans and recipes tailored to preferences, allergies, dietary needs, and health conditions.

For every search or query, the system infers the intent, then runs three retrieval paths over a knowledge graph: by meaning (what the query is about), by structure (similar users and recipes), and by strict rules (allergens, diets, calorie limits). Results are filtered, merged into one ranked list, and turned into a natural-language response. The pipeline is designed so recommendations remain safe, relevant, and personalized.

---

## 2. The Three Retrieval Strategies — Role of Each

**Strategy 1: Meaning-based (semantic) retrieval**  
- **What:** Converts the query to a meaning vector and finds nodes with similar meaning.  
- **Why:** Handles vague or natural-language queries ("light Mediterranean lunch", "comfort food") where exact filters or keywords fail.  
- **Scope:** Recipes, ingredients, products, cuisines.  
- **Limitation:** Cannot enforce hard constraints (e.g., allergens, diet) or numeric limits; used for relevance, not safety.

**Strategy 2: Structure-based (collaborative) retrieval**  
- **What:** Uses graph structure (who connects to whom and how) to find similar customers or recipes.  
- **Why:** Powers collaborative filtering and discovery:
  - Similar users (similar allergies, diets, saved/rated recipes) → their liked recipes as candidates
  - Similar recipes (similar ingredients, cuisines, diets) → discovery beyond text
  - Helps when a user has little history ("cold start") by using structure.
- **Requires:** A logged-in user and a seed for structure search.  
- **Limitation:** Does not enforce safety rules; that's done by the structured strategy.

**Strategy 3: Structured (rule-based) retrieval**  
- **What:** Extracts explicit filters (diets, calorie limits, nutrients, allergens) and runs graph pattern queries.  
- **Why:** Essential for:
  - **Safety:** Allergen exclusion must be exact.
  - **Precision:** "Under 600 calories, high protein, gluten‑free" needs property filters and graph paths.
  - **Complex patterns:** e.g., "recipes using ingredients in my pantry."
- **Limitation:** Only finds what the extracted rules describe; misses conceptual or fuzzy matches.

**How they work together**  
The system picks which strategies to use per query. For "suggest dinner recipes for me," it uses semantic retrieval for relevance, structure-based retrieval for personalization, and structured retrieval to enforce constraints.

---

## 3. Search Intent and User Intent

**Search intent**  
Before retrieval, the system classifies each query into an intent (e.g., find recipes, get nutrition, compare foods, check diet, substitution). Intent drives:
- Which retrieval strategy(ies) to use
- Which graph indexes to query (recipes vs ingredients vs products)
- How to format the results

**User intent (entities)**  
The system also extracts entities, e.g.:
- Diets (Vegan, Gluten‑Free, Keto, etc.)
- Nutrients and limits (calories, protein, sodium)
- Ingredients to include/exclude
- Meal type (breakfast, lunch, dinner)
- Allergen constraints

These entities become filters in the structured retrieval and direct the overall pipeline. Entity enrichment can infer missing entities from query keywords (e.g., "vegan lunch" → diet: Vegan, course: lunch).

---

## 4. Retrieval Guarding with Thresholds

Raw retrieval scores can be weak or noisy. To avoid passing low‑quality results downstream:

- **Minimum scores for meaning-based retrieval:** Results below a threshold (e.g., 0.5) are discarded.
- **Minimum scores for structure-based retrieval:** Graph-structure similarity below a threshold is filtered out.
- **Row limits for structured retrieval:** Caps on the number of returned rows to keep responses manageable.

**Effects:**
- Reduces hallucination by not surfacing irrelevant matches.
- Improves recommendation quality.
- Lowers risk of unsafe recommendations when weak matches would otherwise be forwarded.

Thresholds are tunable and applied before fusion.

---

## 5. Score Fusion — Reciprocal Rank Fusion (RRF)

The three retrieval strategies return different result sets and scoring schemes. To get one ranked list:

- **What RRF does:** Combines the ranked lists into a single list. Items that appear in multiple sources receive a higher fused score.
- **Why it's used:**
  - Balances multiple signals: meaning, structure, and constraints.
  - Items confirmed by more than one strategy are ranked higher.
  - No need to align different score ranges; RRF uses ranks only.
- **Example:** If the same recipe appears in meaning-based, structure-based, and structured retrieval, it rises to the top; a recipe in only one list ranks lower.

---

## 6. End-to-End Flow (High Level)

1. **Query understanding:** Parse the user query → intent + entities.
2. **Retrieval:** Run the chosen strategies in parallel:
   - Meaning-based retrieval (for relevance)
   - Structure-based retrieval (for personalization when a user is logged in)
   - Structured retrieval (for constraints)
3. **Guardrails:** Apply thresholds and filters.
4. **Fusion:** Merge results with RRF into one ranked list.
5. **Context building:** Build a prompt that includes:
   - User profile (allergens, diets, health conditions)
   - Top fused results
   - Safety instructions
6. **Generation:** A language model produces natural-language recommendations grounded in this context.

---

## 7. Retrieval Testing Approach

- **Intent extraction:** Tests ensure intent parsing handles valid and malformed input, fallbacks, and entity enrichment.
- **Retrieval quality:** A small evaluation set of queries with expected entities/nodes is used as a baseline.
- **Semantic retrieval:** Design includes a similarity threshold to treat weak matches as "no confident match" and avoid hallucination.
- **Validation:** Checks that extracted entities (e.g., diets, nutrient limits) are structurally valid before use.

---

## 8. Recommendation Evaluation

The pipeline runs each test question through the full RAG flow to retrieve recipes and generate a recommendation. A separate LLM then acts as an evaluator: it receives the original question, the recipes that were retrieved, and the recommendation, and scores the output on two dimensions.

**Scoring dimensions**

| Dimension | What is measured | Example |
|-----------|------------------|---------|
| **Query relevance** | How well the recommendation answers the question | "I want something quick" → did it suggest recipes with short cook times? |
| **Faithfulness to recipes** | Whether the response stays grounded in the retrieved recipes | No invented ingredients, times, or steps that do not appear in the source recipes |

**Output**
- Each dimension gets a score from 0 to 1.
- Short explanations support each score.
- Average scores across all test questions give an overall measure of recommendation quality.

This evaluation loop helps monitor end-to-end performance and catch gaps in relevance or grounding over time.

---

## 9. Improving Recommendations with More Customer Interaction Data

The structure-based strategy depends on behavioral and preference data. As more of this is captured, recommendations improve:

- **Behavioral relationships** (saved, rated, viewed, tried): Encode what users actually like. More interactions improve similarity estimates between users and recipes.
- **Graph structure:** The structural model encodes each node's connections and neighborhood. More users, recipes, and interactions make neighborhoods richer.
- **Collaborative signals:** Similar-user and similar-recipe discovery becomes more accurate with more data.
- **Model retraining:** The structural embeddings can be periodically retrained on updated graphs to reflect new data.

**Result:** Recommendations shift from mainly constraint- and query-based to increasingly personalized (similar users' preferences and similar recipes) while staying safe through the structured retrieval layer.

---

## 10. Why This Architecture Is Suitable for Our Use Case

| Need | How the architecture supports it |
|------|----------------------------------|
| Safety (allergens) | Structured retrieval enforces hard exclusion rules; user profile injected into prompts. |
| Dietary preferences | Structured retrieval filters by diet; semantic retrieval adds conceptual matches. |
| Health conditions | Health profiles and nutrient limits applied via structured retrieval. |
| Vague vs precise queries | Semantic retrieval for vague; structured retrieval for precise. |
| Personalization | Structure-based retrieval uses behavior and similar users/recipes. |
| Natural responses | LLM generates conversational answers from graph-grounded context. |
