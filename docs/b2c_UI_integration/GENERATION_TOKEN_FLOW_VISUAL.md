# RAG Generation: Visual Token Flow

**Purpose:** Show exactly what goes INTO and OUT OF the LLM during generation, and how token cost is calculated.

---

## When Does Generation Run?

Generation **only** runs in one place: when a **chat message** triggers a **data intent** (e.g. `find_recipe`, `get_nutritional_info`).

```
User sends chat: "find me a high-protein dinner"
        │
        ▼
   NLU detects: intent = find_recipe
        │
        ▼
   orchestrate() → retrieval (semantic + structural + cypher + RRF)
        │
        ▼
   build_augmented_prompt() → builds the full prompt string
        │
        ▼
   generate_chat_response() → injects conversation history
        │
        ▼
   generate_response() → client.chat.completions.create(...)  ← THIS IS THE LLM CALL
        │
        ▼
   LLM returns natural language answer
```

**Search (`/search/hybrid`)** does NOT call generation — it returns structured recipe IDs. Only the **chat** endpoint uses generation.

---

## What Exactly Gets Sent to the LLM?

The `augmented_prompt` is built by `prompt_builder.build_augmented_prompt()`. It looks like this:

### Structure (sections in order)

```
[SYSTEM]
<system prompt - always the same>

[USER PROFILE]          ← only if customer is logged in
<customer name, diets, allergens, health conditions, recent meals>

[RANKED CONTEXT]        ← retrieval results (recipes/ingredients from Neo4j)
<1. Recipe A [cuisine, difficulty, 30 min] (sources: semantic, cypher)
 2. Recipe B ...
 3. Recipe C ...>

[USER QUERY]
<user's message>

[CONVERSATION HISTORY]   ← only in chat, only if multi-turn
User: earlier message
Assistant: earlier reply
```

The generator then splits this:
- **System message** = everything from `[SYSTEM]` to the first `[SECTION]`
- **User message** = everything else (profile, context, query, history)

---

## Concrete Example (Character / Token Count)

**User:** "find me a high-protein dinner for tonight"

**Customer profile (logged in):**
- Name: Priya
- Diets: Vegan
- Allergens: Tree nuts
- Recent meals: Pasta Primavera, Lentil Soup

**Retrieval returns 10 recipes** (RRF fused). `format_fused_results_as_text` turns them into:

```
Ranked results (semantic + collaborative + graph):
1. Quinoa Buddha Bowl [Mediterranean, easy, 25 min] (sources: semantic, cypher)
   High-protein quinoa with roasted vegetables and tahini...
2. Tempeh Stir Fry [Asian, easy, 20 min] (sources: semantic, structural)
   Protein-rich tempeh with broccoli and brown rice...
3. Chickpea Curry [Indian, medium, 35 min] (sources: semantic, cypher)
   ...
(and 7 more)
```

---

## Actual Text That Goes to the LLM

### System message (goes as `role: "system"`)

```
You are a Nutrition assistant. Recommend recipes and answer food/nutrition questions using ONLY the context below.

RULES: Use only recipes/ingredients from the context. Respect allergens, diets, and health conditions. If no suitable options, say so and suggest refining the query. Be concise and practical.

PERSONALIZATION: When [USER PROFILE] is provided: use the customer's name when greeting; respect diets, allergens, and health conditions; tailor suggestions to their health goal and activity level; reference recent meals when avoiding repetition helps.
```

**Character count:** ~450  
**Token estimate:** ~115 tokens (1 token ≈ 4 chars)

---

### User message (goes as `role: "user"`)

```
[USER PROFILE]
Customer name: Priya
Dietary preferences: Vegan
Allergens (NEVER include in recommendations): Tree nuts
Recent meals: Pasta Primavera, Lentil Soup

[RANKED CONTEXT]
Ranked results (semantic + collaborative + graph):
1. Quinoa Buddha Bowl [Mediterranean, easy, 25 min] (sources: semantic, cypher)
   High-protein quinoa with roasted vegetables and tahini...
2. Tempeh Stir Fry [Asian, easy, 20 min] (sources: semantic, structural)
   Protein-rich tempeh with broccoli and brown rice...
3. Chickpea Curry [Indian, medium, 35 min] (sources: semantic, cypher)
   Creamy chickpea curry with spinach...
4. Black Bean Tacos [Mexican, easy, 15 min] (sources: cypher)
   ...
5. Tofu Scramble [American, easy, 20 min] (sources: semantic, structural)
   ...
6. Lentil Dal [Indian, easy, 40 min] (sources: semantic, cypher)
   ...
7. Edamame Salad [Asian, easy, 10 min] (sources: semantic)
   ...
8. Moroccan Tagine [Mediterranean, medium, 45 min] (sources: cypher)
   ...
9. Falafel Bowl [Mediterranean, medium, 30 min] (sources: semantic, structural)
   ...
10. Bean Chili [American, easy, 35 min] (sources: semantic, cypher)
   ...

[USER QUERY]
find me a high-protein dinner for tonight
```

**Character count:** ~1,400  
**Token estimate:** ~350 tokens

---

### Total INPUT to the LLM

| Part | Chars | Est. Tokens |
|------|-------|-------------|
| System message | ~450 | ~115 |
| User message (profile + context + query) | ~1,400 | ~350 |
| **Total INPUT** | **~1,850** | **~465** |

*(If there's conversation history, add ~50–200 tokens per prior turn.)*

With 10 recipes and `max_fused=10`, context is typically larger. A more realistic estimate:
- 10 recipes × ~80 chars each ≈ 800 chars ≈ 200 tokens
- Profile: ~150 tokens
- Query: ~10 tokens
- **Total input:** ~115 + 200 + 150 + 10 ≈ **475 tokens** (small case) to **1,200 tokens** (richer context)

---

## What Comes Back (OUTPUT)

The LLM generates something like:

```
Here are some high-protein vegan dinner ideas for tonight, Priya:

1. **Quinoa Buddha Bowl** — 25 min, Mediterranean. Great protein from quinoa and tahini.
2. **Tempeh Stir Fry** — 20 min, Asian. High-protein tempeh with vegetables.
3. **Chickpea Curry** — 35 min, Indian. Chickpeas pack plenty of protein.

All of these fit your vegan diet and avoid tree nuts. Would you like details for any of them?
```

**Character count:** ~350  
**Token estimate:** ~90 tokens

`embedding_config.yaml` has `max_tokens: 1024`, so the model is *allowed* up to 1024 output tokens, but it usually finishes earlier (often 100–400 tokens).

---

## How Cost Is Calculated

**Pricing (gpt-4o-mini):**
- Input: $0.15 per 1M tokens
- Output: $0.60 per 1M tokens

**This example:**
- Input: 1,200 tokens × ($0.15 / 1,000,000) = **$0.00018**
- Output: 250 tokens × ($0.60 / 1,000,000) = **$0.00015**
- **Total generation cost:** **$0.00033**

---

## Token Count by Section (Typical Ranges)

| Section | What it contains | Typical tokens |
|---------|------------------|----------------|
| System prompt | Fixed instructions | ~115 |
| User profile | Name, diets, allergens, recent meals (capped at 5) | 0–150 |
| Ranked context | Up to 10 fused results (title, cuisine, difficulty, time, short desc) | 200–1,000 |
| User query | "find me a high-protein dinner" | 5–50 |
| Conversation history | Prior turns (only in multi-turn chat) | 0–300 |
| **Total INPUT** | | **320 – 1,600** |
| **OUTPUT** | Natural language answer | **80 – 400** |

---

## Code Path Recap

```
chat_process (api/app.py)
    │
    ├─ DATA_INTENTS_NEEDING_RETRIEVAL (e.g. find_recipe)
    │
    ▼
orchestrate() → retrieves recipes
    │
    ▼
build_augmented_prompt(orch_result, user_query, customer_profile, max_fused=10)
    │  → builds the big string with [SYSTEM], [USER PROFILE], [RANKED CONTEXT], [USER QUERY]
    ▼
format_conversation_history() + inject before [USER QUERY] if multi-turn
    │
    ▼
generate_response(base_prompt)
    │
    ├─ _split_prompt() → system_content, user_content
    │
    ▼
client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
        {"role": "system", "content": system_content},   ← INPUT tokens
        {"role": "user", "content": user_content}        ← INPUT tokens
    ],
    max_tokens=1024
)
    │
    ▼
response.choices[0].message.content   ← OUTPUT tokens
```

---

## Quick Reference

| Variable | Where set | Effect on tokens |
|----------|-----------|------------------|
| `max_fused` | `generate_chat_response(..., max_fused=10)` | More recipes in context → more input tokens |
| `_PROFILE_RECENT_RECIPES_CAP` | `prompt_builder.py` = 5 | Limits recent meals in profile |
| `generation.max_tokens` | `embedding_config.yaml` = 1024 | Max output tokens (model usually uses fewer) |
| Conversation history | `format_conversation_history()` | Each prior turn adds tokens to user message |
