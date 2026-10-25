import type { ChatItem } from "../useAgent";

type ToolItem = Extract<ChatItem, { kind: "tool" }>;

export function ToolCallCard({ item }: { item: ToolItem }) {
  const state = item.result === undefined ? "running" : item.isError ? "error" : "ok";
  const icon = { running: "⏳", ok: "✓", error: "✗" }[state];
  return (
    <details className={`tool-card ${state}`}>
      <summary>
        <span className="icon">{icon}</span>
        <code>{item.name}</code>
        <span className="badge">{item.source}</span>
      </summary>
      <div className="tool-body">
        <div className="label">arguments</div>
        <pre>{JSON.stringify(item.args, null, 2)}</pre>
        {item.result !== undefined && (
          <>
            <div className="label">result</div>
            <pre>{item.result}</pre>
          </>
        )}
      </div>
    </details>
  );
}
