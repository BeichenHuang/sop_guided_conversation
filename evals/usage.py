"""Token counts and estimated cost for evaluation runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

# US dollars per million tokens (input, output): OpenAI's pricing page in September 2026, and
# Anthropic's list prices. Cached-input discounts are ignored, so estimates are an upper bound.
# Models not listed show no cost estimate.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-6-luna": (0.10, 0.50),
    "gpt-6-sol": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, usage: Any) -> None:
        self.calls += 1
        if usage is None:
            return
        self.input_tokens += usage.prompt_tokens or 0
        self.output_tokens += usage.completion_tokens or 0
        details = getattr(usage, "completion_tokens_details", None)
        self.reasoning_tokens += (getattr(details, "reasoning_tokens", None) or 0) if details else 0

    def record(self, input_tokens: int, output_tokens: int, reasoning_tokens: int) -> None:
        """One model call's tokens, as an adapter reports them (``on_usage``)."""
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.reasoning_tokens += reasoning_tokens

    def merge(self, other: Usage) -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_tokens += other.reasoning_tokens

    def cost(self, model: str) -> float | None:
        if model not in PRICES:
            return None
        per_input, per_output = PRICES[model]
        return (self.input_tokens * per_input + self.output_tokens * per_output) / 1_000_000

    def to_dict(self) -> dict[str, int]:
        return asdict(self)
