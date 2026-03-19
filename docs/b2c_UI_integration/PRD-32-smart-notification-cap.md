# PRD 32: Smart Notification Cap

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), PostgreSQL `gold` schema, LiteLLM proxy → OpenAI models  
> **Auth:** Appwrite (B2C auth)  
> **Repos:** `nutrib2c-frontend` (frontend), `nutrition-backend-b2c` (backend), `rag-pipeline-hybrid-reterival` (RAG API)  
> **Family Context:** All features support household/family member switching. The `households` table is the root entity; each member is a `b2c_customers` row linked via `household_id`.  
> **Depends On:** PRD-29 (Auto-Notifications), PRD-31 (Background Push Notifications)

---

## 32.1 Overview

Implement a configurable daily notification cap to limit the number of notifications dispatched per user per day. Currently, the notification engine has 10 trigger types and runs 4 times daily via cron, with no global daily limit — a single user could potentially receive up to 10 notifications/day across multiple cron runs.

**Vijay Sir's Directive:**
> _"We generally as a user don't like to be notified frequently... let's limit to two notifications."_ — (4:18–4:57)

**Current State:**

- `notificationEngine.ts` has 10 triggers with per-type dedup (won't fire same type twice in 8h) but **no global daily cap**
- `scheduler.ts` runs the notification cron **4 times daily** at 11:00, 14:00, 18:00, 21:00 UTC
- No environment variable exists for configuring notification limits
- No priority ordering between triggers — all eligible triggers fire

**Target State:**

- Configurable cap via `MAX_DAILY_NOTIFICATIONS` environment variable (default: `2`)
- Priority-ordered trigger evaluation so the most important notifications fire first
- Daily dispatch count tracked per user — cron skips users who've reached their cap
- Scheduler adjusted from 4x to 2x daily runs

## 32.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| NC-1 | As a user, I receive at most 2 notifications per day (1 meal reminder + 1 nutritional gap alert) | P0 |
| NC-2 | As a user, the most relevant notifications are prioritized when the cap would be exceeded | P0 |
| NC-3 | As a user, notification types I've already received today are not repeated | P0 |
| NC-4 | As a product owner, I can change the daily cap via environment variable without code changes | P0 |
| NC-5 | As a product owner, the cap is enforced per user — different users can have different delivery patterns | P1 |

## 32.3 Technical Architecture

### 32.3.1 Notification Priority Model

The existing 10 triggers must be evaluated in priority order. When `MAX_DAILY_NOTIFICATIONS=2`, typically only the top 2 eligible triggers will fire:

| Priority | Trigger Type(s) | Category | Why This Priority |
|----------|-----------------|----------|-------------------|
| 1 (highest) | `missed_breakfast`, `missed_lunch`, `missed_dinner` | 🍽️ Meal logging reminder | Direct user action prompt — highest engagement |
| 2 | `suggest_breakfast`, `suggest_lunch`, `suggest_dinner` | 🍽️ Meal suggestion | Proactive value — "here's what to eat" |
| 3 | `low_protein_3day`, `high_fat_2day`, `calorie_overshoot_3day` | 📊 Nutritional gap alert | Health insight — educational value |
| 4 (lowest) | `no_water`, `streak_milestone`, `streak_broken` | 💧 Engagement / Gamification | Nice-to-have, lowest impact if skipped |

With a default cap of 2, the typical daily pattern is:
- **Notification 1:** Meal logging reminder (missed a meal) — fired during morning/afternoon cron
- **Notification 2:** Nutritional gap alert (low protein / high fat trend) — fired during evening cron

### 32.3.2 Schema Changes

No new tables required. The existing `gold.b2c_notification_dispatch_log` table (from PRD-29) already tracks dispatched notifications with timestamps. The daily count query filters on `dispatched_at >= start_of_today`.

### 32.3.3 Backend Changes

#### [MODIFY] `server/services/notificationEngine.ts`

**Change 1: Add configurable cap constant**

```typescript
// At top of file
const MAX_DAILY_NOTIFICATIONS = parseInt(
  process.env.MAX_DAILY_NOTIFICATIONS ?? "2",
  10
);
```

**Change 2: Add daily dispatch count query**

```typescript
async function getTodayDispatchCount(customerId: string): Promise<number> {
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  const rows = await executeRaw(
    `SELECT COUNT(*) AS cnt 
     FROM gold.b2c_notification_dispatch_log 
     WHERE b2c_customer_id = $1 
       AND dispatched_at >= $2`,
    [customerId, today.toISOString()]
  );
  return parseInt((rows[0] as any)?.cnt ?? "0", 10);
}
```

**Change 3: Modify `evaluateAndDispatchNotifications()` to enforce cap**

```typescript
export async function evaluateAndDispatchNotifications(
  customerId: string,
  memberId: string | null
): Promise<DispatchResult[]> {
  // 1. Check daily cap
  const todayCount = await getTodayDispatchCount(customerId);
  if (todayCount >= MAX_DAILY_NOTIFICATIONS) {
    return []; // User already at cap — skip entirely
  }

  const remaining = MAX_DAILY_NOTIFICATIONS - todayCount;

  // 2. Evaluate ALL triggers, collect eligible ones
  const eligible: EligibleTrigger[] = [];
  for (const trigger of ALL_TRIGGERS) {
    const result = await evaluateTrigger(trigger, customerId, memberId);
    if (result.shouldFire) {
      eligible.push({ trigger, result });
    }
  }

  // 3. Sort by priority (lower number = higher priority)
  eligible.sort((a, b) => TRIGGER_PRIORITY[a.trigger.type] - TRIGGER_PRIORITY[b.trigger.type]);

  // 4. Dispatch only top N (remaining before cap)
  const toDispatch = eligible.slice(0, remaining);
  const results: DispatchResult[] = [];

  for (const { trigger, result } of toDispatch) {
    const dispatched = await dispatchNotification(customerId, trigger, result);
    results.push(dispatched);
  }

  return results;
}
```

**Change 4: Define priority map**

```typescript
const TRIGGER_PRIORITY: Record<string, number> = {
  missed_breakfast: 1,
  missed_lunch: 1,
  missed_dinner: 1,
  suggest_breakfast: 2,
  suggest_lunch: 2,
  suggest_dinner: 2,
  low_protein_3day: 3,
  high_fat_2day: 3,
  calorie_overshoot_3day: 3,
  no_water: 4,
  streak_milestone: 4,
  streak_broken: 4,
};
```

#### [MODIFY] `server/scheduler.ts`

Reduce cron from 4x daily to 2x daily (morning + evening):

```diff
- cron.schedule("0 11,14,18,21 * * *", async () => {
+ cron.schedule("0 11,19 * * *", async () => {
    // Morning run (11 UTC): meal reminders for missed breakfast/lunch
    // Evening run (19 UTC): nutritional gap alerts + dinner reminders
```

### 32.3.4 Frontend Changes

No frontend changes required. Notifications are pushed via the existing push notification system. The cap is entirely backend-enforced.

## 32.RAG — RAG Team Scope

> **Repo:** `rag-pipeline-hybrid-reterival`  
> **Owner:** RAG Pipeline Engineer  
> **The B2C team does NOT touch these files.**

### Deliverables

#### 1. No core RAG changes needed

The notification cap is enforced in the B2C backend **before** any RAG call is made. The existing `ragNotification()` call in `ragClient.ts` remains unchanged — it simply won't be called once the daily cap is reached.

#### 2. Priority-aware content generation (optional enhancement)

If RAG is used for notification content personalization, the RAG team can optionally:

- Accept a `priority` field in the notification content request
- For P1 notifications (meal reminders): generate highly actionable, concise content
- For P3 notifications (nutritional gaps): generate insight-rich, educational content

**Request schema extension (optional):**

```json
{
  "customer_id": "uuid",
  "trigger_type": "low_protein_3day",
  "priority": 3,
  "context": {
    "protein_deficit_g": 25,
    "days_below_target": 3
  }
}
```

This is a **P2 enhancement** and not required for the cap to work.

## 32.4 Acceptance Criteria

- [ ] `MAX_DAILY_NOTIFICATIONS` env var is respected (default: 2)
- [ ] A user with 2 notifications already sent today does not receive more
- [ ] Notifications are dispatched in priority order (meal reminders before engagement)
- [ ] Changing `MAX_DAILY_NOTIFICATIONS=5` allows 5 notifications/day
- [ ] Setting `MAX_DAILY_NOTIFICATIONS=0` disables all automated notifications
- [ ] Cron runs 2x daily (11:00 UTC and 19:00 UTC)
- [ ] Existing per-type dedup (8h cooldown) remains functional
- [ ] Daily count resets at midnight UTC
- [ ] No frontend changes needed — cap is backend-only

## 32.5 Environment Variables

```env
MAX_DAILY_NOTIFICATIONS=2  # Maximum notifications per user per day. Default: 2
```

## 32.6 Files Summary

| File | Action | Lines (est.) |
|------|--------|-------------|
| `server/services/notificationEngine.ts` | MODIFY | +60 (cap logic, priority map, count query) |
| `server/scheduler.ts` | MODIFY | +5 (cron schedule change) |
| `.env.example` | MODIFY | +1 (add MAX_DAILY_NOTIFICATIONS) |
