"""Planners decide the agent's next move. The runner is the same for all of them.

- ClaudePlanner: Claude (Opus 5.5) with tool use.
- BaselinePlanner: the fixed rules most AP automation ships with (pay the lesser of
  ordered/received at the PO price, escalate anything off-PO). It needs no API key, runs in
  CI, and gives the eval a baseline to beat.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

MODEL = os.environ.get("THREEWAY_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("THREEWAY_EFFORT", "high")
# Opus 5.5 list price, USD per million tokens (input, output, cache read).
PRICES = {"claude-opus-5-5": (4.00, 20.00, 0.20)}


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Turn:
    content: list[Any]  # appended to history unchanged (keeps thinking blocks valid)
    tool_calls: list[ToolCall]
    text: str
    stop_reason: str
    usage: dict[str, int] = field(default_factory=dict)


class Planner(Protocol):
    name: str

    def next(self, system: str, messages: list[dict], tools: list[dict], context: dict) -> Turn: ...


class ClaudePlanner:
    name = "claude"

    def __init__(self, client=None, model: str = MODEL, effort: str = EFFORT):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.effort = effort

    def next(self, system, messages, tools, context) -> Turn:
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=system,
            tools=tools,
            messages=messages,
            output_config={"effort": self.effort},
            # Cache the stable prefix (tools + system + earlier turns) across loop iterations.
            cache_control={"type": "ephemeral"},
            # On a safety-classifier decline, retry server-side on the recommended model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        calls = [
            ToolCall(b.id, b.name, dict(b.input)) for b in response.content if b.type == "tool_use"
        ]
        text = "\n".join(b.text for b in response.content if b.type == "text")
        u = response.usage
        return Turn(
            content=response.content,
            tool_calls=calls,
            text=text,
            stop_reason=response.stop_reason or "",
            usage={
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
                "cache_read_input_tokens": u.cache_read_input_tokens or 0,
                "cache_creation_input_tokens": u.cache_creation_input_tokens or 0,
            },
        )


def estimate_cost_usd(model: str, usage: dict[str, int]) -> float | None:
    if model not in PRICES:
        return None
    p_in, p_out, p_cache = PRICES[model]
    return round(
        (
            usage.get("input_tokens", 0) * p_in
            + usage.get("cache_creation_input_tokens", 0) * p_in * 1.25
            + usage.get("cache_read_input_tokens", 0) * p_cache
            + usage.get("output_tokens", 0) * p_out
        )
        / 1_000_000,
        4,
    )


def _block(kind: str, **fields) -> dict:
    return {"type": kind, **fields}


class BaselinePlanner:
    """Rules-only AP clerk. Three turns: read invoice, gather in parallel, decide."""

    name = "baseline"

    def next(self, system, messages, tools, context) -> Turn:
        step = sum(1 for m in messages if m["role"] == "assistant")
        results = self._results(messages)
        if step == 0:
            return self._calls([("get_invoice", {"invoice_number": context["invoice_number"]})])
        invoice = json.loads(results["get_invoice"][1])
        if step == 1:
            po, vendor = invoice["po_number"], invoice["vendor_id"]
            return self._calls(
                [
                    ("get_purchase_order", {"po_number": po}),
                    ("get_receiving_log", {"po_number": po}),
                    ("find_related_invoices", {"vendor_id": vendor, "po_number": po}),
                ]
            )
        return self._calls([("submit_resolution", self._decide(invoice, results))])

    @staticmethod
    def _results(messages) -> dict[str, tuple[str, str]]:
        """Map tool name -> (tool_use_id, output) from the conversation so far."""
        names: dict[str, str] = {}
        out: dict[str, tuple[str, str]] = {}
        for m in messages:
            for block in m["content"] if isinstance(m["content"], list) else []:
                if block.get("type") == "tool_use":
                    names[block["id"]] = block["name"]
                elif block.get("type") == "tool_result":
                    out[names[block["tool_use_id"]]] = (block["tool_use_id"], block["content"])
        return out

    def _decide(self, invoice: dict, results: dict) -> dict:
        evidence = [tid for tid, _ in results.values()]
        if any(not out.lstrip().startswith(("{", "[")) for _, out in results.values()):
            return self._resolution("escalate", 0, [], "a lookup failed", evidence)
        po = json.loads(results["get_purchase_order"][1])
        received = json.loads(results["get_receiving_log"][1])["totals"]
        related = json.loads(results["find_related_invoices"][1])
        line_total = invoice["line_total_cents"]

        for other in related:
            if (
                other["invoice_number"] != invoice["invoice_number"]
                and other["stated_total_cents"] == invoice["stated_total_cents"]
                and (other["invoice_date"], other["invoice_number"])
                < (invoice["invoice_date"], invoice["invoice_number"])
            ):
                return self._resolution(
                    "reject_duplicate", 0, [], f"same total as {other['invoice_number']}", evidence
                )

        po_lines = {line["sku"]: line for line in po["lines"]}
        if any(line["sku"] not in po_lines for line in invoice["lines"]):
            return self._resolution("escalate", 0, [], "invoice has items not on the PO", evidence)

        adjustments = []
        for line in invoice["lines"]:
            po_line = po_lines[line["sku"]]
            qty = min(line["qty"], po_line["qty"], received.get(line["sku"], 0))
            price = min(line["unit_price_cents"], po_line["unit_price_cents"])
            short = line["qty"] * line["unit_price_cents"] - qty * price
            if short:
                adjustments.append(
                    {
                        "sku": line["sku"],
                        "reason": "pay lesser of PO/received",
                        "amount_cents": short,
                    }
                )
        if not adjustments:
            return self._resolution("approve", line_total, [], "matches PO and receipt", evidence)
        deducted = sum(a["amount_cents"] for a in adjustments)
        return self._resolution(
            "approve_adjusted", line_total - deducted, adjustments, "short-pay to PO", evidence
        )

    @staticmethod
    def _resolution(decision, amount, adjustments, rationale, evidence) -> dict:
        return {
            "decision": decision,
            "approved_amount_cents": amount,
            "adjustments": adjustments,
            "rationale": rationale,
            "evidence": evidence,
            "vendor_message": None,
        }

    @staticmethod
    def _calls(calls: list[tuple[str, dict]]) -> Turn:
        tool_calls = [ToolCall(f"base_{name}", name, args) for name, args in calls]
        content = [_block("tool_use", id=c.id, name=c.name, input=c.input) for c in tool_calls]
        return Turn(content=content, tool_calls=tool_calls, text="", stop_reason="tool_use")
