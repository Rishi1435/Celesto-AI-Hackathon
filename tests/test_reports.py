"""Submission checks. --require-evidence turns pending live evidence into failures."""

import hashlib
import json
import math
import re
import statistics
from datetime import datetime

import pytest

from app.config import ROOT

NAME = re.compile(r"sbx-[a-z0-9]{8,}\Z")
ALLOWED = {
    "persistence",
    "snapshots",
    "celestofs",
    "browser",
    "published_ports",
    "network_policy",
    "safety_hooks",
    "host_mounts",
    "coding_agent_preset",
    "agentor_durable",
}


def read(name):
    return json.loads((ROOT / "reports" / name).read_text(encoding="utf-8"))


def json_lines(path):
    return [
        json.loads(line) for line in (ROOT / path).read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def require_live(request, ready):
    if ready:
        return
    if request.config.getoption("--require-evidence"):
        pytest.fail("Required real Celesto evidence is pending. Run the live harness; do not fabricate rows.")
    pytest.skip("Live Celesto evidence pending; use --require-evidence for the submission gate")


def test_problem_and_capabilities():
    problem = read("problem.json")
    assert problem["idea_source"] in {"idea_bank", "original"}
    for key, length in [("who", 40), ("agent_work", 40), ("proof_output", 20)]:
        assert len(problem[key]) >= length
    capabilities = read("capabilities.json")["capabilities"]
    assert len(capabilities) >= 2
    assert len({c["id"] for c in capabilities}) == len(capabilities)
    for capability in capabilities:
        assert capability["id"] in ALLOWED
        assert len(capability["why"]) >= 80
        assert (ROOT / capability["evidence_path"]).is_file()


def test_failure_policy_design():
    policy = read("failure_policy.json")
    assert policy["failed_step"] == 7 and policy["total_steps"] == 10
    assert policy["policy"] in {"resume", "rollback", "fresh"}
    assert len(policy["rationale"]) >= 200
    assert policy["trace_path"] == "reports/failure_trace.jsonl"
    assert (ROOT / policy["trace_path"]).exists()


def test_readme_required_phrases():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for phrase in [
        "Problem statement",
        "How I used Celesto",
        "Eval report",
        "Containment test",
        "State and failure design",
        "Setup",
    ]:
        assert phrase in text
    assert "mermaid" in text and "LLM" in text


@pytest.mark.submission
def test_command_audit(request):
    lines = json_lines("logs/commands.jsonl")
    require_live(request, bool(lines))
    for line in lines:
        assert NAME.fullmatch(line["sandbox_name"])
        assert line["decision"] in {"allow", "block"}
        assert line["execution_target"] == "celesto_sandbox"
        assert isinstance(line["command"], str) and line["command"]
        datetime.fromisoformat(line["ts"])


@pytest.mark.submission
def test_reliability_matches_rows(request):
    r = read("reliability.json")
    require_live(request, r["run_count"] >= 20)
    runs, n = r["runs"], r["run_count"]
    assert n == len(runs) >= 20
    assert len({run["sandbox_name"] for run in runs}) == n
    for run in runs:
        assert NAME.fullmatch(run["sandbox_name"])
        assert run["fresh"] is True and type(run["success"]) is bool
        for key in ["wall_clock_s", "boot_to_ready_s", "cost_usd"]:
            assert type(run[key]) in {int, float} and math.isfinite(run[key]) and run[key] >= 0
    walls = sorted(run["wall_clock_s"] for run in runs)
    assert r["success_rate"] == pytest.approx(sum(run["success"] for run in runs) / n, abs=0.001)
    assert r["median_wall_clock_s"] == pytest.approx(statistics.median(walls), abs=0.001)
    assert r["p90_wall_clock_s"] == pytest.approx(walls[math.ceil(0.9 * n) - 1], abs=0.001)
    assert r["median_boot_to_ready_s"] == pytest.approx(
        statistics.median(run["boot_to_ready_s"] for run in runs), abs=0.001
    )
    assert r["total_cost_usd"] == pytest.approx(sum(run["cost_usd"] for run in runs), abs=0.01)
    assert r["postmortem_path"] == "reports/postmortem.md"
    assert len((ROOT / r["postmortem_path"]).read_text()) >= 200


@pytest.mark.submission
def test_cleanup_observed_on_all_paths(request):
    r = read("lifecycle.json")
    require_live(request, r.get("status") == "measured")
    assert all(r[key] is True for key in ["cleanup_on_success", "cleanup_on_failure", "cleanup_on_interrupt"])
    names = r["sandbox_names"]
    assert names and len(names) == len(set(names))
    observed = {line["sandbox_name"] for line in json_lines("logs/commands.jsonl")}
    observed |= {row["sandbox_name"] for row in read("reliability.json")["runs"]}
    assert set(names) <= observed
    assert all(NAME.fullmatch(name) for name in names)


@pytest.mark.submission
def test_containment_observed(request):
    r = read("containment.json")
    require_live(request, r.get("status") == "measured")
    assert r["attack"] in {"prompt_injection_exfil", "destructive_command", "suspicious_package"}
    assert r["blocked"] is True and r["host_untouched"] is True
    assert r["evidence_paths"]
    assert all((ROOT / path).is_file() for path in r["evidence_paths"])


@pytest.mark.submission
def test_recovery_policy_actually_applied(request):
    policy = read("failure_policy.json")
    trace = json_lines(policy["trace_path"])
    require_live(request, bool(trace))
    assert any(
        line.get("event") == "policy_applied" and line.get("policy") == policy["policy"] for line in trace
    )


@pytest.mark.submission
def test_five_real_jobs(request):
    r = read("concurrency.json")
    require_live(request, len(r["jobs"]) >= 5)
    assert len({job["sandbox_name"] for job in r["jobs"]}) == len(r["jobs"])
    for job in r["jobs"]:
        assert NAME.fullmatch(job["sandbox_name"])
        datetime.fromisoformat(job["started_at"])
    assert len(r["breakage_notes"]) >= 40


@pytest.mark.submission
def test_live_guest_demo_exists(request):
    # A storyboard is not a demo. Require a real recorded guest or an explicit external video link.
    video = ROOT / "demo" / "walkthrough.mp4"
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    has_link = bool(re.search(r"https://(?:www\.)?(?:youtube\.com|youtu\.be|loom\.com)/", text))
    require_live(request, video.is_file() or has_link)
    if video.is_file():
        assert video.stat().st_size > 1024
        manifest = json.loads((ROOT / "demo/evidence_index.json").read_text())
        assert 0 < manifest["duration_s"] <= 180
        assert manifest["video_sha256"] == hashlib.sha256(video.read_bytes()).hexdigest()
        for source in manifest["sources"]:
            path = (ROOT / source["path"]).resolve()
            assert path.is_relative_to(ROOT.resolve())
            assert hashlib.sha256(path.read_bytes()).hexdigest() == source["sha256"]
