"""Command line: seed, investigate, eval, serve."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from threeway.agent.planners import BaselinePlanner, ClaudePlanner
from threeway.db import Store
from threeway.models import fmt


def _planner(name: str):
    return ClaudePlanner() if name == "claude" else BaselinePlanner()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="threeway", description=__doc__)
    parser.add_argument("--db", default=os.environ.get("THREEWAY_DB", "threeway.db"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("seed", help="load sample master data and EDI files")

    inv = sub.add_parser("investigate", help="run the agent on one invoice")
    inv.add_argument("invoice")
    inv.add_argument("--planner", choices=["claude", "baseline"], default="claude")
    inv.add_argument("--trace", action="store_true", help="print the full trace")

    ev = sub.add_parser("eval", help="score a planner on the labeled scenarios")
    ev.add_argument("--planner", choices=["claude", "baseline"], default="baseline")
    ev.add_argument("--out", type=Path, help="write the JSON report here")

    srv = sub.add_parser("serve", help="run the HTTP API")
    srv.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)

    if args.cmd == "seed":
        from threeway.seed import seed

        n = seed(Store(args.db))
        print(f"loaded {n} EDI documents into {args.db}")
    elif args.cmd == "investigate":
        from threeway.agent.runner import investigate

        store = Store(args.db)
        outcome = investigate(store, args.invoice, _planner(args.planner))
        r = outcome.resolution
        print(f"{outcome.invoice_number}: {r.decision.value} {fmt(r.approved_amount_cents)}")
        print(f"status: {outcome.status.value} ({'; '.join(outcome.route_reasons)})")
        for a in r.adjustments:
            print(f"  - {a.sku}: -{fmt(a.amount_cents)} {a.reason}")
        print(f"rationale: {r.rationale}")
        if r.vendor_message:
            print(f"vendor message: {r.vendor_message}")
        print(f"case: {outcome.case_id} · turns {outcome.turns} · usage {outcome.usage}")
        if args.trace:
            print(json.dumps(store.case(outcome.case_id)["trace"], indent=2))
    elif args.cmd == "eval":
        from threeway import evals

        report = evals.run(_planner(args.planner))
        print(evals.render(report))
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report, indent=2))
    elif args.cmd == "serve":
        import uvicorn

        os.environ["THREEWAY_DB"] = args.db
        uvicorn.run("threeway.api:app", port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
