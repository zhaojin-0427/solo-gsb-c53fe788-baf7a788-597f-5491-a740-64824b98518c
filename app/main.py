"""FastAPI 应用入口：POST /plans。

* 非法请求体由 Pydantic 校验，自动返回 HTTP 422。
* 可行 / 无解 / 超时都在 200 响应中以 status 字段区分；
  无解时 infeasible_reason 指出 CAPACITY 或 PRECEDENCE。
"""

from __future__ import annotations

from fastapi import FastAPI

from .schemas import PlanRequest, PlanResponse
from .solver import solve

app = FastAPI(
    title="多材料 3D 打印换料规划 API",
    version="1.0.0",
    description=(
        "在 purge 容量与作业截止时间约束下，规划多材料打印作业的层段交错顺序。"
        "目标优先级：总 purge 体积最小 → 总逾期最小 → 计划字典序最小。"
    ),
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/plans", response_model=PlanResponse)
def create_plan(req: PlanRequest) -> dict:
    return solve(req)
