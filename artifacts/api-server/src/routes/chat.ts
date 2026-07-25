import { Router } from "express";
import { db } from "@workspace/db";
import { chatMessagesTable, insertChatMessageSchema } from "@workspace/db";
import { desc } from "drizzle-orm";

const router = Router();

// GET /api/chat/messages
router.get("/messages", async (_req, res) => {
  const rows = await db
    .select()
    .from(chatMessagesTable)
    .orderBy(desc(chatMessagesTable.createdAt))
    .limit(50);
  return res.json(rows.reverse());
});

// POST /api/chat/messages
router.post("/messages", async (req, res) => {
  const body = insertChatMessageSchema.parse(req.body);
  const [row] = await db.insert(chatMessagesTable).values(body).returning();
  return res.status(201).json(row);
});

export default router;
