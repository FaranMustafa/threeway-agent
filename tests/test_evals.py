from threeway import evals
from threeway.agent.planners import BaselinePlanner


def test_baseline_report_is_stable():
    report = evals.run(BaselinePlanner())
    assert report["cases"] == 10
    assert report["correct"] == 5
    assert report["overpaid_cents"] == 18000  # paid for the damaged units
    assert report["underpaid_cents"] == 116000  # short-paid the price increase and the UOM case
    assert report["wrong_escalations"] == 2
    assert "correct 5/10" in evals.render(report)
