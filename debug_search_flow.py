#!/usr/bin/env python3
"""
Debug script for /search/hybrid empty results.
Run: python debug_search_flow.py "vegan recipes"
Traces each pipeline stage without modifying code.
"""
import asyncio
import json
import os
import sys

# Load env before imports
from dotenv import load_dotenv
load_dotenv()

def step1_intent():
    """Step 1: Intent + entity extraction"""
    from extractor_classifier import extract_intent, parse_extractor_output
    query = sys.argv[1] if len(sys.argv) > 1 else "vegan recipes"
    raw = extract_intent(query)
    parsed = parse_extractor_output(raw)
    print("=" * 60)
    print("STEP 1: Intent extraction")
    print("=" * 60)
    print("Raw:", raw[:300])
    print("Parsed:", parsed)
    return parsed, query

def step2_cypher_query(entities, intent):
    """Step 2: What Cypher would be generated"""
    from cypher_query_generator import generate_cypher_query
    try:
        cypher, params = generate_cypher_query(intent, entities)
        print("\n" + "=" * 60)
        print("STEP 2: Cypher query (for find_recipe + diet)")
        print("=" * 60)
        print("Query:\n", cypher)
        print("Params:", params)
        return cypher, params
    except ValueError as e:
        print("Cypher generation error:", e)
        return None, None

def step3_run_cypher(cypher, params):
    """Step 3: Run Cypher directly"""
    from rag_pipeline.neo4j_client import create_neo4j_driver, neo4j_settings_from_env
    if not cypher:
        return
    driver = create_neo4j_driver(neo4j_settings_from_env())
    db = os.getenv("NEO4J_DATABASE", "neo4j")
    try:
        with driver.session(database=db) as session:
            rows = list(session.run(cypher, **params))
        print("\n" + "=" * 60)
        print("STEP 3: Cypher execution")
        print("=" * 60)
        print("Cypher rows returned:", len(rows))
        for i, r in enumerate(rows[:3]):
            print(f"  Row {i+1}:", dict(r))
    except Exception as e:
        print("Cypher error:", e)
    finally:
        driver.close()

def step4_semantic(query):
    """Step 4: Semantic retrieval"""
    from rag_pipeline.retrieval.service import retrieve_semantic, SemanticRetrievalRequest
    from rag_pipeline.config import load_embedding_config
    from rag_pipeline.neo4j_client import create_neo4j_driver, neo4j_settings_from_env
    from rag_pipeline.embeddings.openai_embedder import OpenAIQueryEmbedder
    from openai import OpenAI
    cfg = load_embedding_config("embedding_config.yaml")
    driver = create_neo4j_driver(neo4j_settings_from_env())
    embedder = OpenAIQueryEmbedder(
        client=OpenAI(
            base_url=os.getenv("OPENAI_BASE_URL"),
            api_key=os.getenv("OPENAI_API_KEY"),
        ),
        model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
    )
    print("\n" + "=" * 60)
    print("STEP 4: Semantic retrieval (embedding + vector search)")
    print("=" * 60)
    try:
        results = retrieve_semantic(
            driver, cfg=cfg, embedder=embedder,
            request=SemanticRetrievalRequest(query=query, top_k=10, label="Recipe"),
            database=os.getenv("NEO4J_DATABASE"),
        )
        print("Semantic count:", len(results))
        for i, r in enumerate(results[:3]):
            print(f"  {i+1}. title={r.payload.get('title')}, id={r.payload.get('id')}, score={r.score_raw:.4f}")
    except Exception as e:
        print("Semantic error:", e)
    finally:
        driver.close()

def step5_full_orchestrate(query):
    """Step 5: Full orchestrate() output"""
    from rag_pipeline.orchestrator.orchestrator import orchestrate
    from rag_pipeline.config import load_embedding_config
    from rag_pipeline.neo4j_client import create_neo4j_driver, neo4j_settings_from_env
    from rag_pipeline.embeddings.openai_embedder import OpenAIQueryEmbedder
    from openai import OpenAI
    cfg = load_embedding_config("embedding_config.yaml")
    driver = create_neo4j_driver(neo4j_settings_from_env())
    embedder = OpenAIQueryEmbedder(
        client=OpenAI(
            base_url=os.getenv("OPENAI_BASE_URL"),
            api_key=os.getenv("OPENAI_API_KEY"),
        ),
        model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
    )
    print("\n" + "=" * 60)
    print("STEP 5: Full orchestrate()")
    print("=" * 60)
    try:
        result = asyncio.run(orchestrate(
            driver, cfg=cfg, embedder=embedder,
            user_query=query,
            customer_node_id=None,
            top_k=10,
            database=os.getenv("NEO4J_DATABASE"),
        ))
        print("intent:", result.intent)
        print("entities:", result.entities)
        print("semantic_count:", len(result.semantic_results))
        print("structural_count:", len(result.structural_results.get("expanded_context", [])))
        print("cypher_count:", len(result.cypher_results))
        print("fused_count:", len(result.fused_results))
        print("errors:", result.errors)
        if result.fallback_message:
            print("fallback_message:", result.fallback_message[:200])
    except Exception as e:
        print("Orchestrate error:", e)
        import traceback
        traceback.print_exc()
    finally:
        driver.close()

def main():
    parsed, query = step1_intent()
    intent = parsed.get("intent", "")
    entities = parsed.get("entities", {})
    cypher, params = step2_cypher_query(entities, intent)
    step3_run_cypher(cypher, params)
    step4_semantic(query)
    step5_full_orchestrate(query)
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print("If semantic_count=0: Embedding API failing OR vector index empty OR min_score 0.5 filter")
    print("If cypher_count=0: No path (B2C_Customer)-[:FOLLOWS_DIET]->Vegan AND (cust)-[:SAVED|VIEWED]->Recipe")
    print("If both 0: fused_count=0 -> empty results")
    print("\nNeo4j validation queries (run in Neo4j Browser):")
    print("  MATCH (c:B2C_Customer)-[:FOLLOWS_DIET]->(dp:Dietary_Preferences {name: 'Vegan'}) RETURN count(c)")
    print("  MATCH (c:B2C_Customer)-[:FOLLOWS_DIET]->(dp:Dietary_Preferences {name: 'Vegan'})")
    print("        -[:SAVED|VIEWED]->(r:Recipe) RETURN count(r)")
    print("  MATCH (r:Recipe) RETURN count(r) AS total_recipes")
    print("  CALL db.indexes() YIELD name WHERE name CONTAINS 'vec' RETURN name")

if __name__ == "__main__":
    main()
