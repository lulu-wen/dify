"""OpenAI Whisper ↔ WhisperX schema translation (PR #13).

EMS provides WhisperX (Blackwell-accelerated whisper-large-v3 with
speaker diarisation) as the ASR service. WhisperX's native API differs
from OpenAI's ``/v1/audio/transcriptions`` in three places:

1. **Request shape**: WhisperX accepts only ``file``, ``language``,
   ``num_speakers``, ``min_speakers``, ``max_speakers``. There is no
   ``model`` (it's hardcoded to whisper-large-v3), no
   ``response_format``, no ``temperature``, no ``prompt``.
2. **Response shape**: WhisperX returns
   ``{segments: [{start, end, text, speaker, words: [...]}], language}``
   with NO top-level ``text`` field. OpenAI's response shape (per the
   ``response_format`` parameter) is ``{"text": "..."}`` (default
   ``json``) or a richer envelope (``verbose_json``).
3. **Speaker diarisation**: a WhisperX-only feature; OpenAI Whisper API
   has no equivalent. We surface it via ``extra_body`` on input and
   inside ``segments[i].speaker`` on output (OpenAI clients tolerate
   extra fields).

This module exposes:

* :func:`build_whisperx_request` — convert OpenAI-shaped form fields
  into the WhisperX form payload, logging warnings for the OpenAI
  fields WhisperX cannot honour.
* :func:`translate_whisperx_response` — flip the WhisperX response into
  the requested OpenAI ``response_format``.

Intentionally NO HTTP I/O here — keep this pure data translation so it
can be unit-tested without mocking httpx. The router module owns the
upload + forwarding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

import structlog

from gateway.errors import InvalidRequestError

logger = structlog.get_logger(__name__)

# OpenAI's documented response_format values. We support the two JSON
# variants; the text/srt/vtt formats would require rewriting WhisperX's
# segments as different surface syntaxes — out of scope for PR #13.
WhisperxResponseShape = Literal["json", "verbose_json"]


@dataclass(frozen=True)
class WhisperxRequest:
    """The form payload the gateway sends to WhisperX's ``POST /transcribe``."""

    # ``language`` defaults to ``"auto"`` so WhisperX runs its language
    # detector when the client doesn't specify (matches OpenAI behaviour).
    language: str
    # Optional speaker-hint fields (WhisperX uses these to bound its
    # diarisation pipeline; absent → automatic).
    num_speakers: int | None
    min_speakers: int | None
    max_speakers: int | None

    def to_form(self) -> dict[str, str]:
        """Return the dict shape ``httpx.AsyncClient.post(..., data=...)`` expects.

        Optional speaker fields are stringified when present and omitted
        when None — WhisperX rejects empty strings for these int fields.
        """
        out: dict[str, str] = {"language": self.language}
        if self.num_speakers is not None:
            out["num_speakers"] = str(self.num_speakers)
        if self.min_speakers is not None:
            out["min_speakers"] = str(self.min_speakers)
        if self.max_speakers is not None:
            out["max_speakers"] = str(self.max_speakers)
        return out


def build_whisperx_request(
    *,
    language: str | None,
    response_format: str,
    prompt: str | None,
    temperature: float,
    extra_body: str | None,
) -> tuple[WhisperxRequest, WhisperxResponseShape]:
    """Translate OpenAI Whisper form fields into a WhisperX form payload.

    Args:
        language: OpenAI's ``language`` field. None / empty → ``"auto"``
            (WhisperX's language-detector).
        response_format: OpenAI's ``response_format`` (``json``,
            ``verbose_json``, ``text``, ``srt``, ``vtt``). Only
            ``json`` and ``verbose_json`` are supported in PR #13;
            others raise :class:`InvalidRequestError` with an actionable
            message.
        prompt: OpenAI's ``prompt`` field (style nudge). WhisperX has
            no equivalent — logged at warn and dropped.
        temperature: OpenAI's ``temperature``. WhisperX runs at fixed
            settings; non-zero values are logged at warn and dropped.
        extra_body: Optional JSON string carrying WhisperX-specific
            speaker hints under the keys ``num_speakers`` /
            ``min_speakers`` / ``max_speakers``. Clients pass these via
            OpenAI SDK's ``extra_body`` param; on the wire it becomes
            another form field. Invalid JSON → log warn + ignore.

    Returns:
        ``(WhisperxRequest, response_shape)``. ``response_shape`` is
        what :func:`translate_whisperx_response` should use to render
        the result.

    Raises:
        InvalidRequestError: ``response_format`` is one we can't render
            from WhisperX output.
    """
    if response_format not in ("json", "verbose_json"):
        raise InvalidRequestError(
            f"response_format='{response_format}' is not supported by the "
            "WhisperX backend. Use 'json' (default) or 'verbose_json'.",
            param="response_format",
        )

    if prompt:
        # Keep the value out of the log to avoid leaking client-side
        # content; just record that it was present.
        logger.warning(
            "audio.transcriptions.prompt_unsupported",
            prompt_length=len(prompt),
        )
    if temperature != 0.0:
        logger.warning(
            "audio.transcriptions.temperature_unsupported",
            temperature=temperature,
        )

    num_speakers: int | None = None
    min_speakers: int | None = None
    max_speakers: int | None = None
    if extra_body:
        try:
            extras = json.loads(extra_body)
            if isinstance(extras, dict):
                num_speakers = _coerce_positive_int(extras.get("num_speakers"))
                min_speakers = _coerce_positive_int(extras.get("min_speakers"))
                max_speakers = _coerce_positive_int(extras.get("max_speakers"))
        except json.JSONDecodeError:
            logger.warning("audio.transcriptions.extra_body_invalid_json")

    return (
        WhisperxRequest(
            language=language or "auto",
            num_speakers=num_speakers,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
        ),
        response_format,  # type: ignore[return-value]
    )


def _coerce_positive_int(v: Any) -> int | None:
    """Return ``v`` if it's a positive int, else None.

    WhisperX rejects 0 or negative speaker counts so we drop them silently
    rather than forward and have WhisperX 422.

    R1 finding: ``isinstance(True, int)`` is True in Python (bool is an int
    subclass), so an extra_body of ``{"num_speakers": true}`` would have
    passed through and serialised as the literal string "True", which
    WhisperX then rejects with 422 — exactly the failure mode this
    coercion exists to prevent. Exclude bool explicitly.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, int) and v > 0:
        return v
    return None


def translate_whisperx_response(
    whisperx_result: dict[str, Any],
    *,
    response_shape: WhisperxResponseShape,
    requested_language: str,
) -> dict[str, Any]:
    """Flip a WhisperX response into the OpenAI shape the client asked for.

    Args:
        whisperx_result: The parsed JSON body returned by WhisperX's
            ``POST /transcribe``. Expected shape:
            ``{"segments": [{"start": ..., "end": ..., "text": ...,
            "speaker": ..., "words": [...]}, ...], "language": "..."}``.
        response_shape: Either ``"json"`` (minimal ``{"text": ...}``) or
            ``"verbose_json"`` (full segments + words).
        requested_language: Falls back to this if WhisperX's response
            omits ``language``.

    Returns:
        Dict that can be JSONified to the client.
    """
    segments = whisperx_result.get("segments") or []
    if not isinstance(segments, list):
        # WhisperX returned an unexpected shape — surface as empty text
        # rather than crash; the warning log gives operators a signal.
        logger.warning(
            "audio.transcriptions.whisperx_segments_not_list",
            actual_type=type(segments).__name__,
        )
        segments = []

    detected_language = whisperx_result.get("language") or requested_language

    # WhisperX has NO top-level ``text`` — concat segment texts. Strip
    # each piece and join with a space; the language-detector might
    # leave leading whitespace.
    full_text = " ".join(
        (str(s.get("text") or "")).strip()
        for s in segments
        if isinstance(s, dict) and s.get("text")
    ).strip()

    if response_shape == "json":
        return {"text": full_text}

    # response_shape == "verbose_json": OpenAI's richer shape with
    # ``task``, ``language``, ``duration``, ``text``, ``segments``,
    # ``words``. We pass through WhisperX's speaker info as extra
    # ``speaker`` fields on segments + words (OpenAI clients tolerate
    # unknown fields, and the WhisperX feature is the main reason
    # operators picked it).
    duration = 0.0
    if segments:
        last = segments[-1]
        if isinstance(last, dict):
            duration = float(last.get("end") or 0.0)

    openai_segments: list[dict[str, Any]] = []
    openai_words: list[dict[str, Any]] = []
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continue
        openai_seg: dict[str, Any] = {
            "id": i,
            "seek": 0,
            "start": float(seg.get("start") or 0.0),
            "end": float(seg.get("end") or 0.0),
            "text": str(seg.get("text") or "").strip(),
            "tokens": [],
            "temperature": 0.0,
            "avg_logprob": 0.0,
            "compression_ratio": 0.0,
            "no_speech_prob": 0.0,
        }
        if "speaker" in seg:
            openai_seg["speaker"] = seg["speaker"]
        openai_segments.append(openai_seg)

        for w in seg.get("words") or []:
            if not isinstance(w, dict):
                continue
            entry: dict[str, Any] = {
                "word": str(w.get("word") or ""),
                "start": float(w.get("start") or 0.0),
                "end": float(w.get("end") or 0.0),
            }
            if "speaker" in w:
                entry["speaker"] = w["speaker"]
            openai_words.append(entry)

    return {
        "task": "transcribe",
        "language": detected_language,
        "duration": duration,
        "text": full_text,
        "segments": openai_segments,
        "words": openai_words,
    }
