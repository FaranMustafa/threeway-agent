"""The agent loop: plan -> call tools -> validate -> route -> persist a case with a full trace."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from threeway.agent import policy
from threeway.agent.planners import Planner, estimate_cost_usd
from threeway.agent.prompts import SYSTEM_PROMPT, task_message
from threeway.agent.tools import SUBMIT, TOOL_DEFINITIONS, Toolbox, ToolError
from threeway.db import Store
from threeway.matching import match_invoice
from threeway.models import CaseStatus, Decision, Resolution

log = logging.getLogger("threeway.agent")

MAX_TURNS = 12
MAX_REJECTED_SUBMISSIONS = 3
TRACE_OUTPUT_LIMIT = 4000


@dataclass
class CaseOutcome:
    case_id: str
    invoice_number: str
    status: CaseStatus
    resolution: Resolution
    route_reasons: list[str]
    turns: int
    usage: dict[str, Any]


def _escalation(reason: str) -> Resolution:
    return Resolution(decision=Decision.ESCALATE, approved_amount_cents=0, rationale=reason)


def investigate(
    store: Store, invoice_number: str, planner: Planner, max_turns: int = MAX_TURNS
) -> CaseOutcome:
    invoice = store.invoice(invoice_number)
    if invoice is None:
        raise KeyError(f"invoice {invoice_number} not found")
    match = match_invoice(store, invoice)
    exceptions = [e.model_dump(mode="json") for e in match.exceptions]

    if match.clean:
        resolution = Resolution(
            decision=Decision.APPROVE,
            approved_amount_cents=invoice.line_total_cents,
            rationale="clean three-way match",
        )
        case_id = store.create_case(
            invoice_number,
            "rules",
            CaseStatus.AUTO_RESOLVED,
            [],
            resolution.model_dump(mode="json"),
            [],
            {},
        )
        return CaseOutcome(
            case_id, invoice_number, CaseStatus.AUTO_RESOLVED, resolution, ["clean match"], 0, {}
        )

    toolbox = Toolbox(store)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": task_message(invoice_number, exceptions)}
    ]
    trace: list[dict[str, Any]] = [{"event": "exceptions", "exceptions": exceptions}]
    usage: dict[str, int] = {}
    called_ids: set[str] = set()
    rejected = 0
    nudged = False
    resolution: Resolution | None = None
    turn_no = 0

    for turn_no in range(1, max_turns + 1):
        turn = planner.next(
            SYSTEM_PROMPT, messages, TOOL_DEFINITIONS, {"invoice_number": invoice_number}
        )
        for key, value in turn.usage.items():
            usage[key] = usage.get(key, 0) + value
        messages.append({"role": "assistant", "content": turn.content})
        trace.append(
            {
                "event": "assistant",
                "turn": turn_no,
                "text": turn.text,
                "tool_calls": [c.__dict__ for c in turn.tool_calls],
                "stop_reason": turn.stop_reason,
            }
        )

        if turn.stop_reason == "refusal":
            resolution = _escalation("model declined the request")
            break
        if not turn.tool_calls:
            if nudged:
                resolution = _escalation("agent stopped without submitting a resolution")
                break
            nudged = True
            messages.append({"role": "user", "content": "Finish by calling submit_resolution."})
            continue

        results = []
        for call in turn.tool_calls:
            output, is_error = _execute(call, toolbox, invoice, match, called_ids)
            if call.name == SUBMIT and not is_error:
                resolution = Resolution.model_validate(call.input)
            elif call.name == SUBMIT:
                rejected += 1
            elif not is_error:
                called_ids.add(call.id)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": output,
                    **({"is_error": True} if is_error else {}),
                }
            )
            trace.append(
                {
                    "event": "policy_rejection" if call.name == SUBMIT and is_error else "tool",
                    "turn": turn_no,
                    "id": call.id,
                    "name": call.name,
                    "is_error": is_error,
                    "output": output[:TRACE_OUTPUT_LIMIT],
                }
            )
        # All results go back in one user message so parallel tool use keeps working.
        messages.append({"role": "user", "content": results})

        if resolution is not None:
            break
        if rejected >= MAX_REJECTED_SUBMISSIONS:
            resolution = _escalation("resolution failed policy validation repeatedly")
            break
    else:
        resolution = _escalation(f"no resolution within {max_turns} turns")

    decision = policy.route(resolution, match)
    trace.append({"event": "route", "status": decision.status.value, "reasons": decision.reasons})
    model = getattr(planner, "model", None)
    usage_out: dict[str, Any] = {**usage, "turns": turn_no}
    if model and usage:
        usage_out["model"] = model
        usage_out["cost_usd"] = estimate_cost_usd(model, usage)

    case_id = store.create_case(
        invoice_number,
        planner.name,
        decision.status,
        exceptions,
        resolution.model_dump(mode="json"),
        trace,
        usage_out,
    )
    log.info("case %s %s -> %s", case_id, invoice_number, decision.status.value)
    return CaseOutcome(
        case_id, invoice_number, decision.status, resolution, decision.reasons, turn_no, usage_out
    )


def _execute(call, toolbox: Toolbox, invoice, match, called_ids: set[str]) -> tuple[str, bool]:
    if call.name == SUBMIT:
        try:
            resolution = Resolution.model_validate(call.input)
        except ValidationError as exc:
            return f"invalid resolution: {exc.errors(include_url=False)}", True
        errors = policy.validate(resolution, invoice, match, called_ids)
        if errors:
            return "Resolution rejected by policy:\n- " + "\n- ".join(errors), True
        return json.dumps({"accepted": True}), False
    try:
        return toolbox.call(call.name, call.input), False
    except ToolError as exc:
        return str(exc), True
    except TypeError as exc:  # wrong or missing arguments
        return f"bad arguments for {call.name}: {exc}", True
