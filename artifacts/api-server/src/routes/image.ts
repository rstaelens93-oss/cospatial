import { Router } from "express";

const router = Router();

const PYTHON_API = `http://localhost:${process.env.PYTHON_PORT ?? "8001"}`;

// POST /api/image/generate — proxy to Python FastAPI Pollinations endpoint
router.post("/generate", async (req, res) => {
  try {
    const response = await fetch(`${PYTHON_API}/python-api/generate-image`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req.body),
      signal: AbortSignal.timeout(15_000),
    });
    const data = await response.json();
    if (!response.ok) {
      return res.status(response.status).json(data);
    }
    return res.json(data);
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    req.log?.error({ err }, "Python backend unreachable for image generation");
    return res.status(503).json({ imageUrl: null, error: `Python backend unavailable: ${message}` });
  }
});

export default router;
