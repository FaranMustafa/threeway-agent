"""ClaudePlanner request shape, checked against a fake client (no network)."""

from types import SimpleNamespace as NS

from threeway.agent.planners import ClaudePlanner, estimate_cost_usd
from threeway.agent.runner import investigate
from threeway.agent.tools import TOOL_DEFINITIONS
from threeway.models import CaseStatus


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def response(blocks, stop="tool_use"):
    usage = NS(
        input_tokens=1000,
        output_tokens=200,
        cache_read_input_tokens=500,
        cache_creation_input_tokens=0,
    )
    return NS(content=blocks, stop_reason=stop, usage=usage)


def tool_use(id_, name, input_):
    return NS(type="tool_use", id=id_, name=name, input=input_)


def test_request_shape_and_loop(store):
    thinking = NS(type="thinking", thinking="", signature="sig")
    msgs = FakeMessages(
        [
            response([thinking, tool_use("t1", "get_vendor_profile", {"vendor_id": "V100"})]),
            response(
                [
                    NS(type="text", text="Freight is prepaid per terms."),
                    tool_use(
                        "s1",
                        "submit_resolution",
                        {
                            "decision": "approve_adjusted",
                            "approved_amount_cents": 30000,
                            "adjustments": [
                                {"sku": "FRT", "reason": "prepaid", "amount_cents": 8500}
                            ],
                            "rationale": "terms say freight prepaid",
                            "evidence": ["t1"],
                            "vendor_message": "Freight removed per FOB destination terms.",
                        },
                    ),
                ]
            ),
        ]
    )
    planner = ClaudePlanner(client=NS(beta=NS(messages=msgs)))
    out = investigate(store, "INV-1006", planner)

    assert out.status == CaseStatus.AUTO_RESOLVED
    assert out.resolution.approved_amount_cents == 30000
    first = msgs.calls[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default"
    assert first["betas"] == ["server-side-fallback-2026-07-01"]
    assert "tool_choice" not in first  # forced tool use is a 400 on Opus 5.5
    assert all(t["strict"] for t in first["tools"]) and first["tools"] == TOOL_DEFINITIONS
    # History is append-only: the thinking block goes back exactly as received.
    second_history = msgs.calls[1]["messages"]
    assert second_history[1]["content"][0] is thinking
    assert out.usage["cost_usd"] == estimate_cost_usd("claude-opus-5-5", out.usage)


def test_cost_estimate():
    usage = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 0}
    assert estimate_cost_usd("claude-opus-5-5", usage) == 6.0
    assert estimate_cost_usd("unknown", usage) is None
