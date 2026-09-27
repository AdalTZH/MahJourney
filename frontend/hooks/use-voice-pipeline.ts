"use client";

/**
 * useVoicePipeline — hands-free, full-duplex voice for the Master Dispatcher
 * via a chained STT -> agent -> TTS pipeline over a WebSocket.
 *
 * The browser is authoritative for turn-taking and barge-in (all decided from
 * local mic RMS); the server only transcribes one finalized utterance per turn,
 * runs the agent graph, and streams the spoken reply back as audio chunks.
 *
 * Client state machine:
 *   IDLE ─start()→ LISTENING ─(sustained silence)→ UPLOADING ─(blob sent)→
 *   THINKING ─(first tts_chunk)→ SPEAKING ─(tts_end)→ LISTENING
 *   SPEAKING ─(sustained voice = barge-in)→ cut audio + send interrupt → LISTENING
 *
 * VAD algorithm is ported from a local sounddevice assistant. TIMING constants
 * (silence/interrupt hold) transfer verbatim; AMPLITUDE thresholds are only
 * starting points — browser AnalyserNode output and Chrome's audio processing
 * use a different scale than raw sounddevice, so these are tunable.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export type VoiceState =
  | "idle"
  | "listening"
  | "uploading"
  | "thinking"
  | "speaking"
  | "error";

// ── Tuned constants (ported from the reference project) ──────────────────────
// Timing (scale-independent — ported verbatim):
const SILENCE_DURATION_MS = 1500; // silence before an utterance is finalized
const INTERRUPT_HOLD_MS = 200;    // sustained voice before a barge-in fires
const MAX_UTTERANCE_MS = 30000;   // hard cap on one recording
const MIN_UTTERANCE_MS = 300;     // ignore sub-300ms blips (stray noise)
// Amplitude (browser scale — starting points, may need re-tuning):
const SPEECH_START_RMS = 0.015;   // RMS to consider the user as speaking
// Barge-in gate. A fixed 0.08 (the value ported from the native reference) sits
// ~5x above SPEECH_START_RMS, which a normal speaking voice never reaches with
// autoGainControl off — so it is now the LARGER of an absolute floor and a
// multiple of the residual noise measured during playback. The adaptive half
// rejects TTS that leaks past echo cancellation; the absolute half keeps a very
// quiet room from making the gate trivially easy to trip.
const INTERRUPT_RMS = 0.025;      // absolute floor for a barge-in
const INTERRUPT_FLOOR_MULT = 2.5; // ...and must beat the measured playback floor
const FLOOR_EMA = 0.05;           // EMA weight when learning that floor
// Analysis cadence:
const FRAME_MS = 50;              // RMS sampled every 50ms (matches reference)

type VoicePipelineArgs = {
  /** Called with each finalized transcript line so the UI thread can show it. */
  onTranscript?: (line: { role: "user" | "agent"; text: string }) => void;
  /** Called with a backend ui_directive (navigate/refresh/activate_plan). */
  onDirective?: (directive: unknown) => void;
  onError?: (message: string) => void;
};

// Build the ws(s):// URL for the voice socket. The socket lives at "/ws/voice"
// on the same host (Caddy proxies /ws/*), so we derive it from window.location.
function voiceSocketUrl(): string {
  if (typeof window === "undefined") return "";
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/voice`;
}

export function useVoicePipeline({ onTranscript, onDirective, onError }: VoicePipelineArgs) {
  const [state, setState] = useState<VoiceState>("idle");
  const stateRef = useRef<VoiceState>("idle");
  const setVoiceState = useCallback((s: VoiceState) => {
    stateRef.current = s;
    setState(s);
  }, []);

  const onTranscriptRef = useRef(onTranscript);
  const onDirectiveRef = useRef(onDirective);
  const onErrorRef = useRef(onError);
  useEffect(() => { onTranscriptRef.current = onTranscript; }, [onTranscript]);
  useEffect(() => { onDirectiveRef.current = onDirective; }, [onDirective]);
  useEffect(() => { onErrorRef.current = onError; }, [onError]);

  // WebSocket + audio graph refs.
  const wsRef = useRef<WebSocket | null>(null);
  const micRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const frameTimerRef = useRef<number | null>(null);

  // Recording (utterance capture).
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const recStartRef = useRef(0);
  const recordingRef = useRef(false);
  const speechSeenRef = useRef(false);
  const silenceMsRef = useRef(0);

  // Barge-in detection (while speaking).
  const loudMsRef = useRef(0);
  // Rolling estimate of the mic floor while the assistant speaks: room noise
  // plus whatever of our own output survives echo cancellation.
  const echoFloorRef = useRef(0);

  // TTS playback queue.
  const audioElRef = useRef<HTMLAudioElement | null>(null);
  const ttsQueueRef = useRef<string[]>([]);   // object URLs
  const playingRef = useRef(false);
  const ttsEndedRef = useRef(false);          // server said reply is complete
  // True only between "we uploaded an utterance" and "that reply finished (or we
  // cut it off)". Everything the server sends for a turn is gated on this, so
  // audio and tts_end belonging to a turn we already abandoned cannot leak into
  // the next one. Without it, a stale tts_end makes the drain check below flip
  // the session out of SPEAKING mid-reply, and `onFrame` then takes the
  // listening branch instead of the barge-in branch — i.e. interrupt stops
  // working while the assistant is still audibly talking.
  const expectingTtsRef = useRef(false);

  // ── RMS of the current analyser frame (sqrt(mean(x^2)) — same as reference) ─
  const currentRms = useCallback((): number => {
    const analyser = analyserRef.current;
    if (!analyser) return 0;
    const buf = new Float32Array(analyser.fftSize);
    analyser.getFloatTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
    return Math.sqrt(sum / buf.length);
  }, []);

  // ── TTS playback: sequential chunks through one <audio> element ────────────
  const playNext = useCallback(() => {
    const el = audioElRef.current;
    if (!el) return;
    const url = ttsQueueRef.current.shift();
    if (!url) {
      playingRef.current = false;
      // If the server has signalled the end and the queue is drained, the reply
      // is fully spoken → go back to listening.
      if (ttsEndedRef.current && stateRef.current === "speaking") {
        setVoiceState("listening");
      }
      return;
    }
    playingRef.current = true;
    el.src = url;
    el.play().catch(() => { /* autoplay guard; will retry on next chunk */ });
  }, [setVoiceState]);

  const enqueueTts = useCallback((blob: Blob) => {
    const url = URL.createObjectURL(blob);
    ttsQueueRef.current.push(url);
    // Any state that isn't already SPEAKING must promote when real audio is
    // queued — not just THINKING. The server streams one sentence per chunk, so
    // playback can drain between sentences; if that momentarily dropped us to
    // LISTENING, the next chunk has to put us back into SPEAKING or barge-in
    // would stay disabled for the rest of the reply.
    const st = stateRef.current;
    if (st !== "speaking" && st !== "idle" && st !== "error") setVoiceState("speaking");
    if (!playingRef.current) playNext();
  }, [playNext, setVoiceState]);

  // Immediately stop and clear all TTS playback (used on barge-in / stop).
  const cutPlayback = useCallback(() => {
    const el = audioElRef.current;
    if (el) { el.pause(); el.removeAttribute("src"); el.load(); }
    ttsQueueRef.current.forEach((u) => URL.revokeObjectURL(u));
    ttsQueueRef.current = [];
    playingRef.current = false;
    ttsEndedRef.current = false;
    // Stop accepting anything further from the turn we just abandoned, so
    // chunks already in flight on the socket don't resume the reply.
    expectingTtsRef.current = false;
    loudMsRef.current = 0;
    echoFloorRef.current = 0;
  }, []);

  // ── Utterance recording control ────────────────────────────────────────────
  const finalizeUtterance = useCallback(() => {
    const rec = recorderRef.current;
    if (!rec || !recordingRef.current) return;
    recordingRef.current = false;
    // onstop handler (set in start()) sends the blob and flips to UPLOADING.
    if (rec.state !== "inactive") rec.stop();
  }, []);

  const beginRecording = useCallback(() => {
    const rec = recorderRef.current;
    if (!rec || recordingRef.current) return;
    chunksRef.current = [];
    speechSeenRef.current = false;
    silenceMsRef.current = 0;
    recStartRef.current = Date.now();
    recordingRef.current = true;
    if (rec.state === "inactive") rec.start();
  }, []);

  // ── The per-frame RMS loop — drives both VAD and barge-in ──────────────────
  const onFrame = useCallback(() => {
    const rms = currentRms();
    const st = stateRef.current;

    if (st === "listening") {
      if (!recordingRef.current) beginRecording();
      const elapsed = Date.now() - recStartRef.current;
      if (rms >= SPEECH_START_RMS) {
        speechSeenRef.current = true;
        silenceMsRef.current = 0;
      } else if (speechSeenRef.current) {
        silenceMsRef.current += FRAME_MS;
      }
      // End of utterance: had speech, then sustained silence.
      if (speechSeenRef.current && silenceMsRef.current >= SILENCE_DURATION_MS) {
        if (elapsed >= MIN_UTTERANCE_MS) finalizeUtterance();
        else { // too short — reset and keep listening
          speechSeenRef.current = false;
          silenceMsRef.current = 0;
        }
      }
      // Hard cap.
      if (elapsed >= MAX_UTTERANCE_MS && speechSeenRef.current) finalizeUtterance();
    } else if (st === "speaking" || st === "thinking") {
      // Barge-in: sustained voice above the adaptive interrupt gate.
      const gate = Math.max(INTERRUPT_RMS, echoFloorRef.current * INTERRUPT_FLOOR_MULT);
      if (rms >= gate) {
        loudMsRef.current += FRAME_MS;
        if (loudMsRef.current >= INTERRUPT_HOLD_MS) {
          loudMsRef.current = 0;
          // Client-authoritative: cut our own audio NOW, tell the server, then
          // immediately start listening for the new utterance.
          cutPlayback();
          wsRef.current?.send(JSON.stringify({ type: "interrupt" }));
          setVoiceState("listening");
        }
      } else {
        loudMsRef.current = 0;
        // Only learn the floor from frames that aren't barge-in candidates, so
        // the user's own voice can never raise the gate above itself.
        echoFloorRef.current += (rms - echoFloorRef.current) * FLOOR_EMA;
      }
    }
  }, [currentRms, beginRecording, finalizeUtterance, cutPlayback, setVoiceState]);

  // ── Teardown ───────────────────────────────────────────────────────────────
  const stop = useCallback(() => {
    if (frameTimerRef.current !== null) { clearInterval(frameTimerRef.current); frameTimerRef.current = null; }
    cutPlayback();
    try {
      if (recorderRef.current && recorderRef.current.state !== "inactive") recorderRef.current.stop();
    } catch { /* noop */ }
    recorderRef.current = null;
    recordingRef.current = false;
    micRef.current?.getTracks().forEach((t) => t.stop());
    micRef.current = null;
    analyserRef.current = null;
    void audioCtxRef.current?.close().catch(() => {});
    audioCtxRef.current = null;
    wsRef.current?.close();
    wsRef.current = null;
    setVoiceState("idle");
  }, [cutPlayback, setVoiceState]);

  // ── Startup ────────────────────────────────────────────────────────────────
  const start = useCallback(async () => {
    if (stateRef.current !== "idle" && stateRef.current !== "error") return;
    try {
      // Mic with echo cancellation so the agent's own TTS (through the
      // speakers) doesn't bleed in and trip the barge-in detector.
      const mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false },
      });
      micRef.current = mic;

      const ctx = new AudioContext();
      audioCtxRef.current = ctx;
      const source = ctx.createMediaStreamSource(mic);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      analyser.smoothingTimeConstant = 0.2;
      source.connect(analyser);
      analyserRef.current = analyser;

      // Single <audio> element for sequential TTS playback.
      if (!audioElRef.current) {
        const el = document.createElement("audio");
        el.autoplay = false;
        el.addEventListener("ended", () => {
          const url = el.getAttribute("src");
          if (url) URL.revokeObjectURL(url);
          playNext();
        });
        audioElRef.current = el;
      }

      // MediaRecorder captures each utterance as a blob.
      const mime = MediaRecorder.isTypeSupported("audio/webm")
        ? "audio/webm"
        : "audio/mp4";
      const rec = new MediaRecorder(mic, { mimeType: mime });
      rec.addEventListener("dataavailable", (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      });
      rec.addEventListener("stop", () => {
        const blob = new Blob(chunksRef.current, { type: mime });
        chunksRef.current = [];
        // Only send if we actually captured speech.
        if (speechSeenRef.current && blob.size > 0 && wsRef.current?.readyState === WebSocket.OPEN) {
          setVoiceState("uploading");
          void blob.arrayBuffer().then((buf) => {
            wsRef.current?.send(buf);
            // A fresh turn starts here: clear the previous reply's end flag and
            // open the gate for this turn's audio.
            ttsEndedRef.current = false;
            expectingTtsRef.current = true;
            loudMsRef.current = 0;
            echoFloorRef.current = 0;
            setVoiceState("thinking");
          });
        } else if (stateRef.current === "listening") {
          // Nothing captured — keep listening (recorder restarts next frame).
        }
      });
      recorderRef.current = rec;

      // Connect the voice socket (session cookie is sent automatically).
      const ws = new WebSocket(voiceSocketUrl());
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.addEventListener("open", () => {
        setVoiceState("listening");
        // Start the RMS loop once everything is live.
        frameTimerRef.current = window.setInterval(onFrame, FRAME_MS);
      });
      ws.addEventListener("message", (e) => {
        if (typeof e.data === "string") {
          let msg: Record<string, unknown>;
          try { msg = JSON.parse(e.data); } catch { return; }
          // Safely read a string field from the (untrusted) message.
          const str = (v: unknown): string => (typeof v === "string" ? v : "");
          if (msg.type === "transcript") {
            onTranscriptRef.current?.({
              role: msg.role === "user" ? "user" : "agent",
              text: str(msg.text),
            });
          } else if (msg.type === "directive") {
            // A navigate/refresh directive from the agent — run it through the
            // same handler the text chat uses (router push, etc.).
            if (msg.directive) onDirectiveRef.current?.(msg.directive);
          } else if (msg.type === "tts_end") {
            // The server sends tts_end from a `finally`, so a turn we cancelled
            // on a barge-in still emits one. Ignoring it unless we're actually
            // awaiting this turn's reply is what stops that late frame from
            // poisoning the next turn's drain check.
            if (!expectingTtsRef.current) return;
            expectingTtsRef.current = false;
            ttsEndedRef.current = true;
            // If nothing is queued/playing, return to listening now.
            if (!playingRef.current && ttsQueueRef.current.length === 0) {
              if (stateRef.current !== "listening") setVoiceState("listening");
            }
          } else if (msg.type === "error") {
            cutPlayback();
            onErrorRef.current?.(str(msg.message) || "Voice error.");
            setVoiceState("listening");
          }
        } else {
          // Binary frame = a TTS audio chunk. Dropped unless it belongs to the
          // turn we're still waiting on, so audio already in flight when the
          // user barged in can't restart the reply we just cut off.
          if (!expectingTtsRef.current) return;
          enqueueTts(new Blob([e.data as ArrayBuffer], { type: "audio/mpeg" }));
        }
      });
      ws.addEventListener("close", (ev) => {
        if (ev.code === 4401) onErrorRef.current?.("Your session expired — please log in again.");
        stop();
      });
      ws.addEventListener("error", () => {
        onErrorRef.current?.("Voice connection failed.");
        stop();
      });
    } catch (err) {
      onErrorRef.current?.(err instanceof Error ? err.message : "Could not start voice.");
      setVoiceState("error");
      stop();
    }
  }, [onFrame, enqueueTts, playNext, cutPlayback, stop, setVoiceState]);

  useEffect(() => () => stop(), [stop]);

  return { state, start, stop };
}
