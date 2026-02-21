# Semantic Retrieval (Neo4j Vector Index) — Implementation Steps

## What the config implies (important)
From `embedding_config.yaml`:

- **Semantic embedding property**: `semantic.semantic.write_property = semanticEmbedding`
- **Semantic dimension**: `vector_indexes.semantic[*].dimensions = 1536`  
  → your query-time embedder **must output 1536-d vectors** (same model as ingestion).
- **Semantic retrievable labels (currently indexed)**: `Recipe`, `Ingredient`, `Product` (these are the ones you can vector-search immediately)
- **How node text was constructed (per label)**: `semantic.label_text_rules`  
  e.g., `Recipe` uses `[title, description, meal_type, instructions]`, etc.
- **Structural embedding property (future GraphRAG)**: `graph_sage.write_property = graphSageEmbedding`, dim `128`, indexed (currently) for label `B2C_Customer`.

## Refined semantic-retrieval implementation steps (GraphRAG-ready)

### 1) Create a retrieval config layer (driven by your YAML)
Use the YAML as the source of truth for:
- **Labels to retrieve**: start with the ones in `vector_indexes.semantic` (`Recipe`, `Ingredient`, `Product`)
- **Embedding property**: `semanticEmbedding`
- **Expected dimensions**: `1536`
- **Index names**: list the Neo4j index names per label (explicitly)

Output: a small in-code structure like `SemanticIndexSpec{ label, embedding_property, dimensions, index_name }`.

### 2) Implement query embedding (contract)
- `embed_query(text) -> float[1536]`
- Ensure it’s **the same embedding model** you used to produce `semanticEmbedding` during ingestion.

Output: one function used by retrieval (and later also by rerankers/tools if needed).

### 3) Confirm/standardize Neo4j vector index names
List the Neo4j semantic vector index names per label.

Output: retrieval code can reliably call “the right index” for each label.

### 4) Write semantic vector search per label (core retrieval)
For each label spec (Recipe/Product/Ingredient):
- Embed query → call Neo4j vector query (native procedure)
- Return a uniform record shape:
  - `nodeId` (or internal id), `label`, `scoreRaw`
  - minimal fields you’ll later use as context (e.g., `title/name`, `description`, etc.)
  - `source = "semantic"` and `index = <index_name>`

Output: `List[RetrievalResult]` per label.

### 5) Multi-label retrieval policy (how to search “across KG”)
Decide behavior now (so you don’t refactor later):
- **If query implies type** (e.g., “recipe for …”): search `Recipe` index only

Output: one `retrieve_semantic(query, topK_by_label, filters)` entrypoint.

### 6) Keep the score as-is (raw score only)
- Keep `scoreRaw`

Output: final ranked `RetrievalResult[]` with stable schema (this becomes the input to GraphRAG later).

### 7) Add filtering + guardrails (GraphRAG-friendly)
- Optional filters (source, brand, region, dietary tags, etc.) **after** retrieval or inside query if supported
- Similarity threshold: if top score is weak, return “no confident match” (prevents hallucination later)
- Dedup rules (e.g., same `Product` returned multiple ways)

Output: predictable retrieval behavior you can evaluate and tune.

### 8) Define the “future GraphRAG hook” now (but don’t implement yet)
Keep the output structure rich enough to support:
- **Seed expansion** (1–N hops) from retrieved `Recipe/Product/Ingredient`
- **Structural retrieval** using `graphSageEmbedding` later
- **Hybrid fusion** (semantic + structural) later

Concretely: include identifiers/metadata that make traversals easy (e.g., stable ids, key properties).

### 9) Mini evaluation set
Create a small set of queries and expected nodes for:
- `Recipe`-type queries
- `Product`-type queries
- `Ingredient`-type queries

This will be your baseline before adding GraphRAG/hybrid.

