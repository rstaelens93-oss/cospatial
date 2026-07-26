import {
  pgTable,
  text,
  serial,
  integer,
  boolean,
  timestamp,
  pgEnum,
} from "drizzle-orm/pg-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";

// ─── Enum ──────────────────────────────────────────────────────────────────────
export const pricingTierEnum = pgEnum("pricing_tier", [
  "solo_trial",
  "solo_paid",
  "team_paid",
  "enterprise_paid",
]);

// ─── Table ─────────────────────────────────────────────────────────────────────
// One row per tier. Paid tiers carry both monthly and annual prices.
// annual_price_cents is always exactly 10× monthly_price_cents
// (pay 10 months, get 12 — universal 2-months-free discount).
export const pricingPlansTable = pgTable("pricing_plans", {
  id: serial("id").primaryKey(),

  // Tier identifier
  tier: pricingTierEnum("tier").notNull().unique(),

  // Display
  name: text("name").notNull(),
  description: text("description").notNull(),

  // Access rules
  isFree: boolean("is_free").notNull().default(false),
  trialDays: integer("trial_days"),            // non-null only for solo_trial
  maxUsers: integer("max_users"),              // null = unlimited (enterprise)
  allowsMultiplayer: boolean("allows_multiplayer").notNull().default(true),

  // Billing — cents (USD). Null for free tier.
  monthlyPriceCents: integer("monthly_price_cents"),
  annualPriceCents: integer("annual_price_cents"),  // always = monthly * 10

  // Billing period availability flags
  hasMonthlyBilling: boolean("has_monthly_billing").notNull().default(false),
  hasAnnualBilling: boolean("has_annual_billing").notNull().default(false),

  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
});

// ─── Schemas & Types ───────────────────────────────────────────────────────────
export const insertPricingPlanSchema = createInsertSchema(pricingPlansTable).omit({
  id: true,
  createdAt: true,
  updatedAt: true,
});

export type InsertPricingPlan = z.infer<typeof insertPricingPlanSchema>;
export type PricingPlan = typeof pricingPlansTable.$inferSelect;

// ─── Seed data ─────────────────────────────────────────────────────────────────
// Annual price = monthly * 10 in every paid tier (2 months free).
export const PRICING_SEED: InsertPricingPlan[] = [
  {
    tier: "solo_trial",
    name: "Solo Trial",
    description: "7-day free trial for individual use. Standalone only — WebSocket multiplayer rooms are blocked.",
    isFree: true,
    trialDays: 7,
    maxUsers: 1,
    allowsMultiplayer: false,
    monthlyPriceCents: null,
    annualPriceCents: null,
    hasMonthlyBilling: false,
    hasAnnualBilling: false,
  },
  {
    tier: "solo_paid",
    name: "Solo",
    description: "Full individual access with multiplayer participation. $10/mo or $100/yr (2 months free).",
    isFree: false,
    trialDays: null,
    maxUsers: 1,
    allowsMultiplayer: true,
    monthlyPriceCents: 1000,   // $10.00
    annualPriceCents: 10000,   // $100.00  (10 × $10)
    hasMonthlyBilling: true,
    hasAnnualBilling: true,
  },
  {
    tier: "team_paid",
    name: "Team",
    description: "Shared 6-person collaboration room. $48/mo or $480/yr (2 months free).",
    isFree: false,
    trialDays: null,
    maxUsers: 6,
    allowsMultiplayer: true,
    monthlyPriceCents: 4800,   // $48.00
    annualPriceCents: 48000,   // $480.00  (10 × $48)
    hasMonthlyBilling: true,
    hasAnnualBilling: true,
  },
  {
    tier: "enterprise_paid",
    name: "Enterprise",
    description: "Multi-team spaces with unlimited room capacity. $240/mo or $2,400/yr (2 months free).",
    isFree: false,
    trialDays: null,
    maxUsers: null,            // unlimited
    allowsMultiplayer: true,
    monthlyPriceCents: 24000,  // $240.00
    annualPriceCents: 240000,  // $2,400.00  (10 × $240)
    hasMonthlyBilling: true,
    hasAnnualBilling: true,
  },
];
