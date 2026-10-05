# 木构件修复验收 API

木构古建修复施工的验收服务：登记**固定支承点、承重构件（含最低试载值）、榫卯连接、临时支撑、替换件**，然后按**严格递增的工序号**提交拆卸、安装、试载、卸撑回执。每一步都重建当前连接图并做结构校验；失败则**整步拒绝**，已有回执与状态不变。全部通过卸撑后可**固化验收快照**，后续修复不会改写旧快照。

- 仅使用 Python 标准库：`http.server` + `sqlite3`（无任何第三方依赖）
- 数据落盘在 SQLite 文件（默认 `/data/repair.db`，容器卷持久化）
- 所有写操作串行化 + 单事务提交；校验只在内存候选状态上进行，失败即丢弃

## 一条命令启动

```bash
docker compose up --build -d
```

- 访问地址 / 端口：`http://localhost:8080`
- 健康检查：`curl http://localhost:8080/health`
- 停止：`docker compose down`（数据保留在卷 `timber-data` 中；连数据一起清掉用 `docker compose down -v`）

不想用 Docker 时：`python3 app.py`（设置 `PORT` 与 `DB_PATH` 环境变量即可，默认 `8080` / `./repair.db`）。

> 示例脚本 `examples.sh` 假设使用全新数据库；重复执行前请先 `docker compose down -v` 再起。

## 结构判定规则

连接图节点 = 固定支承点 / 在位承重构件（含已安装的替换件）；边 = 在位连接。

1. **施工态**（拆卸/安装/试载后）：每个在位承重构件必须能经「在位榫卯 **∪** 在位临时支撑」抵达至少一个固定支承点。
2. **卸撑态**（拆除任一临时支撑后）：
   - 每个在位承重构件必须能**仅凭在位榫卯**抵达固定支承点；
   - 且每个承重构件都持有在**当前永久连接状态**下完成的、**通过且载荷 ≥ 自身最低试载值**的试载。
3. **改接限制**：只允许使用事先登记的端点组合（连接编码）；动作端点所属构件必须在位。
4. **工序号**：必须从 1 开始连续递增，跳号、重号、补号一律拒绝。
5. **整步原子性**：一步可含多个动作，按顺序施加；任一动作或动作后的图校验失败，则整步不落库，响应给出 `first_affected_member`（按登记次序最早出问题的承重构件）。
6. **登记锁定**：出现第一条工序回执后，禁止再登记任何节点/端点/连接（替换件及其端点组合必须在开工前登记）。

「永久连接状态」用 `perm_version` 标识：断/接榫卯、拆/装构件才会推进版本，撑立或拆除临时支撑、做试载均不推进。因此「支撑在位时完成的达标试载」在随后卸撑时仍被承认；一旦永久连接改了，旧试载自动失效，必须重做。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/health` | 健康检查 |
| POST | `/register/support` | 登记固定支承点 `{code,name,endpoints[]}` |
| POST | `/register/member` | 登记承重构件 `{code,name,min_load,endpoints[]}` |
| POST | `/register/part` | 登记替换件（不在位；`replaces` 指向原构件编码） |
| POST | `/register/joint` | 登记榫卯连接 `{code,a:"节点:端点",b:"节点:端点"}`，开工即在位（替换件相关连接随安装接通） |
| POST | `/register/shore` | 登记临时支撑（初始未撑立） |
| POST | `/steps` | 提交工序回执，见下 |
| GET  | `/steps` `/steps/{seq}` | 查询全部/单条工序结果 |
| GET  | `/state` | 当前连接图状态、可达性、是否可固化 |
| GET  | `/registry` | 已登记的节点、端点、连接清单 |
| POST | `/snapshots` | 固化验收快照 `{code?,label?}`，要求无在位支撑且卸撑条件全满足 |
| GET  | `/snapshots` `/snapshots/{code}` | 快照列表 / 快照全文（含当时全部节点、连接、工序、试载） |

### 工序回执格式

```json
{ "seq": 4, "type": "install", "actions": [ ... ] }
```

`type` 与动作：

- `install`：`{"op":"member","code":"P"}`（装构件/替换件）、`{"op":"joint","link":"J3"}`（接榫卯）、`{"op":"shore","link":"SH"}`（撑立临时支撑）
- `remove`：`{"op":"sever","link":"J1"}`（断榫卯）、`{"op":"member","code":"B"}`（拆构件；仍有在位榫卯时拒绝）
- `test`：`{"op":"test","code":"B","load":100}`（载荷 ≥ 该构件 `min_load` 记为通过；不达标也会作为回执保留）
- `unshore`：`{"op":"shore","link":"SH"}`（拆除临时支撑；多根时每拆一根都立即校验卸撑条件）

成功响应（201）示例：

```json
{ "ok": true, "accepted_seq": 3, "type": "unshore",
  "next_expected_seq": 4, "perm_version": 0,
  "result": { "placed_members": ["B"], "erected_shores": [],
              "permanent_only_reachable": true, "can_finalize": true } }
```

整步拒绝响应（409，不产生任何状态变化）示例：

```json
{ "ok": false, "error": "unshore_condition_failed",
  "message": "拆除支撑 SH2 后卸撑条件不满足：P 最近一次试载未通过",
  "first_affected_member": "P", "reason": "missing_valid_load_test" }
```

其它错误：`seq_out_of_order`（工序跳号/重号）、`endpoint_not_registered`（端点组合未事先登记）、`endpoint_not_in_place`（端点不在位）、`unreachable_support`（施工态无法经支撑抵达支承点）、`joint_still_connected`、`unknown_link`、`registration_locked` 等。

## 可执行调用示例

```bash
./examples.sh
```

脚本覆盖：登记 → 撑支撑/试载/卸撑 → 固化第一张快照 → 换梁（拆卸、安装、改接）→ 一次**载荷不足导致的整步拒绝** → 补做达标试载 → 卸撑 → 第二张快照 → 查询。

另附纯标准库端到端测试（自带临时数据库，自动启动/关闭服务）：

```bash
python3 test_client.py    # 82 项断言，覆盖正常流程与各类整步拒绝、快照不可改写
```

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `app.py` | 服务本体（标准库，单文件） |
| `Dockerfile` | `python:3.11-slim` 镜像，数据卷 `/data`，含 HEALTHCHECK |
| `compose.yaml` | 构建并映射 `8080:8080`，SQLite 存于命名卷 `timber-data` |
| `examples.sh` | 可直接执行的 curl 全流程示例 |
| `test_client.py` | 端到端自动测试 |
