"use client";

/**
 * GlobalChatPanel — persistent floating chat with the master agent.
 *
 * Rendered inside AppShell so it survives page navigation. Opens/closes via
 * the toggle button anchored to the bottom-right corner. History never resets
 * between pages because state lives in ChatContext, which is mounted above the
 * page boundary.
 *
 * Voice: the LIVE button opens a hands-free chained voice pipeline
 * (see useVoicePipeline) — the browser captures the mic, does voice-activity
 * detection and client-authoritative barge-in locally, and streams one
 * utterance per turn to the /ws/voice WebSocket. The server transcribes it,
 * runs the SAME agent graph the text chat uses, and streams the spoken reply
 * back. Transcripts land in this thread alongside typed turns.
 *
 * The plain mic button remains a single-shot browser dictation aid for the
 * text box, independent of the live session.
 */

import {
  Bot,
  ChevronDown,
  Clock3,
  Mic,
  MicOff,
  Radio,
  Send,
  Trash2,
  X,
} from "lucide-react";
import {
  type SyntheticEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { useChatContext } from "@/lib/chat-context";
import type { UiDirective } from "@/lib/api";
import { useSpeech } from "@/hooks/use-speech";
import { useVoicePipeline } from "@/hooks/use-voice-pipeline";

export function GlobalChatPanel() {
  const { messages, busy, open, setOpen, stepLabel, stepLog, thinkingMs, submit, appendVoiceMessage, executeDirective, clearHistory } =
    useChatContext();

  const [message, setMessage] = useState("");
  const [clearing, setClearing] = useState(false);
  const [voiceError, setVoiceError] = useState<string | null>(null);

  const speech = useSpeech();
  const threadEndRef = useRef<HTMLDivElement>(null);

  // ── Hands-free chained voice pipeline (STT → agent → TTS over WebSocket) ───
  // The server echoes both the recognized speech and the agent reply as
  // transcripts; we render them straight into the thread. Turn-taking and
  // barge-in are decided in the hook (browser-side), not here.
  const voice = useVoicePipeline({
    onTranscript: useCallback(
      (line: { role: "user" | "agent"; text: string }) => appendVoiceMessage(line.role, line.text),
      [appendVoiceMessage],
    ),
    onDirective: useCallback(
      (directive: unknown) => executeDirective(directive as UiDirective),
      [executeDirective],
    ),
    onError: useCallback((msg: string) => setVoiceError(msg), []),
  });
  const voiceLive = voice.state !== "idle" && voice.state !== "error";

  const toggleVoiceSession = useCallback(() => {
    setVoiceError(null);
    if (voiceLive) voice.stop();
    else void voice.start();
  }, [voiceLive, voice]);

  // Scroll to newest message whenever the thread grows or busy changes.
  useEffect(() => {
    if (open) threadEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy, open]);

  // Flag the chat's open state on <body> so global CSS can lift the toast
  // notification viewport above the open chat panel (they share the
  // bottom-right corner and would otherwise overlap).
  useEffect(() => {
    if (typeof document === "undefined") return;
    document.body.dataset.chatOpen = open ? "true" : "false";
    return () => {
      delete document.body.dataset.chatOpen;
    };
  }, [open]);

  const commandValue = speech.listening ? speech.transcript : message;

  async function handleSubmit(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = speech.listening ? speech.transcript : message;
    if (!text.trim() || busy) return;
    speech.clearTranscript();
    setMessage("");
    await submit(text);
  }

  // Single-shot dictation into the text box (independent of the live session).
  function toggleMic() {
    if (speech.listening) {
      setMessage(speech.transcript);
      speech.stopListening();
      return;
    }
    setMessage("");
    speech.listen(
      (finalText) => { setMessage(finalText); void submit(finalText); },
      () => { /* single-shot: ignore no-speech */ },
    );
  }

  // Unread badge: agent messages beyond the welcome message while panel is closed.
  const unreadCount = open ? 0 : messages.filter((m) => m.role === "agent").length - 1;

  // LIVE button label reflects the voice pipeline state machine.
  const liveLabel = (() => {
    switch (voice.state) {
      case "idle": return "LIVE OFF";
      case "error": return "LIVE OFF";
      case "listening": return "LISTENING…";
      case "uploading": return "…";
      case "thinking": return "WORKING…";
      case "speaking": return "SPEAKING…";
      default: return "LIVE";
    }
  })();

  return (
    <>
      {/* ── Floating toggle button ─────────────────────────────────────── */}
      <button
        type="button"
        className="gchat-toggle"
        onClick={() => setOpen(!open)}
        aria-label={open ? "Close master agent chat" : "Open master agent chat"}
        aria-expanded={open}
      >
        {open ? <ChevronDown size={18} /> : <Bot size={18} />}
        {!open && <span className="gchat-toggle-label">AGENT</span>}
        {!open && unreadCount > 0 && (
          <span className="gchat-unread" aria-label={`${unreadCount} new replies`}>
            {unreadCount}
          </span>
        )}
        {!open && busy && <span className="gchat-busy-dot" aria-label="Agent thinking" />}
      </button>

      {/* ── Sliding panel ──────────────────────────────────────────────── */}
      <aside
        className={`gchat-panel${open ? " gchat-panel--open" : ""}`}
        aria-label="Master agent chat"
        aria-hidden={!open}
      >
        {/* Header */}
        <div className="gchat-header">
          <div className="bot-mark" aria-hidden="true"><Bot size={16} /></div>
          <div>
            <span className="eyebrow">MASTER DISPATCHER</span>
            <strong>Command console</strong>
          </div>

          {/* Hands-free Realtime voice session toggle. */}
          <button
            type="button"
            className={voiceLive ? "voice-toggle active continuous-toggle" : "voice-toggle continuous-toggle"}
            onClick={toggleVoiceSession}
            aria-pressed={voiceLive}
            title={voiceLive ? "End the hands-free voice session" : "Start a hands-free voice session — talk naturally, interrupt any time"}
          >
            <Radio size={13} />
            <span>{liveLabel}</span>
          </button>

          <span className="online-label" style={{ marginLeft: "auto" }}>
            {busy ? "THINKING…" : "ONLINE"}
          </span>

          <button
            type="button"
            className="icon-button gchat-clear"
            onClick={async () => {
              if (clearing || busy) return;
              setClearing(true);
              await clearHistory();
              setClearing(false);
            }}
            disabled={clearing || busy}
            aria-label="Clear chat history"
            title="Clear conversation history"
          >
            <Trash2 size={13} />
          </button>

          <button
            type="button"
            className="icon-button gchat-close"
            onClick={() => setOpen(false)}
            aria-label="Close chat panel"
          >
            <X size={14} />
          </button>
        </div>

        {/* Live voice banner */}
        {voiceLive && (
          <div className={`gchat-voice-banner${voice.state === "speaking" ? " speaking" : voice.state === "listening" ? " listening" : ""}`}>
            <span className="gchat-voice-orb" aria-hidden="true" />
            <span>
              {voice.state === "speaking"
                ? "Speaking… talk to interrupt."
                : voice.state === "thinking" || voice.state === "uploading"
                  ? "Working on it…"
                  : "Listening — just talk."}
            </span>
          </div>
        )}

        {/* Thread */}
        <div className="chat-thread gchat-thread">
          {messages.map((msg) => {
            if (msg.role === "divider") {
              return (
                <div key={msg.id} className="gchat-divider" aria-label="Previous conversation">
                  <span>{msg.text}</span>
                </div>
              );
            }
            return (
              <div key={msg.id} className={`chat-msg ${msg.role}`}>
                {msg.role === "agent" && (
                  <span className="chat-msg-avatar" aria-hidden="true">
                    <Bot size={13} />
                  </span>
                )}
                <p>{msg.text}</p>
                {msg.evidence && <span>Evidence: {msg.evidence}</span>}
                {msg.role === "agent" && msg.elapsedMs !== undefined && (
                  <span className="chat-msg-elapsed">
                    <Clock3 size={11} aria-hidden />
                    Answered in {(msg.elapsedMs / 1000).toFixed(1)}s
                  </span>
                )}
              </div>
            );
          })}

          {busy && (
            <output className="chat-loading" aria-live="polite">
              <div className="chat-loading-row">
                <span className="chat-loading-dot" />
                <span className="chat-loading-text">
                  {stepLabel || "Contacting the master agent…"}
                  {thinkingMs > 30000 && " (taking longer than usual)"}
                </span>
                <span className="chat-loading-timer" aria-label="elapsed time">
                  {(thinkingMs / 1000).toFixed(1)}s
                </span>
              </div>
              {stepLog.length > 1 && (
                <ol className="chat-step-log">
                  {stepLog.slice(0, -1).map((s, i) => (
                    <li key={`${s}-${i}`}>{s}</li>
                  ))}
                </ol>
              )}
              <span className="chat-loading-bar" />
            </output>
          )}

          <div ref={threadEndRef} />
        </div>

        {/* Input */}
        <form onSubmit={handleSubmit} className="command-form gchat-form">
          {speech.recognitionSupported && (
            <button
              type="button"
              className={speech.listening ? "mic-button listening" : "mic-button"}
              onClick={toggleMic}
              aria-label={speech.listening ? "Stop voice input" : "Dictate a message"}
              aria-pressed={speech.listening}
              disabled={voiceLive}
              title={voiceLive ? "Dictation is off during a live voice session" : "Dictate a message"}
            >
              {speech.listening ? <Mic size={16} /> : <MicOff size={16} className="mic-idle" />}
            </button>
          )}
          <input
            aria-label="Message Master Dispatcher"
            value={commandValue}
            onChange={(e) => setMessage(e.target.value)}
            readOnly={speech.listening || voiceLive}
            placeholder={
              voiceLive
                ? "Live voice session — just speak"
                : speech.listening
                  ? "Listening…"
                  : "Ask the Master Agent…"
            }
          />
          <button
            disabled={busy || voiceLive || !commandValue.trim()}
            aria-label="Send command"
          >
            <Send size={16} />
          </button>
        </form>

        {(voiceError || speech.error) && (
          <p className="voice-error">{voiceError || speech.error}</p>
        )}
      </aside>
    </>
  );
}
