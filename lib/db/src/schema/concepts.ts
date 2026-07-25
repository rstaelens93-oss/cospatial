import { pgTable, text, serial, timestamp } from "drizzle-orm/pg-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";

export const conceptsTable = pgTable("concepts", {
  id: serial("id").primaryKey(),
  prompt: text("prompt").notNull(),
  description: text("description").notNull(),
  imagePrompt: text("image_prompt").notNull(),
  imageUrl: text("image_url"),
  createdAt: timestamp("created_at").defaultNow().notNull(),
});

export const insertConceptSchema = createInsertSchema(conceptsTable).omit({ id: true, createdAt: true });
export type InsertConcept = z.infer<typeof insertConceptSchema>;
export type Concept = typeof conceptsTable.$inferSelect;
