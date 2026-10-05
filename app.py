#!/usr/bin/env python3
"""木构件修复验收 API（仅依赖 Python 标准库：http.server + sqlite3）。

领域模型
--------
- 固定支承点 supports：基础/柱础等锚固点，永远在位。
- 承重构件 components：登记时带最低试载值 min_load；origin 为 original（初始在位）
  或 replacement（替换件，登记时不在位，安装工序后在位）。
- 榫卯连接 connections：事先登记的"允许端点组合"，in_place 表示是否在位；
  改接（安装连接）只允许使用已登记的端点组合。
- 临时支撑 temp_supports：登记后可在位/卸除；不计入"永久连接状态版本"。
- 工序回执 steps：按递增工序号提交 install / disassemble / test_load / remove_support。

校验规则
--------
- 工序号必须连续递增（last+1），跳号整步拒绝。
- 每个改变连接图的工序提交后重建连接图：施工中每个在位承重构件必须能经
  在位连接或临时支撑抵达某个固定支承点，否则整步回滚并指出首个受影响构件。
- 卸撑（remove_support）：卸除后每个在位承重构件必须仅凭永久连接抵达支承点，
  且每个在位承重构件都有在当前连接状态版本下通过、载荷不低于其最低值的试载。
- 连接状态版本 version：永久连接图每次变化（装/拆连接、拆构件、装构件）递增；
  试载记录当时版本，版本过期即失效。临时支撑的装/卸不改变版本。
- 卸撑全部完成后可 POST /snapshots 固化验收快照；快照只增不改，
  后续修复工序不会改写旧快照。
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = os.environ.get(
    "APP_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "app.db")
)
PORT = int(os.environ.get("APP_PORT", "8080"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supports (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS components (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    min_load REAL NOT NULL,
    origin TEXT NOT NULL DEFAULT 'original',   -- original | replacement
    in_place INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    a TEXT NOT NULL,
    b TEXT NOT NULL,
    in_place INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS temp_supports (
    id TEXT PRIMARY KEY,
    a TEXT NOT NULL,
    b TEXT NOT NULL,
    in_place INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    seq INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    payload TEXT NOT NULL,   -- JSON：工序参数
    result TEXT NOT NULL,    -- JSON：本步校验/执行结果
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS test_loads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id TEXT NOT NULL,
    load REAL NOT NULL,
    passed INTEGER NOT NULL,
    version INTEGER NOT NULL,  -- 试载时的永久连接状态版本
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL         -- JSON：固化后的验收快照，只增不改
);
"""


# ---------------------------------------------------------------- 基础设施

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES('version', '0')")
        conn.commit()
    finally:
        conn.close()


class ApiError(Exception):
    """HTTP 错误响应；整步拒绝时 status=409。"""

    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self):
        d = {"error": self.code, "message": self.message}
        d.update(self.extra)
        return d


def reject(code, message, component=None, **extra):
    """构造 409 工序拒绝；component 为首个受影响构件。"""
    if component is not None:
        extra["first_affected_component"] = component
    return ApiError(409, code, message, **extra)


def bad_request(message):
    return ApiError(400, "bad_request", message)


# ---------------------------------------------------------------- 图与版本

def _version(conn):
    return int(conn.execute("SELECT v FROM meta WHERE k='version'").fetchone()["v"])


def _bump(conn):
    v = _version(conn) + 1
    conn.execute("UPDATE meta SET v=? WHERE k='version'", (str(v),))
    return v


def _graph(conn):
    """重建当前连接图：在位构件、支承点、在位永久连接、在位临时支撑。"""
    comps = conn.execute(
        "SELECT id, name, min_load, origin, in_place FROM components ORDER BY rowid"
    ).fetchall()
    sups = [r["id"] for r in conn.execute("SELECT id FROM supports ORDER BY rowid")]
    perm = [(r["a"], r["b"]) for r in
            conn.execute("SELECT a, b FROM connections WHERE in_place=1")]
    temp = [(r["a"], r["b"]) for r in
            conn.execute("SELECT a, b FROM temp_supports WHERE in_place=1")]
    return comps, sups, perm, temp


def _reachable(supports, edges):
    """从所有固定支承点出发沿边扩展，返回可达节点集合。"""
    adj = {}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    seen = set(supports)
    stack = list(supports)
    while stack:
        n = stack.pop()
        for m in adj.get(n, ()):
            if m not in seen:
                seen.add(m)
                stack.append(m)
    return seen


def _first_unreachable(comps, seen):
    """按登记顺序返回首个无法抵达支承点的在位构件 id。"""
    for c in comps:
        if c["in_place"] and c["id"] not in seen:
            return c["id"]
    return None


def _first_untested(conn, comps, version):
    """按登记顺序返回首个在当前版本下无合格试载的在位构件 id。"""
    for c in comps:
        if not c["in_place"]:
            continue
        row = conn.execute(
            "SELECT 1 FROM test_loads WHERE component_id=? AND version=? AND passed=1 LIMIT 1",
            (c["id"], version),
        ).fetchone()
        if row is None:
            return c["id"]
    return None


def _check_construction_reachability(conn):
    """施工中校验：每个在位构件经在位连接或临时支撑抵达固定支承点。"""
    comps, sups, perm, temp = _graph(conn)
    seen = _reachable(sups, perm + temp)
    bad = _first_unreachable(comps, seen)
    if bad:
        raise reject(
            "unreachable",
            f"本工序后构件 {bad} 无法经在位连接或临时支撑抵达固定支承点，整步拒绝",
            component=bad,
        )


def _get(conn, table, oid, what):
    if not isinstance(oid, str) or not oid:
        raise bad_request(f"缺少合法字段 id（{what}）")
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (oid,)).fetchone()
    if row is None:
        raise reject("not_registered", f"{what} {oid} 未事先登记")
    return row


def _require_endpoints(conn, *eps):
    """端点须已登记且在位（支承点恒在位）。"""
    for ep in eps:
        if conn.execute("SELECT 1 FROM supports WHERE id=?", (ep,)).fetchone():
            continue
        row = conn.execute("SELECT in_place FROM components WHERE id=?", (ep,)).fetchone()
        if row is None:
            raise reject("not_registered", f"端点 {ep} 未登记", component=ep)
        if not row["in_place"]:
            raise reject("endpoint_not_in_place", f"端点 {ep} 不在位，整步拒绝", component=ep)


# ---------------------------------------------------------------- 登记接口

def _insert_unique(conn, sql, params, dup_msg):
    try:
        conn.execute(sql, params)
    except sqlite3.IntegrityError:
        raise ApiError(409, "duplicate", dup_msg)


def _check_endpoint_registered(conn, ep):
    if conn.execute("SELECT 1 FROM supports WHERE id=?", (ep,)).fetchone():
        return
    if conn.execute("SELECT 1 FROM components WHERE id=?", (ep,)).fetchone():
        return
    raise ApiError(409, "not_registered", f"端点 {ep} 未登记（须为支承点或构件）")


def create_support(body):
    sid, name = body.get("id"), body.get("name")
    if not isinstance(sid, str) or not sid:
        raise bad_request("缺少合法字段 id")
    if not isinstance(name, str) or not name:
        raise bad_request("缺少合法字段 name")
    conn = connect()
    try:
        with conn:
            _insert_unique(
                conn,
                "INSERT INTO supports(id, name, created_at) VALUES(?,?,?)",
                (sid, name, now()),
                f"支承点 {sid} 已登记",
            )
        return 201, {"id": sid, "name": name}
    finally:
        conn.close()


def _create_component(body, origin, in_place):
    cid, name, min_load = body.get("id"), body.get("name"), body.get("min_load")
    if not isinstance(cid, str) or not cid:
        raise bad_request("缺少合法字段 id")
    if not isinstance(name, str) or not name:
        raise bad_request("缺少合法字段 name")
    if isinstance(min_load, bool) or not isinstance(min_load, (int, float)) or min_load < 0:
        raise bad_request("min_load 须为非负数值")
    conn = connect()
    try:
        with conn:
            _insert_unique(
                conn,
                "INSERT INTO components(id, name, min_load, origin, in_place, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (cid, name, float(min_load), origin, in_place, now()),
                f"构件 {cid} 已登记",
            )
        return 201, {
            "id": cid, "name": name, "min_load": min_load,
            "origin": origin, "in_place": bool(in_place),
        }
    finally:
        conn.close()


def create_component(body):
    return _create_component(body, "original", 1)


def create_replacement(body):
    return _create_component(body, "replacement", 0)


def _create_link(body, table, what, default_in_place):
    lid, a, b = body.get("id"), body.get("a"), body.get("b")
    in_place = body.get("in_place", default_in_place)
    if not isinstance(lid, str) or not lid:
        raise bad_request("缺少合法字段 id")
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        raise bad_request("缺少合法端点字段 a、b")
    if a == b:
        raise bad_request("端点 a、b 不能相同")
    if not isinstance(in_place, bool):
        raise bad_request("in_place 须为布尔值")
    conn = connect()
    try:
        with conn:
            _check_endpoint_registered(conn, a)
            _check_endpoint_registered(conn, b)
            _insert_unique(
                conn,
                f"INSERT INTO {table}(id, a, b, in_place, created_at) VALUES(?,?,?,?,?)",
                (lid, a, b, 1 if in_place else 0, now()),
                f"{what} {lid} 已登记",
            )
        return 201, {"id": lid, "a": a, "b": b, "in_place": in_place}
    finally:
        conn.close()


def create_connection(body):
    # 榫卯连接：默认视为现状在位；为改接预留的组合可登记 in_place=false
    return _create_link(body, "connections", "连接", True)


def create_temp_support(body):
    # 临时支撑：默认登记时未搭设
    return _create_link(body, "temp_supports", "临时支撑", False)


# ---------------------------------------------------------------- 工序回执

def _apply_install(conn, body):
    target, oid = body.get("target"), body.get("id")
    if target == "connection":
        row = _get(conn, "connections", oid, "连接")
        if row["in_place"]:
            raise reject("already_in_place", f"连接 {oid} 已在位")
        _require_endpoints(conn, row["a"], row["b"])
        conn.execute("UPDATE connections SET in_place=1 WHERE id=?", (oid,))
        _bump(conn)
        return {"connection": oid, "endpoints": [row["a"], row["b"]]}
    if target == "temp_support":
        row = _get(conn, "temp_supports", oid, "临时支撑")
        if row["in_place"]:
            raise reject("already_in_place", f"临时支撑 {oid} 已在位")
        _require_endpoints(conn, row["a"], row["b"])
        conn.execute("UPDATE temp_supports SET in_place=1 WHERE id=?", (oid,))
        return {"temp_support": oid, "endpoints": [row["a"], row["b"]]}
    if target in ("component", "replacement"):
        row = _get(conn, "components", oid, "构件")
        if target == "replacement" and row["origin"] != "replacement":
            raise bad_request(f"{oid} 不是已登记的替换件")
        if row["in_place"]:
            raise reject("already_in_place", f"构件 {oid} 已在位", component=oid)
        # 允许同一步内附带安装该构件的已登记连接（端点组合仍须事先登记），
        # 否则新装构件必然悬空、无法通过施工可达性校验。
        jids = body.get("connections") or []
        if not isinstance(jids, list) or not all(isinstance(j, str) for j in jids):
            raise bad_request("connections 须为连接 id 数组")
        conn.execute("UPDATE components SET in_place=1 WHERE id=?", (oid,))
        installed = []
        for jid in jids:
            j = _get(conn, "connections", jid, "连接")
            if j["in_place"]:
                raise reject("already_in_place", f"连接 {jid} 已在位")
            _require_endpoints(conn, j["a"], j["b"])
            conn.execute("UPDATE connections SET in_place=1 WHERE id=?", (jid,))
            installed.append(jid)
        _bump(conn)
        return {"component": oid, "connections": installed}
    raise bad_request("install 的 target 须为 connection/temp_support/component/replacement")


def _apply_disassemble(conn, body):
    target, oid = body.get("target"), body.get("id")
    if target == "connection":
        row = _get(conn, "connections", oid, "连接")
        if not row["in_place"]:
            raise reject("not_in_place", f"连接 {oid} 不在位，无法拆卸")
        conn.execute("UPDATE connections SET in_place=0 WHERE id=?", (oid,))
        _bump(conn)
        return {"connection": oid}
    if target == "component":
        row = _get(conn, "components", oid, "构件")
        if not row["in_place"]:
            raise reject("not_in_place", f"构件 {oid} 不在位，无法拆卸", component=oid)
        conn.execute("UPDATE components SET in_place=0 WHERE id=?", (oid,))
        # 联动卸下依附于该构件的在位连接与临时支撑
        jc = [r["id"] for r in conn.execute(
            "SELECT id FROM connections WHERE in_place=1 AND (a=? OR b=?)", (oid, oid))]
        tc = [r["id"] for r in conn.execute(
            "SELECT id FROM temp_supports WHERE in_place=1 AND (a=? OR b=?)", (oid, oid))]
        conn.execute(
            "UPDATE connections SET in_place=0 WHERE in_place=1 AND (a=? OR b=?)", (oid, oid))
        conn.execute(
            "UPDATE temp_supports SET in_place=0 WHERE in_place=1 AND (a=? OR b=?)", (oid, oid))
        _bump(conn)
        return {"component": oid, "removed_connections": jc, "removed_temp_supports": tc}
    raise bad_request("disassemble 的 target 须为 connection/component")


def _apply_test_load(conn, seq, body):
    cid, load = body.get("component"), body.get("load")
    row = _get(conn, "components", cid, "构件")
    if not row["in_place"]:
        raise reject("endpoint_not_in_place", f"构件 {cid} 不在位，无法试载", component=cid)
    if isinstance(load, bool) or not isinstance(load, (int, float)) or load < 0:
        raise bad_request("load 须为非负数值")
    v = _version(conn)
    passed = load >= row["min_load"]
    conn.execute(
        "INSERT INTO test_loads(component_id, load, passed, version, seq, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (cid, float(load), 1 if passed else 0, v, seq, now()),
    )
    return {
        "component": cid, "load": load, "min_load": row["min_load"],
        "passed": passed, "version": v,
    }


def _apply_remove_support(conn, body):
    oid = body.get("id")
    row = _get(conn, "temp_supports", oid, "临时支撑")
    if not row["in_place"]:
        raise reject("not_in_place", f"临时支撑 {oid} 不在位，无法卸撑")
    conn.execute("UPDATE temp_supports SET in_place=0 WHERE id=?", (oid,))
    # 卸撑条件一：仅凭永久连接抵达固定支承点
    comps, sups, perm, _temp = _graph(conn)
    seen = _reachable(sups, perm)
    bad = _first_unreachable(comps, seen)
    if bad:
        raise reject(
            "support_unreachable",
            f"卸撑后构件 {bad} 无法仅凭永久连接抵达固定支承点，整步拒绝",
            component=bad,
        )
    # 卸撑条件二：每个在位构件都有当前连接状态下通过的合格试载
    v = _version(conn)
    bad = _first_untested(conn, comps, v)
    if bad:
        raise reject(
            "test_load_missing",
            f"构件 {bad} 缺少在当前连接状态(v{v})下通过且载荷不低于最低值的试载，整步拒绝",
            component=bad,
        )
    return {
        "temp_support": oid,
        "permanent_only": True,
        "version": v,
        "components_checked": [c["id"] for c in comps if c["in_place"]],
    }


def create_step(body):
    seq, action = body.get("seq"), body.get("action")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise bad_request("seq 须为正整数")
    if action not in ("install", "disassemble", "test_load", "remove_support"):
        raise bad_request("action 须为 install/disassemble/test_load/remove_support")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        last = conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM steps").fetchone()["s"]
        if seq != last + 1:
            raise reject(
                "sequence_gap",
                f"工序跳号：期望工序号 {last + 1}，实收 {seq}，整步拒绝",
                expected=last + 1, got=seq,
            )
        if action == "install":
            result = _apply_install(conn, body)
        elif action == "disassemble":
            result = _apply_disassemble(conn, body)
        elif action == "test_load":
            result = _apply_test_load(conn, seq, body)
        else:
            result = _apply_remove_support(conn, body)
        # 每步重建连接图做施工中校验（卸撑已在 _apply_remove_support 内做更严校验）
        if action in ("install", "disassemble"):
            _check_construction_reachability(conn)
        receipt = {
            "seq": seq,
            "action": action,
            "payload": {k: v for k, v in body.items() if k not in ("seq", "action")},
            "result": result,
            "created_at": now(),
        }
        conn.execute(
            "INSERT INTO steps(seq, action, payload, result, created_at) VALUES(?,?,?,?,?)",
            (seq, action,
             json.dumps(receipt["payload"], ensure_ascii=False),
             json.dumps(result, ensure_ascii=False),
             receipt["created_at"]),
        )
        conn.commit()
        return 201, receipt
    except Exception:
        conn.rollback()   # 整步拒绝：已有回执与状态不变
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 快照

def create_snapshot(_body):
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        left = [r["id"] for r in conn.execute(
            "SELECT id FROM temp_supports WHERE in_place=1 ORDER BY rowid")]
        if left:
            raise reject(
                "shoring_incomplete",
                f"卸撑未完成：临时支撑 {left[0]} 仍在位，不能固化验收快照",
                temp_support=left[0],
            )
        comps, sups, perm, _temp = _graph(conn)
        seen = _reachable(sups, perm)
        bad = _first_unreachable(comps, seen)
        if bad:
            raise reject(
                "support_unreachable",
                f"构件 {bad} 无法仅凭永久连接抵达固定支承点，不能固化快照",
                component=bad,
            )
        v = _version(conn)
        bad = _first_untested(conn, comps, v)
        if bad:
            raise reject(
                "test_load_missing",
                f"构件 {bad} 缺少在当前连接状态(v{v})下通过的试载，不能固化快照",
                component=bad,
            )
        last = conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM steps").fetchone()["s"]
        comp_list = []
        for c in comps:
            if not c["in_place"]:
                continue
            t = conn.execute(
                "SELECT load, seq, version FROM test_loads"
                " WHERE component_id=? AND version=? AND passed=1"
                " ORDER BY id DESC LIMIT 1",
                (c["id"], v),
            ).fetchone()
            comp_list.append({
                "id": c["id"], "name": c["name"], "min_load": c["min_load"],
                "origin": c["origin"],
                "accepted_test": dict(t) if t else None,
            })
        joints = [dict(r) for r in conn.execute(
            "SELECT id, a, b FROM connections WHERE in_place=1 ORDER BY rowid")]
        receipts = [{
            "seq": r["seq"], "action": r["action"],
            "payload": json.loads(r["payload"]), "result": json.loads(r["result"]),
            "created_at": r["created_at"],
        } for r in conn.execute("SELECT * FROM steps ORDER BY seq")]
        data = {
            "version": v,
            "last_seq": last,
            "supports": sups,
            "components": comp_list,
            "connections_in_place": joints,
            "steps": receipts,
        }
        created = now()
        cur = conn.execute(
            "INSERT INTO snapshots(created_at, data) VALUES(?,?)",
            (created, json.dumps(data, ensure_ascii=False)),
        )
        conn.commit()
        return 201, {"id": cur.lastrowid, "created_at": created,
                     "version": v, "last_seq": last,
                     "components": [c["id"] for c in comp_list]}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 查询接口

def list_table(table):
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for r in rows:
            if "in_place" in r:
                r["in_place"] = bool(r["in_place"])
        return 200, rows
    finally:
        conn.close()


def list_steps():
    conn = connect()
    try:
        out = []
        for r in conn.execute("SELECT * FROM steps ORDER BY seq"):
            out.append({
                "seq": r["seq"], "action": r["action"],
                "payload": json.loads(r["payload"]), "result": json.loads(r["result"]),
                "created_at": r["created_at"],
            })
        return 200, out
    finally:
        conn.close()


def get_step(seq):
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM steps WHERE seq=?", (seq,)).fetchone()
        if r is None:
            raise ApiError(404, "not_found", f"工序 {seq} 不存在")
        return 200, {
            "seq": r["seq"], "action": r["action"],
            "payload": json.loads(r["payload"]), "result": json.loads(r["result"]),
            "created_at": r["created_at"],
        }
    finally:
        conn.close()


def get_state():
    conn = connect()
    try:
        comps, sups, perm, temp = _graph(conn)
        v = _version(conn)
        seen_all = _reachable(sups, perm + temp)
        seen_perm = _reachable(sups, perm)
        comp_list = []
        for c in comps:
            t = conn.execute(
                "SELECT load, passed, version, seq FROM test_loads"
                " WHERE component_id=? ORDER BY id DESC LIMIT 1",
                (c["id"],),
            ).fetchone()
            comp_list.append({
                "id": c["id"], "name": c["name"], "min_load": c["min_load"],
                "origin": c["origin"], "in_place": bool(c["in_place"]),
                "reachable": c["id"] in seen_all if c["in_place"] else None,
                "permanent_reachable": c["id"] in seen_perm if c["in_place"] else None,
                "last_test": dict(t) if t else None,
            })
        joints = [dict(r, in_place=bool(r["in_place"])) for r in conn.execute(
            "SELECT id, a, b, in_place FROM connections ORDER BY rowid")]
        shores = [dict(r, in_place=bool(r["in_place"])) for r in conn.execute(
            "SELECT id, a, b, in_place FROM temp_supports ORDER BY rowid")]
        last = conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM steps").fetchone()["s"]
        return 200, {
            "version": v,
            "last_seq": last,
            "supports": sups,
            "components": comp_list,
            "connections": joints,
            "temp_supports": shores,
        }
    finally:
        conn.close()


def list_snapshots():
    conn = connect()
    try:
        return 200, [{"id": r["id"], "created_at": r["created_at"]}
                     for r in conn.execute("SELECT id, created_at FROM snapshots ORDER BY id")]
    finally:
        conn.close()


def get_snapshot(sid):
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
        if r is None:
            raise ApiError(404, "not_found", f"快照 {sid} 不存在")
        data = json.loads(r["data"])
        data["id"] = r["id"]
        data["created_at"] = r["created_at"]
        return 200, data
    finally:
        conn.close()


# ---------------------------------------------------------------- 路由

def route(method, path, body):
    if path == "/health":
        return 200, {"ok": True}
    if path == "/" and method == "GET":
        return 200, {
            "service": "木构件修复验收 API",
            "endpoints": [
                "POST /supports            登记固定支承点",
                "POST /components          登记承重构件（含 min_load 最低试载值）",
                "POST /connections         登记榫卯连接（允许的端点组合）",
                "POST /temp-supports       登记临时支撑",
                "POST /replacements        登记替换件",
                "POST /steps               按递增工序号提交拆卸/安装/试载/卸撑回执",
                "GET  /steps | /steps/{n}  查询各工序回执与结果",
                "GET  /state               当前连接图与各构件状态",
                "POST /snapshots           卸撑完成后固化验收快照（只增不改）",
                "GET  /snapshots | /snapshots/{id}",
            ],
        }
    if method == "POST" and path == "/supports":
        return create_support(body)
    if method == "POST" and path == "/components":
        return create_component(body)
    if method == "POST" and path == "/connections":
        return create_connection(body)
    if method == "POST" and path == "/temp-supports":
        return create_temp_support(body)
    if method == "POST" and path == "/replacements":
        return create_replacement(body)
    if method == "POST" and path == "/steps":
        return create_step(body)
    if method == "POST" and path == "/snapshots":
        return create_snapshot(body)
    if method == "GET" and path in ("/supports", "/components", "/connections",
                                    "/temp-supports"):
        return list_table(path.strip("/").replace("-", "_"))
    if method == "GET" and path == "/replacements":
        conn = connect()
        try:
            rows = [dict(r, in_place=bool(r["in_place"])) for r in conn.execute(
                "SELECT * FROM components WHERE origin='replacement' ORDER BY rowid")]
            return 200, rows
        finally:
            conn.close()
    if method == "GET" and path == "/steps":
        return list_steps()
    m = re.fullmatch(r"/steps/(\d+)", path)
    if method == "GET" and m:
        return get_step(int(m.group(1)))
    if method == "GET" and path == "/state":
        return get_state()
    if method == "GET" and path == "/snapshots":
        return list_snapshots()
    m = re.fullmatch(r"/snapshots/(\d+)", path)
    if method == "GET" and m:
        return get_snapshot(int(m.group(1)))
    raise ApiError(404, "not_found", f"路由不存在：{method} {path}")


class Handler(BaseHTTPRequestHandler):
    server_version = "TimberRepair/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise bad_request("请求体不是合法 JSON")
        if not isinstance(obj, dict):
            raise bad_request("请求体须为 JSON 对象")
        return obj

    def _handle(self, method):
        try:
            body = self._read_body() if method == "POST" else {}
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            status, obj = route(method, path, body)
            self._send(status, obj)
        except ApiError as e:
            self._send(e.status, e.body())
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": "internal", "message": str(e)})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, fmt, *args):
        pass  # 静默访问日志，保持容器输出干净


def main():
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"木构件修复验收 API  listening on :{PORT}  db={DB_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
