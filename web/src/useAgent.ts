// React hook that owns the WebSocket and turns server events into chat items.
import { useCallback, useEffect, useRef, useState } from "react";
import { INSTANT_COMMAND, type ClientMessage, type ServerEvent, type ToolInfo } from "./protocol";

export type ChatItem =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string; reasoning: string | null }
  | {
      kind: "tool";
      id: string;
      name: string;
      source: string;
      args: Record<string, unknown>;
      result?: string;
      isError?: boolean;
    }
  | { kind: "error"; text: string }
  | { kind: "info"; text: string }
  | { kind: "claude"; lines: string[] }; // progress of a running /claude task

export interface PendingApproval {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export type Status = "connecting" | "connected" | "disconnected";

export function useAgent() {
  const ws = useRef<WebSocket | null>(null);
  const [status, setStatus] = useState<Status>("connecting");
  const [items, setItems] = useState<ChatItem[]>([]);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [model, setModel] = useState("");
  const [mail, setMail] = useState("");
  const [claudeMode, setClaudeMode] = useState(false);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [approval, setApproval] = useState<PendingApproval | null>(null);

  const add = (item: ChatItem) => setItems((prev) => [...prev, item]);

  const handle = useCallback((ev: ServerEvent) => {
    switch (ev.type) {
      case "ready":
        setTools(ev.tools);
        setModel(`${ev.provider} / ${ev.model}`);
        setMail(ev.mail?.label ? [ev.mail.label, ev.mail.address].filter(Boolean).join(" · ") : "");
        setWarnings(ev.warnings);
        break;
      case "assistant_text":
        add({ kind: "assistant", text: ev.text, reasoning: ev.reasoning });
        break;
      case "tool_call":
        add({ kind: "tool", id: ev.id, name: ev.name, source: ev.source, args: ev.arguments });
        break;
      case "tool_result":
        // Attach the result to the matching tool card.
        setItems((prev) =>
          prev.map((it) =>
            it.kind === "tool" && it.id === ev.id ? { ...it, result: ev.result, isError: ev.is_error } : it,
          ),
        );
        break;
      case "approval_request":
        setApproval({ id: ev.id, name: ev.name, arguments: ev.arguments });
        break;
      case "error":
        add({ kind: "error", text: ev.message });
        break;
      case "notice":
        add({ kind: "info", text: ev.text });
        break;
      case "mode_changed":
        setClaudeMode(ev.claude_mode);
        add({ kind: "info", text: ev.message });
        break;
      case "claude_progress":
        // Append to the running Claude Code card, or start one.
        setItems((prev) => {
          const last = prev[prev.length - 1];
          if (last?.kind === "claude") return [...prev.slice(0, -1), { kind: "claude", lines: [...last.lines, ev.text] }];
          return [...prev, { kind: "claude", lines: [ev.text] }];
        });
        break;
      case "task_report":
        add({ kind: "assistant", text: ev.text, reasoning: null });
        break;
      case "claude_done":
        setBusy(false);
        add({ kind: "assistant", text: ev.text, reasoning: null });
        break;
      case "model_changed":
        setModel(`${ev.model} (${ev.profile})`);
        add({ kind: "info", text: ev.message });
        break;
      case "turn_done":
        setBusy(false);
        add({
          kind: "info",
          text: `${ev.steps} steps · ${ev.prompt_tokens}+${ev.completion_tokens} tokens`,
        });
        break;
    }
  }, []);

  useEffect(() => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${proto}://${location.host}/ws`);
    ws.current = socket;
    socket.onopen = () => setStatus("connected");
    socket.onclose = () => {
      setStatus("disconnected");
      setBusy(false);
    };
    socket.onmessage = (msg) => handle(JSON.parse(msg.data) as ServerEvent);
    return () => socket.close();
  }, [handle]);

  const send = (message: ClientMessage) => ws.current?.send(JSON.stringify(message));

  return {
    status,
    items,
    tools,
    model,
    mail,
    claudeMode,
    warnings,
    busy,
    approval,
    sendMessage(text: string) {
      add({ kind: "user", text });
      setBusy(!INSTANT_COMMAND.test(text.trim())); // instant commands start no turn
      send({ type: "user_message", text });
    },
    answerApproval(approved: boolean) {
      if (!approval) return;
      send({ type: "approval_response", id: approval.id, approved });
      setApproval(null);
    },
    reset() {
      send({ type: "reset" });
      setItems([]);
    },
  };
}
