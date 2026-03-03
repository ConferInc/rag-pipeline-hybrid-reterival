# PRD 14: Graph-Enhanced Scanner Alternatives

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema  
> **Auth:** Appwrite (B2C auth)  
> **Repos:** `nutrib2c-frontend` (frontend), `nutrition-backend-b2c` (backend), `rag-pipeline-hybrid-reterival` (RAG API)  
> **Family Context:** All features support household/family member switching. The `households` table is the root entity; each member is a `b2c_customers` row linked via `household_id`.  
> **Depends On:** PRD-09 (Foundation & Resilience)

---

## 14.1 Overview

After a user scans a product barcode, show graph-powered alternative product suggestions. If the scanned product contains allergens relevant to the user's profile (or a family member's), the graph finds safer alternatives via `[:CAN_SUBSTITUTE]` edges. Even for allergen-safe products, alternatives may include cheaper, healthier, or more nutritious options.

**Why this matters:** The current `lookupProductByBarcode()` in `scan.ts` (566 lines) handles product lookup, caching from OpenFoodFacts, allergen warnings, and health warnings — but it stops there. After showing "⚠ Contains gluten (allergen for Sarah)", there's no follow-up suggestion like "Try Bob's GF Crackers instead — $1.20 cheaper and gluten-free." The graph enables this via product substitution relationships.

**Current State:**

- Backend: `server/services/scan.ts` — `lookupProductByBarcode()`, `generateAllergenWarnings()`, `generateHealthWarnings()`, `saveScanHistory()`, `getScanHistory()`. Fully functional.
- Frontend: Scan result sheet (`components/scan/scan-result-sheet.tsx`) shows product info, allergen warnings, nutrition facts. No alternatives section.

**SQL Fallback:** If `USE_GRAPH_SCANNER=false` or RAG API is down → scan result shows normally without the alternatives section. The user gets all existing warnings and nutrition info; they just don't see substitution suggestions.

## 14.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| SA-1 | As a user, after scanning a product with my allergen, I see 3 safer alternatives | P0 |
| SA-2 | As a user, each alternative shows why it's recommended ("Gluten-free", "Save $1.20") | P0 |
| SA-3 | As a user, alternatives are relevant to the same product category | P1 |
| SA-4 | As a user, I can tap an alternative to view its full product details | P1 |
| SA-5 | As a user, scan results still work normally if alternatives aren't available | P0 |

## 14.3 Technical Architecture

### 14.3.1 Backend API

#### [MODIFY] `server/services/scan.ts`

Add alternatives after the existing lookup flow:

```typescript
import { ragAlternatives } from "./ragClient.js";

export async function lookupProductWithAlternatives(
  barcode: string,
  b2cCustomerId: string,
  memberId?: string
): Promise<ScanLookupResult & { alternatives: ProductAlternative[] }> {
  // Step 1: Existing lookup (unchanged)
  const scanResult = await lookupProductByBarcode(barcode, b2cCustomerId, memberId);

  // Step 2: If product found, try graph alternatives
  let alternatives: ProductAlternative[] = [];
  if (scanResult.product && scanResult.source !== "not_found") {
    const memberAllergens = await getMemberAllergenIds(memberId || b2cCustomerId);
    const graphAlts = await ragAlternatives(scanResult.product.id, memberAllergens);

    if (graphAlts) {
      alternatives = graphAlts.alternatives.map((alt: any) => ({
        productId: alt.product_id,
        name: alt.name,
        brand: alt.brand,
        price: alt.price,
        imageUrl: alt.image_url,
        reason: alt.reason,
        savings: alt.savings,
        allergenSafe: alt.allergen_safe,
      }));
    }
  }

  return { ...scanResult, alternatives };
}
```

#### [MODIFY] `server/routes/scan.ts`

```typescript
router.post("/lookup", requireAuth, async (req, res) => {
  const { barcode, memberId } = req.body;
  const result = await lookupProductWithAlternatives(barcode, req.user.id, memberId);
  res.json(result);
});
```

### 14.3.2 RAG API Endpoint

`POST /recommend/alternatives`:

```json
// Request
{
  "product_id": "uuid",
  "customer_allergens": ["uuid-gluten", "uuid-dairy"]
}

// Response
{
  "alternatives": [
    {
      "product_id": "alt-uuid",
      "name": "Bob's Red Mill GF Crackers",
      "brand": "Bob's Red Mill",
      "price": 4.49,
      "image_url": "...",
      "reason": "Gluten-free alternative",
      "savings": 1.20,
      "allergen_safe": true,
      "category": "Crackers"
    }
  ]
}
```

### 14.3.3 Database Tables Used (Already Exist)

| Table | Usage |
|-------|-------|
| `gold.products` | Product lookup and cache |
| `gold.scan_history` | Save scan events |
| `gold.product_allergens` | Allergen detection |
| `gold.b2c_customer_allergens` | User/member allergen profile |

### 14.3.4 Frontend Changes

| File | Change |
|------|--------|
| `components/scan/scan-result-sheet.tsx` | Add "Alternatives" section below warnings |
| **[NEW]** `components/scan/product-alternative-card.tsx` | Card for each alternative product |
| `lib/scan-api.ts` | Update response type to include `alternatives[]` |

**Alternatives Section in Scan Result:**

```tsx
{result.alternatives.length > 0 && (
  <div className="mt-4">
    <h3 className="text-sm font-semibold mb-2">
      {result.allergenWarnings.length > 0
        ? "🛡️ Safer Alternatives"
        : "💡 You Might Also Like"}
    </h3>
    <div className="space-y-2">
      {result.alternatives.map(alt => (
        <ProductAlternativeCard key={alt.productId} alternative={alt} />
      ))}
    </div>
  </div>
)}
```

## 14.4 Acceptance Criteria

- [ ] After scanning an allergen-containing product, 1–3 alternatives appear
- [ ] Each alternative shows reason and optional savings
- [ ] Alternatives are allergen-safe for the selected member
- [ ] Scan result displays normally when alternatives are unavailable (RAG down)
- [ ] No alternatives section shown when product is not found
- [ ] Response time for scan + alternatives < 60s (testing timeout)

---

## 14.RAG — RAG Team Scope

> **Repo:** `rag-pipeline-hybrid-reterival`  
> **Owner:** RAG Pipeline Engineer  
> **The B2C team does NOT touch these files.**

### Deliverables

#### 1. `POST /recommend/alternatives` Endpoint

Find allergen-safe product alternatives for a scanned product:

**Request:**

```json
{
  "product_id": "uuid",
  "customer_allergens": ["uuid-gluten", "uuid-dairy"]
}
```

**Response:**

```json
{
  "alternatives": [
    {
      "product_id": "alt-uuid",
      "name": "Bob's Red Mill GF Crackers",
      "brand": "Bob's Red Mill",
      "price": 4.49,
      "image_url": "...",
      "reason": "Gluten-free alternative",
      "savings": 1.20,
      "allergen_safe": true,
      "category": "Crackers"
    }
  ]
}
```

#### 2. Cypher for Product Alternatives

Traverse `[:CAN_SUBSTITUTE]` edges, filtering out products with customer allergens. Sort by price savings descending. Limit to 3 results.

## 14.5 Route Registration

No new routes — modifies existing scan lookup endpoint.

## 14.6 Environment Variables

```env
USE_GRAPH_SCANNER=false  # Set to 'true' to enable scanner alternatives
```
