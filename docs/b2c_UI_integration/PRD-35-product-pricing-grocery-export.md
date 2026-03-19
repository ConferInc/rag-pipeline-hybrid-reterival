# PRD 35: Product Pricing & Grocery List Export

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), PostgreSQL `gold` schema, Kroger Product API, Walmart Affiliate API, LiteLLM proxy → OpenAI models  
> **Auth:** Appwrite (B2C auth)  
> **Repos:** `nutrib2c-frontend` (frontend), `nutrition-backend-b2c` (backend), `rag-pipeline-hybrid-reterival` (RAG API)  
> **Family Context:** All features support household/family member switching. The `households` table is the root entity; each member is a `b2c_customers` row linked via `household_id`.  
> **Depends On:** PRD-05 (Smart Grocery List), PRD-13 (Graph Grocery Substitutions), PRD-33 (Contextual Recommendations — provides location/zip)  
> **References:** [price_fallback_strategies.md](../price_fallback_strategies.md)

---

## 35.1 Overview

Integrate real-time product pricing from retail APIs (Kroger, Walmart) into the grocery list and add export/share capabilities. Currently, the grocery list shows estimated prices from the database only — there is no external pricing or export functionality.

**Vijay Sir's Directive:**
> _"Both Amazon, Walmart have product APIs where you can create a profile and look up data."_ — (17:37)
> _"Don't need exact prices — MSRP/base price is sufficient."_ — (17:37–20:21)
> _"Use UPC or GTIN as the lookup key — they're unique per product."_ — (17:37–20:21)

**Current State:**

- `groceryList.ts` — product prices fetched from `gold.products` table only. No external API pricing
- `products` table has `barcode` and `gtin_type` columns — ready for UPC/GTIN lookup
- `shoppingListItems` has `estimatedPrice` but no `priceSource`, `priceConfidence`, or `externalProductUrl`
- No export/share/download functionality exists for grocery lists
- Pricing strategy documented in `docs/price_fallback_strategies.md` — validated 3-layer stack

**Target State:**

- 3-layer pricing: Kroger API (70% hit) → Walmart API (20%) → LLM estimation (10%)
- Location-aware pricing via user's preferred store (from PRD-33/36)
- Price cache with 24h TTL to minimize API calls
- Export grocery list as CSV, plain text, or via mobile share
- Product deep-links to retailer websites

## 35.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| PP-1 | As a user, I see estimated real-time prices for products in my grocery list | P0 |
| PP-2 | As a user, I see a price source badge ("Kroger price" / "Walmart price" / "Estimated") on each item | P1 |
| PP-3 | As a user, I can click a product name to open it on the retailer's website | P1 |
| PP-4 | As a user, I can download my grocery list as a CSV file | P0 |
| PP-5 | As a user, I can copy my grocery list as plain text | P0 |
| PP-6 | As a user, I can share my grocery list via my phone's native share menu | P0 |
| PP-7 | As a user, the estimated total considers live prices, not just DB prices | P0 |
| PP-8 | As a product owner, prices are cached for 24h to minimize API costs | P0 |

## 35.3 Technical Architecture

### 35.3.1 Pricing Strategy — 3-Layer Fallback

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  1. KROGER API   │────▶│  2. WALMART API   │────▶│ 3. LLM ESTIMATE │
│  (Name search)   │     │ (UPC/GTIN lookup) │     │  (GPT-mini)     │
│  Free, 70% hit   │     │ Free, ~20% of     │     │ ~$0.001/product │
│  Store-specific   │     │   remaining       │     │ ±20-30% accuracy│
│  pricing w/ ID   │     │ National pricing   │     │ All products    │
└─────────────────┘     └──────────────────┘     └─────────────────┘
         ↓                       ↓                        ↓
         └───────────────────────┴────────────────────────┘
                                 ↓
                    ┌──────────────────────┐
                    │  product_price_cache  │
                    │  TTL: 24 hours        │
                    │  Keyed: product+source │
                    └──────────────────────┘
```

### 35.3.2 Schema Changes

#### [MODIFY] `shared/goldSchema.ts` — Add fields to `shoppingListItems`

```diff
  export const shoppingListItems = gold.table("shopping_list_items", {
    ...
    estimatedPrice: numeric("estimated_price", { precision: 10, scale: 2 }),
+   priceSource: varchar("price_source", { length: 30 }),
+   priceConfidence: varchar("price_confidence", { length: 10 }),
+   priceFetchedAt: timestamp("price_fetched_at"),
+   externalProductUrl: varchar("external_product_url", { length: 1000 }),
    ...
  });
```

#### [NEW] `gold.product_price_cache` table

```sql
CREATE TABLE gold.product_price_cache (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id UUID REFERENCES gold.products(id) ON DELETE CASCADE,
  product_name VARCHAR(500),
  barcode VARCHAR(50),
  price NUMERIC(10,2) NOT NULL,
  currency VARCHAR(3) DEFAULT 'USD',
  source VARCHAR(30) NOT NULL,             -- 'kroger' | 'walmart' | 'llm_estimate'
  store_id VARCHAR(100),                   -- Retailer store ID (for Kroger location pricing)
  product_url VARCHAR(1000),               -- Deep-link to product on retailer site
  confidence VARCHAR(10) DEFAULT 'high',   -- 'high' (real API) | 'medium' (different store) | 'low' (LLM estimate)
  fetched_at TIMESTAMP DEFAULT NOW(),
  expires_at TIMESTAMP DEFAULT NOW() + INTERVAL '24 hours',
  created_at TIMESTAMP DEFAULT NOW(),
  UNIQUE(product_id, source, store_id)
);

CREATE INDEX idx_price_cache_product ON gold.product_price_cache(product_id);
CREATE INDEX idx_price_cache_barcode ON gold.product_price_cache(barcode);
CREATE INDEX idx_price_cache_expires ON gold.product_price_cache(expires_at);

-- Cleanup cron: delete expired entries daily
-- (handled by scheduler.ts)
```

### 35.3.3 Backend Changes

#### [NEW] `server/services/pricingService.ts`

Core service implementing 3-layer fallback with caching:

```typescript
interface PricingResult {
  price: number;
  currency: string;
  source: "kroger" | "walmart" | "llm_estimate" | "db";
  confidence: "high" | "medium" | "low";
  productUrl: string | null;
  cached: boolean;
}

/**
 * Look up price for a product using 3-layer fallback.
 * 1. Check gold.product_price_cache (within TTL)
 * 2. Kroger API (name search, with storeId if available)
 * 3. Walmart API (UPC/GTIN lookup)
 * 4. LLM estimation (GPT-mini)
 * 5. Fall back to DB price
 */
export async function lookupPrice(
  productId: string,
  productName: string,
  barcode: string | null,
  storeId: string | null
): Promise<PricingResult> {
  // Step 1: Cache check
  const cached = await getCachedPrice(productId, storeId);
  if (cached) return { ...cached, cached: true };

  // Step 2: Kroger API
  try {
    const krogerResult = await krogerProductSearch(productName, storeId);
    if (krogerResult) {
      await cachePrice(productId, productName, barcode, krogerResult);
      return { ...krogerResult, cached: false };
    }
  } catch (err) {
    console.warn("[Pricing] Kroger API failed:", err);
  }

  // Step 3: Walmart API (UPC lookup)
  if (barcode) {
    try {
      const walmartResult = await walmartUpcLookup(barcode);
      if (walmartResult) {
        await cachePrice(productId, productName, barcode, walmartResult);
        return { ...walmartResult, cached: false };
      }
    } catch (err) {
      console.warn("[Pricing] Walmart API failed:", err);
    }
  }

  // Step 4: LLM estimation
  try {
    const llmResult = await estimatePriceWithLLM(productName);
    if (llmResult) {
      await cachePrice(productId, productName, barcode, llmResult);
      return { ...llmResult, cached: false };
    }
  } catch (err) {
    console.warn("[Pricing] LLM estimation failed:", err);
  }

  // Step 5: DB fallback
  const dbPrice = await getDbPrice(productId);
  return {
    price: dbPrice?.price ?? 0,
    currency: "USD",
    source: "db",
    confidence: "medium",
    productUrl: null,
    cached: false,
  };
}

/**
 * Batch price lookup for an entire grocery list
 */
export async function enrichGroceryListPrices(
  items: GroceryItem[],
  storeId: string | null
): Promise<EnrichedGroceryItem[]> {
  return Promise.all(
    items.map(async (item) => {
      if (item.productId) {
        const pricing = await lookupPrice(
          item.productId,
          item.productName ?? item.ingredientName,
          item.barcode ?? null,
          storeId
        );
        return {
          ...item,
          estimatedPrice: pricing.price,
          priceSource: pricing.source,
          priceConfidence: pricing.confidence,
          externalProductUrl: pricing.productUrl,
        };
      }
      return item;
    })
  );
}
```

#### [NEW] `server/services/krogerApi.ts`

```typescript
const KROGER_API_BASE = "https://api.kroger.com/v1";

interface KrogerProduct {
  productId: string;
  description: string;
  price: number;
  promo_price?: number;
  size: string;
  upc: string;
}

/**
 * Search Kroger for a product by name, optionally at a specific store
 */
export async function krogerProductSearch(
  productName: string,
  storeId: string | null
): Promise<PricingResult | null> {
  const params: Record<string, string> = {
    "filter.term": productName,
    "filter.limit": "1",
  };
  if (storeId) {
    params["filter.locationId"] = storeId;
  }

  const response = await fetch(
    `${KROGER_API_BASE}/products?${new URLSearchParams(params)}`,
    {
      headers: {
        Authorization: `Bearer ${await getKrogerAccessToken()}`,
        Accept: "application/json",
      },
    }
  );

  if (!response.ok) return null;
  const data = await response.json();
  const product = data.data?.[0];
  if (!product?.items?.[0]?.price?.regular) return null;

  return {
    price: product.items[0].price.promo ?? product.items[0].price.regular,
    currency: "USD",
    source: "kroger",
    confidence: storeId ? "high" : "medium",
    productUrl: `https://www.kroger.com/p/${product.productId}`,
  };
}
```

#### [NEW] `server/services/walmartApi.ts`

```typescript
const WALMART_API_BASE = "https://developer.api.walmart.com/api-proxy/service/affil/product/v2";

/**
 * Look up a product on Walmart by UPC
 */
export async function walmartUpcLookup(upc: string): Promise<PricingResult | null> {
  const response = await fetch(
    `${WALMART_API_BASE}/items?upc=${upc}`,
    {
      headers: {
        "WM_SEC.ACCESS_TOKEN": process.env.WALMART_API_KEY!,
        "WM_CONSUMER.CHANNEL.TYPE": process.env.WALMART_CHANNEL_TYPE ?? "",
        Accept: "application/json",
      },
    }
  );

  if (!response.ok) return null;
  const data = await response.json();
  const item = data.items?.[0];
  if (!item?.salePrice && !item?.msrp) return null;

  return {
    price: item.salePrice ?? item.msrp,
    currency: "USD",
    source: "walmart",
    confidence: "high",
    productUrl: item.productUrl ?? `https://www.walmart.com/ip/${item.itemId}`,
  };
}
```

#### [MODIFY] `server/services/groceryList.ts` — Enrich with live pricing

In `generateGroceryList()`, after product matching:

```diff
+ import { enrichGroceryListPrices } from "./pricingService.js";

  // After matchProductsWithRAG() or SQL product matching:
+ const household = await getOrCreateHousehold(b2cCustomerId);
+ const storeId = household.preferredStoreId ?? null;
+ const enrichedItems = await enrichGroceryListPrices(matchedItems, storeId);
+
+ // Persist pricing metadata to shopping_list_items
+ for (const item of enrichedItems) {
+   if (item.priceSource) {
+     await db.update(shoppingListItems).set({
+       estimatedPrice: String(item.estimatedPrice),
+       priceSource: item.priceSource,
+       priceConfidence: item.priceConfidence,
+       priceFetchedAt: new Date(),
+       externalProductUrl: item.externalProductUrl,
+     }).where(eq(shoppingListItems.id, item.id));
+   }
+ }
```

#### [NEW] `server/routes/groceryExport.ts` — Export Endpoint

```typescript
import { Router } from "express";
import { authMiddleware } from "../middleware/auth.js";
import { requireB2cCustomerIdFromReq } from "../services/b2cIdentity.js";

const router = Router();

/**
 * GET /api/v1/grocery-lists/:id/export?format=csv|text|json
 */
router.get("/:id/export", authMiddleware, async (req, res, next) => {
  try {
    const customerId = requireB2cCustomerIdFromReq(req);
    const listId = req.params.id;
    const format = (req.query.format as string) ?? "text";

    const { items, summary } = await getGroceryListForExport(customerId, listId);

    switch (format) {
      case "csv": {
        const csv = generateCsv(items);
        res.setHeader("Content-Type", "text/csv");
        res.setHeader("Content-Disposition", `attachment; filename="grocery-list-${listId}.csv"`);
        return res.send(csv);
      }
      case "json": {
        return res.json({ items, summary });
      }
      case "text":
      default: {
        const text = generatePlainText(items, summary);
        res.setHeader("Content-Type", "text/plain");
        return res.send(text);
      }
    }
  } catch (err) {
    next(err);
  }
});

function generateCsv(items: ExportItem[]): string {
  const header = "Category,Item,Quantity,Unit,Estimated Price,Price Source,Buy Link";
  const rows = items.map(
    (i) =>
      `"${i.category}","${i.name}",${i.quantity},"${i.unit}",${
        i.price ? `$${i.price.toFixed(2)}` : ""
      },"${i.priceSource ?? ""}","${i.productUrl ?? ""}"`
  );
  return [header, ...rows].join("\n");
}

function generatePlainText(items: ExportItem[], summary: any): string {
  const grouped = groupByCategory(items);
  let text = `🛒 Grocery List\n`;
  text += `Estimated Total: $${summary.estimatedTotal?.toFixed(2) ?? "N/A"}\n`;
  text += `Items: ${items.length}\n\n`;

  for (const [category, categoryItems] of Object.entries(grouped)) {
    text += `── ${category} ──\n`;
    for (const item of categoryItems as ExportItem[]) {
      const price = item.price ? ` — $${item.price.toFixed(2)}` : "";
      text += `  □ ${item.name} (${item.quantity} ${item.unit})${price}\n`;
    }
    text += "\n";
  }
  return text;
}

export default router;
```

### 35.3.4 Frontend Changes

#### [MODIFY] Grocery List Detail Page — Add Export Buttons & Price Source

| Element | Implementation |
|---------|---------------|
| Price source badge | Small pill next to price: "Kroger" (green) / "Walmart" (blue) / "Est." (gray) |
| Product link icon | 🔗 icon next to product name → opens `externalProductUrl` |
| Export button bar | `[📋 Copy] [📄 CSV] [📱 Share]` — below list header |

**Copy to clipboard:**
```typescript
async function handleCopy() {
  const text = await fetch(`/api/v1/grocery-lists/${listId}/export?format=text`);
  await navigator.clipboard.writeText(await text.text());
  toast.success("Copied to clipboard!");
}
```

**Download CSV:**
```typescript
function handleDownloadCsv() {
  window.open(`/api/v1/grocery-lists/${listId}/export?format=csv`, "_blank");
}
```

**Native share (mobile):**
```typescript
async function handleShare() {
  const text = await fetch(`/api/v1/grocery-lists/${listId}/export?format=text`);
  if (navigator.share) {
    await navigator.share({ title: "My Grocery List", text: await text.text() });
  }
}
```

## 35.RAG — RAG Team Scope

> **Repo:** `rag-pipeline-hybrid-reterival`  
> **Owner:** RAG Pipeline Engineer  
> **The B2C team does NOT touch these files.**

### Deliverables

#### 1. No Core RAG Changes Required

The pricing integration is entirely a B2C backend concern — it happens **after** RAG product matching. The existing `/recommend/products` (ragProducts) endpoint remains unchanged.

#### 2. Optional Enhancement: Return Product Barcodes in RAG Responses

Currently, `ragProducts()` returns `product_id`, `name`, `brand`, and other fields. If available, include `barcode` and `gtin_type` in the response to enable direct UPC lookup without an extra DB query:

```diff
  # In product recommendation response
  {
    "product_id": "uuid",
    "name": "Organic Greek Yogurt",
    "brand": "Chobani",
+   "barcode": "818290014000",
+   "gtin_type": "UPC-A",
    "score": 0.88
  }
```

This is a **P2 optimization** — the B2C backend can also fetch barcodes from the products table.

## 35.4 Acceptance Criteria

- [ ] Products in grocery list show live prices from Kroger/Walmart when available
- [ ] `priceSource` badge shows origin of price (Kroger / Walmart / Estimated / DB)
- [ ] Price cache works — same product shows cached price within 24h
- [ ] Export as CSV downloads a properly formatted CSV file
- [ ] Export as plain text copies categorized list to clipboard
- [ ] Share button triggers native mobile share dialog (or copies on desktop)
- [ ] Product deep-links open the correct product page on retailer's website
- [ ] Estimated total uses live/cached prices over DB prices when available
- [ ] Pricing failure at any tier falls through to next tier gracefully
- [ ] API key absence (Kroger/Walmart not configured) skips that tier without errors
- [ ] Expired cache entries are cleaned up daily

## 35.5 Environment Variables

```env
# Kroger API
KROGER_CLIENT_ID=your_client_id
KROGER_CLIENT_SECRET=your_client_secret

# Walmart Affiliate API
WALMART_API_KEY=your_api_key
WALMART_CHANNEL_TYPE=your_channel_type

# LLM estimation (uses existing LiteLLM proxy)
# No additional env vars needed

# Price cache TTL (optional, defaults to 24 hours)
PRICE_CACHE_TTL_HOURS=24
```

## 35.6 Files Summary

| File | Action | Lines (est.) |
|------|--------|-------------|
| SQL migration (product_price_cache + shoppingListItems columns) | **NEW** | ~30 |
| `shared/goldSchema.ts` | MODIFY | +15 (price_cache table + shoppingListItems columns) |
| **`server/services/pricingService.ts`** | **NEW** | ~150 (3-layer fallback + caching) |
| **`server/services/krogerApi.ts`** | **NEW** | ~80 (Kroger API client + OAuth) |
| **`server/services/walmartApi.ts`** | **NEW** | ~60 (Walmart API client) |
| `server/services/groceryList.ts` | MODIFY | +30 (enrich with live pricing) |
| **`server/routes/groceryExport.ts`** | **NEW** | ~100 (export endpoint: CSV, text, JSON) |
| `server/routes.ts` | MODIFY | +2 (register export route) |
| `app/(main)/grocery/list/page.tsx` | MODIFY | +50 (export buttons, price badges, links) |
| `.env.example` | MODIFY | +6 (API keys) |
