# 多材料 3D 打印换料规划 API

基于 **Python + FastAPI + Google OR-Tools（CP-SAT）** 的排产服务。给定 2～8 个打印作业
（每个作业由若干**按顺序执行的层段**组成）、初始材料、材料切换 purge 体积矩阵、
单次切换耗时与总 purge 容量，计算层段交错执行计划：

- 作业之间**只能在层段边界交错**，层段不可拆分，作业内层段严格按序；
- 每次材料切换按 `purge_matrix[from][to]` 消耗 purge 容量，并占用 `switch_seconds` 秒
  （首层段材料与初始材料不同时，也要先切换再打印）；同材料相邻无需切换；
- 作业逾期 = `max(0, 该作业最后层段完成时刻 - 截止秒数)`；
- purge 总消耗超过 `total_purge_capacity_mm3` 即**无解**。

优化目标按严格优先级依次为：

1. **总 purge 体积最小**；
2. 在 purge 最优的前提下，**总逾期秒数最小**；
3. 前两者相同时，按执行序列的 `(作业 ID, 层序)` 取**字典序最小**计划
   （作业 ID 字符串升序，再按作业内层段序号升序，逐位比较）。

返回切换记录、purge 余量、逐作业逾期明细与求解状态；未在限时内证明最优时
**不会**标记为最优。

---

## 一、Docker Compose 一键启动

前置要求：已安装 Docker 与 Docker Compose 插件。

```bash
docker compose up --build -d
```

服务监听在宿主机 `http://localhost:8000`。查看日志 / 停止：

```bash
docker compose logs -f
docker compose down
```

健康检查：

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

交互式 API 文档（浏览器打开）：

- Swagger UI：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc

## 二、本地直接运行（可选）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install pytest
pytest -q
```

## 三、调用接口

### `POST /plans`

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `jobs` | 数组（2～8 项） | 作业列表 |
| `jobs[].job_id` | 非空字符串 | 作业 ID，全局唯一 |
| `jobs[].deadline_seconds` | 非负整数 | 截止秒数（相对开工时刻 0） |
| `jobs[].segments` | 非空数组（至少 1 段，无固定上限） | 按打印顺序排列的层段；单作业 25 段及以上的长作业同样进入 CP-SAT 规划 |
| `segments[].material_id` | 非空字符串 | 层段材料 ID |
| `segments[].print_seconds` | 非负整数 | 层段打印秒数 |
| `segments[].volume_mm3` | 非负整数 | 层段耗材体积 mm³（记录用） |
| `initial_material` | 非空字符串 | 喷头初始材料 |
| `purge_matrix` | 对象 | `{from: {to: purge体积}}`，非负整数；对角必须为 0；所有被引用材料（含初始材料）构成的子矩阵行列必须完整 |
| `switch_seconds` | 非负整数 | 每次材料切换的固定耗时（秒） |
| `total_purge_capacity_mm3` | 非负整数 | 全流程 purge 总容量（mm³） |
| `time_limit_seconds` | 数值，0.1～300，可选 | 求解器时间上限，默认 15 秒 |

请求示例（文件见 `examples/request.json`）：

```json
{
  "jobs": [
    {
      "job_id": "J1",
      "deadline_seconds": 120,
      "segments": [
        {"material_id": "PLA", "print_seconds": 60, "volume_mm3": 100},
        {"material_id": "ABS", "print_seconds": 30, "volume_mm3": 40}
      ]
    },
    {
      "job_id": "J2",
      "deadline_seconds": 220,
      "segments": [
        {"material_id": "ABS", "print_seconds": 80, "volume_mm3": 120},
        {"material_id": "PLA", "print_seconds": 20, "volume_mm3": 30}
      ]
    }
  ],
  "initial_material": "PLA",
  "purge_matrix": {
    "PLA": {"PLA": 0, "ABS": 120},
    "ABS": {"PLA": 80, "ABS": 0}
  },
  "switch_seconds": 10,
  "total_purge_capacity_mm3": 400,
  "time_limit_seconds": 15
}
```

调用：

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d @examples/request.json
```

**响应（与服务实际输出一致）**：

```json
{
  "status": "OPTIMAL",
  "optimal": true,
  "objective": {
    "total_purge_mm3": 200,
    "total_tardiness_seconds": 0,
    "purge_optimal": true,
    "tardiness_optimal": true,
    "lexicographic_minimal": true
  },
  "plan": [
    {"order": 1, "job_id": "J1", "segment_index": 1, "material_id": "PLA",
     "print_seconds": 60, "volume_mm3": 100, "start_seconds": 0, "end_seconds": 60},
    {"order": 2, "job_id": "J1", "segment_index": 2, "material_id": "ABS",
     "print_seconds": 30, "volume_mm3": 40, "start_seconds": 70, "end_seconds": 100},
    {"order": 3, "job_id": "J2", "segment_index": 1, "material_id": "ABS",
     "print_seconds": 80, "volume_mm3": 120, "start_seconds": 100, "end_seconds": 180},
    {"order": 4, "job_id": "J2", "segment_index": 2, "material_id": "PLA",
     "print_seconds": 20, "volume_mm3": 30, "start_seconds": 190, "end_seconds": 210}
  ],
  "switches": [
    {"order": 1, "from_material": "PLA", "to_material": "ABS",
     "purge_mm3": 120, "switch_seconds": 10,
     "after_segment_order": 1, "at_seconds": 60},
    {"order": 2, "from_material": "ABS", "to_material": "PLA",
     "purge_mm3": 80, "switch_seconds": 10,
     "after_segment_order": 3, "at_seconds": 180}
  ],
  "job_tardiness": [
    {"job_id": "J1", "completion_seconds": 100, "deadline_seconds": 120, "tardy_seconds": 0},
    {"job_id": "J2", "completion_seconds": 210, "deadline_seconds": 220, "tardy_seconds": 0}
  ],
  "purge_used_mm3": 200,
  "purge_remaining_mm3": 200,
  "total_tardiness_seconds": 0,
  "makespan_seconds": 210,
  "infeasible_reason": null,
  "solver_message": "已证明三级目标（purge、逾期、字典序）均最优"
}
```

结果解读：最优排产把两个 ABS 层段相邻（J1.2 后紧跟 J2.1，免一次切换），
全流程仅 PLA→ABS（120 mm³）与 ABS→PLA（80 mm³）两次切换，共 200 mm³，
两个作业均按期完成，purge 余量 200 mm³。

### 无解响应（容量超限）

将 `examples/request_infeasible.json` 中容量改为 150 mm³：

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d @examples/request_infeasible.json
```

```json
{
  "status": "INFEASIBLE",
  "optimal": false,
  "objective": null,
  "plan": [],
  "switches": [],
  "job_tardiness": [],
  "purge_used_mm3": 0,
  "purge_remaining_mm3": null,
  "total_tardiness_seconds": 0,
  "makespan_seconds": 0,
  "infeasible_reason": {
    "code": "CAPACITY",
    "message": "任何可行排产至少需要 purge 200 mm^3，超过总容量 150 mm^3",
    "minimum_required_purge_mm3": 200,
    "capacity_mm3": 150
  },
  "solver_message": "模型无解，归因: CAPACITY"
}
```

无解归因 `infeasible_reason.code`：

- `CAPACITY`：层段先后本身可排，但最小 purge 需求仍超过容量（响应给出
  `minimum_required_purge_mm3`）；
- `PRECEDENCE`：层段前置约束冲突（本服务层序由输入保证，正常数据下不会出现，
  仅作为防御性归因）；
- `UNKNOWN`：限时内无法完成归因诊断。

### 长作业（单作业 25 段及以上）

层段数量没有固定上限：单个作业包含 25 个或更多按序层段时是**合法请求**，
不会被拒绝（HTTP 422 仅针对真正非法的输入），而是直接进入 OR-Tools
CP-SAT 规划。示例 `examples/request_long_job.json` 中 `LONG` 作业含 25 个
按序层段（PLA/ABS/TPU 循环）：

```bash
docker compose up --build -d

curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d @examples/request_long_job.json
```

响应与普通请求结构完全一致：`plan` 覆盖全部层段且每个作业内的
`segment_index` 严格按 1…N 排列，`switches` / `job_tardiness` /
`purge_used_mm3` / `purge_remaining_mm3` 等明细同样完整返回；三级目标
全部由 CP-SAT 证明时才标记 `"status": "OPTIMAL"`。

当模型较大或 `time_limit_seconds` 到期时：

- CP-SAT 已给出可行解但未完成最优性证明 → `FEASIBLE`，`objective` 中
  `purge_optimal` / `tardiness_optimal` / `lexicographic_minimal` 分别
  如实反映各级目标**是否确已证明**（可能 purge 已证最优而字典序未证明）；
- CP-SAT 连可行解都未找到 → 服务用免求解的**构造性排列**兜底（严格遵守
  作业内层段顺序，purge 经精确核算不超容），仍返回完整、按序的计划与全部
  明细，状态为 `FEASIBLE`，且三个最优性标记均为 `false`；
- 构造性候选也不满足 purge 容量（或确属容量无解）→ `UNKNOWN` /
  `INFEASIBLE`，此时 `plan` 为空，不会伪造计划。

可用极短时限观察该行为（大模型通常来不及证明最优）：

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d @examples/request_long_job.json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["objective"])'
# OPTIMAL {'total_purge_mm3': 2320, ..., 'lexicographic_minimal': True}
```

### 求解状态语义

| `status` | `optimal` | 含义 |
| --- | --- | --- |
| `OPTIMAL` | `true` | 三级目标（含字典序决胜）均已由 CP-SAT 证明最优 |
| `FEASIBLE` | `false` | 已得到可行计划并给出完整明细，但**未证明全部目标最优**；`objective` 中三个 `*_optimal` 标记分别如实表示 purge / 逾期 / 字典序是否确已证明（含 CP-SAT 超时后由构造性排列兜底的情形，此时三者均为 `false`） |
| `INFEASIBLE` | `false` | 无解，见 `infeasible_reason` |
| `UNKNOWN` | `false` | 限时内连可行计划都无法给出（构造性候选也不满足容量） |

### 非法数据（HTTP 422）

以下情况返回 **422 Unprocessable Entity**（FastAPI/Pydantic 标准错误体，含字段定位）：

- 作业数不在 2～8；`job_id` 重复或为空；
- 层段列表为空；打印秒数 / 体积 / 截止秒数 / 切换耗时 / 容量为负；
- purge 矩阵单元格为负、对角非 0；
- 初始材料或某层段材料在矩阵中缺行或缺列；
- 出现未声明的额外字段、类型错误等。

示例：

```bash
curl -i -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{"jobs":[],"initial_material":"PLA",
       "purge_matrix":{"PLA":{"PLA":0}},
       "switch_seconds":10,"total_purge_capacity_mm3":100}'
# HTTP/1.1 422 Unprocessable Entity
```

---

## 四、项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 应用：GET /health、POST /plans
│   ├── schemas.py     # 请求/响应模型与 422 输入校验
│   └── solver.py      # CP-SAT 建模、分阶段求解与字典序决胜
├── examples/
│   ├── request.json              # 可行示例
│   ├── request_long_job.json     # 长作业示例（单作业 25 段）
│   └── request_infeasible.json   # 容量超限示例
├── tests/
│   └── test_plans.py  # 13 个端到端测试（最优性、无解、交错、优先级、长作业、超时兜底、422 等）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## 五、建模与求解说明

- 每个层段为不可拆分节点，另设虚拟起/止节点；节点间有向弧表示“紧随其后执行”，
  弧上携带 purge 体积与切换耗时；`AddCircuit` 强制选出一条覆盖全部层段的
  Hamilton 回路（即合法排列）。
- 作业内层段用先后约束限制；弧被选中时强制层段开始时刻 = 上一层段结束 +
  切换耗时，因此计划天然紧密无空转。
- 求解分三阶段：① CP-SAT 最小化总 purge（容量为硬约束）；② 固定 purge 最优值
  后最小化总逾期；③ **逐位贪心 + 可行性证明**：对执行序列的每个位置，按
  `(作业 ID, 层序)` 升序逐一尝试固定候选层段并用 CP-SAT 验证可行性，第一个可行者
  即为该位字典序最优选择；每一步都经过可行性证明，全部位置完成即字典序最小。
  任何一步在限时内无法判定，则返回当前最优目标值下的可行计划并保守地标为
  `FEASIBLE`，绝不声称未证明的最优性。
- 无解时临时移除容量上限再求最小 purge：仍无解归因为 `PRECEDENCE`，
  最小 purge 超出容量则归因为 `CAPACITY` 并给出最小需求值。
- 层段数没有固定上限，长作业直接建模；大模型在 `time_limit_seconds` 内
  未求得可行解时，用免求解的构造性排列（作业顺序、截止时间升序等候选）
  兜底并精确核算 purge，仍返回完整、按序的计划与全部明细，状态如实标为
  `FEASIBLE`，仅当兜底排列也超容时才返回 `UNKNOWN`。三级目标的最优性
  标记互相独立，只有 CP-SAT 对相应目标（以及字典序决胜的每一步可行性）
  确已证明时才置为 `true`。
