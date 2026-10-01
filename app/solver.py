"""多材料 3D 打印换料规划求解器。

建模（OR-Tools CP-SAT）
----------------------

* 每个层段是一个不可拆分的加工节点；另设虚拟起点 START 与终点 END。
* 节点间的有向弧 ``u -> v`` 表示层段 v 紧跟在 u 之后执行，弧上携带
  purge 体积与切换耗时；用 ``AddCircuit`` 强制选出一条
  ``START -> (全部层段的某个排列) -> END -> START`` 的 Hamilton 回路。
* 同一作业内的层段用先后约束保证顺序；作业之间只能在层段边界交错。
* 弧被选中时强制 ``start[v] = end[u] + 切换耗时``，故排产是紧密无空转的。
* 作业逾期 ``max(0, 最后层段完成时刻 - 截止秒数)``。

三级目标（严格优先级）：
1. 总 purge 体积最小；
2. 总逾期秒数最小（purge 固定为最优值）；
3. 计划按 ``(作业 ID, 层序)`` 的字典序最小——采用逐位贪心固定前缀 +
   可行性检查的方式，每一步都经过 CP-SAT 可行性证明，因此完成全部位置
   后即"证明字典序最小"；任何一步在限时内无法判定则保守地不声称最优。

长作业 / 大模型（单作业层段数不设上限）
--------------------------------------

建模规模为 O(n²) 条弧，数千层段时 CP-SAT 可能在限时内走不到第一个可行
解。为此：

* 始终先构造两个满足作业内先后的合法序列（作业 ID 串联、同材料贪心），
  作为**完整 solution hint**；求解前在克隆模型上固定 hint 值做一次极短
  验证，把该可行解登记为 incumbent。
* 三阶段共享同一个墙钟截止时刻（``time_limit_seconds``），时间到期即带
  上当前最佳计划返回；即便优化器毫无产出，也用构造序列兜底，保证响应里
  始终有完整、作业内严格按序的计划、切换记录、purge 与逾期明细。
* 仅当 purge 最优、逾期最优、字典序最小**都被证明**时才标 ``OPTIMAL``；
  有可行计划但未证明时标 ``FEASIBLE``；连容量内可行构造都没有时标
  ``UNKNOWN``（仍附完整构造计划）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ortools.sat.python import cp_model

from .schemas import PlanRequest

START = 0


@dataclass
class JobData:
    job_id: str
    deadline: int
    segments: List[dict]  # [{global_idx, material_id, print_seconds, volume_mm3, seg_index}]
    last_idx: int


@dataclass
class Instance:
    jobs: List[JobData]
    n: int
    initial_material: str
    purge_matrix: Dict[str, Dict[str, int]]
    switch_seconds: int
    purge_capacity: int
    # 以全局层段号（1..n）为索引的属性表
    job_of: Dict[int, str] = field(default_factory=dict)
    seg_index_of: Dict[int, int] = field(default_factory=dict)
    material_of: Dict[int, str] = field(default_factory=dict)
    duration_of: Dict[int, int] = field(default_factory=dict)
    volume_of: Dict[int, int] = field(default_factory=dict)
    predecessor_of: Dict[int, Optional[int]] = field(default_factory=dict)
    rank_of: Dict[int, Tuple[str, int]] = field(default_factory=dict)
    horizon: int = 0


def build_instance(req: PlanRequest) -> Instance:
    jobs: List[JobData] = []
    idx = 0
    total_print = 0
    job_of: Dict[int, str] = {}
    seg_index_of: Dict[int, int] = {}
    material_of: Dict[int, str] = {}
    duration_of: Dict[int, int] = {}
    volume_of: Dict[int, int] = {}
    predecessor_of: Dict[int, Optional[int]] = {}
    rank_of: Dict[int, Tuple[str, int]] = {}

    for job in req.jobs:
        segs = []
        prev_idx: Optional[int] = None
        for k, seg in enumerate(job.segments, start=1):
            idx += 1
            segs.append(
                {
                    "global_idx": idx,
                    "material_id": seg.material_id,
                    "print_seconds": seg.print_seconds,
                    "volume_mm3": seg.volume_mm3,
                    "seg_index": k,
                }
            )
            job_of[idx] = job.job_id
            seg_index_of[idx] = k
            material_of[idx] = seg.material_id
            duration_of[idx] = seg.print_seconds
            volume_of[idx] = seg.volume_mm3
            predecessor_of[idx] = prev_idx
            rank_of[idx] = (job.job_id, k)
            prev_idx = idx
            total_print += seg.print_seconds
        jobs.append(
            JobData(
                job_id=job.job_id,
                deadline=job.deadline_seconds,
                segments=segs,
                last_idx=prev_idx,  # type: ignore[arg-type]
            )
        )

    n = idx
    # 任意相邻层段最多切换一次，再加首段前可能的一次切换
    horizon = total_print + (n + 1) * req.switch_seconds + 1
    return Instance(
        jobs=jobs,
        n=n,
        initial_material=req.initial_material,
        purge_matrix=req.purge_matrix,
        switch_seconds=req.switch_seconds,
        purge_capacity=req.total_purge_capacity_mm3,
        job_of=job_of,
        seg_index_of=seg_index_of,
        material_of=material_of,
        duration_of=duration_of,
        volume_of=volume_of,
        predecessor_of=predecessor_of,
        rank_of=rank_of,
        horizon=horizon,
    )


@dataclass
class ModelArtifacts:
    model: cp_model.CpModel
    starts: Dict[int, cp_model.IntVar]
    ends: Dict[int, object]
    arc_lit: Dict[Tuple[int, int], cp_model.IntVar]
    tardy: Dict[str, cp_model.IntVar]
    purge_var: cp_model.IntVar
    tardiness_var: cp_model.IntVar
    completion: Dict[str, cp_model.IntVar]


def _arc_costs(inst: Instance, u: int, v: int, end_node: int) -> Tuple[int, int]:
    """返回弧 (u,v) 的 (切换秒数, purge 体积)。

    START -> v：从初始材料换到 v 的材料；
    u -> END / END -> START：收尾弧，无切换无 purge；
    u -> v（均为层段）：按两层层段材料查矩阵（同材料矩阵对角为 0）。
    """
    if v == START or v == end_node or u == end_node:
        return 0, 0
    to_mat = inst.material_of[v]
    if u == START:
        from_mat = inst.initial_material
    else:
        from_mat = inst.material_of[u]
    if from_mat == to_mat:
        return 0, 0
    return inst.switch_seconds, int(inst.purge_matrix[from_mat][to_mat])


def _simulate(inst: Instance, order: List[int]) -> Tuple[int, int]:
    """按给定合法序列紧密排产，返回 (总 purge, 总逾期)。"""
    cur = inst.initial_material
    t = 0
    purge = 0
    completion: Dict[str, int] = {}
    last_idx_to_job = {jd.last_idx: jd for jd in inst.jobs}
    for gi in order:
        mat = inst.material_of[gi]
        if mat != cur:
            purge += int(inst.purge_matrix[cur][mat])
            t += inst.switch_seconds
            cur = mat
        t += inst.duration_of[gi]
        jd = last_idx_to_job.get(gi)
        if jd is not None:
            completion[jd.job_id] = t
    tardiness = sum(
        max(0, completion.get(jd.job_id, 0) - jd.deadline) for jd in inst.jobs
    )
    return purge, tardiness


def _constructive_orders(inst: Instance) -> List[List[int]]:
    """生成若干个一定满足作业内先后约束的构造性序列。

    既作为 CP-SAT 的初始 hint（大模型下快速获得首个可行解），
    也在求解器限时到期仍无 incumbent 时作为完整计划的兜底来源。

    1. 作业 ID 顺序串联（也是字典序最小的构造序列）；
    2. 贪心：每个位置优先选择与当前材料相同、可开工（前置已完成）的层段，
       尽量免切换以降低 purge。
    """
    n = inst.n
    concat: List[int] = []
    for jd in sorted(inst.jobs, key=lambda j: j.job_id):
        concat.extend(s["global_idx"] for s in jd.segments)

    greedy: List[int] = []
    placed: set = set()
    cur = inst.initial_material
    while len(greedy) < n:
        ready = [
            v
            for v in range(1, n + 1)
            if v not in placed
            and (
                inst.predecessor_of[v] is None
                or inst.predecessor_of[v] in placed
            )
        ]
        ready.sort(
            # 同材料优先；其次保持作业/层序稳定，避免抖动
            key=lambda v: (
                inst.material_of[v] != cur,
                inst.rank_of[v],
            )
        )
        pick = ready[0]
        greedy.append(pick)
        placed.add(pick)
        cur = inst.material_of[pick]

    if greedy == concat:
        return [concat]
    return [concat, greedy]


def build_model(
    inst: Instance,
    *,
    purge_eq: Optional[int] = None,
    tardiness_eq: Optional[int] = None,
    force_arcs: Optional[Dict[Tuple[int, int], bool]] = None,
) -> ModelArtifacts:
    m = cp_model.CpModel()
    n = inst.n
    end_node = n + 1

    # 绑定高频方法，避免每个弧都走一次属性查找 / 弃用包装层
    new_int = m.NewIntVar
    new_bool = m.NewBoolVar
    add_ct = m.Add
    add_max = m.AddMaxEquality

    starts = {i: new_int(0, inst.horizon, f"s_{i}") for i in range(1, n + 1)}
    ends = {i: new_int(0, inst.horizon, f"e_{i}") for i in range(1, n + 1)}
    for i in range(1, n + 1):
        if inst.duration_of[i] > 0:
            add_ct(ends[i] == starts[i] + inst.duration_of[i])
        else:
            add_ct(ends[i] == starts[i])

    # 作业内层段先后顺序
    for i in range(1, n + 1):
        pred = inst.predecessor_of[i]
        if pred is not None:
            add_ct(starts[i] >= ends[pred])

    # 预取层段属性，弧循环中不再反复查字典
    mat = inst.material_of
    switch_sec = inst.switch_seconds
    pmat = inst.purge_matrix
    init_mat = inst.initial_material

    arc_lit: Dict[Tuple[int, int], cp_model.IntVar] = {}
    arcs: List[Tuple[int, int, cp_model.IntVar]] = []
    # 直接用 弧布尔 * purge常量 的加权线性和表达总 purge，
    # 避免为每条弧再建一个辅助 IntVar（大模型下可少建数万个变量与约束）。
    purge_terms: List[cp_model.LinearExpr] = []

    def add_segment_arc(u: int, v: int) -> None:
        """u -> v（v 为真实层段）：建弧、记 purge、绑定紧密开工时刻。"""
        lit = new_bool(f"a_{u}_{v}")
        arcs.append((u, v, lit))
        arc_lit[(u, v)] = lit
        to_m = mat[v]
        if u == START:
            from_m = init_mat
            base = 0
        else:
            from_m = mat[u]
            base = ends[u]
        if from_m != to_m:
            purge = int(pmat[from_m][to_m])
            if purge > 0:
                purge_terms.append(purge * lit)
            if switch_sec:
                add_ct(starts[v] == base + switch_sec).OnlyEnforceIf(lit)
            else:
                add_ct(starts[v] == base).OnlyEnforceIf(lit)

    # 起点 -> 首层段；末层段 -> 终点；回路闭合
    for v in range(1, n + 1):
        add_segment_arc(START, v)
        lit_end = new_bool(f"a_{v}_{end_node}")
        arcs.append((v, end_node, lit_end))
        arc_lit[(v, end_node)] = lit_end
    lit_close = new_bool(f"a_{end_node}_{START}")
    arcs.append((end_node, START, lit_close))
    arc_lit[(end_node, START)] = lit_close
    # 层段之间任意先后（作业内顺序由上面的约束保证合法）
    for u in range(1, n + 1):
        from_m = mat[u]
        base_u = ends[u]
        for v in range(1, n + 1):
            if u == v:
                continue
            lit = new_bool(f"a_{u}_{v}")
            arcs.append((u, v, lit))
            arc_lit[(u, v)] = lit
            to_m = mat[v]
            if from_m != to_m:
                purge = int(pmat[from_m][to_m])
                if purge > 0:
                    purge_terms.append(purge * lit)
                if switch_sec:
                    add_ct(starts[v] == base_u + switch_sec).OnlyEnforceIf(lit)
                else:
                    add_ct(starts[v] == base_u).OnlyEnforceIf(lit)
            else:
                add_ct(starts[v] == base_u).OnlyEnforceIf(lit)

    m.AddCircuit(arcs)

    # 固定前缀（字典序决胜阶段使用）
    if force_arcs:
        for (u, v), val in force_arcs.items():
            add_ct(arc_lit[(u, v)] == 1 if val else arc_lit[(u, v)] == 0)

    purge_var = new_int(0, max(inst.purge_capacity * 2 + 1, 1), "purge")
    if purge_terms:
        add_ct(purge_var == sum(purge_terms))
    else:
        add_ct(purge_var == 0)

    # purge 容量：硬约束，超限即无解
    if purge_eq is not None:
        add_ct(purge_var == purge_eq)
    else:
        add_ct(purge_var <= inst.purge_capacity)

    completion: Dict[str, cp_model.IntVar] = {}
    tardy: Dict[str, cp_model.IntVar] = {}
    tardy_terms = []
    for job in inst.jobs:
        comp = new_int(0, inst.horizon, f"comp_{job.job_id}")
        add_ct(comp == ends[job.last_idx])
        completion[job.job_id] = comp
        t = new_int(0, inst.horizon, f"tardy_{job.job_id}")
        add_max(t, [0, comp - job.deadline])
        tardy[job.job_id] = t
        tardy_terms.append(t)

    tardiness_var = new_int(0, inst.horizon * max(n, 1), "tardiness")
    add_ct(tardiness_var == sum(tardy_terms))
    if tardiness_eq is not None:
        add_ct(tardiness_var == tardiness_eq)

    return ModelArtifacts(
        model=m,
        starts=starts,
        ends=ends,
        arc_lit=arc_lit,
        tardy=tardy,
        purge_var=purge_var,
        tardiness_var=tardiness_var,
        completion=completion,
    )


@dataclass
class SolveOutcome:
    status: str  # OPTIMAL / FEASIBLE / INFEASIBLE / UNKNOWN
    message: str
    order: Optional[List[int]] = None
    purge: Optional[int] = None
    tardiness: Optional[int] = None


def _solve(
    artifacts: ModelArtifacts,
    objective: cp_model.IntVar,
    time_limit: float,
    *,
    hint_order: Optional[List[int]] = None,
    inst: Optional[Instance] = None,
) -> SolveOutcome:
    artifacts.model.Minimize(objective)

    # 用已知可行序列给求解器完整提示。大模型（数千条弧）在多线程默认搜索下
    # 可能在整个限时内都走不到第一个可行解；因此先在**克隆模型**上把变量固定
    # 为 hint 值做一次极短的验证性求解，把该可行解确认为 incumbent，
    # 随后原模型上的正常优化即使到期 UNKNOWN，也已有完整可行计划兜底。
    incumbent_order: Optional[List[int]] = None
    incumbent_purge: Optional[int] = None
    incumbent_tard: Optional[int] = None
    if hint_order is not None and inst is not None and time_limit > 0.05:
        _apply_hint(artifacts, inst, hint_order)
        fixed = artifacts.model.clone()
        loader = cp_model.CpSolver()
        loader.parameters.max_time_in_seconds = min(2.0, time_limit)
        loader.parameters.num_workers = 1
        loader.parameters.fix_variables_to_their_hinted_value = True
        loader.parameters.log_search_progress = False
        code0 = loader.Solve(fixed)
        if code0 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            incumbent_order = list(hint_order)
            incumbent_purge = int(loader.Value(artifacts.purge_var))
            incumbent_tard = int(
                sum(loader.Value(t) for t in artifacts.tardy.values())
            )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = 8
    solver.parameters.log_search_progress = False

    status_code = solver.Solve(artifacts.model)

    if status_code == cp_model.OPTIMAL:
        status = "OPTIMAL"
    elif status_code == cp_model.FEASIBLE:
        status = "FEASIBLE"
    elif status_code == cp_model.INFEASIBLE:
        return SolveOutcome(status="INFEASIBLE", message=solver.StatusName(status_code))
    else:
        if incumbent_order is not None and inst is not None:
            # 正常优化未及产出解：返回已由装载步骤确认可行的 hint 解
            return SolveOutcome(
                status="FEASIBLE",
                message="装载初始可行解后，优化在限时内未找到更优解",
                order=incumbent_order,
                purge=incumbent_purge,
                tardiness=incumbent_tard,
            )
        return SolveOutcome(status="UNKNOWN", message=solver.StatusName(status_code))

    # 沿被选中的弧从 START 开始还原执行顺序
    order: List[int] = []
    succ = _successor_map(solver, artifacts.arc_lit)
    node = succ[START]
    end_node = inst.n + 1  # type: ignore[union-attr]
    while node != end_node:
        order.append(node)
        node = succ[node]

    return SolveOutcome(
        status=status,
        message=solver.StatusName(status_code),
        order=order,
        purge=int(solver.Value(artifacts.purge_var)),
        tardiness=int(sum(solver.Value(t) for t in artifacts.tardy.values())),
    )


def _apply_hint(
    art: ModelArtifacts,
    inst: Instance,
    hint_order: List[int],
    *,
    purge_value: Optional[int] = None,
    tardiness_value: Optional[int] = None,
) -> None:
    """给模型填入一个**完整**可行解作为 hint。

    CP-SAT 只有在所有非固定变量都被提示时才会把 hint 当作初始解
    （否则日志报 "solution hint is incomplete" 并忽略它）。因此除路径弧=1、
    starts/ends 外，还需把其余弧显式置 0，并补齐 completion/tardy/purge 等变量。
    可对同一模型重复调用（如阶段 1 后换 hint 做阶段 2）。
    """
    m = art.model
    end_node = inst.n + 1
    hinted: set = getattr(art, "_hinted", set())

    def hint(var, val) -> None:
        if id(var) not in hinted:
            m.AddHint(var, int(val))
            hinted.add(id(var))

    path_arcs = set()
    prev = START
    t = 0
    completion: Dict[str, int] = {}
    purge_used = 0
    last_idx_to_job = {jd.last_idx: jd for jd in inst.jobs}
    for v in hint_order:
        setup_sec, purge = _arc_costs(inst, prev, v, end_node)
        path_arcs.add((prev, v))
        hint(art.starts[v], t + setup_sec)
        hint(art.ends[v], t + setup_sec + inst.duration_of[v])
        t += setup_sec + inst.duration_of[v]
        purge_used += purge
        jd = last_idx_to_job.get(v)
        if jd is not None:
            completion[jd.job_id] = t
        prev = v
    path_arcs.add((prev, end_node))
    path_arcs.add((end_node, START))

    for uv, lit in art.arc_lit.items():
        hint(lit, 1 if uv in path_arcs else 0)

    total_tard = 0
    for jd in inst.jobs:
        comp = completion.get(jd.job_id, 0)
        tardy_v = max(0, comp - jd.deadline)
        total_tard += tardy_v
        hint(art.completion[jd.job_id], comp)
        hint(art.tardy[jd.job_id], tardy_v)
    hint(art.purge_var, purge_used if purge_value is None else purge_value)
    hint(
        art.tardiness_var,
        total_tard if tardiness_value is None else tardiness_value,
    )
    art._hinted = hinted


def _successor_map(solver: cp_model.CpSolver, arc_lit: dict) -> Dict[int, int]:
    succ: Dict[int, int] = {}
    for (u, v), lit in arc_lit.items():
        if solver.Value(lit) == 1:
            succ[u] = v
    return succ


def _feasibility_check(
    inst: Instance,
    force_arcs: Dict[Tuple[int, int], bool],
    purge_star: int,
    tard_star: int,
    time_limit: float,
    hint_order: Optional[List[int]],
) -> Tuple[str, Optional[List[int]]]:
    """在强制弧前缀 + 两级目标值锁定下判定可行性，返回 (状态, 可行时的序列)。"""
    art = build_model(
        inst,
        purge_eq=purge_star,
        tardiness_eq=tard_star,
        force_arcs=force_arcs,
    )
    # 常量目标：纯可行性判定
    art.model.Minimize(art.purge_var)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = 8
    solver.parameters.log_search_progress = False
    if hint_order is not None:
        _apply_hint(
            art,
            inst,
            hint_order,
            purge_value=purge_star,
            tardiness_value=tard_star,
        )

    code = solver.Solve(art.model)
    if code in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        order: List[int] = []
        succ = _successor_map(solver, art.arc_lit)
        node = succ[START]
        while node != inst.n + 1:
            order.append(node)
            node = succ[node]
        return "FEASIBLE", order
    if code == cp_model.INFEASIBLE:
        return "INFEASIBLE", None
    return "UNKNOWN", None


def _lexicographic_fix(
    inst: Instance,
    best_order: List[int],
    purge_star: int,
    tard_star: int,
    per_check_limit: float,
    total_cap: float,
) -> Tuple[List[int], bool]:
    """逐位贪心选出字典序最小的最优序列。

    返回 (序列, 是否全程被证明)。任一可行性判定在限时内 UNKNOWN，
    则保守终止并返回当前已知最优序列，调用方不得据此声称字典序最优。
    """
    n = inst.n
    forced: Dict[Tuple[int, int], bool] = {}
    placed: set[int] = set()
    result: List[int] = []
    prev = START
    hint = list(best_order)
    deadline_wall = time.monotonic() + total_cap

    for _ in range(n):
        candidates = [
            v
            for v in range(1, n + 1)
            if v not in placed
            and (inst.predecessor_of[v] is None or inst.predecessor_of[v] in placed)
        ]
        candidates.sort(key=lambda v: inst.rank_of[v])

        chosen: Optional[int] = None
        for v in candidates:
            trial = dict(forced)
            trial[(prev, v)] = True
            remaining = max(0.05, deadline_wall - time.monotonic())
            limit = min(per_check_limit, remaining) if remaining > 0 else 0.05
            status, feas_order = _feasibility_check(
                inst, trial, purge_star, tard_star, limit, hint
            )
            if status == "FEASIBLE" and feas_order is not None:
                chosen = v
                forced = trial
                hint = feas_order
                break
            if status == "UNKNOWN":
                return best_order, False
            # INFEASIBLE：该候选与前缀不相容，试下一个
        if chosen is None:
            # 理论上不会发生（当前前缀本身可行），保守处理
            return best_order, False
        result.append(chosen)
        placed.add(chosen)
        prev = chosen

    return result, True


def diagnose_infeasible(inst: Instance, time_limit: float) -> Tuple[str, dict]:
    """去掉 purge 容量上限重新最小化 purge，区分容量超限与前置冲突。"""
    # 构造无容量约束模型：临时放大容量
    saved = inst.purge_capacity
    inst.purge_capacity = 10**12
    art = build_model(inst)
    inst.purge_capacity = saved
    outcome = _solve(art, art.purge_var, time_limit, inst=inst)
    if outcome.status == "INFEASIBLE":
        return "PRECEDENCE", {
            "code": "PRECEDENCE",
            "message": "层段先后约束无法满足（作业内层序冲突）",
        }
    if outcome.status in ("OPTIMAL", "FEASIBLE") and outcome.purge is not None:
        if outcome.purge > saved:
            return "CAPACITY", {
                "code": "CAPACITY",
                "message": (
                    f"任何可行排产至少需要 purge {outcome.purge} mm^3，"
                    f"超过总容量 {saved} mm^3"
                ),
                "minimum_required_purge_mm3": outcome.purge,
                "capacity_mm3": saved,
            }
    return (
        "UNKNOWN",
        {
            "code": "UNKNOWN",
            "message": "求解器在限时内无法给出无解的确切归因",
        },
    )


def _fallback_plan(
    inst: Instance,
    candidates: List[List[int]],
    *,
    purge_proven: bool,
    tardiness_proven: bool,
    reason: str,
) -> dict:
    """求解器限时内未给出 incumbent 时的兜底响应。

    从构造性可行序列中取 purge 最小者，始终返回**完整且作业内有序**的计划。
    若该序列 purge 在容量内，它就是被构造本身证明的可行解，标 ``FEASIBLE``；
    若连它都超容量（而是否存在更省 purge 的排产尚未证明），则如实标 ``UNKNOWN``。
    """
    scored = sorted(
        ((_simulate(inst, order), order) for order in candidates),
        key=lambda x: (x[0][0], x[0][1]),
    )
    (fb_purge, fb_tard), best = scored[0]
    feasible_by_construction = fb_purge <= inst.purge_capacity

    timeline = _materialize(inst, best)
    if feasible_by_construction:
        timeline["status"] = "FEASIBLE"
        timeline["optimal"] = False
    else:
        timeline["status"] = "UNKNOWN"
        timeline["optimal"] = False
    timeline["objective"] = {
        "total_purge_mm3": timeline["purge_used_mm3"],
        "total_tardiness_seconds": timeline["total_tardiness_seconds"],
        "purge_optimal": purge_proven,
        "tardiness_optimal": tardiness_proven,
        "lexicographic_minimal": False,
    }
    timeline["infeasible_reason"] = None
    if feasible_by_construction:
        timeline["solver_message"] = (
            f"{reason}；已返回一个构造性可行计划（完整、作业内层段按序），"
            "但其最优性未经 CP-SAT 证明"
        )
    else:
        timeline["solver_message"] = (
            f"{reason}；构造性计划的 purge（{fb_purge} mm^3）已超过容量"
            f"（{inst.purge_capacity} mm^3），限时内既未找到容量内可行解也未证明无解"
        )
    return timeline


def solve(req: PlanRequest) -> dict:
    """求解入口，返回可直接用于构造 PlanResponse 的结果字典。"""
    inst = build_instance(req)
    time_limit = float(req.time_limit_seconds or 15.0)
    # 各阶段预算（在不超过总限时的前提下取用）：
    # 阶段 1/2 各至多 0.4 * time_limit，字典序阶段至多 0.35 * time_limit。
    t_phase = max(1.0, min(10.0, 0.4 * time_limit))
    per_check = max(0.2, min(1.5, 0.1 * time_limit))
    greedy_cap = max(2.0, 0.35 * time_limit)

    # 全局截止时刻：三个阶段共享同一个 time_limit_seconds，时间到期即携带
    # 已找到的最佳可行计划返回，而不是让各阶段各自再花一份预算。
    deadline_wall = time.monotonic() + time_limit

    def remaining() -> float:
        return max(0.0, deadline_wall - time.monotonic())

    # 构造性合法序列：给 CP-SAT 做初始 hint，并在到期无 incumbent 时兜底
    constructive = _constructive_orders(inst)
    constructive.sort(key=lambda o: _simulate(inst, o))
    seed_order = constructive[0]

    # ---- 阶段 1：最小化 purge ----
    art1 = build_model(inst)
    r1 = _solve(
        art1,
        art1.purge_var,
        min(t_phase, remaining()),
        inst=inst,
        hint_order=seed_order,
    )

    if r1.status == "INFEASIBLE":
        # CP-SAT 中 max_time_in_seconds=0 表示无限制，诊断阶段至少给 0.05 秒
        diag_limit = max(0.05, min(t_phase, remaining()))
        code, detail = diagnose_infeasible(inst, diag_limit)
        return {
            "status": "INFEASIBLE",
            "optimal": False,
            "infeasible_reason": detail,
            "solver_message": f"模型无解，归因: {code}",
        }
    if r1.order is None or r1.purge is None:
        # 到期连一个可行 incumbent 都没有（大模型 + 极短限时可能发生）。
        # 先用剩余预算尝试容量归因：只有"无容量模型证明最优 purge 仍超容量"
        # 才能如实判 CAPACITY；归因本身也未证实时，再用构造序列兜底。
        diag_limit = max(0.05, min(t_phase, remaining()))
        code, detail = diagnose_infeasible(inst, diag_limit)
        if code in ("CAPACITY", "PRECEDENCE"):
            return {
                "status": "INFEASIBLE",
                "optimal": False,
                "infeasible_reason": detail,
                "solver_message": f"模型无解，归因: {code}",
            }
        return _fallback_plan(
            inst,
            constructive,
            purge_proven=False,
            tardiness_proven=False,
            reason="阶段 1（最小化 purge）限时内未找到可行解",
        )
    purge_proven = r1.status == "OPTIMAL"
    purge_star = r1.purge
    best_order = r1.order

    # ---- 阶段 2：purge 固定，最小化逾期（复用阶段 1 的模型，省去重复建模）----
    tardiness_proven = False
    if remaining() > 0.05:
        art1.model.Add(art1.purge_var == purge_star)
        r2 = _solve(
            art1,
            art1.tardiness_var,
            min(t_phase, remaining()),
            inst=inst,
            hint_order=r1.order,
        )
        if r2.status == "INFEASIBLE" or r2.order is None or r2.tardiness is None:
            # 不应发生：阶段 1 的解本身满足 purge==purge_star；
            # 退回阶段 1 解重算逾期，保守地不声称逾期最优
            tard_star = _materialize(inst, r1.order)["total_tardiness_seconds"]
        else:
            best_order = r2.order
            tard_star = r2.tardiness
            # 只有在 purge 最优值本身已被证明的前提下，锁定 purge 后证明的
            # 逾期最优才是真实的逾期最优；否则一律保守标记。
            tardiness_proven = purge_proven and r2.status == "OPTIMAL"
    else:
        # 时间已耗尽：保留阶段 1 的可行解与真实逾期值，但不声称逾期最优
        tard_star = _materialize(inst, r1.order)["total_tardiness_seconds"]

    # ---- 阶段 3：字典序最小决胜（前两级均已证最优、且仍有余量时才做）----
    lex_proven = False
    lex_budget = min(greedy_cap, remaining())
    if purge_proven and tardiness_proven and lex_budget >= 0.2:
        best_order, lex_proven = _lexicographic_fix(
            inst,
            best_order,
            purge_star,
            tard_star,
            per_check,
            lex_budget,
        )

    timeline = _materialize(inst, best_order)
    all_proven = purge_proven and tardiness_proven and lex_proven
    timeline["status"] = "OPTIMAL" if all_proven else "FEASIBLE"
    timeline["optimal"] = all_proven
    timeline["objective"] = {
        "total_purge_mm3": timeline["purge_used_mm3"],
        "total_tardiness_seconds": timeline["total_tardiness_seconds"],
        "purge_optimal": purge_proven,
        "tardiness_optimal": tardiness_proven,
        "lexicographic_minimal": lex_proven,
    }
    timeline["infeasible_reason"] = None
    if all_proven:
        timeline["solver_message"] = "已证明三级目标（purge、逾期、字典序）均最优"
    elif purge_proven and tardiness_proven:
        timeline["solver_message"] = (
            "purge 与逾期均已证最优；字典序决胜在限时内未完全证明，"
            "返回当前最优目标值下的可行计划（不声称字典序最小）"
        )
    elif purge_proven:
        timeline["solver_message"] = (
            "purge 已证最优；逾期最优在限时内未证明，"
            "返回当前最优 purge 下的可行计划"
        )
    else:
        timeline["solver_message"] = (
            "限时内得到可行解并给出了目标值，但未证明最优"
        )
    return timeline


def _materialize(inst: Instance, order: List[int]) -> dict:
    """按执行顺序紧密重排，生成输出计划、切换记录与逾期明细。"""
    t = 0
    cur = inst.initial_material
    plan = []
    switches = []
    purge_used = 0
    completion: Dict[str, int] = {}
    last_idx_to_job = {jd.last_idx: jd for jd in inst.jobs}

    for pos, gi in enumerate(order, start=1):
        mat = inst.material_of[gi]
        if mat != cur:
            purge = int(inst.purge_matrix[cur][mat])
            switches.append(
                {
                    "order": len(switches) + 1,
                    "from_material": cur,
                    "to_material": mat,
                    "purge_mm3": purge,
                    "switch_seconds": inst.switch_seconds,
                    "after_segment_order": pos - 1,
                    "at_seconds": t,
                }
            )
            purge_used += purge
            t += inst.switch_seconds
            cur = mat
        start = t
        t += inst.duration_of[gi]
        plan.append(
            {
                "order": pos,
                "job_id": inst.job_of[gi],
                "segment_index": inst.seg_index_of[gi],
                "material_id": mat,
                "print_seconds": inst.duration_of[gi],
                "volume_mm3": inst.volume_of[gi],
                "start_seconds": start,
                "end_seconds": t,
            }
        )
        # 若该层段是某作业最后一段，记录完成时刻
        jd = last_idx_to_job.get(gi)
        if jd is not None:
            completion[jd.job_id] = t

    job_tardiness = []
    total_tardiness = 0
    for job in inst.jobs:
        comp = completion.get(job.job_id, 0)
        tardy = max(0, comp - job.deadline)
        total_tardiness += tardy
        job_tardiness.append(
            {
                "job_id": job.job_id,
                "completion_seconds": comp,
                "deadline_seconds": job.deadline,
                "tardy_seconds": tardy,
            }
        )

    return {
        "plan": plan,
        "switches": switches,
        "job_tardiness": job_tardiness,
        "purge_used_mm3": purge_used,
        "purge_remaining_mm3": inst.purge_capacity - purge_used,
        "total_tardiness_seconds": total_tardiness,
        "makespan_seconds": t,
    }
