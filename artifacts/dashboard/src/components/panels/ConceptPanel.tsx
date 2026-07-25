import { useState, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  useGenerateConcept,
  useListConcepts,
  getListConceptsQueryKey,
} from "@workspace/api-client-react";
import type { Concept } from "@workspace/api-client-react";
import { Sparkles, Clock, AlertCircle, ChevronRight } from "lucide-react";

interface ConceptPanelProps {
  onConceptGenerated: (concept: Concept) => void;
  latestConcept: Concept | null;
}

function formatRelativeTime(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function ConceptPanel({ onConceptGenerated, latestConcept }: ConceptPanelProps) {
  const [prompt, setPrompt] = useState("");
  const queryClient = useQueryClient();
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const { data: concepts, isLoading: conceptsLoading } = useListConcepts();

  const generateConcept = useGenerateConcept({
    mutation: {
      onSuccess: (concept) => {
        onConceptGenerated(concept);
        queryClient.invalidateQueries({ queryKey: getListConceptsQueryKey() });
        setPrompt("");
      },
    },
  });

  const handleGenerate = () => {
    if (!prompt.trim() || generateConcept.isPending) return;
    generateConcept.mutate({ data: { prompt: prompt.trim() } });
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      handleGenerate();
    }
  };

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Prompt input */}
      <div className="p-3 flex-shrink-0">
        <div
          className="relative rounded-md overflow-hidden"
          style={{
            border: "1px solid hsl(213 18% 20%)",
            background: "hsl(215 28% 7%)",
          }}
        >
          <textarea
            ref={textareaRef}
            data-testid="input-concept-prompt"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Describe a concept to generate... (Ctrl+Enter to run)"
            rows={4}
            className="w-full bg-transparent text-foreground placeholder:text-muted-foreground resize-none outline-none p-3 text-sm leading-relaxed"
            style={{ fontFamily: "var(--app-font-sans)" }}
          />
          <div
            className="absolute bottom-0 left-0 right-0 h-px"
            style={{
              background: prompt
                ? "linear-gradient(90deg, rgba(0,212,255,0.4) 0%, rgba(0,212,255,0.1) 60%, transparent 100%)"
                : "transparent",
              transition: "background 0.3s ease",
            }}
          />
        </div>

        <button
          data-testid="button-generate-concept"
          onClick={handleGenerate}
          disabled={!prompt.trim() || generateConcept.isPending}
          className="mt-2 w-full flex items-center justify-center gap-2 py-2.5 px-4 rounded-md text-sm font-medium transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
          style={{
            background: generateConcept.isPending
              ? "rgba(0, 212, 255, 0.1)"
              : "rgba(0, 212, 255, 0.12)",
            border: "1px solid rgba(0, 212, 255, 0.3)",
            color: "#00d4ff",
          }}
          onMouseEnter={(e) => {
            if (!generateConcept.isPending && prompt.trim()) {
              (e.currentTarget as HTMLButtonElement).style.background = "rgba(0, 212, 255, 0.2)";
              (e.currentTarget as HTMLButtonElement).style.borderColor = "rgba(0, 212, 255, 0.5)";
            }
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLButtonElement).style.background = "rgba(0, 212, 255, 0.12)";
            (e.currentTarget as HTMLButtonElement).style.borderColor = "rgba(0, 212, 255, 0.3)";
          }}
        >
          {generateConcept.isPending ? (
            <>
              <span className="inline-flex gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-cyan-400 animate-dot-pulse" />
                <span className="w-1.5 h-1.5 rounded-full bg-cyan-400 animate-dot-pulse-delay-1" />
                <span className="w-1.5 h-1.5 rounded-full bg-cyan-400 animate-dot-pulse-delay-2" />
              </span>
              <span>Generating...</span>
            </>
          ) : (
            <>
              <Sparkles size={14} />
              <span>Generate AI Concept</span>
            </>
          )}
        </button>

        {generateConcept.isError && (
          <div
            className="mt-2 flex items-center gap-2 px-3 py-2 rounded text-xs"
            style={{
              background: "rgba(239, 68, 68, 0.08)",
              border: "1px solid rgba(239, 68, 68, 0.2)",
              color: "#f87171",
            }}
          >
            <AlertCircle size={12} />
            <span>Generation failed. Check your prompt and retry.</span>
          </div>
        )}
      </div>

      {/* Latest concept description */}
      {latestConcept && (
        <div
          className="mx-3 mb-3 p-3 rounded-md flex-shrink-0 animate-fade-up"
          style={{
            background: "rgba(0, 212, 255, 0.05)",
            border: "1px solid rgba(0, 212, 255, 0.15)",
          }}
        >
          <div className="flex items-center gap-1.5 mb-1.5">
            <div className="w-1.5 h-1.5 rounded-full bg-cyan-400 animate-pulse" />
            <span
              className="text-xs font-medium tracking-wider uppercase"
              style={{ color: "rgba(0, 212, 255, 0.7)" }}
            >
              Latest Concept
            </span>
          </div>
          <p className="text-xs leading-relaxed text-foreground opacity-90 line-clamp-4">
            {latestConcept.description}
          </p>
        </div>
      )}

      {/* Concepts history */}
      <div className="flex-1 overflow-hidden flex flex-col min-h-0">
        <div
          className="px-3 py-1.5 flex items-center gap-2 flex-shrink-0"
          style={{ borderTop: "1px solid hsl(213 18% 16%)" }}
        >
          <Clock size={11} style={{ color: "hsl(200 15% 52%)" }} />
          <span
            className="text-xs font-medium tracking-wider uppercase"
            style={{ color: "hsl(200 15% 52%)" }}
          >
            History
          </span>
          {concepts && concepts.length > 0 && (
            <span
              className="ml-auto text-xs px-1.5 py-0.5 rounded"
              style={{
                background: "rgba(0, 212, 255, 0.1)",
                color: "rgba(0, 212, 255, 0.7)",
              }}
            >
              {concepts.length}
            </span>
          )}
        </div>

        <div className="flex-1 overflow-y-auto px-3 pb-3 space-y-1">
          {conceptsLoading ? (
            <>
              {[1, 2, 3].map((i) => (
                <div
                  key={i}
                  className="h-10 rounded animate-shimmer"
                  style={{ opacity: 0.6 - i * 0.1 }}
                />
              ))}
            </>
          ) : concepts && concepts.length > 0 ? (
            [...concepts]
              .sort(
                (a, b) =>
                  new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime()
              )
              .map((concept) => (
                <div
                  key={concept.id}
                  data-testid={`concept-item-${concept.id}`}
                  className="group flex items-start gap-2 p-2 rounded cursor-pointer transition-all duration-150"
                  style={{
                    background: "rgba(255,255,255,0.02)",
                    border: "1px solid transparent",
                  }}
                  onClick={() => onConceptGenerated(concept)}
                  onMouseEnter={(e) => {
                    (e.currentTarget as HTMLDivElement).style.background = "rgba(0, 212, 255, 0.05)";
                    (e.currentTarget as HTMLDivElement).style.borderColor = "rgba(0, 212, 255, 0.1)";
                  }}
                  onMouseLeave={(e) => {
                    (e.currentTarget as HTMLDivElement).style.background = "rgba(255,255,255,0.02)";
                    (e.currentTarget as HTMLDivElement).style.borderColor = "transparent";
                  }}
                >
                  <ChevronRight
                    size={12}
                    className="flex-shrink-0 mt-0.5 opacity-0 group-hover:opacity-100 transition-opacity"
                    style={{ color: "#00d4ff" }}
                  />
                  <div className="flex-1 min-w-0">
                    <p className="text-xs truncate text-foreground opacity-80">{concept.prompt}</p>
                    <p className="text-xs mt-0.5" style={{ color: "hsl(200 15% 42%)" }}>
                      {formatRelativeTime(concept.createdAt)}
                    </p>
                  </div>
                </div>
              ))
          ) : (
            <div className="flex flex-col items-center justify-center py-8 text-center">
              <Sparkles
                size={24}
                style={{ color: "hsl(200 15% 30%)", marginBottom: "8px" }}
              />
              <p className="text-xs" style={{ color: "hsl(200 15% 40%)" }}>
                No concepts yet.
                <br />
                Generate your first one above.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
