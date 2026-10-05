#!/usr/bin/env sh
# 可执行 HTTP 调用示例（curl）：完整走一遍“原梁验收 -> 换梁 -> 再验收”。
# 用法: 先启动服务（docker compose up --build -d），再执行 ./examples.sh
set -e
BASE="${BASE:-http://localhost:8080}"
j() { curl -sS -H 'Content-Type: application/json' "$@"; echo; }

echo "## 健康检查"
j "$BASE/health"

echo "## 1. 登记固定支承点 / 承重构件 / 替换件 / 榫卯 / 临时支撑"
j -X POST "$BASE/register/support" -d '{"code":"S1","name":"前檐柱顶","endpoints":["b","sh"]}'
j -X POST "$BASE/register/support" -d '{"code":"S2","name":"后檐柱顶","endpoints":["b"]}'
j -X POST "$BASE/register/member" -d '{"code":"B","name":"五架梁","min_load":100,"endpoints":["e1","e2","mid"]}'
j -X POST "$BASE/register/part"   -d '{"code":"P","name":"替换五架梁","min_load":100,"replaces":"B","endpoints":["f1","f2","fm"]}'
j -X POST "$BASE/register/joint"  -d '{"code":"J1","a":"S1:b","b":"B:e1"}'
j -X POST "$BASE/register/joint"  -d '{"code":"J2","a":"S2:b","b":"B:e2"}'
j -X POST "$BASE/register/joint"  -d '{"code":"J3","a":"S1:b","b":"P:f1"}'
j -X POST "$BASE/register/joint"  -d '{"code":"J4","a":"S2:b","b":"P:f2"}'
j -X POST "$BASE/register/shore"  -d '{"code":"SH","a":"S1:sh","b":"B:mid"}'
j -X POST "$BASE/register/shore"  -d '{"code":"SH2","a":"S1:sh","b":"P:fm"}'

echo "## 2. 工序 1-3：撑临时支撑 -> 试载达标 -> 卸撑"
j -X POST "$BASE/steps" -d '{"seq":1,"type":"install","actions":[{"op":"shore","link":"SH"}]}'
j -X POST "$BASE/steps" -d '{"seq":2,"type":"test","actions":[{"op":"test","code":"B","load":100}]}'
j -X POST "$BASE/steps" -d '{"seq":3,"type":"unshore","actions":[{"op":"shore","link":"SH"}]}'

echo "## 3. 固化验收快照（后续工序不改写它）"
j -X POST "$BASE/snapshots" -d '{"code":"accept-original","label":"原梁验收"}'

echo "## 4. 工序 4-10：撑住 -> 断榫卯拆下 B -> 装 P 并接榫卯 -> 试载 -> 卸撑"
j -X POST "$BASE/steps" -d '{"seq":4,"type":"install","actions":[{"op":"shore","link":"SH"}]}'
j -X POST "$BASE/steps" -d '{"seq":5,"type":"remove","actions":[{"op":"sever","link":"J1"},{"op":"sever","link":"J2"},{"op":"member","code":"B"}]}'
j -X POST "$BASE/steps" -d '{"seq":6,"type":"install","actions":[{"op":"member","code":"P"},{"op":"joint","link":"J3"},{"op":"joint","link":"J4"}]}'
j -X POST "$BASE/steps" -d '{"seq":7,"type":"test","actions":[{"op":"test","code":"P","load":50}]}'
j -X POST "$BASE/steps" -d '{"seq":8,"type":"install","actions":[{"op":"shore","link":"SH2"}]}'
echo "-- 工序9 试图直接卸撑：试载 50 < 最低 100，整步拒绝（注意回执数不变）"
j -X POST "$BASE/steps" -d '{"seq":9,"type":"unshore","actions":[{"op":"shore","link":"SH2"}]}'
j -X POST "$BASE/steps" -d '{"seq":9,"type":"test","actions":[{"op":"test","code":"P","load":120}]}'
j -X POST "$BASE/steps" -d '{"seq":10,"type":"unshore","actions":[{"op":"shore","link":"SH2"}]}'

echo "## 5. 固化第二张快照并查询"
j -X POST "$BASE/snapshots" -d '{"code":"accept-repaired","label":"换梁后验收"}'
j "$BASE/state"
j "$BASE/steps"
j "$BASE/snapshots"
j "$BASE/snapshots/accept-original"
