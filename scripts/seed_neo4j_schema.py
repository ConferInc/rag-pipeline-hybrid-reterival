"""
Neo4j Schema Seeding Script — P0 + P1 gaps
==========================================
Reads from PostgreSQL gold.* tables and writes missing relationships/properties
to Neo4j. Safe to re-run (all writes are idempotent MERGE / SET).

Usage:
    python scripts/seed_neo4j_schema.py              # run all P0+P1 seeding
    python scripts/seed_neo4j_schema.py --p0-only    # safety-critical only
    python scripts/seed_neo4j_schema.py --validate   # print counts, no writes

P0 (Safety-Critical):
  1. Ingredient -[:CONTAINS_ALLERGEN]-> Allergens  (from gold.ingredient_allergens)
  2. severity on all ALLERGIC_TO relationships     (from gold.b2b_customer_allergens)
  3. severity on all HAS_CONDITION relationships   (from gold.b2b_customer_health_conditions)
  4. quantity/unit/is_primary/ingredient_order on  (from gold.product_ingredients)
     all CONTAINS_INGREDIENT relationships

P1 (Feature-Enabling):
  5. Product -[:COMPATIBLE_WITH_DIET]-> Dietary_Preferences
  6. Health_Condition -[:RESTRICTS_INGREDIENT]-> Ingredient
  7. strictness on FOLLOWS_DIET relationships
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

try:
    from neo4j import GraphDatabase
except ImportError:
    print("ERROR: neo4j not installed. Run: pip install neo4j")
    sys.exit(1)

from dotenv import load_dotenv

load_dotenv()

# ── Connection helpers ────────────────────────────────────────────────────────

def _pg_connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)
    # OLD: required SSL unless DB_SSL_REJECT_UNAUTHORIZED == "false" — but direct-IP hosts don't support SSL
    # ssl = {"sslmode": "require"} if "supabase" in url or os.getenv("DB_SSL_REJECT_UNAUTHORIZED") != "false" else {}
    # NEW: only require SSL for Supabase cloud hosts (URL contains "supabase")
    ssl = {"sslmode": "require"} if "supabase" in url else {}
    return psycopg2.connect(url, **ssl)


def _neo4j_driver():
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USERNAME", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "")
    database = os.environ.get("NEO4J_DATABASE", "neo4j")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()
    return driver, database


def _run_cypher(driver, database: str, cypher: str, params: dict | None = None) -> list[dict]:
    with driver.session(database=database) as session:
        result = session.run(cypher, **(params or {}))
        return [dict(r) for r in result]


def _unwind_cypher(driver, database: str, cypher: str, batch: list[dict], batch_size: int = 500) -> int:
    total = 0
    for i in range(0, len(batch), batch_size):
        chunk = batch[i : i + batch_size]
        with driver.session(database=database) as session:
            result = session.run(cypher, rows=chunk)
            summary = result.consume()
            total += (
                summary.counters.relationships_created
                + summary.counters.properties_set
                + summary.counters.nodes_created
            )
    return total


# ── Validation queries ────────────────────────────────────────────────────────

VALIDATION_QUERIES = {
    "CONTAINS_ALLERGEN (P0-1)": "MATCH ()-[r:CONTAINS_ALLERGEN]->() RETURN COUNT(r) AS n",
    # OLD used wrong rel type ALLERGIC_TO (doesn't exist); actual rel is IS_ALLERGIC
    # "ALLERGIC_TO with severity (P0-2)": "MATCH ()-[r:ALLERGIC_TO]->() WHERE r.severity IS NOT NULL RETURN COUNT(r) AS n",
    # "ALLERGIC_TO missing severity": "MATCH ()-[r:ALLERGIC_TO]->() WHERE r.severity IS NULL RETURN COUNT(r) AS n",
    "IS_ALLERGIC with severity (P0-2)": "MATCH ()-[r:IS_ALLERGIC]->() WHERE r.severity IS NOT NULL RETURN COUNT(r) AS n",
    "IS_ALLERGIC missing severity": "MATCH ()-[r:IS_ALLERGIC]->() WHERE r.severity IS NULL RETURN COUNT(r) AS n",
    "HAS_CONDITION with severity (P0-3)": "MATCH ()-[r:HAS_CONDITION]->() WHERE r.severity IS NOT NULL RETURN COUNT(r) AS n",
    "HAS_CONDITION missing severity": "MATCH ()-[r:HAS_CONDITION]->() WHERE r.severity IS NULL RETURN COUNT(r) AS n",
    "CONTAINS_INGREDIENT with quantity (P0-4)": "MATCH ()-[r:CONTAINS_INGREDIENT]->() WHERE r.quantity IS NOT NULL RETURN COUNT(r) AS n",
    "CONTAINS_INGREDIENT missing quantity": "MATCH ()-[r:CONTAINS_INGREDIENT]->() WHERE r.quantity IS NULL AND r.is_primary IS NULL RETURN COUNT(r) AS n",
    "COMPATIBLE_WITH_DIET (P1-5)": "MATCH ()-[r:COMPATIBLE_WITH_DIET]->() RETURN COUNT(r) AS n",
    "RESTRICTS_INGREDIENT (P1-6)": "MATCH ()-[r:RESTRICTS_INGREDIENT]->() RETURN COUNT(r) AS n",
    "FOLLOWS_DIET with strictness (P1-7)": "MATCH ()-[r:FOLLOWS_DIET]->() WHERE r.strictness IS NOT NULL RETURN COUNT(r) AS n",
    # Population check from item 3
    "B2BCustomer nodes": "MATCH (c:B2BCustomer) RETURN COUNT(c) AS n",
    "Product nodes": "MATCH (p:Product) RETURN COUNT(p) AS n",
    # OLD: "ALLERGIC_TO total": "MATCH ()-[r:ALLERGIC_TO]->() RETURN COUNT(r) AS n",
    "IS_ALLERGIC total": "MATCH ()-[r:IS_ALLERGIC]->() RETURN COUNT(r) AS n",
    "CONTAINS_INGREDIENT total": "MATCH ()-[r:CONTAINS_INGREDIENT]->() RETURN COUNT(r) AS n",
}


def run_validation(driver, database: str) -> None:
    print("\n=== Neo4j Schema Validation ===\n")
    for label, cypher in VALIDATION_QUERIES.items():
        try:
            rows = _run_cypher(driver, database, cypher)
            n = rows[0]["n"] if rows else 0
            status = "✓" if n > 0 else "✗ EMPTY"
            print(f"  {status:10s} {label}: {n:,}")
        except Exception as e:
            print(f"  ERROR      {label}: {e}")
    print()


# ── P0-1: CONTAINS_ALLERGEN edges ────────────────────────────────────────────

def seed_contains_allergen(pg, driver, database: str) -> int:
    """Ingredient -[:CONTAINS_ALLERGEN]-> Allergens from gold.ingredient_allergens."""
    with pg.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        try:
            cur.execute("""
                SELECT ingredient_id::text, allergen_id::text
                FROM gold.ingredient_allergens
                WHERE ingredient_id IS NOT NULL AND allergen_id IS NOT NULL
            """)
            rows = [{"ing_id": r["ingredient_id"], "allergen_id": r["allergen_id"]} for r in cur.fetchall()]
        except psycopg2.Error as e:
            print(f"  WARN: gold.ingredient_allergens not accessible: {e}")
            return 0

    if not rows:
        print("  No rows in gold.ingredient_allergens")
        return 0

    cypher = """
UNWIND $rows AS row
MATCH (i:Ingredient {id: row.ing_id})
MATCH (a:Allergens {id: row.allergen_id})
MERGE (i)-[:CONTAINS_ALLERGEN]->(a)
"""
    total = _unwind_cypher(driver, database, cypher, rows)
    print(f"  CONTAINS_ALLERGEN: {total:,} ops ({len(rows):,} source rows)")
    return total


# ── P0-2: IS_ALLERGIC edges + severity for B2BCustomers ──────────────────────

def seed_allergic_to_severity(pg, driver, database: str) -> int:
    """
    MERGE IS_ALLERGIC edges for B2BCustomers and set severity.

    The existing 10 IS_ALLERGIC edges belong to B2C_Customer nodes (not B2BCustomer).
    B2B allergen data lives only in gold.b2b_customer_allergens — we must MERGE the
    edges from scratch for B2BCustomer nodes, then set severity.
    """
    with pg.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        try:
            cur.execute("""
                SELECT b2b_customer_id::text AS customer_id, allergen_id::text,
                       COALESCE(severity, 'mild') AS severity
                FROM gold.b2b_customer_allergens
                WHERE b2b_customer_id IS NOT NULL AND allergen_id IS NOT NULL
                  AND (is_active IS NULL OR is_active = true)
            """)
            rows = [
                {"cust_id": r["customer_id"], "allergen_id": r["allergen_id"], "severity": r["severity"]}
                for r in cur.fetchall()
            ]
        except psycopg2.Error as e:
            print(f"  WARN: gold.b2b_customer_allergens not accessible: {e}")
            return 0

    if not rows:
        print("  No rows in gold.b2b_customer_allergens")
        return 0

    # OLD: tried to SET severity on existing IS_ALLERGIC edges — but B2BCustomers had none
    # OLD: MATCH (c:B2BCustomer {id: row.cust_id})-[r:IS_ALLERGIC]->(a:Allergens {id: row.allergen_id})
    # OLD: SET r.severity = row.severity
    # NEW: MERGE the edge first (creates if missing), then set severity
    cypher = """
UNWIND $rows AS row
MATCH (c:B2BCustomer {id: row.cust_id})
MATCH (a:Allergens {id: row.allergen_id})
MERGE (c)-[r:IS_ALLERGIC]->(a)
SET r.severity = row.severity
"""
    total = _unwind_cypher(driver, database, cypher, rows)
    print(f"  B2BCustomer IS_ALLERGIC (merged + severity): {total:,} ops ({len(rows):,} source rows)")
    return total


# ── P0-3: severity on HAS_CONDITION ──────────────────────────────────────────

def seed_has_condition_severity(pg, driver, database: str) -> int:
    """
    MERGE HAS_CONDITION edges for B2BCustomers and set severity.

    The existing 4 HAS_CONDITION edges belong to B2C_Customer→B2C_Customer_Health_Conditions.
    B2B health condition data lives in gold.b2b_customer_health_conditions — we MERGE from scratch
    targeting Health_Condition nodes (the shared condition type in the graph).
    """
    with pg.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        try:
            cur.execute("""
                SELECT b2b_customer_id::text AS customer_id, condition_id::text,
                       COALESCE(severity, 'moderate') AS severity
                FROM gold.b2b_customer_health_conditions
                WHERE b2b_customer_id IS NOT NULL AND condition_id IS NOT NULL
                  AND (is_active IS NULL OR is_active = true)
            """)
            rows = [
                {"cust_id": r["customer_id"], "cond_id": r["condition_id"], "severity": r["severity"]}
                for r in cur.fetchall()
            ]
        except psycopg2.Error as e:
            print(f"  WARN: gold.b2b_customer_health_conditions not accessible: {e}")
            return 0

    if not rows:
        print("  No rows in gold.b2b_customer_health_conditions")
        return 0

    # OLD: tried to SET severity on existing HAS_CONDITION — but B2BCustomers had none
    # OLD: MATCH (c:B2BCustomer {id: row.cust_id})-[r:HAS_CONDITION]->(hc:Health_Condition {id: row.cond_id})
    # NEW: MERGE edge from B2BCustomer to Health_Condition node
    cypher = """
UNWIND $rows AS row
MATCH (c:B2BCustomer {id: row.cust_id})
MATCH (hc:Health_Condition {id: row.cond_id})
MERGE (c)-[r:HAS_CONDITION]->(hc)
SET r.severity = row.severity
"""
    total = _unwind_cypher(driver, database, cypher, rows)
    print(f"  HAS_CONDITION severity: {total:,} ops ({len(rows):,} source rows)")
    return total


# ── P0-4: CONTAINS_INGREDIENT relationship properties ────────────────────────

def seed_contains_ingredient_props(pg, driver, database: str) -> int:
    """Enrich CONTAINS_INGREDIENT with quantity/unit/is_primary/ingredient_order."""
    with pg.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        try:
            cur.execute("""
                SELECT product_id::text, ingredient_id::text,
                       quantity, unit,
                       COALESCE(is_primary, false) AS is_primary,
                       COALESCE(ingredient_order, 0) AS ingredient_order
                FROM gold.product_ingredients
                WHERE product_id IS NOT NULL AND ingredient_id IS NOT NULL
            """)
            rows = [
                {
                    "product_id": r["product_id"],
                    "ing_id": r["ingredient_id"],
                    "quantity": float(r["quantity"]) if r["quantity"] is not None else None,
                    "unit": r["unit"],
                    "is_primary": bool(r["is_primary"]),
                    "ingredient_order": int(r["ingredient_order"]),
                }
                for r in cur.fetchall()
            ]
        except psycopg2.Error as e:
            print(f"  WARN: gold.product_ingredients not accessible: {e}")
            return 0

    if not rows:
        print("  No rows in gold.product_ingredients")
        return 0

    cypher = """
UNWIND $rows AS row
MATCH (p:Product {id: row.product_id})-[r:CONTAINS_INGREDIENT]->(i:Ingredient {id: row.ing_id})
SET r.quantity = row.quantity,
    r.unit = row.unit,
    r.is_primary = row.is_primary,
    r.ingredient_order = row.ingredient_order
"""
    total = _unwind_cypher(driver, database, cypher, rows)
    print(f"  CONTAINS_INGREDIENT props: {total:,} ops ({len(rows):,} source rows)")
    return total


# ── P1-5: COMPATIBLE_WITH_DIET edges ─────────────────────────────────────────

def seed_compatible_with_diet(pg, driver, database: str) -> int:
    """Product -[:COMPATIBLE_WITH_DIET]-> Dietary_Preferences from gold.product_dietary_preferences."""
    with pg.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        try:
            # OLD: included diet_name column which doesn't exist in this table
            # SELECT product_id::text, diet_id::text, diet_name FROM gold.product_dietary_preferences
            cur.execute("""
                SELECT product_id::text,
                       diet_id::text
                FROM gold.product_dietary_preferences
                WHERE product_id IS NOT NULL AND diet_id IS NOT NULL
                  AND (is_compatible IS NULL OR is_compatible = true)
            """)
            rows = [
                {
                    "product_id": r["product_id"],
                    "diet_id": r["diet_id"],
                }
                for r in cur.fetchall()
            ]
        except psycopg2.Error as e:
            print(f"  WARN: gold.product_dietary_preferences not accessible: {e}")
            return 0

    if not rows:
        print("  No rows in gold.product_dietary_preferences")
        return 0

    cypher = """
UNWIND $rows AS row
MATCH (p:Product {id: row.product_id})
MATCH (dp:Dietary_Preferences {id: row.diet_id})
MERGE (p)-[:COMPATIBLE_WITH_DIET]->(dp)
"""
    total = _unwind_cypher(driver, database, cypher, rows)
    print(f"  COMPATIBLE_WITH_DIET: {total:,} ops ({len(rows):,} source rows)")
    return total


# ── P1-6: RESTRICTS_INGREDIENT edges ─────────────────────────────────────────

def seed_restricts_ingredient(pg, driver, database: str) -> int:
    """Health_Condition -[:RESTRICTS_INGREDIENT]-> Ingredient."""
    with pg.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        try:
            cur.execute("""
                SELECT condition_id::text, ingredient_id::text
                FROM gold.health_condition_ingredient_restrictions
                WHERE condition_id IS NOT NULL AND ingredient_id IS NOT NULL
            """)
            rows = [
                {"cond_id": r["condition_id"], "ing_id": r["ingredient_id"]}
                for r in cur.fetchall()
            ]
        except psycopg2.Error as e:
            print(f"  WARN: gold.health_condition_ingredient_restrictions not accessible: {e}")
            return 0

    if not rows:
        print("  No rows in gold.health_condition_ingredient_restrictions")
        return 0

    cypher = """
UNWIND $rows AS row
MATCH (hc:Health_Condition {id: row.cond_id})
MATCH (i:Ingredient {id: row.ing_id})
MERGE (hc)-[:RESTRICTS_INGREDIENT]->(i)
"""
    total = _unwind_cypher(driver, database, cypher, rows)
    print(f"  RESTRICTS_INGREDIENT: {total:,} ops ({len(rows):,} source rows)")
    return total


# ── P1-7: strictness on FOLLOWS_DIET ─────────────────────────────────────────

def seed_follows_diet_strictness(pg, driver, database: str) -> int:
    """Set strictness on FOLLOWS_DIET edges from gold.b2b_customer_dietary_preferences."""
    with pg.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        try:
            cur.execute("""
                SELECT b2b_customer_id::text AS customer_id, diet_id::text,
                       COALESCE(strictness, 'moderate') AS strictness
                FROM gold.b2b_customer_dietary_preferences
                WHERE b2b_customer_id IS NOT NULL AND diet_id IS NOT NULL
                  AND (is_active IS NULL OR is_active = true)
            """)
            rows = [
                {"cust_id": r["customer_id"], "diet_id": r["diet_id"], "strictness": r["strictness"]}
                for r in cur.fetchall()
            ]
        except psycopg2.Error as e:
            print(f"  WARN: gold.b2b_customer_dietary_preferences not accessible: {e}")
            return 0

    if not rows:
        print("  No rows in gold.b2b_customer_dietary_preferences")
        return 0

    # OLD: tried to SET on existing FOLLOWS_DIET — but B2BCustomers had none (only B2C_Customer does)
    # OLD: MATCH (c:B2BCustomer {id: row.cust_id})-[r:FOLLOWS_DIET]->(dp:Dietary_Preferences {id: row.diet_id})
    # NEW: MERGE the edge first
    cypher = """
UNWIND $rows AS row
MATCH (c:B2BCustomer {id: row.cust_id})
MATCH (dp:Dietary_Preferences {id: row.diet_id})
MERGE (c)-[r:FOLLOWS_DIET]->(dp)
SET r.strictness = row.strictness
"""
    total = _unwind_cypher(driver, database, cypher, rows)
    print(f"  FOLLOWS_DIET strictness: {total:,} ops ({len(rows):,} source rows)")
    return total


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Seed Neo4j schema gaps from PostgreSQL")
    parser.add_argument("--p0-only", action="store_true", help="Run P0 safety-critical changes only")
    parser.add_argument("--validate", action="store_true", help="Print counts only, no writes")
    args = parser.parse_args()

    print("Connecting to Neo4j...")
    driver, database = _neo4j_driver()
    print(f"  Connected to {os.getenv('NEO4J_URI')} database={database}")

    if args.validate:
        run_validation(driver, database)
        driver.close()
        return

    print("Connecting to PostgreSQL...")
    pg = _pg_connect()
    print("  Connected")

    t0 = time.time()
    total_ops = 0

    print("\n── P0: Safety-Critical ────────────────────────────────────────────")
    total_ops += seed_contains_allergen(pg, driver, database)
    total_ops += seed_allergic_to_severity(pg, driver, database)
    total_ops += seed_has_condition_severity(pg, driver, database)
    total_ops += seed_contains_ingredient_props(pg, driver, database)

    if not args.p0_only:
        print("\n── P1: Feature-Enabling ───────────────────────────────────────────")
        total_ops += seed_compatible_with_diet(pg, driver, database)
        total_ops += seed_restricts_ingredient(pg, driver, database)
        total_ops += seed_follows_diet_strictness(pg, driver, database)

    elapsed = time.time() - t0
    print(f"\nDone: {total_ops:,} Neo4j ops in {elapsed:.1f}s")

    print("\nRunning post-seed validation...")
    run_validation(driver, database)

    pg.close()
    driver.close()


if __name__ == "__main__":
    main()
