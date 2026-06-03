"""Remote OpenAI-compatible summariser backend.

Mirrors the remote embedding backend: when ``--summarize-model`` carries
the ``openai-summarize/`` prefix, summaries go to an OpenAI-compatible
``POST {base}/chat/completions`` endpoint configured from the
environment (``IETF_LLM_SUMMARIZE_BASE_URL`` / ``_TOKEN`` / ``_HEADERS``),
so a deployment configures both AI touch-points the same way and no
secret is persisted. A model id without the prefix is handled by the
``llm`` library as before.
"""

from __future__ import annotations

import os
from typing import Optional

from .. import oai_compat
from ..utils import LogLevel, Verbosity, log

#: Protocol-neutral prefix selecting the OpenAI-compatible chat backend,
#: parallel to embeddings' ``openai-embed/``. The id after the prefix is
#: the model name sent to the endpoint, e.g.
#: "openai-summarize/@cf/meta/llama-3.1-8b-instruct".
OPENAI_SUMMARIZE_PREFIX = "openai-summarize/"


def is_remote_summarize_model(model_name: str) -> bool:
    """True if ``model_name`` selects the remote OpenAI-compatible backend."""
    return model_name.startswith(OPENAI_SUMMARIZE_PREFIX)


class _ChatResponse:
    """Minimal stand-in for an ``llm`` response: exposes ``.text()``."""

    def __init__(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text


class _OpenAICompatChatModel:
    """Summaries via an OpenAI-compatible ``POST {base}/chat/completions``.

    Exposes the ``.prompt(text).text()`` surface the summariser already
    expects from an ``llm`` model, so it drops in behind ``_Summarizer``
    with no change to the call site. Provider-neutral: endpoint, auth
    headers, and model id are configuration, so identical code serves
    Cloudflare Workers AI, OpenAI, a self-hosted vLLM, etc.
    """

    def __init__(
        self,
        model_id: str,
        base_url: str,
        headers: dict[str, str],
        *,
        timeout: float,
        max_retries: int,
    ) -> None:
        self._model_id = model_id
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Content-Type": "application/json", **headers}
        self._timeout = timeout
        self._max_retries = max_retries

    def prompt(self, text: str) -> _ChatResponse:
        payload = {
            "model": self._model_id,
            "messages": [{"role": "user", "content": text}],
        }
        body = oai_compat.post_json_with_retry(
            self._url,
            payload,
            self._headers,
            timeout=self._timeout,
            max_retries=self._max_retries,
        )
        choices = body.get("choices") or []
        if not choices:
            return _ChatResponse("")
        content = (choices[0].get("message") or {}).get("content") or ""
        return _ChatResponse(str(content))


def load_openai_compat_chat(
    model_name: str, verbose: Verbosity
) -> Optional[_OpenAICompatChatModel]:
    """Build the remote summariser from the environment, or None.

    The model id is the part after the prefix; the endpoint, auth, and
    tuning come from the environment so secrets never live in code or
    persisted config. Returns None (with a logged reason) when the
    endpoint isn't configured, so the summariser stays inactive rather
    than failing the gather.
    """
    model_id = model_name[len(OPENAI_SUMMARIZE_PREFIX) :]
    base_url = os.environ.get("IETF_LLM_SUMMARIZE_BASE_URL", "").strip()
    if not base_url:
        log(
            "Remote summariser configured but IETF_LLM_SUMMARIZE_BASE_URL is "
            "not set. Set the chat-completions endpoint base URL (e.g. "
            "https://host/v1) in the environment.",
            verbose,
            level=LogLevel.ERROR,
        )
        return None
    headers = oai_compat.build_headers(
        os.environ.get("IETF_LLM_SUMMARIZE_TOKEN", ""),
        os.environ.get("IETF_LLM_SUMMARIZE_HEADERS", ""),
        "IETF_LLM_SUMMARIZE_HEADERS",
        verbose,
    )
    return _OpenAICompatChatModel(
        model_id,
        base_url,
        headers,
        timeout=oai_compat.env_float("IETF_LLM_SUMMARIZE_TIMEOUT", 30.0),
        max_retries=oai_compat.env_int("IETF_LLM_SUMMARIZE_RETRIES", 3),
    )
