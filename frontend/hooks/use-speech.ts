"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// The Web Speech API is not part of the standard TS DOM lib and ships under a
// vendor-prefixed constructor in Chromium browsers. Firefox and Safari have
// partial or no support for SpeechRecognition, so every call site must check
// `supported` before use rather than assuming availability.
interface SpeechRecognitionResultEvent extends Event {
  results: { [index: number]: { [index: number]: { transcript: string }; isFinal: boolean } };
  resultIndex: number;
}
interface SpeechRecognitionLike extends EventTarget {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start: () => void;
  stop: () => void;
  onresult: ((event: SpeechRecognitionResultEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  onend: (() => void) | null;
}

declare global {
  interface Window {
    SpeechRecognition?: new () => SpeechRecognitionLike;
    webkitSpeechRecognition?: new () => SpeechRecognitionLike;
  }
}

/**
 * Voice input/output for the dispatcher console.
 *
 * - `listen(onFinalResult, onError)` transcribes speech into `transcript` via
 *   the browser's SpeechRecognition. Interim results update live; the caller
 *   receives the final transcript via `onFinalResult` for auto-submission, and
 *   `onError` for recoverable errors (e.g. no-speech timeout) so continuous
 *   mode can decide whether to restart.
 * - `speak(text, onDone)` reads text aloud via SpeechSynthesis. `onDone` fires
 *   when the utterance ends (or is cancelled), letting continuous mode restart
 *   the mic after the agent finishes speaking.
 */
export function useSpeech() {
  const [listening, setListening] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [speaking, setSpeaking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  // Track the latest final text inside the closure so onend can read it even
  // after the recognition instance is replaced.
  const finalTextRef = useRef("");

  const recognitionSupported = typeof window !== "undefined" && !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  const synthesisSupported = typeof window !== "undefined" && "speechSynthesis" in window;

  useEffect(() => () => { recognitionRef.current?.stop(); window.speechSynthesis?.cancel(); }, []);

  const listen = useCallback((
    onFinalResult?: (text: string) => void,
    onError?: (reason: "no-speech" | "error") => void,
  ) => {
    if (!recognitionSupported) { setError("Voice input is not supported in this browser."); return; }
    const Recognition = window.SpeechRecognition ?? window.webkitSpeechRecognition;
    if (!Recognition) return;
    const recognition = new Recognition();
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.lang = "en-SG";
    let finalText = "";
    finalTextRef.current = "";
    recognition.onresult = (event) => {
      let interim = "";
      finalText = "";
      const keys = Object.keys(event.results).length;
      for (let index = 0; index < keys; index += 1) {
        const result = event.results[index];
        if (!result?.[0]) continue;
        if (result.isFinal) finalText += result[0].transcript;
        else interim += result[0].transcript;
      }
      finalTextRef.current = finalText;
      setTranscript((finalText + interim).trim());
    };
    recognition.onerror = (event) => {
      // "no-speech" is a normal timeout (silence), not a hard error; pass it
      // to the caller so continuous mode can restart cleanly without showing
      // an error banner.
      const isNoSpeech = (event as unknown as { error?: string }).error === "no-speech";
      if (!isNoSpeech) setError("Could not hear that clearly. Try again.");
      setListening(false);
      onError?.(isNoSpeech ? "no-speech" : "error");
    };
    recognition.onend = () => {
      setListening(false);
      const spoken = finalTextRef.current.trim();
      if (spoken && onFinalResult) onFinalResult(spoken);
    };
    recognitionRef.current = recognition;
    setError(null);
    setTranscript("");
    setListening(true);
    recognition.start();
  }, [recognitionSupported]);

  const stopListening = useCallback(() => { recognitionRef.current?.stop(); setListening(false); }, []);

  const clearTranscript = useCallback(() => {
    setTranscript("");
    finalTextRef.current = "";
  }, []);

  const speak = useCallback((text: string, onDone?: () => void) => {
    if (!synthesisSupported || !text.trim()) { onDone?.(); return; }
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.02;
    utterance.pitch = 0.95;
    utterance.onstart = () => setSpeaking(true);
    utterance.onend = () => { setSpeaking(false); onDone?.(); };
    utterance.onerror = () => { setSpeaking(false); onDone?.(); };
    window.speechSynthesis.speak(utterance);
  }, [synthesisSupported]);

  const stopSpeaking = useCallback(() => { window.speechSynthesis?.cancel(); setSpeaking(false); }, []);

  return {
    recognitionSupported,
    synthesisSupported,
    listening,
    transcript,
    error,
    speaking,
    listen,
    stopListening,
    speak,
    stopSpeaking,
    clearTranscript,
  };
}
