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
 * - `listen()` transcribes speech into `transcript` via the browser's
 *   SpeechRecognition, when available. Interim results update live; the
 *   caller decides what to do with the final transcript rather than this hook
 *   auto-submitting anything, since voice input should be reviewable before
 *   it triggers an action.
 * - `speak(text)` reads text aloud via SpeechSynthesis, which has much broader
 *   browser support than recognition, so it is offered independently.
 */
export function useSpeech() {
  const [listening, setListening] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [speaking, setSpeaking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);

  const recognitionSupported = typeof window !== "undefined" && !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  const synthesisSupported = typeof window !== "undefined" && "speechSynthesis" in window;

  useEffect(() => () => { recognitionRef.current?.stop(); window.speechSynthesis?.cancel(); }, []);

  const listen = useCallback((onFinalResult?: (text: string) => void) => {
    if (!recognitionSupported) { setError("Voice input is not supported in this browser."); return; }
    const Recognition = window.SpeechRecognition ?? window.webkitSpeechRecognition;
    if (!Recognition) return;
    const recognition = new Recognition();
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.lang = "en-SG";
    let finalText = "";
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
      setTranscript((finalText + interim).trim());
    };
    recognition.onerror = () => { setError("Could not hear that clearly. Try again."); setListening(false); };
    recognition.onend = () => {
      setListening(false);
      // Hand the finalized transcript to the caller so it can auto-submit;
      // the transcript state is left intact so the text stays visible.
      const spoken = finalText.trim();
      if (spoken && onFinalResult) onFinalResult(spoken);
    };
    recognitionRef.current = recognition;
    setError(null);
    setTranscript("");
    setListening(true);
    recognition.start();
  }, [recognitionSupported]);

  const stopListening = useCallback(() => { recognitionRef.current?.stop(); setListening(false); }, []);

  const speak = useCallback((text: string) => {
    if (!synthesisSupported || !text.trim()) return;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.02;
    utterance.pitch = 0.95;
    utterance.onstart = () => setSpeaking(true);
    utterance.onend = () => setSpeaking(false);
    utterance.onerror = () => setSpeaking(false);
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
    clearTranscript: () => setTranscript(""),
  };
}
