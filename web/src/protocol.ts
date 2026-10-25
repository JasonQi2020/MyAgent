// Mirrors backend/agent_demo/events.py. Keep the two files in sync by hand.

export interface ToolInfo {
  name: string;
  source: string; // "local" or "mcp:<server>"
  needs_approval: boolean;
  blocked: boolean;
}

// ---- server -> client -------------------------------------------------------
export type ServerEvent =
  | {
      type: "ready";
      provider: string;
      model: string;
      tools: ToolInfo[];
      warnings: string[];
      mail: { provider: string; label: string; address: string };
    }
  | { type: "assistant_text"; text: string; reasoning: string | null }
  | { type: "tool_call"; id: string; name: string; arguments: Record<string, unknown>; source: string }
  | { type: "approval_request"; id: string; name: string; arguments: Record<string, unknown> }
  | { type: "tool_result"; id: string; name: string; result: string; is_error: boolean }
  | { type: "error"; message: string }
  | { type: "turn_done"; steps: number; prompt_tokens: number; completion_tokens: number }
  | { type: "model_changed"; profile: string; model: string; message: string }
  | { type: "notice"; text: string }
  | { type: "mode_changed"; claude_mode: boolean; message: string }
  | { type: "claude_progress"; text: string }
  | { type: "claude_done"; text: string; ok: boolean }
  | { type: "task_report"; text: string }; // a scheduled task created in this UI

// Answered by the server at once (no assistant turn or Claude Code run follows), so the UI
// must not wait for turn_done / claude_done. "/claude <request>" and "/claude new <request>" do run.
export const INSTANT_COMMAND =
  /^(\/(strongmodel|weakmodel|model|tools|help|new)|\/tasks(\s+.*)?|\/claude(\s+(on|off|status|stop|new|folder(\s+.*)?))?)\s*$/i;

// ---- client -> server -------------------------------------------------------
export type ClientMessage =
  | { type: "user_message"; text: string }
  | { type: "approval_response"; id: string; approved: boolean }
  | { type: "reset" };
