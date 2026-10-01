"""请求 / 响应数据模型与输入校验。

非法数据统一由 Pydantic 抛出 ``RequestValidationError``，FastAPI 自动返回
HTTP 422（见 :mod:`app.main` 中对错误响应的统一格式处理）。
"""

from __future__ import annotations

from typing import Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SegmentIn(BaseModel):
    """单个层段：一种材料的连续打印区间。"""

    model_config = ConfigDict(extra="forbid")

    material_id: str = Field(..., min_length=1, description="层段所用材料 ID")
    print_seconds: int = Field(..., ge=0, description="该层段打印耗时（秒）")
    volume_mm3: int = Field(..., ge=0, description="该层段耗材体积（mm^3，仅记录用）")


class JobIn(BaseModel):
    """一个打印作业，按顺序给出层段与交付截止时间。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(..., min_length=1, description="作业 ID")
    deadline_seconds: int = Field(..., ge=0, description="截止秒数（相对开工时刻 0）")
    segments: list[SegmentIn] = Field(
        ...,
        min_length=1,
        max_length=24,
        description="按打印顺序排列的层段（1~24 段）",
    )


class PlanRequest(BaseModel):
    """POST /plans 的请求体。"""

    model_config = ConfigDict(extra="forbid")

    jobs: list[JobIn] = Field(
        ...,
        min_length=2,
        max_length=8,
        description="2~8 个作业",
    )
    initial_material: str = Field(..., min_length=1, description="开机时喷头内的初始材料")
    # purge_matrix[from][to] = 从材料 from 切换到材料 to 需要排出的 purge 体积
    purge_matrix: Dict[str, Dict[str, int]] = Field(
        ...,
        description="材料切换 purge 体积矩阵（mm^3），键为材料 ID",
    )
    switch_seconds: int = Field(..., ge=0, description="每次材料切换的固定耗时（秒）")
    total_purge_capacity_mm3: int = Field(..., ge=0, description="全流程 purge 总容量（mm^3）")
    time_limit_seconds: Optional[float] = Field(
        default=None,
        ge=0.1,
        le=300.0,
        description="求解器时间上限（秒，可选，默认 15）",
    )

    @model_validator(mode="after")
    def _validate(self) -> "PlanRequest":
        # 作业 ID 唯一
        job_ids = [j.job_id for j in self.jobs]
        if len(set(job_ids)) != len(job_ids):
            raise ValueError("jobs 中 job_id 必须唯一")

        used_materials = {self.initial_material}
        for job in self.jobs:
            for seg in job.segments:
                used_materials.add(seg.material_id)

        # purge 矩阵条目必须为非负整数；同材料切换 purge 必须为 0
        for from_m, row in self.purge_matrix.items():
            if not isinstance(from_m, str) or not from_m:
                raise ValueError("purge_matrix 的键必须为非空字符串")
            for to_m, val in row.items():
                if not isinstance(to_m, str) or not to_m:
                    raise ValueError("purge_matrix 的键必须为非空字符串")
                if val < 0:
                    raise ValueError(
                        f"purge_matrix[{from_m}][{to_m}] 不能为负"
                    )
                if from_m == to_m and val != 0:
                    raise ValueError(
                        f"purge_matrix[{from_m}][{to_m}] 同材料切换必须为 0"
                    )

        # 被引用材料构成的子矩阵必须完整：每个来源材料行都必须存在，
        # 且包含每个目标材料列（允许出现未被引用的额外材料）。
        used = sorted(used_materials)
        for from_m in used:
            row = self.purge_matrix.get(from_m)
            if row is None:
                raise ValueError(f"purge_matrix 缺少来源材料行: {from_m}")
            missing = [to_m for to_m in used if to_m not in row]
            if missing:
                raise ValueError(
                    f"purge_matrix[{from_m}] 缺少目标材料列: " + ", ".join(missing)
                )
        return self


class PlannedSegment(BaseModel):
    """输出计划中的单个层段（按实际执行顺序排列）。"""

    order: int = Field(..., description="全局执行序号，从 1 开始")
    job_id: str
    segment_index: int = Field(..., description="作业内层段序号，从 1 开始")
    material_id: str
    print_seconds: int
    volume_mm3: int
    start_seconds: int
    end_seconds: int


class SwitchRecord(BaseModel):
    """一次材料切换（含从初始材料开始的首次切换）。"""

    order: int = Field(..., description="切换序号，从 1 开始")
    from_material: Optional[str] = Field(
        ..., description="切换前材料；首次切换来自初始材料，仍显式给出"
    )
    to_material: str
    purge_mm3: int
    switch_seconds: int
    after_segment_order: int = Field(
        ...,
        description="该切换发生在第几号层段之后；0 表示在首个层段之前",
    )
    at_seconds: int = Field(..., description="切换开始时刻（秒）")


class JobTardiness(BaseModel):
    job_id: str
    completion_seconds: int
    deadline_seconds: int
    tardy_seconds: int


class PlanResponse(BaseModel):
    status: str = Field(
        ...,
        description="OPTIMAL / FEASIBLE / INFEASIBLE / UNKNOWN",
    )
    optimal: bool = Field(..., description="是否已证明最优（含字典序决胜已证最优）")
    objective: Optional[dict] = Field(
        None,
        description="目标值：总 purge、总逾期，以及决胜阶段是否已证字典序最小"
    )
    plan: list[PlannedSegment] = Field(default_factory=list)
    switches: list[SwitchRecord] = Field(default_factory=list)
    job_tardiness: list[JobTardiness] = Field(default_factory=list)
    purge_used_mm3: int = 0
    purge_remaining_mm3: Optional[int] = None
    total_tardiness_seconds: int = 0
    makespan_seconds: int = 0
    infeasible_reason: Optional[dict] = Field(
        None,
        description="无解原因：CAPACITY（容量）或 PRECEDENCE（前置冲突）"
    )
    solver_message: str = ""
