#!/usr/bin/env bash
# 演示脚本：对 localhost:8080 上运行的服务执行一次完整修复验收流程。
# 用法：./demo.sh   （服务须已启动，见 README）
set -euo pipefail
BASE="${BASE:-http://localhost:8080}"

req() {  # req METHOD PATH JSON
  local method="$1" path="$2" data="${3:-}"
  echo "==> $method $path ${data}"
  if [ -n "$data" ]; then
    curl -sS -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$data"
  else
    curl -sS -X "$method" "$BASE$path"
  fi
  echo; echo
}

echo "---- 1. 登记 ----"
req POST /supports      '{"id":"S1","name":"北柱础"}'
req POST /supports      '{"id":"S2","name":"南台基"}'
req POST /components    '{"id":"C1","name":"前金柱","min_load":10}'
req POST /components    '{"id":"C2","name":"后金柱","min_load":8}'
req POST /replacements  '{"id":"R2","name":"替换后金柱","min_load":8}'
req POST /connections   '{"id":"J1","a":"S1","b":"C1"}'
req POST /connections   '{"id":"J2","a":"C1","b":"C2"}'
req POST /connections   '{"id":"J3","a":"C1","b":"R2","in_place":false}'
req POST /connections   '{"id":"J4","a":"S2","b":"R2","in_place":false}'
req POST /temp-supports '{"id":"T1","a":"S2","b":"C1"}'

echo "---- 2. 拒绝示例：跳号 / 端点不在位 / 无支撑拆卸 ----"
req POST /steps '{"seq":5,"action":"install","target":"temp_support","id":"T1"}'
req POST /steps '{"seq":1,"action":"install","target":"connection","id":"J3"}'
req POST /steps '{"seq":1,"action":"disassemble","target":"connection","id":"J1"}'

echo "---- 3. 正常工序 ----"
req POST /steps '{"seq":1,"action":"install","target":"temp_support","id":"T1"}'
req POST /steps '{"seq":2,"action":"disassemble","target":"component","id":"C2"}'
req POST /steps '{"seq":3,"action":"install","target":"replacement","id":"R2","connections":["J3","J4"]}'

echo "---- 4. 拒绝示例：卸撑前缺少合格试载 ----"
req POST /steps '{"seq":4,"action":"remove_support","id":"T1"}'

echo "---- 5. 试载并卸撑 ----"
req POST /steps '{"seq":4,"action":"test_load","component":"C1","load":12}'
req POST /steps '{"seq":5,"action":"test_load","component":"R2","load":9.5}'
req POST /steps '{"seq":6,"action":"remove_support","id":"T1"}'

echo "---- 6. 固化验收快照 ----"
req POST /snapshots '{}'

echo "---- 7. 查询 ----"
req GET /steps
req GET /state
req GET /snapshots/1
