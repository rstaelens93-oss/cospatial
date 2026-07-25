import { Router } from "express";

const router = Router();

const PYTHON_API = `http://localhost:${process.env.PYTHON_PORT ?? "8001"}`;

// POST /api/code/execute — proxy to Python FastAPI backend
router.post("/execute", async (req, res) => {
  try {
    const response = await fetch(`${PYTHON_API}/python-api/execute`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req.body),
      signal: AbortSignal.timeout(15_000),
    });
    const data = await response.json();
    return res.json(data);
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    req.log?.error({ err }, "Python backend unreachable");
    return res.status(503).json({
      success: false,
      stdout: "",
      stderr: `Python backend unavailable: ${message}`,
      executionTime: 0,
      sceneData: null,
    });
  }
});

export default router;
