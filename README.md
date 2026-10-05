# 木构件修复验收 API

仅用 **Python 标准库**（`http.server` + `sqlite3`）实现的木构件修复验收服务。
登记固定支承点、承重构件（含最低试载值）、榫卯连接、临时支撑与替换件；
按递增工序号提交拆卸 / 安装 / 试载 / 卸撑回执；每个工序重建当前连接图并做结构校验；
卸撑完成后可固化不可改写的验收快照。

## 启动

```bash
docker compose up --build
```

服务端口：**8080**（宿主机）→ 容器 8080。SQLite 数据持久化在命名卷 `timber-data`（容器内 `/data/app.db`）。

本地无 Docker 时也可直接运行：`python3 app.py`（监听 8080，数据在 `./data/app.db`；
可用环境变量 `APP_PORT`、`APP_DB` 覆盖）。

## 验收规则

- **工序递增**：`POST /steps` 的 `seq` 必须等于上一条已接受回执 +1，跳号整步拒绝。
- **施工中可达性**：每个改变连接图的工序提交后重建连接图——每个在位承重构件必须能经
  **在位连接或临时支撑**抵达某个固定支承点，否则整步回滚。
- **卸撑**：卸除临时支撑后，每个在位承重构件必须**仅凭永久连接**抵达支承点，且每个构件
  都有在**当前连接状态版本**下通过、载荷不低于其 `min_load` 的试载记录。
- **连接状态版本**：永久连接图每次变化（装/拆连接、拆/装构件）版本 +1；试载记录当时版本，
  图一变旧试载即失效。临时支撑的搭设/卸除不影响版本。
- **改接限制**：安装连接只允许使用事先登记的端点组合（`POST /connections` 登记的 id）。
- **整步拒绝**：跳号、端点不在位、可达性或卸撑条件不满足时返回 409，
  响应含 `first_affected_component`（首个受影响构件），已有回执与状态不变。
- **快照**：所有临时支撑卸除且校验通过后可 `POST /snapshots` 固化；快照只增不改，
  后续修复工序不会改写旧快照。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/supports` | 登记固定支承点 `{id, name}` |
| POST | `/components` | 登记承重构件 `{id, name, min_load}`（初始在位） |
| POST | `/replacements` | 登记替换件 `{id, name, min_load}`（初始不在位） |
| POST | `/connections` | 登记榫卯连接 `{id, a, b, in_place?}`（默认 true；改接预留组合传 false） |
| POST | `/temp-supports` | 登记临时支撑 `{id, a, b, in_place?}`（默认 false） |
| POST | `/steps` | 提交工序回执（见下） |
| GET | `/steps`、`/steps/{seq}` | 查询各工序回执与结果 |
| GET | `/state` | 当前连接图、各构件可达性与最近试载 |
| POST | `/snapshots` | 卸撑完成后固化验收快照 |
| GET | `/snapshots`、`/snapshots/{id}` | 查询快照 |
| GET | `/supports` `/components` `/replacements` `/connections` `/temp-supports` | 登记簿查询 |
| GET | `/health` | 健康检查 |

### 工序回执格式（`POST /steps`）

```jsonc
// 搭设临时支撑
{"seq": 1, "action": "install", "target": "temp_support", "id": "T1"}
// 拆卸构件（联动卸下依附的在位连接与临时支撑）
{"seq": 2, "action": "disassemble", "target": "component", "id": "C2"}
// 拆卸单条连接
{"seq": 2, "action": "disassemble", "target": "connection", "id": "J2"}
// 安装替换件，并同步安装其已登记连接（端点组合须事先登记）
{"seq": 3, "action": "install", "target": "replacement", "id": "R2", "connections": ["J3", "J4"]}
// 试载：记录载荷，是否通过 = load >= 构件 min_load
{"seq": 4, "action": "test_load", "component": "C1", "load": 12}
// 卸撑
{"seq": 6, "action": "remove_support", "id": "T1"}
```

## 可执行调用示例

启动服务后执行完整演示（登记 → 三类拒绝 → 正常工序 → 卸撑 → 快照 → 查询）：

```bash
./demo.sh
```

或逐条手动执行：

```bash
B=http://localhost:8080

# 1) 登记
curl -X POST $B/supports      -d '{"id":"S1","name":"北柱础"}'
curl -X POST $B/supports      -d '{"id":"S2","name":"南台基"}'
curl -X POST $B/components    -d '{"id":"C1","name":"前金柱","min_load":10}'
curl -X POST $B/components    -d '{"id":"C2","name":"后金柱","min_load":8}'
curl -X POST $B/replacements  -d '{"id":"R2","name":"替换后金柱","min_load":8}'
curl -X POST $B/connections   -d '{"id":"J1","a":"S1","b":"C1"}'
curl -X POST $B/connections   -d '{"id":"J2","a":"C1","b":"C2"}'
curl -X POST $B/connections   -d '{"id":"J3","a":"C1","b":"R2","in_place":false}'
curl -X POST $B/connections   -d '{"id":"J4","a":"S2","b":"R2","in_place":false}'
curl -X POST $B/temp-supports -d '{"id":"T1","a":"S2","b":"C1"}'

# 2) 拒绝示例
curl -X POST $B/steps -d '{"seq":5,"action":"install","target":"temp_support","id":"T1"}'
#   → 409 sequence_gap（工序跳号，期望 1）
curl -X POST $B/steps -d '{"seq":1,"action":"install","target":"connection","id":"J3"}'
#   → 409 endpoint_not_in_place，first_affected_component=R2（端点不在位）
curl -X POST $B/steps -d '{"seq":1,"action":"disassemble","target":"connection","id":"J1"}'
#   → 409 unreachable，first_affected_component=C1（拆后 C1 失去支承路径）

# 3) 正常工序
curl -X POST $B/steps -d '{"seq":1,"action":"install","target":"temp_support","id":"T1"}'
curl -X POST $B/steps -d '{"seq":2,"action":"disassemble","target":"component","id":"C2"}'
curl -X POST $B/steps -d '{"seq":3,"action":"install","target":"replacement","id":"R2","connections":["J3","J4"]}'

# 4) 卸撑条件不满足
curl -X POST $B/steps -d '{"seq":4,"action":"remove_support","id":"T1"}'
#   → 409 test_load_missing，first_affected_component=C1（当前连接状态下无合格试载）

# 5) 试载并卸撑
curl -X POST $B/steps -d '{"seq":4,"action":"test_load","component":"C1","load":12}'
curl -X POST $B/steps -d '{"seq":5,"action":"test_load","component":"R2","load":9.5}'
curl -X POST $B/steps -d '{"seq":6,"action":"remove_support","id":"T1"}'

# 6) 固化验收快照（此后继续修复不会改写快照 1）
curl -X POST $B/snapshots -d '{}'

# 7) 查询
curl $B/steps        # 各工序回执与结果
curl $B/steps/4      # 单条工序
curl $B/state        # 当前连接图与各构件状态
curl $B/snapshots/1  # 验收快照
```

## 错误响应

统一 JSON：`{"error": <code>, "message": ..., ...}`。工序拒绝一律 409 且状态不变：

| code | 含义 |
|---|---|
| `sequence_gap` | 工序跳号（附 `expected`/`got`） |
| `not_registered` | 连接/支撑/构件未事先登记 |
| `endpoint_not_in_place` | 端点不在位（附 `first_affected_component`） |
| `not_in_place` / `already_in_place` | 拆卸/安装对象状态不符 |
| `unreachable` | 工序后构件无法抵达固定支承点 |
| `support_unreachable` | 卸撑后构件无法仅凭永久连接抵达支承点 |
| `test_load_missing` | 构件缺少当前连接状态下通过的合格试载 |
| `shoring_incomplete` | 临时支撑未卸完，不能固化快照 |

## 项目结构

```
app.py              # 服务全部实现（标准库 HTTP + SQLite）
Dockerfile          # python:3.12-slim 单容器镜像
docker-compose.yml  # 单条命令启动，卷持久化
demo.sh             # 端到端演示脚本
README.md
```
