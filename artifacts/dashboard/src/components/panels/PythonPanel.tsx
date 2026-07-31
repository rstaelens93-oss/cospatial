import { useState, useRef, useEffect } from "react";
import Editor from "@monaco-editor/react";
import { Play, Terminal, CheckCircle, XCircle, Loader2 } from "lucide-react";
import { useExecuteCode } from "@workspace/api-client-react";
import type { CodeResult } from "@workspace/api-client-react";

const DEFAULT_CODE = `import math

# Use emit_scene() to send 3D data to the viewport
points = []
for i in range(100):
    t = i * 0.2
    points.append({
        "x": math.cos(t) * t * 0.3,
        "y": math.sin(t) * t * 0.3,
        "z": i * 0.05 - 2.5
    })

emit_scene({
    "type": "points",
    "points": points,
    "color": "#00ffcc"
})
`;

interface PythonPanelProps {
  onSceneData: (sceneData: string) => void;
  /** When set, the backend has pre-generated a Python script for the latest
   *  image concept. The editor is immediately updated but remains fully editable. */
  injectedCode?: string;
}

export default function PythonPanel({ onSceneData, injectedCode }: PythonPanelProps) {
  const [code, setCode] = useState(DEFAULT_CODE);

  // Pre-fill the editor whenever the backend sends a new generated script.
  // The Monaco editor's onChange handler still fires normally so the user
  // can edit freely after injection.
  useEffect(() => {
    if (injectedCode) setCode(injectedCode);
  }, [injectedCode]);
  const [lastResult, setLastResult] = useState<CodeResult | null>(null);
  const consoleRef = useRef<HTMLDivElement>(null);

  const executeCode = useExecuteCode({
    mutation: {
      onSuccess: (result) => {
        setLastResult(result);
        if (result.sceneData) {
          onSceneData(result.sceneData);
        }
        // Auto-scroll console
        setTimeout(() => {
          if (consoleRef.current) {
            consoleRef.current.scrollTop = consoleRef.current.scrollHeight;
          }
        }, 50);
      },
    },
  });

  const handleRun = () => {
    if (executeCode.isPending) return;
    executeCode.mutate({ data: { code, language: "python" } });
  };

  const consoleOutput = lastResult
    ? [
        lastResult.stdout && `[stdout]\n${lastResult.stdout}`,
        lastResult.stderr && `[stderr]\n${lastResult.stderr}`,
        `[exit] ${lastResult.success ? "ok" : "error"} — ${lastResult.executionTime}ms`,
      ]
        .filter(Boolean)
        .join("\n\n")
    : "";

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Monaco Editor — fills available space */}
      <div className="flex-1 min-h-0 overflow-hidden">
        <Editor
          height="100%"
          defaultLanguage="python"
          value={code}
          onChange={(val) => setCode(val ?? "")}
          theme="vs-dark"
          options={{
            fontSize: 12,
            fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
            fontLigatures: true,
            minimap: { enabled: false },
            scrollbar: {
              vertical: "auto",
              horizontal: "auto",
              verticalScrollbarSize: 4,
              horizontalScrollbarSize: 4,
            },
            lineNumbers: "on",
            lineNumbersMinChars: 3,
            glyphMargin: false,
            folding: false,
            lineDecorationsWidth: 0,
            renderLineHighlight: "line",
            overviewRulerLanes: 0,
            hideCursorInOverviewRuler: true,
            scrollBeyondLastLine: false,
            automaticLayout: true,
            tabSize: 4,
            wordWrap: "off",
            padding: { top: 12, bottom: 12 },
          }}
          beforeMount={(monaco) => {
            monaco.editor.defineTheme("dashboard-dark", {
              base: "vs-dark",
              inherit: true,
              rules: [
                { token: "comment", foreground: "4a6274", fontStyle: "italic" },
                { token: "keyword", foreground: "00d4ff" },
                { token: "string", foreground: "7ec8e3" },
                { token: "number", foreground: "a3f0c4" },
                { token: "type", foreground: "00d4ff" },
              ],
              colors: {
                "editor.background": "#0b1118",
                "editor.foreground": "#c8dce8",
                "editor.lineHighlightBackground": "#131c26",
                "editor.selectionBackground": "#00d4ff22",
                "editorCursor.foreground": "#00d4ff",
                "editorLineNumber.foreground": "#2a4050",
                "editorLineNumber.activeForeground": "#00d4ff80",
                "editor.inactiveSelectionBackground": "#00d4ff11",
              },
            });
          }}
          onMount={(editor, monaco) => {
            monaco.editor.setTheme("dashboard-dark");
          }}
        />
      </div>

      {/* Run button */}
      <div
        className="flex-shrink-0 px-3 py-2 flex items-center gap-2"
        style={{ borderTop: "1px solid hsl(213 18% 16%)" }}
      >
        <button
          data-testid="button-run-code"
          onClick={handleRun}
          disabled={executeCode.isPending}
          className="flex items-center gap-2 px-3 py-1.5 rounded text-xs font-medium transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed"
          style={{
            background: executeCode.isPending
              ? "rgba(0, 212, 255, 0.05)"
              : "rgba(0, 212, 255, 0.1)",
            border: "1px solid rgba(0, 212, 255, 0.25)",
            color: "#00d4ff",
          }}
          onMouseEnter={(e) => {
            if (!executeCode.isPending) {
              (e.currentTarget as HTMLButtonElement).style.background = "rgba(0, 212, 255, 0.18)";
            }
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLButtonElement).style.background = "rgba(0, 212, 255, 0.1)";
          }}
        >
          {executeCode.isPending ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <Play size={12} />
          )}
          <span>{executeCode.isPending ? "Executing..." : "Run 3D Engine"}</span>
        </button>

        {lastResult && (
          <div className="flex items-center gap-1.5 ml-auto">
            {lastResult.success ? (
              <CheckCircle size={12} style={{ color: "#34d399" }} />
            ) : (
              <XCircle size={12} style={{ color: "#f87171" }} />
            )}
            <span
              className="text-xs"
              style={{
                color: lastResult.success ? "#34d399" : "#f87171",
                fontFamily: "var(--app-font-mono)",
              }}
            >
              {lastResult.executionTime}ms
            </span>
          </div>
        )}
      </div>

      {/* Output console */}
      <div
        className="flex-shrink-0"
        style={{
          borderTop: "1px solid hsl(213 18% 16%)",
          maxHeight: "120px",
          minHeight: "40px",
        }}
      >
        <div className="flex items-center gap-1.5 px-3 py-1" style={{ borderBottom: "1px solid hsl(213 18% 14%)" }}>
          <Terminal size={10} style={{ color: "hsl(200 15% 40%)" }} />
          <span
            className="text-xs tracking-wider uppercase"
            style={{ color: "hsl(200 15% 40%)", fontSize: "10px" }}
          >
            Output
          </span>
        </div>

        <div
          ref={consoleRef}
          data-testid="console-output"
          className="overflow-y-auto p-2"
          style={{
            maxHeight: "84px",
            fontFamily: "var(--app-font-mono)",
            fontSize: "11px",
            lineHeight: "1.5",
            color: executeCode.isError
              ? "#f87171"
              : lastResult?.success === false
              ? "#f87171"
              : "hsl(200 15% 65%)",
            background: "hsl(215 28% 6%)",
          }}
        >
          {executeCode.isPending ? (
            <span style={{ color: "#00d4ff" }}>
              <span className="animate-pulse">▶</span> Executing Python code...
            </span>
          ) : executeCode.isError ? (
            <span style={{ color: "#f87171" }}>Connection error. Is the API server running?</span>
          ) : consoleOutput ? (
            <pre style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
              {consoleOutput}
            </pre>
          ) : (
            <span style={{ color: "hsl(200 15% 30%)" }}>
              — Ready. Press Run 3D Engine to execute. —
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
