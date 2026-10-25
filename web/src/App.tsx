import { useEffect, useRef, useState } from "react";
import { ApprovalDialog } from "./components/ApprovalDialog";
import { ToolCallCard } from "./components/ToolCallCard";
import { useAgent } from "./useAgent";

export default function App() {
  const agent = useAgent();
  const [input, setInput] = useState("");
  const bottom = useRef<HTMLDivElement>(null);

  // Braces matter: an effect's return value is treated as its cleanup function, and newer
  // browsers return a Promise from scrollIntoView, which made React crash (blank page).
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [agent.items]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const text = input.trim();
    if (!text || agent.busy || agent.status !== "connected") return;
    agent.sendMessage(text);
    setInput("");
  };

  const mcpCount = agent.tools.filter((t) => t.source.startsWith("mcp:")).length;

  return (
    <div className="app">
      <header>
        <strong>Agent Demo</strong>
        <span className={`status ${agent.status}`}>{agent.status}</span>
        <span className="muted">{agent.model}</span>
        <span className="muted">
          {agent.tools.length} tools ({mcpCount} via MCP)
        </span>
        {agent.mail && <span className="muted">mail: {agent.mail}</span>}
        {agent.claudeMode && <span className="status connected">Claude mode</span>}
        <button className="link" onClick={agent.reset} disabled={agent.busy}>
          New chat
        </button>
      </header>

      {agent.warnings.map((w) => (
        <div key={w} className="warning">
          ⚠ {w}
        </div>
      ))}

      <main className="messages">
        {agent.items.length === 0 && (
          <div className="empty">
            Try: “Which TSX stocks in my watchlist rose most this month?” or “Summarise my last 5 trades and
            email them to me.”
          </div>
        )}
        {agent.items.map((item, i) => {
          switch (item.kind) {
            case "user":
              return (
                <div key={i} className="bubble user">
                  {item.text}
                </div>
              );
            case "assistant":
              return (
                <div key={i} className="bubble assistant">
                  {item.reasoning && (
                    <details className="reasoning">
                      <summary>reasoning</summary>
                      {item.reasoning}
                    </details>
                  )}
                  {item.text}
                </div>
              );
            case "tool":
              return <ToolCallCard key={i} item={item} />;
            case "error":
              return (
                <div key={i} className="error">
                  {item.text}
                </div>
              );
            case "info":
              return (
                <div key={i} className="info">
                  {item.text}
                </div>
              );
            case "claude":
              return (
                <details key={i} className="tool-card ok" open>
                  <summary>
                    <span className="icon">🤖</span>
                    <code>Claude Code</code>
                    <span className="badge">{item.lines.length} steps</span>
                  </summary>
                  <div className="tool-body">
                    <pre>{item.lines.join("\n")}</pre>
                  </div>
                </details>
              );
          }
        })}
        {agent.busy && <div className="info">thinking…</div>}
        <div ref={bottom} />
      </main>

      <form className="composer" onSubmit={submit}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={agent.busy ? "Agent is working…" : "Ask about your stocks or your email"}
          disabled={agent.status !== "connected"}
        />
        <button type="submit" disabled={agent.busy || !input.trim()}>
          Send
        </button>
      </form>

      {agent.approval && <ApprovalDialog approval={agent.approval} onAnswer={agent.answerApproval} />}
    </div>
  );
}
