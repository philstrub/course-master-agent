"""The `api` driver: one cloud model call, provider-agnostic.

Provider SDKs are imported lazily *inside* the call so mitsync imports cleanly
with none of them installed. This module is the only place they may appear.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from ..errors import JudgeUnavailable
from ..logging import get_logger
from .base import JudgeTask, ResultValidationError, validate_result

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

log = get_logger(__name__)

_SYSTEM = (
    "You are a precise filing assistant for a student's course materials. "
    "You answer only with JSON that validates against the provided schema. "
    "No prose, no explanation, no markdown fence."
)


def _build_prompt(task: JudgeTask, extra: str = "") -> str:
    parts = [
        "## Task",
        task.instructions,
    ]
    if task.rules:
        parts += ["", "## Filing rules (authoritative)", task.rules]
    parts += [
        "",
        "## Output JSON schema",
        json.dumps(task.schema_, indent=2),
        "",
        "## Input payload",
        json.dumps(task.payload, indent=2, default=str),
    ]
    if extra:
        parts += ["", "## Your previous answer was rejected", extra, "Return corrected JSON."]
    return "\n".join(parts)


def _extract_json(text: str) -> Any:
    """Parse a model reply, tolerating a stray markdown fence."""
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


class ApiJudge:
    """Calls the configured provider once, retrying once on schema failure."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cfg = settings.llm
        if not self.cfg.api_key:
            raise JudgeUnavailable(
                f"no API key: set ${self.cfg.api_key_env} in your environment, or run with "
                f"--driver agent (no key needed) or --driver rules (heuristics only)."
            )

    def judge(self, task: JudgeTask) -> dict[str, Any]:
        prompt = _build_prompt(task)
        for attempt in (1, 2):
            reply = self._call(prompt)
            try:
                return validate_result(task, _extract_json(reply))
            except (ResultValidationError, json.JSONDecodeError) as exc:
                if attempt == 2:
                    raise
                log.warning("model reply rejected, retrying once: %s", exc)
                prompt = _build_prompt(task, extra=str(exc))
        raise AssertionError("unreachable")

    # --- providers ---
    def _call(self, prompt: str) -> str:
        provider = self.cfg.provider
        if provider == "anthropic":
            return self._call_anthropic(prompt)
        if provider in ("openai", "openai_compatible"):
            return self._call_openai(prompt)
        if provider == "google":
            return self._call_google(prompt)
        raise JudgeUnavailable(f"unsupported provider {provider!r}")

    def _call_anthropic(self, prompt: str) -> str:
        try:
            import anthropic
        except ImportError as exc:
            raise JudgeUnavailable(
                "the anthropic SDK is not installed; run `uv sync --extra anthropic`"
            ) from exc
        client = anthropic.Anthropic(api_key=self.cfg.api_key, base_url=self.cfg.base_url)
        msg = client.messages.create(
            model=self.cfg.model,
            max_tokens=self.cfg.max_output_tokens,
            temperature=self.cfg.temperature,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in msg.content if block.type == "text")

    def _call_openai(self, prompt: str) -> str:
        try:
            import openai
        except ImportError as exc:
            raise JudgeUnavailable(
                "the openai SDK is not installed; run `uv sync --extra openai`"
            ) from exc
        if self.cfg.provider == "openai_compatible" and not self.cfg.base_url:
            raise JudgeUnavailable("provider openai_compatible requires llm.base_url in settings")
        client = openai.OpenAI(api_key=self.cfg.api_key, base_url=self.cfg.base_url)
        resp = client.chat.completions.create(
            model=self.cfg.model,
            max_tokens=self.cfg.max_output_tokens,
            temperature=self.cfg.temperature,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
        )
        return resp.choices[0].message.content or ""

    def _call_google(self, prompt: str) -> str:
        try:
            import google.generativeai as genai
        except ImportError as exc:
            raise JudgeUnavailable(
                "google-generativeai is not installed; run `uv pip install google-generativeai`"
            ) from exc
        genai.configure(api_key=self.cfg.api_key)
        model = genai.GenerativeModel(self.cfg.model, system_instruction=_SYSTEM)
        resp = model.generate_content(
            prompt,
            generation_config={
                "temperature": self.cfg.temperature,
                "max_output_tokens": self.cfg.max_output_tokens,
                "response_mime_type": "application/json",
            },
        )
        return resp.text or ""
