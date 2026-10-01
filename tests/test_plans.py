"""端到端测试：通过 FastAPI TestClient 验证 /plans 的关键行为。"""

import json
import os

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

HERE = os.path.dirname(__file__)


def post(payload: dict):
    return client.post("/plans", json=payload)


def load_example(name: str) -> dict:
    with open(os.path.join(HERE, "..", "examples", name)) as f:
        return json.load(f)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_example_is_optimal_and_consistent():
    r = post(load_example("request.json"))
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "OPTIMAL"
    assert d["optimal"] is True

    order = [(p["job_id"], p["segment_index"]) for p in d["plan"]]
    # purge 最小（200）、逾期 0，字典序最小顺序：J1.1 J1.2 J2.1 J2.2
    assert order == [("J1", 1), ("J1", 2), ("J2", 1), ("J2", 2)]
    assert d["purge_used_mm3"] == 200
    assert d["purge_remaining_mm3"] == 200
    assert d["total_tardiness_seconds"] == 0
    assert d["makespan_seconds"] == 210
    assert d["objective"]["lexicographic_minimal"] is True

    # 切换记录与计划时间线一致
    sw = d["switches"]
    assert [(s["from_material"], s["to_material"], s["purge_mm3"]) for s in sw] == [
        ("PLA", "ABS", 120),
        ("ABS", "PLA", 80),
    ]
    assert sum(s["purge_mm3"] for s in sw) == d["purge_used_mm3"]

    # 层段紧密排列、切换耗时正确：每个切换后第一个层段的开始时刻
    # = 前一层段结束 + 切换耗时（after_segment_order=0 时相对时刻 0）
    for s in sw:
        before = d["plan"][s["after_segment_order"] - 1] if s["after_segment_order"] else None
        after = d["plan"][s["after_segment_order"]]
        base_end = before["end_seconds"] if before else 0
        assert after["start_seconds"] == base_end + s["switch_seconds"]
        assert s["at_seconds"] == base_end
    # 无切换处层段首尾相接
    switch_after = {s["after_segment_order"] for s in sw}
    for i, p in enumerate(d["plan"][:-1], start=1):
        if i not in switch_after:
            nxt = d["plan"][i]
            assert nxt["start_seconds"] == p["end_seconds"]
    # 每个作业内层段按序出现
    for job_id in ("J1", "J2"):
        segs = [p["segment_index"] for p in d["plan"] if p["job_id"] == job_id]
        assert segs == sorted(segs)


def test_infeasible_capacity():
    r = post(load_example("request_infeasible.json"))
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "INFEASIBLE"
    assert d["optimal"] is False
    assert d["infeasible_reason"]["code"] == "CAPACITY"
    assert d["infeasible_reason"]["minimum_required_purge_mm3"] == 200
    assert d["plan"] == []


def test_capacity_exactly_enough():
    payload = load_example("request_infeasible.json")
    payload["total_purge_capacity_mm3"] = 200
    r = post(payload)
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "OPTIMAL"
    assert d["purge_used_mm3"] == 200
    assert d["purge_remaining_mm3"] == 0


def test_three_job_interleaving():
    payload = {
        "jobs": [
            {"job_id": "J1", "deadline_seconds": 200, "segments": [
                {"material_id": "A", "print_seconds": 30, "volume_mm3": 10},
                {"material_id": "B", "print_seconds": 60, "volume_mm3": 10}]},
            {"job_id": "J2", "deadline_seconds": 100, "segments": [
                {"material_id": "B", "print_seconds": 20, "volume_mm3": 10},
                {"material_id": "C", "print_seconds": 40, "volume_mm3": 10}]},
            {"job_id": "J3", "deadline_seconds": 200, "segments": [
                {"material_id": "A", "print_seconds": 20, "volume_mm3": 10}]},
        ],
        "initial_material": "A",
        "purge_matrix": {
            "A": {"A": 0, "B": 100, "C": 100},
            "B": {"A": 100, "B": 0, "C": 100},
            "C": {"A": 100, "B": 100, "C": 0},
        },
        "switch_seconds": 5,
        "total_purge_capacity_mm3": 1000,
    }
    r = post(payload)
    d = r.json()
    assert r.status_code == 200
    assert d["status"] == "OPTIMAL"
    order = [(p["job_id"], p["segment_index"]) for p in d["plan"]]
    # A,A 相邻免 purge；最优 purge 200（A->B, B->C）
    assert order == [("J1", 1), ("J3", 1), ("J1", 2), ("J2", 1), ("J2", 2)]
    assert d["purge_used_mm3"] == 200
    assert d["total_tardiness_seconds"] == 80
    j2 = next(x for x in d["job_tardiness"] if x["job_id"] == "J2")
    assert j2["tardy_seconds"] == 80


def test_purge_priority_beats_tardiness():
    """第一级 purge 优先：即使增加逾期，也必须选 purge 更小的计划。"""
    payload = {
        "jobs": [
            {"job_id": "J1", "deadline_seconds": 50, "segments": [
                {"material_id": "A", "print_seconds": 10, "volume_mm3": 1},
                {"material_id": "B", "print_seconds": 10, "volume_mm3": 1}]},
            {"job_id": "J2", "deadline_seconds": 50, "segments": [
                {"material_id": "A", "print_seconds": 10, "volume_mm3": 1},
                {"material_id": "B", "print_seconds": 10, "volume_mm3": 1}]},
        ],
        "initial_material": "A",
        "purge_matrix": {"A": {"A": 0, "B": 1000}, "B": {"A": 1, "B": 0}},
        "switch_seconds": 0,
        "total_purge_capacity_mm3": 100000,
    }
    d = post(payload).json()
    # A A B B：purge=1000（一次 A->B）；交替 ABAB 需 1000+1+1000+1=2002
    order = [(p["job_id"], p["segment_index"]) for p in d["plan"]]
    assert order == [("J1", 1), ("J2", 1), ("J1", 2), ("J2", 2)]
    assert d["purge_used_mm3"] == 1000
    assert d["objective"]["total_purge_mm3"] == 1000


def test_initial_material_purge_charged():
    payload = {
        "jobs": [
            {"job_id": "J1", "deadline_seconds": 100, "segments": [
                {"material_id": "B", "print_seconds": 10, "volume_mm3": 1}]},
            {"job_id": "J2", "deadline_seconds": 100, "segments": [
                {"material_id": "B", "print_seconds": 10, "volume_mm3": 1}]},
        ],
        "initial_material": "A",
        "purge_matrix": {"A": {"A": 0, "B": 50}, "B": {"A": 1, "B": 0}},
        "switch_seconds": 3,
        "total_purge_capacity_mm3": 1000,
    }
    d = post(payload).json()
    assert d["status"] == "OPTIMAL"
    assert d["purge_used_mm3"] == 50  # 仅首次 A->B
    first = d["plan"][0]
    assert first["start_seconds"] == 3  # 首段前也要切换耗时


def test_lexicographic_tiebreak_prefects_smaller_job_id():
    # 两段同材料、两作业完全对称：字典序应选 ("A",1) 开头
    payload = {
        "jobs": [
            {"job_id": "B", "deadline_seconds": 100, "segments": [
                {"material_id": "X", "print_seconds": 5, "volume_mm3": 1}]},
            {"job_id": "A", "deadline_seconds": 100, "segments": [
                {"material_id": "X", "print_seconds": 5, "volume_mm3": 1}]},
        ],
        "initial_material": "X",
        "purge_matrix": {"X": {"X": 0}},
        "switch_seconds": 0,
        "total_purge_capacity_mm3": 0,
    }
    d = post(payload).json()
    assert [p["job_id"] for p in d["plan"]] == ["A", "B"]
    assert d["objective"]["lexicographic_minimal"] is True


# ----------------------------- 长作业（>=25 段） -----------------------------

def assert_plan_complete_and_ordered(d: dict, nseg: int, job_counts: dict):
    """通用校验：计划完整、作业内层段严格按序、切换/purge/逾期明细自洽。"""
    assert d["status"] in ("OPTIMAL", "FEASIBLE", "UNKNOWN")
    assert len(d["plan"]) == nseg
    assert [p["order"] for p in d["plan"]] == list(range(1, nseg + 1))

    per_job = {}
    for p in d["plan"]:
        per_job.setdefault(p["job_id"], []).append(p["segment_index"])
    assert set(per_job) == set(job_counts)
    for job_id, count in job_counts.items():
        assert per_job[job_id] == list(range(1, count + 1))

    # 切换记录与 purge 账实相符
    assert sum(s["purge_mm3"] for s in d["switches"]) == d["purge_used_mm3"]
    cur = None
    for s in d["switches"]:
        assert s["from_material"] != s["to_material"]

    # 时间线紧密：相邻层段之间要么 0 间隙，要么恰好一次切换耗时
    sw_by_after = {s["after_segment_order"]: s for s in d["switches"]}
    if d["switches"]:
        first_switch = min(s["after_segment_order"] for s in d["switches"])
        if first_switch == 0:
            s0 = sw_by_after[0]
            assert d["plan"][0]["start_seconds"] == s0["switch_seconds"]
    for i, p in enumerate(d["plan"][:-1], start=1):
        gap = sw_by_after[i]["switch_seconds"] if i in sw_by_after else 0
        assert d["plan"][i]["start_seconds"] == p["end_seconds"] + gap

    # 逾期明细与汇总一致
    assert len(d["job_tardiness"]) == len(job_counts)
    assert sum(j["tardy_seconds"] for j in d["job_tardiness"]) == d[
        "total_tardiness_seconds"
    ]
    assert d["objective"]["total_purge_mm3"] == d["purge_used_mm3"]
    assert d["objective"]["total_tardiness_seconds"] == d[
        "total_tardiness_seconds"
    ]
    # 状态与各最优标记严格一致：只有三级全部证明才可标 OPTIMAL
    flags = d["objective"]
    assert d["optimal"] == (d["status"] == "OPTIMAL")
    assert d["optimal"] == all(
        flags[k]
        for k in ("purge_optimal", "tardiness_optimal", "lexicographic_minimal")
    )


def _long_job_payload(k: int) -> dict:
    seg = lambda m: {"material_id": m, "print_seconds": 3, "volume_mm3": 5}
    return {
        "jobs": [
            {
                "job_id": "J1",
                "deadline_seconds": 10**9,
                "segments": [seg("A") if i % 2 == 0 else seg("B") for i in range(k)],
            },
            {"job_id": "J2", "deadline_seconds": 10**9,
             "segments": [seg("A"), seg("B")]},
        ],
        "initial_material": "A",
        "purge_matrix": {"A": {"A": 0, "B": 70}, "B": {"A": 40, "B": 0}},
        "switch_seconds": 3,
        "total_purge_capacity_mm3": 10**9,
        "time_limit_seconds": 15,
    }


def test_twenty_four_segments_still_accepted():
    payload = _long_job_payload(24)
    r = post(payload)
    assert r.status_code == 200
    d = r.json()
    assert_plan_complete_and_ordered(d, 26, {"J1": 24, "J2": 2})


def test_long_job_25_segments_not_rejected():
    """核心回归：单作业 25 段必须进入 CP-SAT 规划，而不是被 422 拒绝。"""
    payload = _long_job_payload(25)
    r = post(payload)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] in ("OPTIMAL", "FEASIBLE")
    assert_plan_complete_and_ordered(d, 27, {"J1": 25, "J2": 2})
    assert len(d["plan"]) == 27
    assert d["purge_used_mm3"] <= 10**9


def test_long_job_well_beyond_old_cap():
    """远超旧 24 段上限的长作业同样可规划并返回完整明细。"""
    payload = _long_job_payload(40)
    r = post(payload)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] in ("OPTIMAL", "FEASIBLE")
    assert_plan_complete_and_ordered(d, 42, {"J1": 40, "J2": 2})


def test_large_model_tight_time_limit_returns_full_plan():
    """大模型 + 极短 time_limit：到期也必须返回完整、作业内有序的计划，
    且只在确实证明时标 OPTIMAL，否则 FEASIBLE/UNKNOWN 如实返回。"""
    mats = ["A", "B", "C", "D"]
    jobs = []
    per_job = 26
    for j in range(8):
        jobs.append({
            "job_id": f"J{j+1}",
            "deadline_seconds": 10**9,
            "segments": [
                {"material_id": mats[(i + j) % 4],
                 "print_seconds": (i % 5) + 1, "volume_mm3": 5}
                for i in range(per_job)
            ],
        })
    payload = {
        "jobs": jobs,
        "initial_material": "A",
        "purge_matrix": {
            m: {m2: (0 if m == m2 else 30 + 10 * ((i + k) % 5))
                for k, m2 in enumerate(mats)}
            for i, m in enumerate(mats)
        },
        "switch_seconds": 2,
        "total_purge_capacity_mm3": 10**9,
        "time_limit_seconds": 0.5,
    }
    r = post(payload)
    assert r.status_code == 200, r.text
    d = r.json()
    nseg = per_job * 8
    assert len(d["plan"]) == nseg
    assert d["status"] in ("OPTIMAL", "FEASIBLE", "UNKNOWN")
    assert_plan_complete_and_ordered(
        d, nseg, {f"J{j+1}": per_job for j in range(8)}
    )
    # 0.5 秒对 208 段模型不足以完成三级证明：不得谎称 OPTIMAL
    assert d["status"] != "OPTIMAL"
    assert d["optimal"] is False
    # FEASIBLE 时给出的 purge 不得超过容量（计划本身容量内可行）
    if d["status"] == "FEASIBLE":
        assert d["purge_used_mm3"] <= 10**9


# ----------------------------- 422 校验 -----------------------------

def test_422_cases():
    base = load_example("request.json")
    bad = {
        "one job": dict(base, jobs=base["jobs"][:1]),
        "nine jobs": dict(base, jobs=base["jobs"] * 5),
        "dup id": dict(base, jobs=[
            dict(base["jobs"][0], job_id="X"),
            dict(base["jobs"][1], job_id="X")]),
        "unknown initial": dict(base, initial_material="PETG"),
        "negative cell": dict(base, purge_matrix={
            "PLA": {"PLA": 0, "ABS": -1}, "ABS": {"PLA": 80, "ABS": 0}}),
        "nonzero diagonal": dict(base, purge_matrix={
            "PLA": {"PLA": 5, "ABS": 120}, "ABS": {"PLA": 80, "ABS": 0}}),
        "missing column": dict(base, purge_matrix={
            "PLA": {"PLA": 0}, "ABS": {"PLA": 80, "ABS": 0}}),
        "negative switch": dict(base, switch_seconds=-1),
        "empty segments": dict(base, jobs=[
            dict(base["jobs"][0], segments=[]), base["jobs"][1]]),
        "extra field": dict(base, foo=1),
        "negative print": dict(base, jobs=[
            dict(base["jobs"][0], segments=[
                {"material_id": "PLA", "print_seconds": -1, "volume_mm3": 1}]),
            base["jobs"][1]]),
        "missing body": None,
    }
    for name, payload in bad.items():
        r = post(payload) if payload is not None else client.post("/plans")
        assert r.status_code == 422, name
