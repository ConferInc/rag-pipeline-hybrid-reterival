# Graph RAG Pipeline — Plan of Action

## Phase 1: Structural Retrieval (GraphSAGE)

Build structural retrieval alongside the existing semantic retrieval, so both are ready before orchestration.

### Checkpoint 1a: Extend config to load structural indexes
- `config.py` currently only loads `vector_indexes.semantic`
- Add loading of `vector_indexes.structural` (label, property, dimensions, index_name)
- Add structural indexes with `index_name` in `embedding_config.yaml`

### Checkpoint 1b: Implement `structural_search_by_label()`
- Same pattern as `semantic_search_by_label()` but queries GraphSAGE vector indexes
- Returns the same `RetrievalResult` type (with `source="structural"`)
- Key difference: no query text embedding — uses a **seed node's GraphSAGE embedding as the probe vector**

### Checkpoint 1c: Implement seed-based structural retrieval flow
- Given a seed node (from semantic retrieval or known `customer_id`), take its `graphSageEmbedding`, then search the structural index for similar nodes
- Use case: find customers structurally similar to this customer → harvest their liked/saved recipes
- Use case: find recipes structurally similar to this recipe

### Checkpoint 1d: Add structural vector indexes in Neo4j for more labels
- Currently only `B2C_Customer` has a structural index
- Add indexes for `Recipe`, `Product`, `Ingredient` (GraphSAGE embeddings exist on all projected nodes)
- Update `embedding_config.yaml` accordingly

### Checkpoint 1e: Test structural retrieval via CLI
- Add a `structural-search` CLI subcommand
- Input: `--seed-node-id "..." --label Recipe --top-k 10`

---

## Phase 2: Retrieval Orchestration

Combine all retrieval paths (semantic, structural, Cypher) into one unified flow.

### Checkpoint 2a: Integrate standalone files into package
- Move `extractor_classifier.py` → `rag_pipeline/intent/extractor.py`
- Move `cypher_query_generator.py` → `rag_pipeline/retrieval/cypher_generator.py`
- Share `.env`, `neo4j_client`, config

### Checkpoint 2b: Build retrieval orchestrator
- Single entry point: `retrieve(query, customer_id=None)`
- Runs intent extractor → decides which path(s) → executes → returns unified `RetrievalOutput`
- Routes per intent:
  - Simple lookups (nutritional info, compare, substitution, diet compliance) → Cypher only
  - Recipe search (`find_recipe`, `find_recipe_by_pantry`) → Cypher primary + optional semantic for fuzzy matching
  - Recommendations (future) → all three paths

### Checkpoint 2c: Add customer context injection
- When `customer_id` is provided, fetch customer's allergens/diet/conditions from graph
- Merge these into the entities dict as additional constraints before Cypher generation
- So even if the user says "suggest dinner recipes," the Cypher automatically excludes their allergens and respects their diet

---

## Phase 3: Augmentation (Contextualized Prompts)

Turn retrieval results into grounding context for the LLM.

### Checkpoint 3a: Context formatter
- Takes `RetrievalOutput` and converts it into a structured text block
- Format depends on intent:
  - `find_recipe` → recipe titles + nutrition + ingredients + diet tags
  - `get_nutritional_info` → ingredient + nutrient values
  - `compare_foods` → side-by-side comparison
  - `check_diet_compliance` → ingredient + diet + status
  - `check_substitution` → original + substitute + ratio
  - `get_substitution_suggestion` → substitutes with nutrition
  - `rank_results` → ordered list with criterion

### Checkpoint 3b: Prompt template builder
- Assemble final LLM prompt from:
  - System message (nutrition assistant role + safety instructions)
  - Customer profile (allergens, diet, conditions — if logged in)
  - Context block (from 3a)
  - User query (verbatim)
- Keep prompt template per-intent (different instructions per intent)

### Checkpoint 3c: Guardrails
- Zero results → prompt LLM to ask clarifying question
- Customer constraints → include explicitly in system message
- Token budget → cap context block size, truncate lower-ranked results

---

## Phase 4: Generation (Recommendation)

Produce the final answer.

### Checkpoint 4a: LLM generation call
- Use existing LiteLLM setup (`OPENAI_BASE_URL` + `OPENAI_API_KEY`)
- Call chat completion model (e.g., `openai/gpt-5-mini`)
- Input: augmented prompt from Phase 3
- Output: natural-language answer

### Checkpoint 4b: Response post-processing
- Validate answer doesn't mention forbidden items (defense-in-depth)
- Structure response: answer text + cited sources (recipe titles / node IDs)

### Checkpoint 4c: End-to-end CLI
- New CLI subcommand: `rag-query`
- Input: `--query "..." --customer-id "..." (optional)`
- Flow: R → A → G → print answer
- Testable end-to-end loop

---

## Execution Summary

| Checkpoint | Phase | What | Status |
|---|---|---|---|
| 1a | Structural Retrieval | Extend config for structural indexes | Pending |
| 1b | Structural Retrieval | `structural_search_by_label()` | Pending |
| 1c | Structural Retrieval | Seed-based structural retrieval flow | Pending |
| 1d | Structural Retrieval | Add structural indexes in Neo4j + update YAML | Pending |
| 1e | Structural Retrieval | CLI `structural-search` + test | Pending |
| 2a | Orchestration | Integrate extractor + cypher generator into package | Pending |
| 2b | Orchestration | Retrieval orchestrator | Pending |
| 2c | Orchestration | Customer context injection | Pending |
| 3a | Augmentation | Context formatter | Pending |
| 3b | Augmentation | Prompt template builder | Pending |
| 3c | Augmentation | Guardrails | Pending |
| 4a | Generation | LLM generation call | Pending |
| 4b | Generation | Response post-processing | Pending |
| 4c | Generation | End-to-end CLI (`rag-query`) | Pending |
