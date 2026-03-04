# LiteLLM Server Usage & Cost Analysis Report

**RAG Pipeline — NutriB2C Application**  
**Generated:** March 3, 2025

---

## Executive Summary

This report analyzes LiteLLM server usage across your RAG pipeline (embedding model + LLM) for cost estimation. It traces every API call path, accounts for your optimizations (caching, heuristics, system prompt optimization), and estimates average token usage and costs for end-user flows via the UI.

**Key Models via LiteLLM Proxy:**
- **Embedding:** `text-embedding-3-small` (1536-d, via `OPENAI_EMBEDDING_MODEL`)
- **LLM:** `gpt-4o-mini` (via `GENERATION_MODEL` / `INTENT_MODEL`)

**Pricing Reference (OpenAI, Jan 2025):**
| Model | Input | Output |
|-------|-------|--------|
| text-embedding-3-small | $0.02 / 1M tokens | N/A (fixed 1536-d output) |
| gpt-4o-mini | $0.15 / 1M tokens | $0.60 / 1M tokens |

---

## 1. API Entry Points & User Flows

When the UI integrates with your API, users interact through these endpoints:

| Endpoint | Flow Type | Embedding | LLM Intent | LLM Label | LLM Generation |
|----------|-----------|-----------|------------|-----------|----------------|
| `POST /search/hybrid` | Search page query | ✅ 1× | ✅ 1× (or heuristic) | ❌ | ❌ |
| `POST /recommend/feed` | Personalized feed | ✅ 1× | ❌ | ❌ | ❌ |
| `POST /recommend/meal-candidates` | Meal plan candidates | ✅ 1× | ❌ | ❌ | ❌ |
| `POST /chat/process` | Chatbot message | Varies | Varies | ❌ | Varies |

---

## 2. Embedding Model Usage (text-embedding-3-small)

### 2.1 Where Embeddings Are Called

| Location | File | Trigger |
|----------|------|---------|
| Semantic retrieval | `rag_pipeline/retrieval/service.py` → `embedder.embed_query(query)` | Every semantic search |
| Orchestrator | `orchestrator.py` → `_run_semantic` → `retrieve_semantic` | Per label in `labels_to_search` |
| Feed | `api/app.py` → `recommend_feed` | Synthetic query from profile |
| Meal candidates | `api/app.py` → `recommend_meal_candidates` | Same as feed |

### 2.2 Embedding Call Count per Request

| Endpoint | Labels Searched | Embedding Calls (Cache Miss) |
|----------|-----------------|------------------------------|
| `/search/hybrid` | 1 (usually) or 2–3 if `broaden_on_low_confidence` | **1** |
| `/recommend/feed` | 1 (Recipe, explicit) | **1** |
| `/recommend/meal-candidates` | 1 (Recipe, explicit) | **1** |
| `/chat/process` (data intents) | 1 (from `intent_semantic_labels`) | **1** |

**Note:** Structural retrieval uses GraphSAGE embeddings from Neo4j (pre-computed, not via LiteLLM). No embedding API call for structural search.

### 2.3 Input Text per Embedding Call

| Flow | Typical Input | Est. Tokens |
|------|---------------|-------------|
| Search | User query (e.g. "keto dinner under 30 min") | 8–40 |
| Feed / Meal candidates | `build_feed_query_text()` e.g. "Vegan low calorie breakfast recipes" | 5–15 |
| Chat | User message | 5–80 |

**Average:** ~25 tokens per embedding call (input only; output is fixed 1536-d vector, no output token charge for embeddings).

### 2.4 Embedding Cache

- **Config:** `embedding_config.yaml` → `embedding_cache.enabled: true`, `max_size: 500`
- **Key:** Normalized query (`strip_lower`)
- **Effect:** Duplicate or similar queries (e.g. "find keto recipes" vs "find keto recipes ") hit cache → **0 embedding API calls**
- **Estimate:** 30–50% cache hit rate for typical usage (repeated searches, feed refreshes)

---

## 3. LLM Usage (gpt-4o-mini)

### 3.1 Three LLM Call Types

| Call Type | Purpose | When Used | Config / File |
|-----------|---------|-----------|---------------|
| **Intent extraction** | NLU: intent + entities | When keyword pre-filter returns `None` | `extractor_classifier.py` |
| **Label inference** | Semantic search label | When heuristics fail & `fallback_to_llm: true` | `label_inference.py` |
| **Response generation** | Natural language answer | Data intents needing retrieval | `generator.py` |

### 3.2 Intent Extraction

**Flow:**
1. Keyword pre-filter (`_keyword_extract`) — zero cost
2. If confident → return immediately, **no LLM**
3. If ambiguous → LLM with compact system prompt

**Token Usage (when LLM is called):**
- System prompt: ~212 tokens (compact zero-shot)
- User message: ~10–50 tokens
- **Input:** ~250–300 tokens
- **Output:** ~50–150 tokens (JSON intent + entities)

**Heuristic bypass rate (from `nlu.py`):** ~60% of messages avoid LLM via rules. For `/search/hybrid`, the orchestrator calls `extract_intent` (or `extract_intent_with_retry` if `on_parse_failure: retry`). The keyword pre-filter in `extractor_classifier._keyword_extract` covers many patterns; estimate **40–50% of search queries** need the LLM. For chat, `extract_hybrid` uses rules first; similar ~40–50% LLM fallback for data intents.

**Intent cache:** Only used when `on_parse_failure: retry` and `extract_intent_with_retry` is called. Default is `abort`, so **intent cache is not used in the default config**. If you enable retry mode, duplicate queries would hit the intent cache.

### 3.3 Label Inference

**When used:** `retrieve_semantic` is called **without** an explicit label. In `/search/hybrid`, `/recommend/feed`, and `/chat/process` (orchestrate path), the label is always derived from `intent_semantic_labels` or explicitly set (`"Recipe"`). **Label inference LLM is rarely called in production UI flows.**

If used (e.g. CLI `ask` without `--label`):
- Input: ~50 tokens (short prompt + query)
- Output: ~5 tokens (single label)
- **Negligible** in cost for UI flows.

### 3.4 Response Generation

**When used:** Only in `/chat/process` when intent ∈ `DATA_INTENTS_NEEDING_RETRIEVAL` (e.g. `find_recipe`, `get_nutritional_info`).

**Token Usage:**
- System prompt: ~120 tokens (`prompt_builder.SYSTEM_PROMPT`)
- User profile (optional): ~50–100 tokens (`_build_profile_section`, capped at 5 recent recipes)
- Ranked context: `format_fused_results_as_text` with `max_fused=10` → ~600–1,200 tokens (10 recipes × ~60–120 tokens each)
- User query: ~10–50 tokens
- Conversation history (multi-turn): ~50–300 tokens
- **Total input:** ~830–1,770 tokens (average ~1,200)
- **Output:** `max_tokens: 1024` (config), typical 150–400 tokens

**Config:** `embedding_config.yaml` → `generation.max_tokens: 1024`, `temperature: 0.5`

---

## 4. Flow-by-Flow Token and Cost Summary

### 4.1 POST /search/hybrid (Search Page)

| Component | Tokens (Avg) | Cost (per request, cache miss) |
|-----------|--------------|--------------------------------|
| Embedding | 25 in | $0.0000005 |
| Intent extraction (50% LLM) | 280 in, 100 out | $0.000042 + $0.00006 = $0.000102 |
| **Total (avg)** | | **~$0.0001** |

- No generation; structured results only.
- With 40% embedding cache hit: **~$0.00006/request**

### 4.2 POST /recommend/feed

| Component | Tokens (Avg) | Cost (per request) |
|-----------|--------------|--------------------|
| Embedding | 10 in | $0.0000002 |

- No intent extraction (fixed `find_recipe`).
- No generation.
- With 50% cache hit (similar profile queries): **~$0.0000001/request**

### 4.3 POST /recommend/meal-candidates

Same as feed: **~$0.0000002/request** (embedding only).

### 4.4 POST /chat/process

**Path depends on intent:**

| Intent Type | Embedding | Intent LLM | Generation | Total Est. Cost |
|-------------|-----------|------------|------------|-----------------|
| Confirmation / Rejection | 0 | 0 | 0 | $0 |
| Template (greeting, help, farewell, out_of_scope) | 0 | 0 | 0 | $0 |
| Data (show_meal_plan, meal_history, nutrition_summary) | 0 | 0 | 0 | $0 |
| Data (find_recipe, get_nutritional_info, etc.) | 1× | 1× (50%) | 1× | **~$0.00035** |

**Per chat message (data intent, retrieval + generation):**
- Embedding: 25 tokens → $0.0000005
- Intent (50%): 280 in, 100 out → $0.000102
- Generation: 1,200 in, 250 out → $0.00018 + $0.00015 = $0.00033  
- **Total:** ~**$0.00035** (worst case, no cache)

With embedding cache (40% hit) and keyword bypass (50%): **~$0.0002/request** for data intents.

---

## 5. Optimization Summary

| Optimization | Location | Effect |
|--------------|----------|--------|
| **Embedding cache** | `CachingQueryEmbedder`, max 500 | Avoids repeat embedding calls for same/similar queries |
| **Intent keyword pre-filter** | `extractor_classifier._keyword_extract` | ~50% of queries skip intent LLM |
| **Intent cache** | `intent_cache` (only when retry mode) | Not active by default |
| **Label heuristics** | `retrieval/service._infer_label_heuristics` | Label inference LLM rarely used in UI flows |
| **Label cache** | `label_cache` | Used when label inference runs (rare in UI) |
| **Compact system prompt** | `extractor_classifier.SYSTEM_PROMPT` | ~212 tokens vs ~1,497 before |
| **Profile cap** | `_PROFILE_RECENT_RECIPES_CAP = 5` | Limits user profile tokens in generation prompt |
| **Heuristic checks** | Diet/course/cuisine maps, regex patterns | Reduces ambiguous queries reaching LLM |
| **No generation for search** | `/search/hybrid` returns structured results | Saves ~$0.00025/request vs chat-style generation |

---

## 6. Monthly Cost Estimates (Example Scenarios)

Assumptions:
- 1 token ≈ 4 characters (English)
- Embedding cache hit: 40%
- Intent keyword bypass: 50%
- Mix: 60% search, 25% feed, 15% chat (data intents)

### Scenario A: 10,000 requests/month

| Endpoint | Requests | Est. Cost |
|----------|----------|-----------|
| Search | 6,000 | $0.36 |
| Feed | 2,500 | $0.00025 |
| Meal candidates | 1,500 | $0.0003 |
| Chat (data) | 1,500 | $0.30 |
| **Total** | | **~$0.66/month** |

### Scenario B: 100,000 requests/month

| Endpoint | Requests | Est. Cost |
|----------|----------|-----------|
| Search | 60,000 | $3.60 |
| Feed | 25,000 | $0.0025 |
| Meal candidates | 15,000 | $0.003 |
| Chat (data) | 15,000 | $3.00 |
| **Total** | | **~$6.60/month** |

### Scenario C: 1,000,000 requests/month

| Endpoint | Requests | Est. Cost |
|----------|----------|-----------|
| Search | 600,000 | $36 |
| Feed | 250,000 | $0.025 |
| Meal candidates | 150,000 | $0.03 |
| Chat (data) | 150,000 | $30 |
| **Total** | | **~$66/month** |

---

## 7. Token Summary Table (Average per Request)

| Flow | Embedding In | Intent In | Intent Out | Gen In | Gen Out | **Total In** | **Total Out** |
|------|--------------|-----------|------------|--------|---------|--------------|---------------|
| Search | 25 | 140* | 50* | 0 | 0 | **165** | **50** |
| Feed | 10 | 0 | 0 | 0 | 0 | **10** | **0** |
| Meal candidates | 10 | 0 | 0 | 0 | 0 | **10** | **0** |
| Chat (template) | 0 | 0 | 0 | 0 | 0 | **0** | **0** |
| Chat (data intent) | 25 | 140* | 50* | 1,200 | 250 | **1,365** | **300** |

\* Intent: 50% of requests use LLM; values are per-request averages.

---

## 8. Recommendations

1. **Enable intent cache:** Set `on_parse_failure: retry` in `embedding_config.yaml` to use `extract_intent_with_retry`, which uses the intent cache. This reduces repeat intent LLM calls.
2. **Monitor cache hit rates:** Log embedding and intent cache hits to refine hit-rate assumptions.
3. **Consider lower `max_tokens` for generation:** 1024 is reasonable; 512 may suffice for short answers and cut output cost.
4. **LiteLLM proxy routing:** Confirm `OPENAI_BASE_URL` routes to the correct models; validate usage in the LiteLLM dashboard.

---

## 9. Files Reference

| Component | Primary Files |
|-----------|---------------|
| Embedding | `rag_pipeline/embeddings/openai_embedder.py`, `caching_embedder.py` |
| Intent extraction | `extractor_classifier.py` |
| Label inference | `rag_pipeline/retrieval/label_inference.py`, `service.py` |
| Generation | `rag_pipeline/generation/generator.py` |
| Orchestration | `rag_pipeline/orchestrator/orchestrator.py` |
| API routes | `api/app.py` |
| Config | `embedding_config.yaml`, `.env` |
