import { useEffect, useState } from "react";
import type { Concept } from "@workspace/api-client-react";
import { ImageIcon, Wand2 } from "lucide-react";

interface ImagePanelProps {
  concept: Concept | null;
  isGenerating: boolean;
}

export default function ImagePanel({ concept, isGenerating }: ImagePanelProps) {
  const [imageLoaded, setImageLoaded] = useState(false);
  const [pulsing, setPulsing] = useState(false);

  useEffect(() => {
    if (isGenerating) {
      setPulsing(true);
      setImageLoaded(false);
      return undefined;
    } else {
      const timer = setTimeout(() => setPulsing(false), 800);
      return () => clearTimeout(timer);
    }
  }, [isGenerating]);

  useEffect(() => {
    if (concept) {
      setPulsing(true);
      setImageLoaded(false);
      const timer = setTimeout(() => setPulsing(false), 600);
      return () => clearTimeout(timer);
    }
    return undefined;
  }, [concept?.id]);

  if (isGenerating) {
    return (
      <div className="flex flex-col h-full items-center justify-center p-4">
        <div
          className="w-full h-full rounded-lg flex flex-col items-center justify-center animate-pulse-cyan"
          style={{
            background: "linear-gradient(135deg, hsl(215 24% 8%) 0%, hsl(215 24% 11%) 100%)",
            border: "1px solid rgba(0, 212, 255, 0.2)",
          }}
        >
          {/* Shimmer loading state */}
          <div className="w-full h-full relative overflow-hidden rounded-lg">
            <div className="animate-shimmer w-full h-full rounded-lg" />
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-3">
              <div className="flex gap-1.5">
                <span className="w-2 h-2 rounded-full bg-cyan-400 animate-dot-pulse" />
                <span className="w-2 h-2 rounded-full bg-cyan-400 animate-dot-pulse-delay-1" />
                <span className="w-2 h-2 rounded-full bg-cyan-400 animate-dot-pulse-delay-2" />
              </div>
              <span
                className="text-sm font-medium tracking-wide"
                style={{ color: "rgba(0, 212, 255, 0.7)" }}
              >
                Synthesizing concept...
              </span>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (!concept) {
    return (
      <div className="flex flex-col h-full items-center justify-center p-6">
        <div
          className="w-full h-full rounded-lg flex flex-col items-center justify-center gap-4 panel-grid-line"
          style={{
            background: "hsl(215 24% 8%)",
            border: "1px solid hsl(213 18% 16%)",
          }}
        >
          {/* Animated placeholder */}
          <div className="relative">
            <div
              className="w-16 h-16 rounded-full flex items-center justify-center animate-breathe"
              style={{
                background: "rgba(0, 212, 255, 0.05)",
                border: "1px solid rgba(0, 212, 255, 0.15)",
              }}
            >
              <ImageIcon size={28} style={{ color: "rgba(0, 212, 255, 0.3)" }} />
            </div>
            {/* Orbiting ring */}
            <div
              className="absolute inset-0 rounded-full animate-breathe"
              style={{
                border: "1px solid rgba(0, 212, 255, 0.08)",
                transform: "scale(1.3)",
                animationDelay: "1s",
              }}
            />
          </div>
          <div className="text-center max-w-48">
            <p
              className="text-sm font-medium mb-1"
              style={{ color: "hsl(200 15% 45%)" }}
            >
              No concept generated yet
            </p>
            <p className="text-xs leading-relaxed" style={{ color: "hsl(200 15% 32%)" }}>
              Generate a concept to see the image prompt appear here
            </p>
          </div>
          {/* Grid corner decorations */}
          <div
            className="absolute top-3 left-3 w-4 h-4"
            style={{
              borderTop: "1px solid rgba(0, 212, 255, 0.2)",
              borderLeft: "1px solid rgba(0, 212, 255, 0.2)",
            }}
          />
          <div
            className="absolute top-3 right-3 w-4 h-4"
            style={{
              borderTop: "1px solid rgba(0, 212, 255, 0.2)",
              borderRight: "1px solid rgba(0, 212, 255, 0.2)",
            }}
          />
          <div
            className="absolute bottom-3 left-3 w-4 h-4"
            style={{
              borderBottom: "1px solid rgba(0, 212, 255, 0.2)",
              borderLeft: "1px solid rgba(0, 212, 255, 0.2)",
            }}
          />
          <div
            className="absolute bottom-3 right-3 w-4 h-4"
            style={{
              borderBottom: "1px solid rgba(0, 212, 255, 0.2)",
              borderRight: "1px solid rgba(0, 212, 255, 0.2)",
            }}
          />
        </div>
      </div>
    );
  }

  return (
    <div
      className="flex flex-col h-full overflow-hidden"
      style={{
        transition: "all 0.4s ease",
        boxShadow: pulsing ? "0 0 32px rgba(0, 212, 255, 0.2) inset" : "none",
      }}
    >
      {concept.imageUrl ? (
        /* Show actual image if available */
        <div className="flex-1 relative overflow-hidden">
          {!imageLoaded && (
            <div className="absolute inset-0 animate-shimmer" />
          )}
          <img
            src={concept.imageUrl}
            alt={concept.prompt}
            data-testid="generated-image"
            className="w-full h-full object-contain"
            style={{
              opacity: imageLoaded ? 1 : 0,
              transition: "opacity 0.4s ease",
            }}
            onLoad={() => setImageLoaded(true)}
          />
        </div>
      ) : (
        /* Show imagePrompt styled card */
        <div className="flex-1 overflow-y-auto p-4">
          {/* Creative brief header */}
          <div className="flex items-center gap-2 mb-4">
            <div
              className="w-6 h-6 rounded flex items-center justify-center flex-shrink-0"
              style={{
                background: "rgba(0, 212, 255, 0.1)",
                border: "1px solid rgba(0, 212, 255, 0.25)",
              }}
            >
              <Wand2 size={12} style={{ color: "#00d4ff" }} />
            </div>
            <span
              className="text-xs font-medium tracking-widest uppercase"
              style={{ color: "rgba(0, 212, 255, 0.6)" }}
            >
              Image Directive
            </span>
          </div>

          {/* Prompt card */}
          <div
            className="rounded-lg p-4 mb-3 relative"
            style={{
              background: "linear-gradient(135deg, rgba(0, 212, 255, 0.04) 0%, rgba(0, 212, 255, 0.02) 100%)",
              border: "1px solid rgba(0, 212, 255, 0.15)",
            }}
          >
            {/* Corner accent */}
            <div
              className="absolute top-0 left-0 w-8 h-8"
              style={{
                borderTop: "1px solid rgba(0, 212, 255, 0.4)",
                borderLeft: "1px solid rgba(0, 212, 255, 0.4)",
                borderRadius: "8px 0 0 0",
              }}
            />
            <p
              className="text-sm leading-relaxed"
              style={{
                color: "hsl(200 20% 82%)",
                fontFamily: "var(--app-font-sans)",
                fontWeight: 400,
                letterSpacing: "0.01em",
              }}
            >
              {concept.imagePrompt}
            </p>
          </div>

          {/* Concept description */}
          <div
            className="rounded p-3"
            style={{
              background: "hsl(215 24% 8%)",
              border: "1px solid hsl(213 18% 16%)",
            }}
          >
            <p
              className="text-xs mb-1 font-medium tracking-wider uppercase"
              style={{ color: "hsl(200 15% 40%)" }}
            >
              Concept
            </p>
            <p
              className="text-xs leading-relaxed"
              style={{ color: "hsl(200 15% 60%)" }}
            >
              {concept.description}
            </p>
          </div>

          {/* Original prompt */}
          <div className="mt-2 flex items-start gap-2">
            <span
              className="text-xs tracking-wider uppercase flex-shrink-0 mt-0.5"
              style={{ color: "hsl(200 15% 35%)", fontSize: "10px" }}
            >
              Prompt
            </span>
            <span className="text-xs" style={{ color: "hsl(200 15% 50%)" }}>
              {concept.prompt}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
