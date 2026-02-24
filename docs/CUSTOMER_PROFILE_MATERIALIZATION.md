# Customer Profile Materialization (Aggregated Text + Semantic Embedding)

## Goal
Because most customer-relevant text lives on neighbor nodes (allergens, dietary preferences, health profiles, disliked ingredients, conditions), create a single canonical customer text field that represents the full customer profile, embed it, and index it for reliable semantic retrieval.

## Approach (no implementation)

### 1) Add a dedicated “customer profile materialization” stage
Insert a stage **before semantic embedding generation** (or as part of it) that produces one canonical text field on each `B2C_Customer`.

- **Output property** (example): `customerProfileText`
- Run this stage:
  - after KG ingestion/relationship creation
  - before computing `semanticEmbedding` for customers

### 2) Drive it from config
Extend/organize config so it defines:

- **Traversal rules (customer neighborhood)**:
  - seed label: `B2C_Customer`
  - allowed relationships (profile-defining): e.g., `IS_ALLERGIC`, `FOLLOWS_DIET`, `HAS_PROFILE`, `HAS_CONDITION`
  - target labels: `Allergens`, `Dietary_Preferences`, `B2C_Customer_Health_Profiles`, `B2C_Customer_Health_Conditions`, `Ingredient` (for dislikes), etc.
  - hop depth: start with 1 hop

- **Text extraction rules per target label**:
  - which properties to collect (e.g., Allergens: `category`, `code`; Diet prefs: `name`; Health profile: disliked ingredients/goals, etc.)

- **Template format**:
  - section headers + separators
  - deterministic ordering requirements

### 3) Materialize deterministically
For each customer:
- collect values from connected nodes
- sort lists deterministically
- deduplicate values
- assemble one structured text block
- write it to `B2C_Customer.customerProfileText`

### 4) Embed and store on customer
Update the semantic embedding stage so that for `B2C_Customer`:
- the “text to embed” is `customerProfileText`
- write embedding to your configured property (e.g., `semanticEmbedding`)
- maintain the Neo4j vector index for `:B2C_Customer(semanticEmbedding)`

### 5) Recompute strategy
Pick one:
- **Batch rebuild**: recompute all customer profiles on-demand or on a schedule
- **Incremental**: recompute for a customer when profile edges/nodes change

### 6) Retrieval benefit
Semantic retrieval can now match customer queries (allergy/diet/dislikes/conditions) reliably because the customer embedding represents the full profile rather than only name/email.

### 7) GraphRAG-ready
Later you can still traverse for richer grounding, but the customer node becomes a strong, self-contained semantic entry point for customer intent queries.

