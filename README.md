# 汉字笔顺轨迹校验 API

基于 **FastAPI + SQLite** 的汉字书写校验服务：接收目标汉字的参考笔画序列与用户书写轨迹，
按**笔画方向、起止点、书写顺序**做归一化匹配，支持**可配置的分叉/转折笔画容差**，
返回**首个不合格笔画、偏差原因与整体得分**。同一提交幂等。

## 快速启动

### Docker Compose（推荐）

```bash
docker compose up --build
```

启动后访问交互式文档（Swagger UI）：**http://localhost:8000/docs**

### 本地开发

```bash
pip install -r requirements.txt
DATABASE_PATH=./data.db uvicorn app.main:app --port 8000
```

运行测试：

```bash
pip install pytest httpx
pytest tests/ -q
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/healthz` | 健康检查 |
| `GET` | `/v1/config` | 查看服务端默认容差 |
| `PUT` | `/v1/references/{character}` | 注册/更新某汉字的参考笔画序列 |
| `GET` | `/v1/references/{character}` | 查询参考笔画 |
| `POST` | `/v1/validate` | 校验一次书写提交（幂等） |
| `GET` | `/v1/submissions/{submission_id}` | 按幂等键查询历史结果 |

### 示例：校验“十”字

```bash
# 1. 注册参考笔画（横、竖，画布坐标）
curl -X PUT http://localhost:8000/v1/references/十 \
  -H 'Content-Type: application/json' \
  -d '{"character":"十","strokes":[[[100,200],[300,200]],[[200,100],[200,300]]]}'

# 2. 提交用户轨迹（reference_id 引用已注册参考；也可内联 reference_strokes）
curl -X POST http://localhost:8000/v1/validate \
  -H 'Content-Type: application/json' \
  -d '{
        "submission_id": "attempt-0001",
        "character": "十",
        "reference_id": "十",
        "strokes": [[[102,198],[298,203]],[[201,99],[199,301]]]
      }'
```

响应（节选）：

```json
{
  "submission_id": "attempt-0001",
  "passed": true,
  "score": 97.7,
  "first_failed_stroke": null,
  "deviation": null,
  "strokes": [
    {"index": 0, "passed": true, "score": 96.8, "reasons": [],
     "metrics": {"start_deviation": 0.0129, "end_deviation": 0.0164,
                  "direction_deviation_deg": 1.46, "shape_deviation": 0.0079,
                  "is_forked": false}}
  ],
  "idempotent_replay": false
}
```

不合格时 `deviation` 给出首个失败笔画的原因码与实测值：

```json
{
  "passed": false,
  "first_failed_stroke": 0,
  "deviation": {
    "code": "START_POINT_DEVIATION",
    "stroke_index": 0,
    "message": "第 1 笔起点偏差过大（0.273，上限 0.150）",
    "metrics": { ... }
  }
}
```

### 偏差原因码

| code | 含义 |
| --- | --- |
| `STROKE_COUNT_MISMATCH` | 笔画数量不符（缺笔/多笔） |
| `WRONG_ORDER` | 笔顺错误（该笔与另一笔参考形状相符） |
| `START_POINT_DEVIATION` | 起点偏差超限 |
| `END_POINT_DEVIATION` | 终点偏差超限 |
| `DIRECTION_DEVIATION` | 运笔方向偏差超限 |
| `SHAPE_DEVIATION` | 笔画形状偏差超限 |

## 匹配算法

1. **归一化**：以参考字外接框（扩成正方形 + 5% 边距）做仿射变换映射到 `0..1` 区间，
   用户轨迹套用**同一变换**，保留位置与尺寸的绝对偏差；请求携带 `canvas_size` 时改按画布归一化。
2. **重采样**：每笔按弧长等距重采样为 32 个点（`RESAMPLE_POINTS` 可调）。
3. **逐笔比对**（按书写顺序一一对应）：
   - 起点/终点：归一化欧氏距离 ≤ `start_end`；
   - 方向：起止位移向量夹角 ≤ `direction_deg`；分叉笔画按拐点分段，取各段最大偏差；
   - 形状：重采样点平均点对点距离 ≤ `shape`。
4. **错序检测**：某笔不合格但与**另一笔**参考完全匹配时，判定为 `WRONG_ORDER`。
5. **计分**：单笔按四项指标在 2 倍容差内线性衰减加权（起点 20% / 终点 20% / 方向 30% / 形状 30%），
   整体分 = 单笔均分 × 笔画覆盖率（缺笔/多笔均扣分）。

### 分叉/转折笔画容差

折、钩类复合笔画（如“┐”）在拐点处书写误差天然更大。引擎先在参考笔画上检测拐点
（转角 ≥ `corner_angle_deg`），对含拐点的笔画自动放宽：

- 方向容差 `+ fork_extra_direction_deg`（默认 +20°）
- 形状容差 `+ fork_extra_shape`（默认 +0.08）

两项均可在请求体 `tolerances` 中逐次覆盖，实现“可配置的分叉笔画误差”：

```json
{
  "submission_id": "attempt-0002",
  "character": "十",
  "reference_strokes": [[[100,150],[300,150],[300,300]]],
  "strokes": [[[100,150],[300,260],[300,300]]],
  "tolerances": {"fork_extra_direction_deg": 0, "fork_extra_shape": 0}
}
```

## 幂等语义

- `submission_id` 是幂等键：**相同键 + 相同负载** → 返回首个提交的结果，
  响应中 `idempotent_replay: true`，不产生新记录；
- **相同键 + 不同负载** → `409 Conflict`；
- 负载哈希为除 `submission_id` 外全部字段的规范化 JSON 的 SHA-256；
- 历史结果可通过 `GET /v1/submissions/{submission_id}` 查询。

## 配置

服务端默认值通过环境变量设置（见 `docker-compose.yml`），请求体 `tolerances` 可逐字段覆盖：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `/data/app.db` | SQLite 文件路径 |
| `TOL_START_END` | `0.15` | 起止点偏差上限（归一化距离） |
| `TOL_DIRECTION_DEG` | `35` | 方向偏差上限（度） |
| `TOL_SHAPE` | `0.18` | 形状平均偏差上限 |
| `TOL_FORK_EXTRA_DEG` | `20` | 分叉笔画额外方向容差（度） |
| `TOL_FORK_EXTRA_SHAPE` | `0.08` | 分叉笔画额外形状容差 |
| `CORNER_ANGLE_DEG` | `50` | 拐点判定最小转角（度） |
| `RESAMPLE_POINTS` | `32` | 每笔重采样点数 |

## 项目结构

```
app/
├── main.py        # FastAPI 应用与路由（幂等流程）
├── models.py      # Pydantic 请求/响应模型
├── validation.py  # 笔顺匹配引擎（方向/起止点/形状/错序/计分）
├── geometry.py    # 归一化、重采样、拐点检测
├── db.py          # SQLite 持久化（参考字 + 幂等提交）
└── config.py      # 环境变量配置
tests/test_api.py  # 21 个端到端用例
Dockerfile / docker-compose.yml
```
