/**
 * ChatPanel.tsx
 * ─────────────
 * Collapsible AI Assistant side panel.
 *
 * Lets the user ask natural-language questions about the uploaded filings.
 * Questions are sent to POST /chat, which grounds Google Gemini in the app's
 * own data (financial statements + ratios + filing text) — no live SEC calls.
 *
 * The panel keeps its own conversation history and streams it back to the
 * backend each turn so follow-up questions have context.
 */

import { useState, useRef, useEffect } from "react";
import type { ChatMessage } from "../types";
import { askChat } from "../api";
import { AGENT_ORDER, AGENT_NAMES } from "./agentMeta";

interface ChatPanelProps {
  /** Whether the panel is expanded. */
  isOpen: boolean;
  /** Collapse the panel. */
  onClose: () => void;
}

// A few starter prompts shown on an empty conversation.
const SUGGESTIONS = [
  "Summarize the latest quarter's performance.",
  "How did gross margin and net margin trend across periods?",
  "What are the biggest risks mentioned in the filings?",
  "Compare revenue and operating income across the uploaded periods.",
];

// ─────────────────────────────────────────────────────────────
// Minimal, dependency-free Markdown rendering (XSS-safe via React nodes).
// Handles: **bold**, `code`, bullet lists, and paragraph/line breaks.
// ─────────────────────────────────────────────────────────────

function renderInline(text: string, keyBase: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  // Split on **bold** or `code`, keeping the delimiters via capture groups.
  const regex = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  const segments = text.split(regex);
  segments.forEach((seg, i) => {
    if (!seg) return;
    if (seg.startsWith("**") && seg.endsWith("**")) {
      nodes.push(<strong key={`${keyBase}-b${i}`}>{seg.slice(2, -2)}</strong>);
    } else if (seg.startsWith("`") && seg.endsWith("`")) {
      nodes.push(<code key={`${keyBase}-c${i}`}>{seg.slice(1, -1)}</code>);
    } else {
      nodes.push(seg);
    }
  });
  return nodes;
}

function renderMarkdown(text: string): React.ReactNode[] {
  const lines = text.split("\n");
  const blocks: React.ReactNode[] = [];
  let bullets: string[] = [];
  let blockKey = 0;

  const flushBullets = () => {
    if (bullets.length === 0) return;
    const items = bullets;
    blocks.push(
      <ul key={`ul-${blockKey++}`} className="chat-md-list">
        {items.map((b, i) => (
          <li key={i}>{renderInline(b, `li-${blockKey}-${i}`)}</li>
        ))}
      </ul>
    );
    bullets = [];
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const bulletMatch = line.match(/^\s*[-*]\s+(.*)$/);
    if (bulletMatch) {
      bullets.push(bulletMatch[1]);
      continue;
    }
    flushBullets();
    if (line.trim() === "") continue;
    // Markdown table separator rows (| --- | --- |) render as noise — skip.
    if (/^\s*\|?\s*:?-{2,}/.test(line) && line.includes("-")) continue;
    blocks.push(
      <p key={`p-${blockKey++}`} className="chat-md-p">
        {renderInline(line, `p-${blockKey}`)}
      </p>
    );
  }
  flushBullets();
  return blocks;
}

export default function ChatPanel({ isOpen, onClose }: ChatPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Which persona the user is chatting with. "general" = the omniscient
  // cross-view assistant; otherwise a single agent scoped to its own data +
  // the debate transcript (requires an /analyze run to have populated them).
  const [selectedAgent, setSelectedAgent] = useState<string>("general");

  const scrollRef = useRef<HTMLDivElement>(null);

  // Switching persona starts a fresh conversation — different agents have
  // isolated contexts, so their histories must not bleed into each other.
  function handleAgentChange(agent: string) {
    if (agent === selectedAgent) return;
    setSelectedAgent(agent);
    setMessages([]);
    setError(null);
  }

  // Auto-scroll to the newest message.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, loading]);

  async function send(question: string) {
    const q = question.trim();
    if (!q || loading) return;

    setError(null);
    // Snapshot history BEFORE adding the new user turn.
    const history = messages;
    const nextMessages: ChatMessage[] = [
      ...messages,
      { role: "user", content: q },
    ];
    setMessages(nextMessages);
    setInput("");
    setLoading(true);

    try {
      const res = await askChat(
        q,
        history,
        selectedAgent === "general" ? undefined : selectedAgent
      );
      setMessages([...nextMessages, { role: "assistant", content: res.answer }]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
      // Roll back the optimistic user turn's assistant slot — keep the question
      // visible so the user can retry.
    } finally {
      setLoading(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter inserts a newline.
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send(input);
    }
  }

  if (!isOpen) return null;

  // Persona-aware empty state, so it's obvious an agent chat is scoped to that
  // agent's data + the debate (and needs an analysis run to have populated them).
  const isGeneral = selectedAgent === "general";
  const personaName =
    selectedAgent === "manager" ? "Manager" : AGENT_NAMES[selectedAgent] ?? "agent";
  const emptyLead = isGeneral
    ? "Ask about the financials, ratios, or filing text of your uploaded filings."
    : selectedAgent === "manager"
    ? "Chat with the Manager about the synthesized verdict and how the debate resolved. Run a Deep Analysis first."
    : `Chat with the ${personaName} agent — scoped to its own data and the debate transcript. Run a Deep Analysis first.`;
  const suggestions = isGeneral
    ? SUGGESTIONS
    : selectedAgent === "manager"
    ? [
        "What was the final recommendation, and why?",
        "What did the agents disagree on, and which side won?",
        "What are the biggest risks to the thesis?",
      ]
    : [
        "Summarize your key findings.",
        "What specific evidence did you cite in the debate?",
        "What are you most and least confident about?",
      ];

  return (
    <aside className="chat-panel">
      <div className="chat-header">
        <div className="chat-header-top">
          <div className="chat-title">
            <span className="chat-title-dot" />
            AI Assistant
          </div>
          <button className="chat-close" onClick={onClose} title="Close assistant">
            ✕
          </button>
        </div>
        <select
          className="chat-agent-select"
          value={selectedAgent}
          onChange={(e) => handleAgentChange(e.target.value)}
          disabled={loading}
          title="Choose who to chat with"
          aria-label="Chat persona"
        >
          <option value="general">General Assistant (All Data)</option>
          <option value="manager">Manager Agent</option>
          {AGENT_ORDER.map((id) => (
            <option key={id} value={id}>
              {AGENT_NAMES[id]} Agent
            </option>
          ))}
        </select>
      </div>

      <div className="chat-messages" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="chat-empty">
            <p className="chat-empty-lead">{emptyLead}</p>
            <div className="chat-suggestions">
              {suggestions.map((s) => (
                <button
                  key={s}
                  className="chat-suggestion"
                  onClick={() => send(s)}
                  disabled={loading}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m, i) => (
          <div key={i} className={`chat-msg chat-msg-${m.role}`}>
            {m.role === "assistant" ? (
              <div className="chat-md">{renderMarkdown(m.content)}</div>
            ) : (
              m.content
            )}
          </div>
        ))}

        {loading && (
          <div className="chat-msg chat-msg-assistant chat-thinking">
            Thinking…
          </div>
        )}

        {error && <div className="chat-error">{error}</div>}
      </div>

      <div className="chat-input-row">
        <textarea
          className="chat-input"
          placeholder="Ask a question…  (Enter to send, Shift+Enter for newline)"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          rows={2}
          disabled={loading}
        />
        <button
          className="chat-send"
          onClick={() => send(input)}
          disabled={loading || !input.trim()}
        >
          Send
        </button>
      </div>
    </aside>
  );
}
