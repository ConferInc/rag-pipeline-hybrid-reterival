# PRD: Auto Notifications via RAG Pipeline

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema  
> **Auth:** Appwrite (B2C auth)  
> **Repos:** `nutrib2c-frontend` (frontend), `nutrition-backend-b2c` (backend), `rag-pipeline-hybrid-reterival` (RAG API)  
> **Family Context:** All features support household/family member switching via `household_id`.  

---

## Overview

Build a proactive notification engine that generates **personalized**, **context-aware** notifications based on user meal logging behavior, nutrition patterns, and health goals. Notifications are generated via RAG (LLM-personalized content) and triggered by data-driven rules evaluated on user login and scheduled intervals.

**Why this matters:** The current notification system is CRUD-only (get/read/mark). Users receive zero proactive dietary guidance. Auto-notifications drive engagement, help users stay on track with health goals, and differentiate the app from simple calorie trackers.

**Current State:**

- Backend: `notifications.ts` → `getNotifications`, `markAsRead`, `markAllAsRead` — NO `createNotification()`, NO auto-generation
- RAG: 6 existing endpoints (`/search, /feed, /meal-candidates, /recommend/products, /recommend/alternatives, /chat`) — NO notification generation
- Frontend: `notifications/page.tsx` lists notifications with type filters; `useUnreadCount` fetches on mount
- DB: `b2c_notifications` table exists with types: `meal, nutrition, grocery, budget, family, system`
- Timezone: `households.timezone` defaults to `'UTC'` — **no UI to set it, no auto-detection**

## User Stories

| ID | Story | Priority | Trigger Category |
|----|-------|----------|-----------------|
| AN-1 | As a user, when I haven't logged breakfast by 11 AM (local time), I receive a notification: "Good morning! You haven't logged breakfast yet — want to log it now?" with a direct link to meal log | P0 | Missed Meal |
| AN-2 | As a user, when I haven't logged any meals by 3 PM (local time), I receive: "Looks like you've been busy! Tap here to quickly log your lunch" | P0 | Missed Meal |
| AN-3 | As a user, when my average fat intake exceeds my target by 30%+ over 2 days, I receive: "Your fat intake has been trending high — here are some lighter alternatives for today" with action to view low-fat recipes | P0 | Dietary Pattern |
| AN-4 | As a user, when my protein intake is consistently below target for 3+ days, I receive: "You've been getting less protein than your goal — try adding these high-protein options" | P1 | Dietary Pattern |
| AN-5 | As a user, when I haven't logged any water intake today, I receive an afternoon reminder: "Stay hydrated! You haven't tracked water today" | P1 | Hydration |
| AN-6 | As a user, when I maintain a 7-day logging streak, I receive a celebratory notification: "🔥 Amazing! 7-day streak! Keep it going!" | P1 | Engagement |
| AN-7 | As a user, when my logging streak breaks (missed yesterday), I receive: "Your 5-day streak ended yesterday — log today to start a new one!" | P1 | Engagement |
| AN-8 | As a user, when I consistently exceed my calorie goal by 15%+ for 3 days, I receive: "You've been over your calorie goal this week — would you like to adjust your target or explore lighter meals?" | P2 | Dietary Pattern |

## Critical Analysis: Timezone

### Current State (Deep Scan Results)

| Layer | Finding |
|-------|---------|
| **Database** | `households.timezone VARCHAR(64) DEFAULT 'UTC'` — exists, with non-empty constraint |
| **Backend** | `getHouseholdTimezone()` resolves timezone → used by nutrition dashboard + budget routes |
| **Backend** | `normalizeTimeZone()` validates timezone strings using `Intl.DateTimeFormat` |
| **Frontend types** | `BudgetWindow.timezone` and `BudgetTrendsResponse.timezone` exist — but read-only from backend |
| **Frontend UI** | **NO timezone selector anywhere** — not in settings, not in profile, not in onboarding |
| **Frontend detection** | **NO `Intl.DateTimeFormat().resolvedOptions().timeZone`** call anywhere in frontend |
| **Auth/Registration** | Auth handled by Appwrite externally — no registration pages in frontend `app/` directory |

### Recommended Approach

**Strategy: Auto-detect + Manual override**

1. **Auto-detect on first authenticated request** (backend middleware):
   - Frontend sends `X-Timezone` header (from `Intl.DateTimeFormat().resolvedOptions().timeZone`) on every API call
   - Backend middleware: if `households.timezone = 'UTC'` AND `X-Timezone` header is present AND it's a valid IANA timezone → auto-update the household's timezone
   - This handles the "registration" case transparently — no Appwrite modification needed

2. **Manual override in Settings page** (Settings > General tab):
   - Add a "Timezone" dropdown to the existing General tab
   - Populated with common IANA timezones (or a searchable list)
   - Calls `PUT /api/v1/household/timezone` to update

3. **Production-ready enhancement** (post-MVP):
   - Add timezone to the profile onboarding health wizard flow
   - Show a confirmation toast on first auto-detection: "We detected your timezone as America/New_York — change it in Settings"

## Technical Architecture

### Backend

#### [NEW] DB Migration: `b2c_notification_dispatch_log`

```sql
CREATE TABLE gold.b2c_notification_dispatch_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    b2c_customer_id uuid NOT NULL,
    trigger_type varchar(50) NOT NULL,
    trigger_date date NOT NULL,
    dispatched_at timestamptz DEFAULT now(),
    notification_id uuid REFERENCES gold.b2c_notifications(id),
    UNIQUE (b2c_customer_id, trigger_type, trigger_date)
);
```

Prevents duplicate notifications per trigger per day.

#### [NEW] `server/services/notificationEngine.ts`

Core engine that evaluates triggers and dispatches notifications:

```typescript
export async function evaluateAndDispatchNotifications(
  customerId?: string  // optional: if provided, evaluate only this customer
): Promise<{ evaluated: number; dispatched: number }>
```

**Trigger implementations:**

| Trigger | Logic | Time Gate |
|---------|-------|-----------|
| `missed_breakfast` | No `meal_log_items` with `meal_type='breakfast'` for today | After 11 AM local |
| `missed_lunch` | No items today + no breakfast + it's afternoon | After 3 PM local |
| `high_fat_2day` | Avg `total_fat_g` last 2 days > `target_fat_g × 1.3` | Once daily |
| `low_protein_3day` | Avg `protein_g` last 3 days < `target_protein_g × 0.7` | Once daily |
| `no_water` | Today's `water_ml = 0` | After 2 PM local |
| `streak_milestone` | `current_streak` in `meal_log_streaks` hits 7, 14, 30, 60, 100 | On login |
| `streak_broken` | `last_logged_date` is before yesterday | On login |
| `calorie_overshoot_3day` | Avg calories last 3 days > `calorie_goal × 1.15` | Once daily |

#### [MODIFY] `server/services/notifications.ts`

Add `createNotification()`:

```typescript
export async function createNotification(input: {
  customerId: string;
  type: 'meal' | 'nutrition' | 'grocery' | 'budget' | 'family' | 'system';
  title: string;
  body: string;
  icon?: string;
  actionUrl?: string;
}): Promise<Notification>
```

#### [MODIFY] `server/services/ragClient.ts`

Add `ragGenerateNotification()`:

```typescript
export async function ragGenerateNotification(params: {
  customer_id: string;
  trigger_type: string;
  meal_log_summary: Record<string, unknown>;
  health_profile: Record<string, unknown>;
  timezone: string;
}): Promise<{ title: string; body: string; action_url: string; icon: string; type: string } | null>
```

Uses existing circuit breaker pattern with `USE_GRAPH_NOTIFICATION` feature flag.

#### [NEW] `server/routes/notificationEngine.ts`

```
POST /api/v1/notifications/evaluate  (called on login + by cron)
```

Evaluates triggers for the authenticated user and dispatches any new notifications.

#### [MODIFY] Backend auth middleware

Add `X-Timezone` header reading logic:

```typescript
// In auth middleware, after user is authenticated:
const clientTz = req.headers['x-timezone'] as string;
if (clientTz && isValidTimezone(clientTz)) {
  // Auto-update household timezone if still default 'UTC'
  await maybeUpdateHouseholdTimezone(actorMemberId, clientTz);
}
```

### RAG API

#### [NEW] `POST /notifications/generate` endpoint

```python
class NotificationGenerateRequest(BaseModel):
    customer_id: str
    trigger_type: str
    meal_log_summary: dict
    health_profile: dict
    timezone: str

@app.post("/notifications/generate")
async def generate_notification(req: NotificationGenerateRequest):
    # Uses LLM to craft personalized notification
    # Returns: { title, body, action_url, icon, type }
```

### Frontend Changes

| File | Change |
|------|--------|
| `lib/api.ts` or API utility | Add `X-Timezone` header to all API calls: `Intl.DateTimeFormat().resolvedOptions().timeZone` |
| `app/settings/page.tsx` | Add timezone dropdown to General tab |
| `app/notifications/page.tsx` | Enhanced notification cards with action buttons (e.g., "Log Breakfast Now") |
| `hooks/use-notifications.ts` | Add `refetchInterval: 60000` to `useUnreadCount` for polling |
| Login/app init | Call `POST /notifications/evaluate` on app mount/login |

### Scheduling Strategy

- **On login/app mount:** Frontend calls `POST /notifications/evaluate` → evaluates triggers for current user only (fast, <500ms)
- **Cron (4x daily):** Backend `node-cron` job at 11 AM, 2 PM, 6 PM, 9 PM UTC → batch evaluates all active users with timezone adjustment
- **Dedup:** `dispatch_log` UNIQUE constraint prevents double-sends per trigger per day

### Production-Ready Enhancements (Post-MVP)

| Enhancement | Description |
|-------------|-------------|
| **Push notifications** | FCM/APNS integration for background alerts |
| **Email digest** | Weekly summary email with nutrition insights |
| **ML send-time optimization** | Track notification open rates, learn optimal send times per user |
| **Notification preferences** | Per-trigger type enable/disable in Settings > Alerts tab |
| **Rate limiting** | Max 3 auto-notifications per day per user |

## Acceptance Criteria

- [ ] Missed breakfast notification fires after 11 AM local time when no breakfast logged
- [ ] High-fat pattern alert fires when 2-day average exceeds target by 30%+
- [ ] Notifications are NOT duplicated (same trigger + same day = skip)
- [ ] Timezone auto-detected from browser on first API call; updatable in Settings
- [ ] Notification cards show action buttons (e.g., "Log Breakfast" → navigates to meal log)
- [ ] `POST /notifications/evaluate` on login returns within 500ms
- [ ] RAG fallback: if RAG is down, use static notification templates
- [ ] Streak milestone notifications fire at 7, 14, 30, 60, 100 day marks

---

## RAG Team Scope

> **Repo:** `rag-pipeline-hybrid-reterival`  
> **Owner:** RAG Pipeline Engineer

### Deliverables

1. `POST /notifications/generate` endpoint with LLM-powered notification content
2. System prompt that generates engaging, personalized notification copy
3. Fallback static templates when LLM is slow (>2s) or unavailable

## Route Registration

```typescript
// server/routes/notificationEngine.ts
router.post("/evaluate", requireAuth, async (req, res) => { ... });

// Register in server/routes.ts
import { notificationEngineRouter } from "./routes/notificationEngine.js";
app.use("/api/v1/notifications", notificationEngineRouter);
```

## Environment Variables

```env
USE_GRAPH_NOTIFICATION=false  # Set to 'true' to enable RAG-powered notification content
NOTIFICATION_CRON_ENABLED=false  # Set to 'true' to enable background cron dispatch
```

## Verification Plan

### Automated Tests

- Unit test each trigger function (`checkMissedMealTrigger`, `checkHighFatTrigger`, etc.)
- Unit test dedup: verify same trigger doesn't fire twice on same day
- Unit test timezone auto-detection middleware
- Integration test: `POST /notifications/generate` on RAG pipeline

### Manual Verification

- Seed test user with no breakfast log → login after 11 AM local → verify notification appears
- Seed test user with high-fat meals (2 days) → verify pattern alert
- Verify Settings > General shows timezone dropdown with correct auto-detected value
- Verify notification action buttons navigate to correct pages
