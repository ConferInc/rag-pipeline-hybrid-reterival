# B2B Neo4j Mandatory Gaps — RAG Team Requirements

> **Audience:** RAG Pipeline Engineers
> **Repo:** `rag-pipeline-hybrid-reterival`
> **Purpose:** Specific Neo4j schema additions needed for the B2B Graph RAG pipeline to work correctly.
> **Source:** Analysis of `gold.sql` schema vs. current Neo4j architecture (`neo4j-complete-graph-architecture-v3.md`)

---

## 1. Current Neo4j State (B2B Nodes & Relationships)

### Existing Nodes

| Node Label | Source Table | Key Properties |
|-----------|------------|----------------|
| `Vendor` | `gold.vendors` | id, name, code |
| `B2BCustomer` | `gold.b2b_customers` | id, full_name, email, age, gender, vendor_id |
| `Product` | `gold.products` | id, name, brand, calories, protein_g, etc. |
| `Ingredient` | `gold.ingredients` | id, name, code |
| `Allergen` | `gold.allergens` | id, name, code, severity_typical |
| `HealthCondition` | `gold.health_conditions` | id, name, code |
| `DietaryPreference` | `gold.dietary_preferences` | id, name, code |
| `B2BHealthProfile` | `gold.b2b_customer_health_profiles` | bmr, tdee, bmi, targets |
| `ProductCategory` | `gold.product_categories` | id, name, parent_id |

### Existing Relationships

| Relationship | Pattern | Properties |
|-------------|---------|------------|
| `BELONGS_TO_VENDOR` | `B2BCustomer → Vendor` | — |
| `SOLD_BY` | `Product → Vendor` | — |
| `CONTAINS_INGREDIENT` | `Product → Ingredient` | ⚠️ Missing quantity, unit, order |
| `ALLERGIC_TO` | `B2BCustomer → Allergen` | ⚠️ Missing severity |
| `HAS_CONDITION` | `B2BCustomer → HealthCondition` | ⚠️ Missing severity |
| `FOLLOWS_DIET` | `B2BCustomer → DietaryPreference` | ⚠️ Missing strictness |
| `HAS_PROFILE` | `B2BCustomer → B2BHealthProfile` | — |
| `BELONGS_TO` | `Product → ProductCategory` | — |

---

## 2. P0 — Mandatory Additions (Safety-Critical)

These MUST be added before any feature can ship. Without these, allergen detection fails.

### 2.1 Relationship: `Ingredient -[:CONTAINS_ALLERGEN]→ Allergen`

**Source Table:** `gold.ingredient_allergens`

**Why Critical:** This is the PRIMARY allergen detection path. Products don't have direct allergen links — detection requires traversing `Product → Ingredient → Allergen`.

```cypher
-- Create from PG data
MATCH (i:Ingredient {id: $ingredient_id})
MATCH (a:Allergen {id: $allergen_id})
MERGE (i)-[r:CONTAINS_ALLERGEN]->(a)
SET r.threshold_ppm = $threshold_ppm,
    r.synced_at = datetime()
```

**Validation:**

```cypher
// Should return > 0 after sync
MATCH (:Ingredient)-[r:CONTAINS_ALLERGEN]->(:Allergen)
RETURN COUNT(r) AS total_ingredient_allergen_links
```

### 2.2 Property: `severity` on `ALLERGIC_TO` relationship

**Source Column:** `gold.b2b_customer_allergens.severity` (values: `mild | moderate | severe | anaphylactic`)

**Why Critical:** Without severity, we cannot distinguish between "mild intolerance" (show as warning) vs "anaphylactic allergy" (hard exclude). PRD-04 and PRD-07 depend on this.

```cypher
-- Update existing relationships during sync
MATCH (c:B2BCustomer {id: $customer_id})-[r:ALLERGIC_TO]->(a:Allergen {id: $allergen_id})
SET r.severity = $severity
```

**Validation:**

```cypher
// All ALLERGIC_TO relationships should have severity
MATCH ()-[r:ALLERGIC_TO]->()
WHERE r.severity IS NULL
RETURN COUNT(r) AS missing_severity_count
// Expected: 0
```

### 2.3 Property: `severity` on `HAS_CONDITION` relationship

**Source Column:** `gold.b2b_customer_health_conditions.severity` (values: `mild | moderate | severe`)

```cypher
MATCH (c:B2BCustomer {id: $customer_id})-[r:HAS_CONDITION]->(hc:HealthCondition {id: $condition_id})
SET r.severity = $severity
```

### 2.4 Properties on `CONTAINS_INGREDIENT`: quantity, unit, is_primary, order

**Source Columns:** `gold.product_ingredients.quantity`, `.unit`, `.is_primary`, `.ingredient_order`

**Why Critical:** Without these, we can't identify primary ingredients for scoring or detect trace amounts for "May Contain" allergen badges (PRD-08).

```cypher
MATCH (p:Product {id: $product_id})-[r:CONTAINS_INGREDIENT]->(i:Ingredient {id: $ingredient_id})
SET r.quantity = $quantity,
    r.unit = $unit,
    r.is_primary = $is_primary,
    r.ingredient_order = $ingredient_order
```

---

## 3. P1 — Required Relationships (Feature-Enabling)

These enable specific features but aren't safety-critical.

### 3.1 Relationship: `Product -[:COMPATIBLE_WITH_DIET]→ DietaryPreference`

**Source Table:** `gold.product_dietary_preferences` (if exists) or computed:

```cypher
-- If junction table exists:
MATCH (p:Product {id: $product_id})
MATCH (dp:DietaryPreference {id: $diet_id})
MERGE (p)-[r:COMPATIBLE_WITH_DIET]->(dp)

-- If no junction table: RAG team computes from ingredients:
// Keto = carbs < 10g AND fat > 15g
// Vegan = no animal-derived ingredients
// Gluten-Free = no gluten-containing ingredients
```

**Used By:** PRD-03 (Search), PRD-08 (Ingredient Intelligence), PRD-09 (Substitution)

### 3.2 Relationship: `HealthCondition -[:RESTRICTS_INGREDIENT]→ Ingredient`

**Source Table:** `gold.health_condition_ingredient_restrictions`

**Why Needed:** Enables the safety engine to detect health-condition-based restrictions (e.g., "Diabetes restricts high-sugar ingredients").

```cypher
MATCH (hc:HealthCondition {id: $condition_id})
MATCH (i:Ingredient {id: $ingredient_id})
MERGE (hc)-[r:RESTRICTS_INGREDIENT]->(i)
SET r.restriction_type = $restriction_type // 'avoid' | 'limit'
```

**Used By:** PRD-02 (Recommendations), PRD-07 (Safety Engine)

### 3.3 Property: `strictness` on `FOLLOWS_DIET` relationship

**Source Column:** `gold.b2b_customer_dietary_preferences.strictness` (if exists)

```cypher
MATCH (c:B2BCustomer)-[r:FOLLOWS_DIET]->(dp:DietaryPreference)
SET r.strictness = $strictness  // 'strict' | 'moderate' | 'flexible'
```

---

## 4. P2 — Optional Enhancements

Not required for launch but improve recommendation quality.

### 4.1 Category Hierarchy: `ProductCategory -[:PARENT_CATEGORY]→ ProductCategory`

Enable "same category" matching at different hierarchy levels.

### 4.2 Interaction Edges: `B2BCustomer -[:PURCHASED]→ Product` / `[:VIEWED]`

**Source Table:** `gold.customer_product_interactions`

Boost products a customer has previously interacted with in recommendation scoring.

---

## 5. Required Uniqueness Constraints & Indexes

Run these **before first sync**:

```cypher
-- Uniqueness constraints (prevent duplicates during MERGE)
CREATE CONSTRAINT b2b_customer_id IF NOT EXISTS
  FOR (c:B2BCustomer) REQUIRE c.id IS UNIQUE;

CREATE CONSTRAINT vendor_id IF NOT EXISTS
  FOR (v:Vendor) REQUIRE v.id IS UNIQUE;

CREATE CONSTRAINT product_id IF NOT EXISTS
  FOR (p:Product) REQUIRE p.id IS UNIQUE;

CREATE CONSTRAINT ingredient_id IF NOT EXISTS
  FOR (i:Ingredient) REQUIRE i.id IS UNIQUE;

CREATE CONSTRAINT allergen_id IF NOT EXISTS
  FOR (a:Allergen) REQUIRE a.id IS UNIQUE;

CREATE CONSTRAINT health_condition_id IF NOT EXISTS
  FOR (hc:HealthCondition) REQUIRE hc.id IS UNIQUE;

CREATE CONSTRAINT dietary_preference_id IF NOT EXISTS
  FOR (dp:DietaryPreference) REQUIRE dp.id IS UNIQUE;

CREATE CONSTRAINT health_profile_id IF NOT EXISTS
  FOR (hp:B2BHealthProfile) REQUIRE hp.id IS UNIQUE;

-- Performance indexes (lookup optimization)
CREATE INDEX b2b_customer_vendor IF NOT EXISTS
  FOR (c:B2BCustomer) ON (c.vendor_id);

CREATE INDEX product_vendor IF NOT EXISTS
  FOR (p:Product) ON (p.vendor_id);

CREATE INDEX product_status IF NOT EXISTS
  FOR (p:Product) ON (p.status);

CREATE INDEX product_category IF NOT EXISTS
  FOR (p:Product) ON (p.category_id);

CREATE INDEX allergen_code IF NOT EXISTS
  FOR (a:Allergen) ON (a.code);

CREATE INDEX health_condition_code IF NOT EXISTS
  FOR (hc:HealthCondition) ON (hc.code);
```

---

## 6. Validation Queries (Post-Sync Verification)

Run these after each sync to verify data integrity:

```cypher
// 1. Verify all B2B customers have vendor links
MATCH (c:B2BCustomer)
WHERE NOT EXISTS { MATCH (c)-[:BELONGS_TO_VENDOR]->(:Vendor) }
RETURN COUNT(c) AS orphaned_customers
// Expected: 0

// 2. Verify all products have vendor links
MATCH (p:Product)
WHERE NOT EXISTS { MATCH (p)-[:SOLD_BY]->(:Vendor) }
RETURN COUNT(p) AS orphaned_products
// Expected: 0

// 3. Verify ALLERGIC_TO has severity
MATCH ()-[r:ALLERGIC_TO]->()
WHERE r.severity IS NULL
RETURN COUNT(r) AS missing_severity
// Expected: 0

// 4. Verify CONTAINS_INGREDIENT has properties
MATCH ()-[r:CONTAINS_INGREDIENT]->()
WHERE r.quantity IS NULL AND r.is_primary IS NULL
RETURN COUNT(r) AS missing_ingredient_props
// Expected: 0 (or close to 0)

// 5. Verify CONTAINS_ALLERGEN exists
MATCH ()-[r:CONTAINS_ALLERGEN]->()
RETURN COUNT(r) AS total_ingredient_allergen_links
// Expected: > 0

// 6. Data summary stats
MATCH (c:B2BCustomer) RETURN 'B2BCustomer' AS label, COUNT(c) AS count
UNION ALL
MATCH (p:Product) RETURN 'Product' AS label, COUNT(p) AS count
UNION ALL
MATCH ()-[r:ALLERGIC_TO]->() RETURN 'ALLERGIC_TO' AS label, COUNT(r) AS count
UNION ALL
MATCH ()-[r:CONTAINS_INGREDIENT]->() RETURN 'CONTAINS_INGREDIENT' AS label, COUNT(r) AS count
UNION ALL
MATCH ()-[r:CONTAINS_ALLERGEN]->() RETURN 'CONTAINS_ALLERGEN' AS label, COUNT(r) AS count
```

---

## 7. Priority Summary

| Priority | Change | Status | Features Blocked |
|----------|--------|--------|-----------------|
| **P0** | `CONTAINS_ALLERGEN` (Ingredient→Allergen) | ❌ MISSING | ALL recommendations & safety features |
| **P0** | `severity` on `ALLERGIC_TO` | ❌ MISSING | PRD-04, PRD-07 (graduated safety) |
| **P0** | `severity` on `HAS_CONDITION` | ❌ MISSING | PRD-02, PRD-07 |
| **P0** | Properties on `CONTAINS_INGREDIENT` | ❌ MISSING | PRD-08, PRD-09 |
| **P1** | `COMPATIBLE_WITH_DIET` | ❌ MISSING | PRD-03, PRD-08, PRD-09 |
| **P1** | `RESTRICTS_INGREDIENT` | ❌ MISSING | PRD-02, PRD-07 |
| **P1** | `strictness` on `FOLLOWS_DIET` | ❌ MISSING | PRD-02 |
| **P2** | `PARENT_CATEGORY` | ❌ MISSING | PRD-09 (better matching) |
| **P2** | Interaction edges | ❌ MISSING | PRD-02 (boost scoring) |

> All P0 changes must be completed before any feature PRD can be tested.
