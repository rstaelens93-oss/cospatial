import { Router } from "express";
import { db } from "@workspace/db";
import { conceptsTable, insertConceptSchema } from "@workspace/db";
import { eq, desc } from "drizzle-orm";
import { z } from "zod/v4";

const router = Router();

// Concept descriptions keyed by theme keywords for mock AI generation
const CONCEPT_TEMPLATES = [
  {
    keywords: ["space", "galaxy", "star", "cosmic"],
    desc: "A vast nebula field where ancient star systems collide, forming new constellations of light. Particles drift in hypnotic spiral patterns as gravity weaves them into emergent structures.",
    imagePrompt: "photorealistic deep space nebula, swirling cosmic dust, vibrant blues and purples, dramatic lighting, 8k ultra detailed"
  },
  {
    keywords: ["ocean", "water", "sea", "wave"],
    desc: "Bioluminescent creatures emerge from the deep, painting the ocean floor with living light. Complex currents orchestrate a ballet of glowing organisms in the abyssal dark.",
    imagePrompt: "bioluminescent deep sea creatures, dark ocean, ethereal blue glow, photorealistic, ultra detailed"
  },
  {
    keywords: ["city", "urban", "neon", "cyber"],
    desc: "A cyberpunk metropolis stretches across the horizon, its towers bristling with holographic advertisements. Rain-slicked streets reflect neon signs as autonomous vehicles weave between pedestrians.",
    imagePrompt: "cyberpunk city at night, neon lights, rain reflections, cinematic lighting, ultra detailed, 8k"
  },
  {
    keywords: ["forest", "nature", "tree", "garden"],
    desc: "An ancient forest breathes with sentient intelligence — roots form a neural network beneath the soil, exchanging chemical signals that guide the growth of every living thing above.",
    imagePrompt: "mystical enchanted forest, glowing mushrooms, shafts of light, photorealistic, cinematic, 8k"
  },
  {
    keywords: ["machine", "robot", "ai", "algorithm"],
    desc: "Fractal algorithms bloom across a digital canvas, each iteration revealing new emergent patterns. The system has learned to dream — generating worlds from pure mathematical logic.",
    imagePrompt: "abstract fractal digital art, geometric patterns, glowing lines, dark background, ultra detailed"
  },
];

function generateConcept(prompt: string) {
  const lower = prompt.toLowerCase();
  for (const template of CONCEPT_TEMPLATES) {
    if (template.keywords.some(k => lower.includes(k))) {
      return { description: template.desc, imagePrompt: template.imagePrompt };
    }
  }
  // Default generative response
  const words = prompt.split(" ").slice(0, 3).join(" ");
  return {
    description: `An AI-imagined vision of "${words}": layered realities converge at a singular point, where imagination crystallizes into form. Light bends around emergent structures, each angle revealing a new dimension of possibility.`,
    imagePrompt: `surreal digital art of ${prompt}, cinematic lighting, dramatic composition, ultra detailed, 8k`
  };
}

// POST /api/ai/generate
router.post("/generate", async (req, res) => {
  const body = insertConceptSchema.parse(req.body);
  const concept = generateConcept(body.prompt);

  const [row] = await db.insert(conceptsTable).values({
    prompt: body.prompt,
    description: concept.description,
    imagePrompt: concept.imagePrompt,
    imageUrl: null,
  }).returning();

  return res.json(row);
});

// GET /api/ai/concepts
router.get("/concepts", async (_req, res) => {
  const rows = await db
    .select()
    .from(conceptsTable)
    .orderBy(desc(conceptsTable.createdAt))
    .limit(20);
  return res.json(rows);
});

export default router;
