import { Router } from "express";
import healthRouter from "./health";
import aiRouter from "./ai";
import codeRouter from "./code";
import chatRouter from "./chat";
import imageRouter from "./image";

const router = Router();

router.use("/healthz", healthRouter);
router.use("/ai", aiRouter);
router.use("/code", codeRouter);
router.use("/chat", chatRouter);
router.use("/image", imageRouter);

export default router;
