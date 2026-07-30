import { useState, useCallback } from "react";
import { apiUrl } from "@/lib/endpoints";
import type { Concept } from "@workspace/api-client-react";
import type { SceneDataPayload } from "@/hooks/useWebSockets";
import ConceptPanel from "@/components/panels/ConceptPanel";
import PythonPanel from "@/components/panels/PythonPanel";
import ImagePanel from "@/components/panels/ImagePanel";
import ViewportPanel from "@/components/panels/ViewportPanel";
import ChatBox from "@/components/ChatBox";
import {
  Cpu,
  Activity,
  Layers,
  Box,
  Sparkles,
  Terminal,
  Image,
  Globe,
} from "lucide-react";

interface PanelHeaderProps {
  icon: React.ReactNode;
  title: string;
  status?: "active" | "idle" | "processing";
  badge?: string;
}

function PanelHeader({ icon, title, status = "idle", badge }: PanelHeaderProps) {
  return (
    <div
      className="flex items-center gap-2 px-3 py-2 flex-shrink-0 relative panel-header-accent"
      style={{
        background: "hsl(215 24% 11%)",
        borderBottom: "1px solid hsl(213 18% 16%)",
        minHeight: "38px",
      }}
    >
      {/* Status indicator */}
      <div
        className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
          status === "active"
            ? "bg-cyan-400 animate-pulse"
            : status === "processing"
            ? "bg-amber-400 animate-pulse"
            : "bg-gray-600"
        }`}
      />

      {/* Icon */}
      <div
        className="flex items-center justify-center w-5 h-5 rounded flex-shrink-0"
        style={{
          background: "rgba(0, 212, 255, 0.08)",
          border: "1px solid rgba(0, 212, 255, 0.15)",
        }}
      >
        {icon}
      </div>

      {/* Title */}
      <span
        className="text-xs font-semibold tracking-wider uppercase flex-1"
        style={{ color: "hsl(200 20% 75%)", letterSpacing: "0.08em" }}
      >
        {title}
      </span>

      {/* Optional badge */}
      {badge && (
        <span
          className="text-xs px-1.5 py-0.5 rounded flex-shrink-0"
          style={{
            background: "rgba(0, 212, 255, 0.08)",
            border: "1px solid rgba(0, 212, 255, 0.15)",
            color: "rgba(0, 212, 255, 0.6)",
            fontFamily: "var(--app-font-mono)",
            fontSize: "10px",
          }}
        >
          {badge}
        </span>
      )}
    </div>
  );
}

export default function Dashboard() {
  const [latestConcept, setLatestConcept] = useState<Concept | null>(null);
  const [liveImageUrl, setLiveImageUrl] = useState<string | null>(null);
  const [isGenerating, setIsGenerating] = useState(false);
  const [sceneData, setSceneData] = useState<string | null>(null);
  const [conceptPanelStatus, setConceptPanelStatus] = useState<"active" | "idle" | "processing">("idle");
  const [pythonPanelStatus, setPythonPanelStatus] = useState<"active" | "idle" | "processing">("idle");

  const handleConceptGenerated = useCallback((concept: Concept) => {
    setLatestConcept(concept);
    setIsGenerating(true);   // show loading while image fetches
    setLiveImageUrl(null);
    setConceptPanelStatus("active");

    // Fire-and-forget: fetch the Pollinations image URL for this concept's prompt.
    fetch(apiUrl("/api/image/generate"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: concept.prompt }),
    })
      .then((res) => res.json())
      .then((data: { imageUrl?: string }) => {
        if (data.imageUrl) setLiveImageUrl(data.imageUrl);
      })
      .catch(() => {
        // Non-fatal — ImagePanel will fall back to the text prompt card.
      })
      .finally(() => {
        setIsGenerating(false);
      });
  }, []);

  // Python panel → viewport (HTTP path: result.sceneData is already a string)
  const handleSceneData = (data: string) => {
    setSceneData(data);
    setPythonPanelStatus("active");
  };

  // WebSocket path: image_scene frame → viewport (data is already parsed object)
  const handleWsSceneData = useCallback((data: SceneDataPayload) => {
    setSceneData(JSON.stringify(data));
    setPythonPanelStatus("active");
  }, []);

  // Merge the live Pollinations URL into the concept object ImagePanel receives.
  const displayConcept = latestConcept
    ? { ...latestConcept, imageUrl: liveImageUrl }
    : null;

  return (
    <div
      style={{
        width: "100vw",
        height: "100vh",
        overflow: "hidden",
        position: "relative",
        background: "hsl(215 28% 7%)",
      }}
    >
      {/* ── Top header bar — fixed so it doesn't consume grid height ── */}
      <header
        className="flex items-center px-4 gap-4"
        style={{
          position: "fixed",
          top: 0,
          left: 0,
          width: "100vw",
          height: "44px",
          zIndex: 20,
          background: "hsl(215 24% 8%)",
          borderBottom: "1px solid hsl(213 18% 15%)",
          boxShadow: "0 1px 0 rgba(0, 212, 255, 0.05)",
        }}
      >
        {/* Logo / title */}
        <div className="flex items-center gap-2.5">
          <div
            className="flex items-center justify-center w-6 h-6 rounded"
            style={{
              background: "rgba(0, 212, 255, 0.12)",
              border: "1px solid rgba(0, 212, 255, 0.3)",
            }}
          >
            <Cpu size={13} style={{ color: "#00d4ff" }} />
          </div>
          <span
            className="text-sm font-semibold tracking-wider"
            style={{
              color: "#00d4ff",
              fontFamily: "var(--app-font-sans)",
              letterSpacing: "0.12em",
            }}
          >
            COLLAB<span style={{ opacity: 0.5 }}>.</span>AI
          </span>
          <span
            className="text-xs"
            style={{
              color: "hsl(200 15% 35%)",
              fontFamily: "var(--app-font-mono)",
            }}
          >
            MISSION CONTROL
          </span>
        </div>

        {/* Center: panel indicators */}
        <div className="flex-1 flex items-center justify-center gap-6">
          {[
            { label: "CONCEPT", icon: <Sparkles size={10} />, active: !!latestConcept },
            { label: "ENGINE", icon: <Terminal size={10} />, active: !!sceneData },
            { label: "RENDER", icon: <Image size={10} />, active: !!latestConcept?.imageUrl },
            { label: "VIEWPORT", icon: <Globe size={10} />, active: true },
          ].map(({ label, icon, active }) => (
            <div key={label} className="flex items-center gap-1.5">
              <div
                className="w-1 h-1 rounded-full"
                style={{
                  background: active ? "#00d4ff" : "hsl(213 18% 22%)",
                  boxShadow: active ? "0 0 4px #00d4ff" : "none",
                }}
              />
              <span
                className="text-xs tracking-widest"
                style={{
                  color: active ? "hsl(200 15% 60%)" : "hsl(200 15% 28%)",
                  fontFamily: "var(--app-font-mono)",
                  fontSize: "10px",
                  letterSpacing: "0.1em",
                }}
              >
                {label}
              </span>
            </div>
          ))}
        </div>

        {/* Right: system status */}
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1.5">
            <Activity size={11} style={{ color: "#34d399" }} />
            <span
              className="text-xs"
              style={{
                color: "#34d399",
                fontFamily: "var(--app-font-mono)",
                fontSize: "10px",
              }}
            >
              ONLINE
            </span>
          </div>
          <div
            className="h-4 w-px"
            style={{ background: "hsl(213 18% 20%)" }}
          />
          <div
            className="flex items-center gap-1.5"
            style={{ color: "hsl(200 15% 35%)", fontFamily: "var(--app-font-mono)", fontSize: "10px" }}
          >
            <Layers size={10} />
            <span>v1.0</span>
          </div>
        </div>
      </header>

      {/* ── Strict 2×2 grid — each cell is exactly 50vw × 50vh ── */}
      <main
        style={{
          display: "grid",
          gridTemplateColumns: "50vw 50vw",
          gridTemplateRows: "50vh 50vh",
          width: "100vw",
          height: "100vh",
          overflow: "hidden",
          gap: "1px",
          background: "hsl(213 18% 13%)", // gap colour — acts as divider lines
        }}
      >
        {/* TOP-LEFT: AI Concept Generator */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            overflow: "hidden",
            background: "hsl(215 24% 9%)",
            width: "100%",
            height: "100%",
          }}
        >
          <PanelHeader
            icon={<Sparkles size={11} style={{ color: "#00d4ff" }} />}
            title="AI Concept Generator"
            status={conceptPanelStatus}
            badge={latestConcept ? `#${latestConcept.id}` : undefined}
          />
          <div style={{ flex: 1, minHeight: 0, overflow: "hidden" }}>
            <ConceptPanel
              onConceptGenerated={handleConceptGenerated}
              latestConcept={latestConcept}
            />
          </div>
        </div>

        {/* TOP-RIGHT: Generated Image */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            overflow: "hidden",
            background: "hsl(215 24% 9%)",
            width: "100%",
            height: "100%",
          }}
        >
          <PanelHeader
            icon={<Image size={11} style={{ color: "#00d4ff" }} />}
            title="Generated Image"
            status={isGenerating ? "processing" : latestConcept ? "active" : "idle"}
            badge={liveImageUrl ? "IMAGE" : latestConcept ? "PROMPT" : undefined}
          />
          <div style={{ flex: 1, minHeight: 0, overflow: "hidden", position: "relative" }}>
            <ImagePanel concept={displayConcept} isGenerating={isGenerating} />
          </div>
        </div>

        {/* BOTTOM-LEFT: Python Engine */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            overflow: "hidden",
            background: "hsl(215 24% 9%)",
            width: "100%",
            height: "100%",
          }}
        >
          <PanelHeader
            icon={<Terminal size={11} style={{ color: "#00d4ff" }} />}
            title="Python Engine"
            status={pythonPanelStatus}
            badge="PYTHON"
          />
          <div style={{ flex: 1, minHeight: 0, overflow: "hidden" }}>
            <PythonPanel onSceneData={handleSceneData} />
          </div>
        </div>

        {/* BOTTOM-RIGHT: 3D Viewport */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            overflow: "hidden",
            background: "hsl(215 24% 9%)",
            width: "100%",
            height: "100%",
          }}
        >
          <PanelHeader
            icon={<Box size={11} style={{ color: "#00d4ff" }} />}
            title="3D Viewport"
            status={sceneData ? "active" : "idle"}
            badge="THREE.JS"
          />
          <div style={{ flex: 1, minHeight: 0, overflow: "hidden" }}>
            <ViewportPanel sceneData={sceneData} />
          </div>
        </div>
      </main>

      {/* ── Floating chat box — also relays image_scene WS frames to viewport ── */}
      <ChatBox onSceneData={handleWsSceneData} />
    </div>
  );
}
