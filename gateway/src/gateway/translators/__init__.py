"""Schema translators between OpenAI-shaped client requests and EMS-managed
upstream services that don't speak OpenAI natively (PR #13).

LLM (vLLM via LiteLLM) and TTS (Kokoro) already expose the OpenAI shape so
those routes are pure passthroughs in :mod:`gateway.routers.audio` /
:mod:`gateway.routers.chat`. Only WhisperX needs translation:

* OpenAI: ``POST /v1/audio/transcriptions`` (multipart, ``model``, ``file``,
  ``language``, ``response_format``, ``temperature``, ``prompt``,
  ``timestamp_granularities[]``)
* WhisperX: ``POST /transcribe`` (multipart, ``file``, ``language``,
  ``num_speakers``, ``min_speakers``, ``max_speakers``); no ``model``,
  no ``response_format``, no ``temperature``, no ``prompt``.

The translator handles both directions: request (OpenAI → WhisperX) and
response (WhisperX → OpenAI verbose_json or simple text).
"""

from gateway.translators.whisperx import (
    WhisperxResponseShape,
    build_whisperx_request,
    translate_whisperx_response,
)

__all__ = [
    "WhisperxResponseShape",
    "build_whisperx_request",
    "translate_whisperx_response",
]
