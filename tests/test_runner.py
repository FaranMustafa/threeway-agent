"""Agent loop behaviour, driven by scripted planners so no API key is needed."""

from threeway.agent.planners import ToolCall, Turn
from threeway.agent.runner import investigate
from threeway.models import CaseStatus, Decision


class Scripted:
    """Plays back a list of turns; each item is a list of (id, name, input) or a Turn."""

    name = "scripted"

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen: list[list[dict]] = []

    def next(self, system, messages, tools, context):
        self.seen.append(list(messages))
        item = self.turns.pop(0)
        if isinstance(item, Turn):
            return item
        calls = [ToolCall(i, n, a) for i, n, a in item]
        content = [
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls
        ]
        return Turn(content, calls, "", "tool_use", {"input_tokens": 10, "output_tokens": 5})


def submit(decision, cents, evidence, adjustments=()):
    return {
        "decision": decision,
        "approved_amount_cents": cents,
        "adjustments": list(adjustments),
        "rationale": "because",
        "evidence": evidence,
        "vendor_message": None,
    }


def test_clean_invoice_skips_the_agent(store):
    out = investigate(store, "INV-1001", Scripted([]))
    assert out.status == CaseStatus.AUTO_RESOLVED and out.turns == 0


def test_agent_self_corrects_after_policy_rejection(store):
    adj = [{"sku": "P-500", "reason": "6 damaged", "amount_cents": 18000}]
    planner = Scripted(
        [
            [("t1", "get_receiving_log", {"po_number": "PO-5005"})],
            [("s1", "submit_resolution", submit("approve_adjusted", 130000, ["t1"], adj))],
            [("s2", "submit_resolution", submit("approve_adjusted", 132000, ["t1"], adj))],
        ]
    )
    out = investigate(store, "INV-1005", planner)
    assert out.resolution.approved_amount_cents == 132000
    assert out.status == CaseStatus.AUTO_RESOLVED
    rejection = planner.seen[2][-1]["content"][0]
    assert rejection["is_error"] and "must equal" in rejection["content"]
    case = store.case(out.case_id)
    assert [e["event"] for e in case["trace"]].count("policy_rejection") == 1


def test_parallel_tool_results_come_back_in_one_message(store):
    planner = Scripted(
        [
            [
                ("a", "get_purchase_order", {"po_number": "PO-5002"}),
                (
                    "b",
                    "get_contract_price",
                    {"vendor_id": "V100", "sku": "W-110", "as_of": "2026-09-10"},
                ),
            ],
            [("s", "submit_resolution", submit("approve", 86000, ["a", "b"]))],
        ]
    )
    out = investigate(store, "INV-1002", planner)
    results = planner.seen[1][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
    assert '"unit_price_cents": 860' in results[1]["content"]
    assert out.status == CaseStatus.AUTO_RESOLVED


def test_tool_errors_are_returned_not_raised(store):
    planner = Scripted(
        [
            [("x", "get_purchase_order", {"po_number": "PO-9999"})],
            [("y", "get_contract_price", {"vendor_id": "V1", "sku": "A", "as_of": "soon"})],
            [("s", "submit_resolution", submit("escalate", 0, []))],
        ]
    )
    out = investigate(store, "INV-1010", planner)
    assert planner.seen[1][-1]["content"][0]["is_error"]
    assert "YYYY-MM-DD" in planner.seen[2][-1]["content"][0]["content"]
    assert out.status == CaseStatus.ESCALATED


def test_stopping_without_submitting_is_nudged_then_escalated(store):
    talk = Turn([{"type": "text", "text": "hmm"}], [], "hmm", "end_turn")
    out = investigate(store, "INV-1003", Scripted([talk, talk]))
    assert out.resolution.decision == Decision.ESCALATE
    assert "without submitting" in out.resolution.rationale


def test_refusal_escalates(store):
    out = investigate(store, "INV-1003", Scripted([Turn([], [], "", "refusal")]))
    assert out.status == CaseStatus.ESCALATED


def test_turn_budget(store):
    loop = [[(f"t{i}", "get_vendor_profile", {"vendor_id": "V200"})] for i in range(3)]
    out = investigate(store, "INV-1003", Scripted(loop), max_turns=3)
    assert "no resolution within 3 turns" in out.resolution.rationale


def test_repeated_policy_failures_escalate(store):
    bad = [[(f"s{i}", "submit_resolution", submit("approve", 1, ["nope"]))] for i in range(3)]
    out = investigate(store, "INV-1003", Scripted(bad))
    assert out.status == CaseStatus.ESCALATED
    assert "repeatedly" in out.resolution.rationale


def test_usage_is_accumulated(store):
    planner = Scripted(
        [
            [("t", "get_invoice", {"invoice_number": "INV-1003"})],
            [
                (
                    "s",
                    "submit_resolution",
                    submit(
                        "approve_adjusted",
                        100000,
                        ["t"],
                        [{"sku": "G-300", "reason": "PO price", "amount_cents": 6000}],
                    ),
                )
            ],
        ]
    )
    out = investigate(store, "INV-1003", planner)
    assert out.usage["input_tokens"] == 20 and out.usage["turns"] == 2
