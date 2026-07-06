"""Optional LLM-backed summariser used by the issues and threads digests.

Wraps Simon Willison's `llm` package so users can pick any provider it
knows about. When generation fails (most commonly: API key missing or
provider plugin not installed), the summariser surfaces a multi-line
setup guide on the first failure and then disables itself so a big WG
doesn't get the same wall of text 500 times.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from ..log import LogLevel, Verbosity, log
from .remote_summarizer import is_remote_summarize_model, load_openai_compat_chat

#: Heartbeat cadence for the summarise progress pulse (wall-clock seconds).
#: Summarisation makes one model call per issue/thread — slow on a remote
#: endpoint — so without this a large WG shows a long silence between
#: "Generating digests..." and the first "Wrote ... digest" line.
_PROGRESS_SECS = 20.0


def _llm_setup_help(model_name: str, underlying_error: str) -> str:
    """Compose a multi-line help message for an --summarize misconfiguration.

    Written for users who installed ietf-llm via `pipx`, which is the
    main install path. The two simplest paths are:

      1. Inline env var (works for OpenAI out of the box; other providers
         need their `llm-<provider>` plugin injected first).
      2. `pipx inject` a provider plugin, then run with the env var.
    """
    return (
        f"--summarize couldn't use model '{model_name}': {underlying_error}\n"
        "\n"
        "ietf-llm uses Simon Willison's `llm` package for summarisation. "
        "OpenAI is built in; Anthropic, Gemini etc. need their `llm-<provider>` "
        "plugin. Two quick fixes (assuming you installed via pipx):\n"
        "\n"
        "  # OpenAI (built in, no plugin needed):\n"
        "  OPENAI_API_KEY=sk-... ietf-llm <wg> --summarize "
        "--summarize-model gpt-4o-mini\n"
        "\n"
        "  # Anthropic (or substitute llm-gemini, llm-mistral, etc.):\n"
        "  pipx inject ietf-llm llm-anthropic\n"
        "  ANTHROPIC_API_KEY=sk-ant-... ietf-llm <wg> --summarize "
        "--summarize-model claude-haiku-4-5\n"
        "\n"
        "For persistent setup (so you don't have to inline the key each "
        "time) see https://llm.datasette.io/en/stable/setup.html — note "
        "that `llm keys set ...` needs the `llm` binary on your PATH, "
        "which pipx only exposes if you reinstall with `--include-deps`."
    )


def _remote_setup_help(model_name: str, underlying_error: str) -> str:
    """Help message for a failed remote (openai-summarize/) summariser."""
    return (
        f"--summarize couldn't reach the remote summariser '{model_name}': "
        f"{underlying_error}\n"
        "\n"
        "The 'openai-summarize/' prefix routes summaries to an "
        "OpenAI-compatible chat-completions endpoint configured from the "
        "environment. Check that these are set on the host running gather:\n"
        "\n"
        "  IETF_LLM_SUMMARIZE_BASE_URL   # the /v1 base URL\n"
        "  IETF_LLM_SUMMARIZE_TOKEN      # bearer token (if the endpoint needs one)\n"
        "  IETF_LLM_SUMMARIZE_HEADERS    # extra headers as JSON (optional)\n"
        "\n"
        "and that the endpoint serves the model named after the prefix."
    )


class _Summarizer:
    """Wraps the `llm` package. No-op if generation fails."""

    def __init__(self, model_name: Optional[str], verbose: Verbosity):
        self.model: Any = None
        self.model_name = model_name
        self.verbose = verbose
        self._warned = False
        self._remote = False
        # Progress accounting across every summarise() call, so the one
        # wrapper reports for all digests rather than each loop reporting
        # itself. _started/_last_pulse are set on the first call.
        self._count = 0
        self._started: Optional[float] = None
        self._last_pulse = 0.0
        if not model_name:
            return
        # Remote OpenAI-compatible path: no llm registry, endpoint and
        # secrets from the environment (parallel to the embed backend).
        if is_remote_summarize_model(model_name):
            self._remote = True
            self.model = load_openai_compat_chat(model_name, verbose)
            return
        try:
            import llm  # pylint: disable=import-outside-toplevel,import-error
        except ImportError:
            log(
                "Summarization requested but `llm` package is missing — "
                "this should ship with ietf-llm. Try reinstalling: "
                "pipx install --force ietf-llm",
                verbose,
                level=LogLevel.ERROR,
            )
            return
        try:
            self.model = llm.get_model(model_name)
        except Exception as err:  # pylint: disable=broad-except
            # Most common case: unknown model (provider plugin not installed).
            # llm and its provider plugins don't share a typed exception
            # hierarchy, so we catch broadly.
            log(
                _llm_setup_help(model_name, f"{type(err).__name__}: {err}"),
                verbose,
                level=LogLevel.ERROR,
            )

    def active(self) -> bool:
        return self.model is not None

    def summarize(self, prompt: str, max_chars: int = 8000) -> str:
        """Return a one-line summary, or empty string on failure."""
        if not self.model:
            return ""
        try:
            response = self.model.prompt(prompt[:max_chars])
            text = str(response.text()).strip().replace("\n", " ")
            # Strip surrounding quotes if any
            if len(text) > 2 and text[0] in "\"'" and text[-1] == text[0]:
                text = text[1:-1]
            self._note_progress()
            return text
        except Exception as err:  # pylint: disable=broad-except
            # Most common case: model loaded but API key missing/invalid.
            # Surface the full setup help on the first failure, then
            # disable the summariser so subsequent chunks don't repeat
            # the error or rack up zero-value API attempts.
            if not self._warned:
                log(
                    self._setup_help(f"{type(err).__name__}: {err}"),
                    self.verbose,
                    level=LogLevel.ERROR,
                )
                self._warned = True
                self.model = None
            return ""

    def _note_progress(self) -> None:
        """Count a successful summary and emit a periodic STATUS heartbeat."""
        now = time.time()
        if self._started is None:
            self._started = now
            self._last_pulse = now
        self._count += 1
        if now - self._last_pulse >= _PROGRESS_SECS:
            log(
                f"  …summarised {self._count} items, "
                f"{now - self._started:.0f}s elapsed",
                self.verbose,
                level=LogLevel.STATUS,
            )
            self._last_pulse = now

    def report(self) -> None:
        """Log a final one-line tally. No-op if nothing was summarised."""
        if self._started is None or self._count == 0:
            return
        log(
            f"Summarised {self._count} items in " f"{time.time() - self._started:.1f}s",
            self.verbose,
            level=LogLevel.STATUS,
        )

    def _setup_help(self, underlying_error: str) -> str:
        name = self.model_name or "(unknown)"
        if self._remote:
            return _remote_setup_help(name, underlying_error)
        return _llm_setup_help(name, underlying_error)
