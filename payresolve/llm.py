import logging
from dataclasses import dataclass

from ollama import Client

from payresolve.config import OLLAMA_HOST, OLLAMA_MODEL

logger = logging.getLogger(__name__)


@dataclass
class LLMResult:
    text: str | None
    used: bool
    error: str | None = None


def generate(prompt: str) -> LLMResult:
    try:
        client = Client(host=OLLAMA_HOST, timeout=60)
        response = client.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": (
                    "You assist a payment support team using only supplied records. "
                    "Never invent transaction facts, claim money moved, or state that a refund was initiated/completed "
                    "unless the supplied records explicitly confirm it. Clearly identify uncertainty. "
                    "Return concise plain text only."
                )},
                {"role": "user", "content": prompt},
            ],
        )
        text = response.get("message", {}).get("content", "").strip()
        if not text:
            raise ValueError("Ollama returned an empty response")
        return LLMResult(text=text, used=True)
    except Exception as exc:
        logger.warning("Ollama inference unavailable for model %s: %s", OLLAMA_MODEL, exc)
        return LLMResult(text=None, used=False, error=str(exc))
