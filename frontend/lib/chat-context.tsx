"use client";

/**
 * ChatContext — global persistent master-agent chat state.
 *
 * Lives above the page boundary (mounted in layout.tsx) so conversation
 * history, open/closed state, and any in-flight turn survive Next.js
 * client-side navigation.
 *
 * The provider accepts an optional `onDirective` callback. When the backend
 * returns a `ui_directive` in its final SSE payload, that directive is passed
 * to this callback so the caller (providers.tsx, which has access to
 * useRouter) can navigate, activate plans, or refresh data without the context
 * taking a hard dependency on Next.js router hooks.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  UnauthorizedError,
  api,
  clearConversation,
  fetchConversationHistory,
  streamDispatch,
  type UiDirective,
} from "@/lib/api";

export type ChatMessage = {
  id: string;
  role: "user" | "agent" | "divider";
  text: string;
  evidence?: string;
  elapsedMs?: number;
};

type ChatContextValue = {
  messages: ChatMessage[];
  busy: boolean;
  open: boolean;
  setOpen: (open: boolean) => void;
  stepLabel: string;
  stepLog: string[];
  thinkingMs: number;
  submit: (text: string) => Promise<void>;
  /**
   * Run a spoken request through the agent graph, append the user turn and the
   * agent reply to the shared thread (so voice turns show up and persist like
   * typed ones), honor any ui_directive, and RETURN the reply text so the
   * Realtime voice model can speak it. Uses the plain (non-streaming) endpoint
   * since the voice model narrates progress itself.
   */
  runAgentTurn: (text: string) => Promise<string>;
  /**
   * Append a voice-turn transcript line to the thread. The chained voice
   * pipeline runs the agent server-side (over the WebSocket) and echoes both
   * the recognized user speech and the agent reply back as transcripts, so the
   * UI just needs to render them — it does not re-run the agent here.
   */
  appendVoiceMessage: (role: "user" | "agent", text: string) => void;
  /**
   * Run a backend ui_directive (navigate / refresh / activate_plan) through the
   * same handler the text chat uses. Exposed so the voice pipeline — which runs
   * the agent server-side over the WebSocket — can honor spoken "go to X"
   * requests too.
   */
  executeDirective: (directive: UiDirective) => void;
  clearHistory: () => Promise<void>;
};

const ChatContext = createContext<ChatContextValue | null>(null);

const CONVERSATION_ID = "global-dispatcher-ui";

// SSR-safe id: counter-based on the server, UUID-based in the browser.
let _counter = 0;
const newId = () => {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `msg-${++_counter}-${Date.now()}`;
};

const WELCOME_MESSAGE: ChatMessage = {
  id: "welcome-0",
  role: "agent",
  text: "Monitoring the live plan. Ask about routes, trade-offs, or disruptions.",
};

type ChatProviderProps = {
  children: ReactNode;
  /**
   * Called with a directive from the backend after each turn. Providers.tsx
   * supplies a router-aware implementation; omit in tests or storybook.
   */
  onDirective?: (directive: UiDirective) => void | Promise<void>;
};

export function ChatProvider({ children, onDirective }: ChatProviderProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([WELCOME_MESSAGE]);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [stepLabel, setStepLabel] = useState("");
  const [stepLog, setStepLog] = useState<string[]>([]);
  const [thinkingMs, setThinkingMs] = useState(0);

  // Keep a stable ref so submit() always calls the latest onDirective without
  // needing it in the useCallback dep array.
  const onDirectiveRef = useRef(onDirective);
  useEffect(() => { onDirectiveRef.current = onDirective; }, [onDirective]);

  // Seed chat history from the backend on first mount so a page refresh
  // doesn't wipe the conversation. Runs once; if persistence is off the
  // endpoint returns [] and we keep the welcome message.
  useEffect(() => {
    fetchConversationHistory(CONVERSATION_ID).then((history) => {
      if (!history.length) return;
      const loaded: ChatMessage[] = history.map((msg, i) => ({
        id: `hist-${i}-${msg.created_at}`,
        role: msg.role === "assistant" ? "agent" : ("user" as const),
        text: msg.content,
      }));
      setMessages([
        WELCOME_MESSAGE,
        { id: "divider-history", role: "divider", text: "Previous conversation" },
        ...loaded,
      ]);
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Drive the elapsed timer while a turn is in flight.
  useEffect(() => {
    if (!busy) return;
    const started = Date.now();
    const tick = window.setInterval(() => setThinkingMs(Date.now() - started), 100);
    return () => {
      window.clearInterval(tick);
      setThinkingMs(0);
      setStepLabel("");
      setStepLog([]);
    };
  }, [busy]);

  /**
   * Execute a ui_directive from the backend. The backend only emits a navigate
   * directive when the dispatcher explicitly asked to be taken somewhere on
   * this turn — never as a side effect of another action — so acting on it
   * immediately is the requested behavior, not an unsolicited jump.
   *
   *   navigate → delegate to onDirective (router.push in providers.tsx)
   *   refresh  → delegate to onDirective so the host page can re-fetch
   */
  const executeDirective = useCallback((directive: UiDirective) => {
    onDirectiveRef.current?.(directive);
  }, []);

  const submit = useCallback(async (rawText: string) => {
    const outgoing = rawText.trim();
    if (!outgoing || busy) return;

    setMessages((prev) => [...prev, { id: newId(), role: "user", text: outgoing }]);
    setBusy(true);
    const startedAt = Date.now();

    try {
      const result = await streamDispatch(outgoing, CONVERSATION_ID, (step) => {
        setStepLabel(step.label);
        setStepLog((log) =>
          log[log.length - 1] === step.label ? log : [...log, step.label],
        );
      });
      const evidence = result.evidence_references?.length
        ? `${result.evidence_references.join(" · ")} · policy trace available`
        : undefined;
      setMessages((prev) => [
        ...prev,
        {
          id: newId(),
          role: "agent",
          text: result.reply,
          evidence,
          elapsedMs: Date.now() - startedAt,
        },
      ]);
      // The backend attaches a navigate directive only when the dispatcher
      // explicitly asked to go somewhere this turn, so honor it immediately.
      if (result.ui_directive) {
        executeDirective(result.ui_directive);
      }
    } catch (streamErr) {
      // Streaming failed — fall back to the plain endpoint.
      if (streamErr instanceof UnauthorizedError) {
        setMessages((prev) => [
          ...prev,
          { id: newId(), role: "agent", text: "Your session has expired. Please refresh the page and log in again." },
        ]);
        setBusy(false);
        return;
      }
      try {
        const result = await api<{ reply: string; evidence_references?: string[] }>(
          "/dispatcher/messages",
          {
            method: "POST",
            body: JSON.stringify({ message: outgoing, conversation_id: CONVERSATION_ID }),
          },
        );
        const evidence = result.evidence_references?.length
          ? `${result.evidence_references.join(" · ")} · policy trace available`
          : undefined;
        setMessages((prev) => [
          ...prev,
          {
            id: newId(),
            role: "agent",
            text: result.reply,
            evidence,
            elapsedMs: Date.now() - startedAt,
          },
        ]);
        // The plain fallback endpoint doesn't stream directives, so no action here.
      } catch (fallbackErr) {
        const text = fallbackErr instanceof UnauthorizedError
          ? "Your session has expired. Please refresh the page and log in again."
          : "The API is unreachable right now. Check your connection and try again.";
        setMessages((prev) => [
          ...prev,
          { id: newId(), role: "agent", text, elapsedMs: Date.now() - startedAt },
        ]);
      }
    } finally {
      setBusy(false);
    }
  }, [busy, executeDirective]);

  // Voice path: run a spoken request through the agent and return the reply
  // text for the Realtime model to speak. Mirrors submit()'s thread/directive
  // handling but uses the plain endpoint and hands the reply back to the caller.
  const runAgentTurn = useCallback(async (rawText: string): Promise<string> => {
    const outgoing = rawText.trim();
    if (!outgoing) return "";

    setMessages((prev) => [...prev, { id: newId(), role: "user", text: outgoing }]);
    setBusy(true);
    const startedAt = Date.now();
    try {
      const result = await api<{
        reply: string;
        evidence_references?: string[];
        ui_directive?: UiDirective | null;
      }>("/dispatcher/messages", {
        method: "POST",
        body: JSON.stringify({ message: outgoing, conversation_id: CONVERSATION_ID }),
      });
      const evidence = result.evidence_references?.length
        ? `${result.evidence_references.join(" · ")} · policy trace available`
        : undefined;
      setMessages((prev) => [
        ...prev,
        {
          id: newId(),
          role: "agent",
          text: result.reply,
          evidence,
          elapsedMs: Date.now() - startedAt,
        },
      ]);
      if (result.ui_directive) executeDirective(result.ui_directive);
      return result.reply;
    } catch (err) {
      // Surface the real error to the console so voice-path failures are
      // debuggable (the spoken/threaded message stays user-friendly).
      console.error("runAgentTurn failed:", err);
      const text = err instanceof UnauthorizedError
        ? "Your session has expired. Please refresh and log in again."
        : "The dispatcher service is unreachable right now.";
      setMessages((prev) => [
        ...prev,
        { id: newId(), role: "agent", text, elapsedMs: Date.now() - startedAt },
      ]);
      return text;
    } finally {
      setBusy(false);
    }
  }, [executeDirective]);

  const appendVoiceMessage = useCallback((role: "user" | "agent", text: string) => {
    const clean = text.trim();
    if (!clean) return;
    setMessages((prev) => [...prev, { id: newId(), role, text: clean }]);
  }, []);

  const clearHistory = useCallback(async () => {
    try {
      await clearConversation(CONVERSATION_ID);
    } catch {
      // Best-effort — even if the server call fails, reset the UI.
    }
    setMessages([WELCOME_MESSAGE]);
  }, []);

  return (
    <ChatContext.Provider
      value={{ messages, busy, open, setOpen, stepLabel, stepLog, thinkingMs, submit, runAgentTurn, appendVoiceMessage, executeDirective, clearHistory }}
    >
      {children}
    </ChatContext.Provider>
  );
}

export function useChatContext(): ChatContextValue {
  const ctx = useContext(ChatContext);
  if (!ctx) {
    // Return a no-op context during SSR or when used outside the provider.
    // The real provider mounts client-side; throwing here causes SSR 500s.
    return {
      messages: [WELCOME_MESSAGE],
      busy: false,
      open: false,
      setOpen: () => {},
      stepLabel: "",
      stepLog: [],
      thinkingMs: 0,
      submit: async () => {},
      runAgentTurn: async () => "",
      appendVoiceMessage: () => {},
      executeDirective: () => {},
      clearHistory: async () => {},
    };
  }
  return ctx;
}
