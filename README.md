# 多材料 3D 打印换料规划 API

基于 **Python + FastAPI + Google OR-Tools（CP-SAT）** 的排产服务。给定 2～8 个打印作业
（每个作业由若干**按顺序执行的层段**组成，**单作业层段数不设上限，25 段及以上的长作业
同样受理**）、初始材料、材料切换 purge 体积矩阵、单次切换耗时与总 purge 容量，计算层段
交错执行计划：

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

服务启动后即可提交规划请求。普通示例与**长作业示例**（单作业 25 段）：

```bash
# 普通示例（4 层段）
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d @examples/request.json

# 长作业示例（J1 含 25 个层段，共 28 层段；过去会被 422 拒绝，现正常进入 CP-SAT）
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d @examples/request_long_job.json
```

长作业在较大规模或限时较短时的状态语义，见后文
[长作业（单作业 25 段及以上）](#长作业单作业-25-段及以上) 一节。

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
| `jobs[].segments` | 非空数组（≥1 段，**无上限**） | 按打印顺序排列的层段；25 段及以上的长作业正常受理 |
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

层段数量**不再有 24 段的隐藏上限**：任何非空层段列表都会进入 CP-SAT 规划。
示例文件 `examples/request_long_job.json` 中 `J1` 含 **25 个层段**、`J2` 含 3 个
层段（共 28 段）。启动服务后：

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d @examples/request_long_job.json
```

返回（2026 年在普通开发机上约 2～3 秒；下方为节选，`…` 处为省略内容）：

```jsonc
{
  "status": "OPTIMAL",
  "optimal": true,
  "objective": {
    "total_purge_mm3": 1200,
    "total_tardiness_seconds": 0,
    "purge_optimal": true,
    "tardiness_optimal": true,
    "lexicographic_minimal": true
  },
  "plan": [
    {"order": 1, "job_id": "J1", "segment_index": 1, "material_id": "PLA",
     "print_seconds": 10, "volume_mm3": 12, "start_seconds": 0, "end_seconds": 10}
    // … 共 28 条：J1 的 segment_index 严格为 1..25、J2 为 1..3 …
  ],
  "switches": [
    // … 24 条切换记录，字段与上方普通示例相同 …
  ],
  "job_tardiness": [
    {"job_id": "J1", "completion_seconds": 390, "deadline_seconds": 100000, "tardy_seconds": 0},
    {"job_id": "J2", "completion_seconds": 400, "deadline_seconds": 100000, "tardy_seconds": 0}
  ],
  "purge_used_mm3": 1200,
  "purge_remaining_mm3": 8800,
  "total_tardiness_seconds": 0,
  "makespan_seconds": 400,
  "infeasible_reason": null,
  "solver_message": "已证明三级目标（purge、逾期、字典序）均最优"
}
```

**更大模型 / 限时到期时的行为**：当层段总数很大（建模规模约为 n² 条弧）或
`time_limit_seconds` 很短时，CP-SAT 可能来不及证明最优。此时服务：

- **始终返回完整计划**（每个层段一条、`order` 连续、作业内层段严格 1..k 按序）、
  完整切换记录、purge 余量与逐作业逾期明细——不会因"没算完"返回空计划；
- 只在 purge、逾期、字典序三级目标**都已被证明**时才标 `"status": "OPTIMAL"`；
  得到容量内可行计划但未证明最优时标 `"FEASIBLE"`；
  限时内连容量内可行构造都不存在时标 `"UNKNOWN"`（仍附上完整构造计划供参考）；
- 服务会先构造"作业 ID 串联"与"同材料贪心"两个合法序列作为 CP-SAT 的初始解，
  三个求解阶段共享同一个墙钟限时，到期即携带当前最佳计划返回。

例如对 8 个作业、每作业 26 段（共 208 层段）的请求把 `time_limit_seconds` 设为
`0.5`，仍会得到 HTTP 200、208 条作业内严格按序的计划与 `FEASIBLE` 状态——
既不会是 422，也不会是空计划；把限时放宽到默认 15 秒则可能进一步证明为
`OPTIMAL`。

### 求解状态语义

| `status` | `optimal` | 含义 |
| --- | --- | --- |
| `OPTIMAL` | `true` | purge、逾期、字典序三级目标均已由 CP-SAT 证明最优 |
| `FEASIBLE` | `false` | 得到容量内可行计划，`objective` 给出当前目标值，但三级目标**未全部证明**（purge/逾期未证或字典序决胜未完成证明时都用此状态） |
| `INFEASIBLE` | `false` | 无解，见 `infeasible_reason`，`plan` 为空 |
| `UNKNOWN` | `false` | 限时内未找到容量内可行解、也未证明无解；响应仍附一份完整的构造性计划供参考（其 purge 可能已超容量，`optimal=false` 且各最优标记均为 false） |

### 非法数据（HTTP 422）

以下情况返回 **422 Unprocessable Entity**（FastAPI/Pydantic 标准错误体，含字段定位）：

- 作业数不在 2～8；`job_id` 重复或为空；
- 层段列表为空；打印秒数 / 体积 / 截止秒数 / 切换耗时 / 容量为负；
  （注意：层段数量**没有**上限，25 段及以上属于合法长作业，见上一节）
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
│   ├── request.json              # 可行示例（4 段）
│   ├── request_long_job.json     # 长作业示例（J1 含 25 段，共 28 段）
│   └── request_infeasible.json   # 容量超限示例
├── tests/
│   └── test_plans.py  # 13 个端到端测试（最优性、长作业、超时语义、无解、交错、优先级、422 等）
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
  后最小化总逾期（复用阶段 1 的模型，避免大模型重复建模）；③ **逐位贪心 +
  可行性证明**：对执行序列的每个位置，按 `(作业 ID, 层序)` 升序逐一尝试固定
  候选层段并用 CP-SAT 验证可行性，第一个可行者即为该位字典序最优选择；
  每一步都经过可行性证明，全部位置完成即字典序最小。
  任何一步在限时内无法判定，则返回当前最优目标值下的可行计划并保守地标为
  `FEASIBLE`，绝不声称未证明的最优性。
- 三个阶段共享同一个墙钟截止时刻（即请求的 `time_limit_seconds`，建模耗时另计）。
  求解前先构造"作业 ID 串联"和"同材料贪心"两个合法序列作为**完整 solution
  hint**，并在克隆模型上固定 hint 值快速确认其为初始可行解；因此即便主优化
  到期毫无产出，也有完整、作业内严格按序的计划可返回。
- 无解时临时移除容量上限再求最小 purge：仍无解归因为 `PRECEDENCE`，
  最小 purge 超出容量则归因为 `CAPACITY` 并给出最小需求值。
