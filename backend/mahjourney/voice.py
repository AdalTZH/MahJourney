"""voice.py — speech helpers for the chained voice console.

The hands-free voice console is a chained pipeline:

    browser mic --(one finalized utterance)--> transcribe() --> text
    text --> agent graph (the same one the text chat uses) --> reply
    reply --> stream_tts() --> audio chunks --> browser speaker

This module owns only the speech legs (STT + TTS). Turn-taking, silence
detection, and barge-in all live in the browser (client-authoritative); the
server just transcribes one utterance per turn and streams the reply back,
cancellable mid-sentence when the client barges in.

Ported in spirit from a local sounddevice/pygame assistant, but adapted for a
web/WebSocket deployment: audio arrives as an uploaded blob and TTS is streamed
over the socket rather than played on a local device.
"""

from __future__ import annotations

import io
import re
from collections.abc import AsyncGenerator

from openai import AsyncOpenAI, OpenAIError

from .config import Settings

# Split a reply into speakable sentences so TTS can start on the first sentence
# while later ones are still being synthesized, and so a barge-in cancels at a
# natural boundary. Keeps the delimiter with the sentence.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text: str) -> list[str]:
    """Break reply text into non-empty, trimmed sentence chunks for streaming TTS."""
    parts = _SENTENCE_SPLIT.split(text.strip())
    return [p.strip() for p in parts if p.strip()]


class VoiceGateway:
    """Thin async wrapper over OpenAI STT + TTS for the voice console.

    Uses AsyncOpenAI so calls never block the event loop that also drives the
    WebSocket. When no API key is configured every method degrades safely: STT
    returns an empty string and TTS yields nothing, so the socket handler can
    surface a spoken/logged error instead of crashing.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = (
            AsyncOpenAI(
                api_key=settings.openai_api_key,
                timeout=settings.openai_request_timeout_seconds,
            )
            if settings.openai_api_key
            else None
        )

    async def transcribe(self, audio: bytes, *, filename: str = "utterance.webm") -> str:
        """Transcribe one finalized utterance blob to text.

        The blob is whatever the browser's MediaRecorder produced (typically
        webm/opus); OpenAI infers the format from the filename extension, so the
        caller passes the recorder's mime as a filename hint.
        """
        if self.client is None or not audio:
            return ""
        buf = io.BytesIO(audio)
        buf.name = filename  # the SDK uses the name to detect the audio format
        try:
            result = await self.client.audio.transcriptions.create(
                model=self.settings.voice_stt_model,
                file=buf,
                language=self.settings.voice_stt_language,
                response_format="text",
            )
        except OpenAIError:
            return ""
        return (result if isinstance(result, str) else getattr(result, "text", "")).strip()

    async def synthesize(self, sentence: str) -> bytes:
        """Synthesize one sentence to audio bytes in the configured format."""
        if self.client is None or not sentence.strip():
            return b""
        kwargs: dict = {
            "model": self.settings.voice_tts_model,
            "voice": self.settings.voice_tts_voice,
            "input": sentence,
            "response_format": self.settings.voice_tts_format,
        }
        # `instructions` (tone/accent) is only supported by gpt-4o-mini-tts.
        if (
            self.settings.voice_tts_instructions
            and "gpt-4o-mini-tts" in self.settings.voice_tts_model
        ):
            kwargs["instructions"] = self.settings.voice_tts_instructions
        try:
            response = await self.client.audio.speech.create(**kwargs)
            return response.content
        except OpenAIError:
            return b""

    async def stream_tts(self, text: str) -> AsyncGenerator[bytes, None]:
        """Yield audio chunks for `text`, one synthesized sentence at a time.

        Yielding per sentence keeps first-audio latency low and gives the socket
        handler natural cancellation points: on a barge-in it simply stops
        iterating, so no further sentences are synthesized or sent.
        """
        for sentence in split_sentences(text):
            audio = await self.synthesize(sentence)
            if audio:
                yield audio
