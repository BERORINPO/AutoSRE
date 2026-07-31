"""Per-run accounting of the agent's own LLM usage.

An on-call agent that cannot say what a diagnosis cost is not observable, it is
just autonomous. The token counts here are read from what the model actually
reported for this run; the dollar figure is DERIVED from a published list price
below, and is labelled as such everywhere it surfaces. We do not have access to
a billed amount at runtime, and presenting a derived number as a measured one
would be the exact dishonesty this instrumentation exists to remove.

Deliberately stdlib-only and free of any ADK/genai import, like agents.limits,
so the offline smoke gate can test the arithmetic without the SDK installed.
Nothing here may raise: accounting is decoration on top of the run, and an
incident response must never fail because its own meter broke.
"""

# Published list price, USD per 1M tokens, as (input, output).
#
# Output covers Gemini 2.5's thinking tokens too - they bill at the output rate,
# which is why _observe folds thoughts_token_count into output rather than
# reporting it as a third free category.
#
# This is a static table on purpose: a price lookup at diagnosis time would add a
# network dependency to the critical path to make a cosmetic number prettier.
# Re-check it when the as-of date gets stale.
PRICING_ASOF = "2026-07"
_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

# What the usd figure is, stated in the payload itself so no downstream renderer
# has to remember. The console prints it; the slide can quote it verbatim.
USD_BASIS = "derived from published list price, not a billed amount"


def price_for(model: str) -> tuple[float, float] | None:
    """Return (input, output) USD per 1M tokens for a model id, or None.

    Longest-prefix match, so a pinned id like "gemini-2.5-flash-002" prices as
    flash, while "gemini-2.5-flash-lite" does not silently price as the more
    expensive flash. Returning None is a first-class outcome: an unpriced model
    reports tokens and no dollars, which is honest, rather than dollars computed
    from the wrong row.
    """
    if not model:
        return None
    best: tuple[float, float] | None = None
    best_len = -1
    for prefix, price in _USD_PER_MTOK.items():
        if model.startswith(prefix) and len(prefix) > best_len:
            best, best_len = price, len(prefix)
    return best


def _int(value: object) -> int:
    """Coerce an SDK counter to a non-negative int, tolerating None/junk."""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


class RunCost:
    """Accumulates token usage across the LLM calls of one incident run."""

    def __init__(self, model: str = "") -> None:
        self.model = model or ""
        self.llm_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.thought_tokens = 0
        self.reported_total_tokens = 0

    def observe(self, event: object) -> None:
        """Fold one ADK event's usage_metadata into the totals. Never raises.

        Events without usage carry no cost signal and are skipped, so this is
        safe to call on every event in the loop.
        """
        try:
            self._observe(event)
        except Exception:  # noqa: BLE001 - a meter must not break the run
            return

    def _observe(self, event: object) -> None:
        # Streaming partials repeat cumulative usage on every chunk; counting
        # them would inflate the total by the number of chunks. RunConfig
        # currently leaves streaming off, so this is a guard against a future
        # change flipping it on and silently corrupting the number.
        if getattr(event, "partial", False):
            return

        usage = getattr(event, "usage_metadata", None)
        if usage is None:
            return

        prompt = _int(getattr(usage, "prompt_token_count", 0))
        candidates = _int(getattr(usage, "candidates_token_count", 0))
        thoughts = _int(getattr(usage, "thoughts_token_count", 0))
        total = _int(getattr(usage, "total_token_count", 0))

        # An event whose usage is present but all-zero is not an LLM call.
        if not (prompt or candidates or thoughts or total):
            return

        self.llm_calls += 1
        self.input_tokens += prompt
        self.output_tokens += candidates + thoughts
        self.thought_tokens += thoughts
        self.reported_total_tokens += total

    def usd(self) -> float | None:
        """Derived cost, or None when the model is not in the price table."""
        price = price_for(self.model)
        if price is None:
            return None
        in_rate, out_rate = price
        return (self.input_tokens * in_rate + self.output_tokens * out_rate) / 1_000_000

    def snapshot(self) -> dict:
        """Serialisable totals for the terminal event and the structured log.

        `accounted` is the honesty flag: it is False when our own sum of the
        parts disagrees with the model's reported total, which is the signature
        of double counting or of a usage field we do not know about. A renderer
        can show the number and still say it does not reconcile, instead of
        quietly presenting a figure we cannot stand behind.

        `accounted` alone cannot be read as "reconciled", though: it is also True
        when the SDK reported no total at all (reported_total_tokens == 0), where
        there was simply nothing to reconcile against. `reported_total_tokens` is
        therefore emitted raw - 0 means "unverified", non-zero and equal to
        input+output means actually cross-checked. Without it the two cases are
        indistinguishable downstream, because total_tokens below already falls
        back to our own sum.
        """
        usd = self.usd()
        summed = self.input_tokens + self.output_tokens
        return {
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thought_tokens": self.thought_tokens,
            "total_tokens": self.reported_total_tokens or summed,
            # 0 = the SDK reported no total, so `accounted` below is vacuous
            "reported_total_tokens": self.reported_total_tokens,
            "model": self.model,
            "priced": usd is not None,
            "usd": round(usd, 6) if usd is not None else None,
            "pricing_asof": PRICING_ASOF,
            "usd_basis": USD_BASIS,
            "accounted": self.reported_total_tokens == 0 or self.reported_total_tokens == summed,
        }
