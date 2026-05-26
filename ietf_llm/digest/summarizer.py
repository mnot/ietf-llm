"""Optional LLM-backed summariser used by the issues and threads digests.

Wraps Simon Willison's `llm` package so users can pick any provider it
knows about. When generation fails (most commonly: API key missing or
provider plugin not installed), the summariser surfaces a multi-line
setup guide on the first failure and then disables itself so a big WG
doesn't get the same wall of text 500 times.
"""

from __future__ import annotations

from typing import Optional

from ..utils import LogLevel, Verbosity, log


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


class _Summarizer:
    """Wraps the `llm` package. No-op if generation fails."""

    def __init__(self, model_name: Optional[str], verbose: Verbosity):
        self.model = None
        self.model_name = model_name
        self.verbose = verbose
        self._warned = False
        if not model_name:
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
            log(_llm_setup_help(model_name, str(err)), verbose, level=LogLevel.ERROR)

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
            return text
        except Exception as err:  # pylint: disable=broad-except
            # Most common case: model loaded but API key missing/invalid.
            # Surface the full setup help on the first failure, then
            # disable the summariser so subsequent chunks don't repeat
            # the error or rack up zero-value API attempts.
            if not self._warned:
                log(
                    _llm_setup_help(self.model_name or "(unknown)", str(err)),
                    self.verbose,
                    level=LogLevel.ERROR,
                )
                self._warned = True
                self.model = None
            return ""
