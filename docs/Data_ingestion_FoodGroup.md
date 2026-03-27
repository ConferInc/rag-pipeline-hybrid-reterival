# USDA food groups — data pipeline enhancement (handoff spec)

**Scope:** data ingestion and **Supabase (Postgres) persistence** only. 

## Core Idea

Precompute USDA-aligned food group labels on each recipe **during data ingestion** so downstream systems do not depend on ad hoc keyword inference at read time. The pipeline should classify each recipe’s ingredients using a **hybrid approach**: deterministic rules first (fast, auditable), **semantic matching** for synonyms and variants that rules miss, and an **LLM fallback** only for unresolved or low-confidence cases. Persist canonical fields (`food_groups`, confidence, source, version) on the **recipe row in Supabase** (or equivalent Postgres schema), run a **backfill** for existing recipes, and expose stable columns or JSON fields so other teams (including graph sync) can consume the same contract later.

---

## Step-by-step implementation

1. **Define the contract**  
   - Canonical groups: `protein`, `dairy`, `vegetables`, `fruits`, `whole_grains` only.  
   - Per recipe, persist at minimum: `food_groups` (list), `food_group_confidence` (JSON or per-group columns), `food_group_source` (`rules` | `semantic` | `llm` | `mixed`), `food_group_version` (string for reprocessing), optional `food_group_unknown_count`, optional `needs_review` (boolean).

2. **Normalize ingredient strings in ETL**  
   - Lowercase, strip punctuation, collapse whitespace, strip quantities where applicable (e.g. “1 cup oats” → “oats”).  
   - Apply a small alias map (e.g. regional names → canonical tokens) aligned with product naming.

3. **Tier 1 — Rule/heuristic classifier**  
   - Reuse or mirror the existing keyword rules (same behavior as `infer_food_groups_for_ingredients` in this repo) so offline classification stays aligned with current runtime behavior.  
   - Output multi-label groups and per-group confidence; count ingredients that matched no rule.

4. **Tier 2 — Semantic classifier (for gaps)**  
   - For ingredients with no rule match or confidence below a threshold, embed the normalized string and match against a small curated set of group anchors or exemplars (or ingredient→group taxonomy).  
   - Accept semantic result only if similarity score ≥ agreed threshold (e.g. 0.75); otherwise escalate.

5. **Tier 3 — LLM fallback (restricted)**  
   - Only for remaining unresolved items or borderline scores.  
   - Require **structured JSON** output with allowed labels only; no free-text groups.  
   - Apply a minimum confidence; if still low, set `needs_review=true` and store best-effort labels.

6. **Aggregate to recipe level**  
   - Union groups from all classified ingredients; merge confidences (e.g. max per group).  
   - Optionally compute recipe-level `food_group_unknown_count` from ingredients that never matched.

7. **Supabase integration — schema and writes**  
   - Add columns (or JSONB) on the authoritative recipe table for `food_groups`, `food_group_confidence`, `food_group_source`
   - Migration: nullable columns first, then backfill, then tighten constraints if needed.  
   - On recipe create/update in the ingestion job, **upsert** these fields idempotently with the same transaction as core recipe data where possible.

8. **Backfill (Supabase)**  
   - Batch job: read recipes with ingredients from the source of truth, run the same classification pipeline, **UPDATE** rows in Supabase in chunks.  
   - Track failures and retries; log pipeline metrics (% with non-empty `food_groups`, % `needs_review`, tier usage: rules vs semantic vs LLM).

---

*Downstream: a separate effort can sync these fields to Neo4j and wire RAG retrieval payloads; this spec stops at Supabase.*
