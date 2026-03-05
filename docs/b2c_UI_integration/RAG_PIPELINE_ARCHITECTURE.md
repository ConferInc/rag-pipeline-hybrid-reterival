# Graph RAG Pipeline — Architecture Document

## 1. Vision & End Goal

This pipeline powers a **nutrition assistant** that recommends recipes, prepares meal plans, and answers food/nutrition queries — all grounded in a rich Neo4j Knowledge Graph (v3.0) containing customers, recipes, products, ingredients, allergens, dietary preferences, health conditions, and nutritional data.

The pipeline follows the **R-A-G** pattern:
- **R (Retrieval)**: Find the right evidence from the KG
- **A (Augmentation)**: Build a contextualized prompt from that evidence
- **G (Generation)**: Produce a natural-language answer/recommendation via an LLM

---

## 2. Why Graph RAG (not just vector RAG)

Traditional vector RAG embeds documents into vectors and retrieves by similarity. That works for unstructured text but misses:

- **Relational constraints**: "this customer is allergic to shellfish" is a graph edge (`ALLERGIC_TO`), not a text passage. Vector search alone cannot enforce it.
- **Multi-hop reasoning**: "recipes safe for this customer" requires traversing Customer → Allergens → Ingredients → Recipes. No single embedding captures this chain.
- **Collaborative signals**: "customers with similar preferences liked these recipes" is a structural pattern (graph neighborhood), not a textual one.
- **Nutritional precision**: "recipes under 600 calories with >20g protein" needs exact property filters, not fuzzy similarity.

Graph RAG solves these by combining **three retrieval strategies** (semantic, structural, symbolic/Cypher), assembling **graph-grounded context**, and generating answers that respect constraints.

---

## 3. The Three Retrieval Strategies

### 3.1 Semantic Retrieval (vector search on GenAI embeddings)

**What it does**: Embeds the user query into a 1536-d vector and searches Neo4j native vector indexes over `semanticEmbedding` properties on nodes.

**Why it's necessary**:
- Handles **fuzzy, natural-language queries** ("something like a light Mediterranean lunch") where exact keyword matching fails.
- Works across entity types: Recipe, Ingredient, Product, B2C_Customer (with materialized profile text).
- Provides the **entry point** for queries where the user's intent is conceptual rather than structured.

**What it returns**: Top-K nodes ranked by cosine similarity, with payload (key text fields from the node).

**Labels indexed**: Recipe, Ingredient, Product, B2C_Customer (1536-d, `text-embedding-3-small` via LiteLLM).

**Limitation**: Cannot enforce relational constraints (allergens, diets, nutrition thresholds) — it only finds "similar" nodes.

### 3.2 Structural Retrieval (vector search on GraphSAGE embeddings)

**What it does**: Uses GraphSAGE embeddings (128-d) that encode each node's **graph neighborhood structure** — who it connects to, through what relationships, and how densely. Retrieval works by taking a **seed node's GraphSAGE vector** and searching the structural index for nodes with similar graph neighborhoods.

**Why it's necessary**:
- **Collaborative filtering**: Find customers with similar behavioral/preference patterns (similar allergens, similar saved/rated recipes, similar diet). Their liked recipes become candidates for the current user.
- **Recipe discovery**: Find recipes that are structurally similar to ones the user already liked (similar ingredient profiles, similar cuisine connections, similar diet suitability) — even if the text descriptions differ.
- **Cold start mitigation**: When a new user has limited history, structural similarity to other users provides recommendations that pure text matching cannot.

**How it differs from semantic retrieval**:
- Semantic = "what does the text *say*?" (meaning)
- Structural = "what does the graph *look like* around this node?" (topology/behavior)
- A recipe about "grilled chicken salad" and one about "baked chicken bowl" might have very different descriptions but nearly identical structural neighborhoods (same ingredients, same cuisine, same diet suitability). Structural retrieval catches this.

**What it returns**: Top-K structurally similar nodes, ranked by GraphSAGE cosine similarity.

**Labels with GraphSAGE embeddings**: B2C_Customer, Recipe, Product, Ingredient, Cuisine, Household, Allergens, Dietary_Preferences, B2C_Customer_Health_Profiles, B2C_Customer_Health_Conditions.

**Limitation**: Cannot be queried directly from text (no text→GraphSAGE alignment). Requires a **seed node** (typically from semantic retrieval or a known customer_id).

### 3.3 Structured Cypher Retrieval (intent-driven graph pattern matching)

**What it does**: An LLM-based intent extractor classifies the user query into one of 8+ intents and extracts structured entities (ingredients, diets, calorie limits, nutrients, etc.). A Cypher generator then builds a parameterized graph query that traverses exact relationships and filters on exact properties.

**Why it's necessary**:
- **Precision constraints**: "under 600 calories, high protein, gluten-free" cannot be reliably enforced by vector similarity — it needs exact property filters and relationship traversals.
- **Safety-critical filtering**: Allergen exclusion must be deterministic, not probabilistic. The Cypher path `Recipe → USES_INGREDIENT → Ingredient → CONTAINS_ALLERGEN → Allergen` gives a hard guarantee.
- **Complex graph patterns**: "recipes that use ingredients I have in my pantry" requires set intersection logic that vector search cannot express.
- **Nutrition architecture**: The v3.0 schema stores 117 nutrients via `RecipeNutritionValue → OF_NUTRIENT → NutrientDefinition`. Querying specific nutrient thresholds requires Cypher traversal.

**Supported intents**: `find_recipe`, `find_recipe_by_pantry`, `get_nutritional_info`, `compare_foods`, `check_diet_compliance`, `check_substitution`, `get_substitution_suggestion`, `rank_results` (and future: `recommend_for_user`, `plan_meal`).

**What it returns**: Exact Cypher result rows (recipe titles, nutrient values, compliance statuses, substitution suggestions, etc.).

**Limitation**: Only finds what the extracted entities describe. Misses conceptual/fuzzy matches and collaborative signals.

---

## 4. How the Three Strategies Work Together

No single strategy covers all use cases. The orchestrator decides which to invoke based on intent:

| User query example | Semantic | Structural | Cypher | Why |
|---|---|---|---|---|
| "light Mediterranean lunch" | **primary** | — | — | Fuzzy concept, no exact constraints |
| "gluten-free dinner under 500 cal" | — | — | **primary** | Exact constraints (diet + calories) |
| "suggest recipes for me" (logged in) | seed selection | **similar customers** | **constraint filter** | Needs personalization + safety |
| "recipes like the one I saved last week" | — | **similar recipes** | constraint filter | Structural similarity from seed |
| "how much protein in quinoa?" | — | — | **primary** | Direct fact lookup |
| "cooking for my friend allergic to peanuts" | fuzzy recipe search | — | **allergen filter** | Concept + hard constraint |

The orchestrator merges results, deduplicates, and passes a unified evidence set to the augmentation layer.

---

## 5. Augmentation — Why and How

### Why augmentation matters
The LLM has no access to your KG. It will hallucinate nutrition facts, invent recipes, and ignore allergens unless you **explicitly provide the evidence** in the prompt. Augmentation converts retrieved graph data into a structured "evidence block" the LLM can ground its answer on.

### What goes into the augmented prompt

**System message** (per-intent instructions):
- Role: "You are a nutrition assistant for [app name]."
- Safety: "Never recommend foods that contain the user's allergens. Never ignore dietary restrictions."
- Grounding: "Answer using ONLY the provided context. If context is insufficient, ask a clarifying question."

**Customer profile** (if `customer_id` provided):
- Allergens (from `ALLERGIC_TO` edges)
- Dietary preferences (from `FOLLOWS_DIET` edges)
- Health conditions and nutrient limits (from `HAS_CONDITION` + `REQUIRES_NUTRIENT_LIMIT`)
- This ensures the LLM *knows* what's forbidden, even if the user didn't mention it.

**Retrieved evidence** (formatted per intent):
- Recipe results: title, cuisine, meal type, key nutrition facts, ingredient list, diet tags
- Nutrition lookups: ingredient name, nutrient values, units
- Comparisons: side-by-side nutrient tables
- Substitutions: candidates with ratios and nutrition comparison

**User query**: verbatim original text.

### Why this structure works for recommendations
When the user asks "suggest dinner recipes for me":
1. The system message tells the LLM to respect allergens/diet
2. The customer profile section lists exactly what's forbidden
3. The evidence section contains pre-filtered, constraint-safe recipes (from Cypher) enriched with collaborative candidates (from GraphSAGE)
4. The LLM's job is reduced to: **select, rank, and explain** — not invent

This dramatically reduces hallucination and constraint violations.

---

## 6. Generation — How It Recommends Recipes

### Flow
1. Augmented prompt → LLM (via LiteLLM, e.g., `gpt-5-mini`)
2. LLM generates a natural-language response:
   - For recipe recommendations: ranked list with brief explanations ("This recipe is high-protein and avoids your shellfish allergy")
   - For nutritional queries: factual answer citing the retrieved data
   - For comparisons: structured comparison with a conclusion
3. Post-processing validates the response doesn't mention forbidden items (defense-in-depth)

### Why the LLM adds value (beyond just returning Cypher rows)
- **Natural language**: Users want conversational answers, not database rows.
- **Explanation**: The LLM can explain *why* a recipe is recommended ("matches your low-carb preference, uses ingredients you've saved before, and similar users rated it highly").
- **Synthesis**: When multiple retrieval paths contribute candidates, the LLM synthesizes them into a coherent recommendation.
- **Follow-up handling**: The LLM can ask clarifying questions when context is insufficient.

---

## 7. How This Pipeline Reaches the End Goal

### Goal 1: Recommend recipes based on constraints and restrictions
- **Cypher retrieval** enforces allergens, diet, health condition nutrient limits, age restrictions
- **Customer context injection** ensures constraints are applied even when the user doesn't mention them
- **Semantic retrieval** handles fuzzy preferences ("something light", "comfort food")

### Goal 2: Prepare meal plans
- Future `plan_meal` intent extends the recipe recommendation flow with:
  - Variety constraints (don't repeat cuisines/ingredients across days)
  - Nutritional balance across the week
  - Budget awareness (via `HouseholdBudget`)
- Structural retrieval provides diverse candidates; Cypher filters; LLM assembles the plan

### Goal 3: Recommend based on activity (saved/liked/tried recipes)
- Customer's behavioral edges (`SAVED`, `RATED`, `TRIED`, `VIEWED`) are captured in GraphSAGE embeddings
- Structural retrieval finds recipes similar to liked ones
- Collaborative filtering finds similar customers' favorites

### Goal 4: Recommend based on similar customers
- GraphSAGE on `B2C_Customer` encodes the full behavioral + preference neighborhood
- Structural search finds top-K similar customers
- Their highly-rated/saved recipes become candidates (filtered by the current user's constraints)

### Goal 5: Answer ad-hoc queries ("cooking for a friend allergic to peanuts")
- Intent extractor identifies the constraint (peanut allergy)
- Cypher generator adds allergen exclusion
- Semantic retrieval finds conceptually relevant recipes
- LLM generates a helpful, safe answer

---

## 8. Data Flow Summary

```
User Query
    │
    ▼
┌─────────────────────┐
│  Intent Extractor    │  (Gemini via LiteLLM)
│  → intent + entities │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────────────────────────────┐
│            Retrieval Orchestrator            │
│                                             │
│  ┌──────────┐ ┌───────────┐ ┌────────────┐ │
│  │ Semantic  │ │Structural │ │   Cypher   │ │
│  │ (vector)  │ │(GraphSAGE)│ │ (pattern)  │ │
│  └────┬─────┘ └─────┬─────┘ └─────┬──────┘ │
│       └──────┬──────┘──────────────┘        │
│              ▼                               │
│     Unified RetrievalOutput                  │
│  (nodes + scores + Cypher rows + provenance) │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────┐
│      Augmentation Layer             │
│                                     │
│  Customer Profile (if logged in)    │
│  + Context Formatter (per intent)   │
│  + Prompt Template                  │
│  + Guardrails                       │
│  → Augmented Prompt                 │
└─────────────────┬───────────────────┘
                  │
                  ▼
┌─────────────────────────────────────┐
│      Generation Layer               │
│                                     │
│  LLM Call (via LiteLLM)             │
│  + Post-validation                  │
│  + Citation/provenance              │
│  → Final Answer / Recommendation    │
└─────────────────────────────────────┘
```
