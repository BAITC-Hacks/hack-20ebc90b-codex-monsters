import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ekt.forecast import evidence


@pytest.fixture(scope="module")
def report():
    return evidence.build_evidence()


def _case(report, case_id):
    return next(case for case in report["cases"] if case["case_id"] == case_id)


def test_synthetic_evidence_has_independent_numeric_expectations(report):
    assert report["status"] == "PASS"
    assert report["mode"] == "synthetic_demo"
    assert report["model_version"].startswith("robust-daily-")
    assert {case["case_id"] for case in report["cases"]} == {f"B-E0{index}" for index in range(1, 6)}
    comparison = _case(report, "B-E01")["tables"][0]["rows"]
    assert comparison[0]["b_forecast"] == 10
    assert comparison[1]["recent_mean_28"] == pytest.approx(45.714285714285715)
    assert comparison[1]["b_forecast"] == 10
    assert _case(report, "B-E01")["recent_window"]["days"] == 28
    allocation = _case(report, "B-E01")["tables"][1]["rows"][0]
    assert allocation["uncertain_qty"] == "1000"
    assert allocation["label"] == "suspected_project" and allocation["review_status"] == "pending"
    counterexamples = _case(report, "B-E02")["tables"][0]["rows"]
    assert counterexamples[0]["regular_qty"] == "3000"
    assert counterexamples[1]["regular_qty"] == "840"
    assert counterexamples[1]["b_next_day_forecast"] == 30
    recovery = _case(report, "B-E03")
    assert recovery["recovery_metrics"]["mae"] == 2
    assert recovery["recovery_metrics"]["wape"] == pytest.approx(1 / 6)
    assert recovery["recovery_metrics"]["bias"] == pytest.approx(-1 / 6)
    assert recovery["tables"][0]["rows"][0]["corrected_demand"] == "0.0"
    components = _case(report, "B-E04")["tables"][0]["rows"]
    assert [row["mean"] for row in components] == pytest.approx([10, 12, 13.2])
    assert components[-1]["growth_delta"] == pytest.approx(1.2)


def test_oracles_do_not_enter_detector_or_recovery(monkeypatch):
    original_classify = evidence.classify_events
    original_recover = evidence.recover_daily
    seen = []

    def inspect_classifier(events, *args, **kwargs):
        for row in events:
            assert not ({"label", "expected_class", "is_project", "truth", "latent_truth"} & row.keys())
        seen.append("detector")
        return original_classify(events, *args, **kwargs)

    def inspect_recovery(events, *args, **kwargs):
        for row in events:
            assert not ({"truth", "latent_truth", "expected_class"} & row.keys())
        seen.append("recovery")
        return original_recover(events, *args, **kwargs)

    monkeypatch.setattr(evidence, "classify_events", inspect_classifier)
    monkeypatch.setattr(evidence, "recover_daily", inspect_recovery)
    result = evidence.build_evidence()
    assert result["status"] == "PASS"
    assert set(seen) == {"detector", "recovery"}


def test_report_is_repeatable_and_metrics_keep_honest_limits(report):
    assert evidence.build_evidence() == report
    evaluation = _case(report, "B-E05")
    assert [origin["origin"] for origin in evaluation["evaluation"]["origins"]] == [120, 150]
    rows = evaluation["tables"][0]["rows"]
    assert [row["wape"] for row in rows] == pytest.approx([0.063212, 0.039729, 0.057977, 0.036515], abs=1e-6)
    assert all(row["mase"] > 1 for row in rows)
    assert all(evaluation["zero_denominator_control"][name] is None for name in ("wape", "bias", "mase"))
    text = evidence.render_markdown(report)
    assert "все данные синтетические" in text
    assert "не оценивают реальную точность" in text
    assert "MASE > 1" in text
    assert "не является условием PASS" in text


def test_cli_writes_only_deterministic_safe_reports(tmp_path, report):
    command = [sys.executable, "-m", "ekt.forecast.evidence", "--output-dir", str(tmp_path)]
    # pytest's pythonpath does not propagate to subprocesses. Exercise this
    # checkout even if the developer has another editable clone installed.
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    completed = subprocess.run(command, capture_output=True, text=True, check=True, env=env)
    summary = json.loads(completed.stdout)
    assert summary["status"] == "PASS" and summary["mode"] == "synthetic_demo"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["forecast-evidence.json", "forecast-evidence.md"]
    assert json.loads(Path(summary["json"]).read_text()) == report
    original = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    subprocess.run(command, capture_output=True, text=True, check=True, env=env)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == original


def test_failed_invariant_is_reported_with_nonzero_exit(tmp_path, report, monkeypatch):
    failed = dict(report, status="FAIL")
    monkeypatch.setattr(evidence, "build_evidence", lambda: failed)
    assert evidence.main(["--output-dir", str(tmp_path)]) == 1
    assert json.loads((tmp_path / "forecast-evidence.json").read_text())["status"] == "FAIL"


def test_evidence_detects_broken_project_classification(monkeypatch):
    original = evidence.classify_events

    def all_regular(events):
        classified = original(events)
        for row in classified:
            row.update(label="regular", review_status="not_required", regular_qty=row["observed_qty"],
                       project_qty=0, uncertain_qty=0)
        return classified

    monkeypatch.setattr(evidence, "classify_events", all_regular)
    result = evidence.build_evidence()
    assert result["status"] == "FAIL"
    project = _case(result, "B-E01")
    assert project["status"] == "FAIL"
    assert not next(check for check in project["checks"] if check["check_id"] == "blind_spike")["passed"]
