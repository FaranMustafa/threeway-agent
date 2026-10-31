"""Score a planner against labeled AP scenarios.

Money metrics matter more than accuracy here: an overpayment leaves the company, while an
underpayment costs a vendor dispute. Both are reported in dollars.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from threeway.agent.planners import Planner
from threeway.agent.runner import investigate
from threeway.db import Store
from threeway.models import fmt
from threeway.seed import seed

LABELS = Path(__file__).resolve().parents[2] / "evals" / "labels.json"


@dataclass
class Row:
    invoice: str
    scenario: str
    expected: str
    got: str
    expected_cents: int
    got_cents: int
    expected_status: str
    status: str
    turns: int
    case_id: str

    @property
    def correct(self) -> bool:
        return self.expected == self.got and self.expected_cents == self.got_cents


def run(planner: Planner, labels_path: Path = LABELS) -> dict:
    labels = json.loads(labels_path.read_text())
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "eval.db")
        seed(store)
        rows, usage = [], {}
        for label in labels:
            outcome = investigate(store, label["invoice"], planner)
            for key, value in outcome.usage.items():
                if isinstance(value, int | float) and key != "turns":
                    usage[key] = round(usage.get(key, 0) + value, 4)
            rows.append(
                Row(
                    invoice=label["invoice"],
                    scenario=label["scenario"],
                    expected=label["decision"],
                    got=outcome.resolution.decision.value,
                    expected_cents=label["amount_cents"],
                    got_cents=outcome.resolution.approved_amount_cents,
                    expected_status=label["status"],
                    status=outcome.status.value,
                    turns=outcome.turns,
                    case_id=outcome.case_id,
                )
            )

    over = sum(max(r.got_cents - r.expected_cents, 0) for r in rows)
    under = sum(max(r.expected_cents - r.got_cents, 0) for r in rows if r.got != "escalate")
    return {
        "planner": planner.name,
        "cases": len(rows),
        "correct": sum(r.correct for r in rows),
        "routed_correctly": sum(r.status == r.expected_status for r in rows),
        "overpaid_cents": over,
        "underpaid_cents": under,
        "wrong_escalations": sum(r.got == "escalate" and r.expected != "escalate" for r in rows),
        "usage": usage,
        "rows": [asdict(r) | {"correct": r.correct} for r in rows],
    }


def render(report: dict) -> str:
    out = [
        f"planner: {report['planner']}",
        f"{'invoice':<9} {'ok':<3} {'expected':<26} {'got':<26} {'status':<15} scenario",
    ]
    for r in report["rows"]:
        exp = f"{r['expected']} {fmt(r['expected_cents'])}"
        got = f"{r['got']} {fmt(r['got_cents'])}"
        mark = "✓" if r["correct"] else "✗"
        out.append(
            f"{r['invoice']:<9} {mark:<3} {exp:<26} {got:<26} {r['status']:<15} {r['scenario']}"
        )
    out.append(
        f"\ncorrect {report['correct']}/{report['cases']} · routed correctly "
        f"{report['routed_correctly']}/{report['cases']} · overpaid "
        f"{fmt(report['overpaid_cents'])} · underpaid {fmt(report['underpaid_cents'])} · "
        f"needless escalations {report['wrong_escalations']}"
    )
    if report["usage"].get("cost_usd") is not None:
        out.append(
            f"tokens in {report['usage'].get('input_tokens', 0):,} / out "
            f"{report['usage'].get('output_tokens', 0):,} · cost ${report['usage']['cost_usd']:.2f}"
        )
    return "\n".join(out)
