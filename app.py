#!/usr/bin/env python3
"""木构件修复验收 API —— 仅使用 Python 标准库（http.server + sqlite3）。

核心模型
========
* 节点(node):
    - support  固定支承点（恒在位；连接图中的接地终点）
    - member   承重构件（有最低试载值 min_load，可拆可装）
    - part     替换件（注册时不在位，安装后成为在位承重构件）
* 端点(endpoint): 属于节点；连接一律引用已登记端点。
* 连接(link):
    - joint  榫卯永久连接（初始可登记为在位，拆装工序中可断/接）
    - shore  临时支撑（安装=撑立，卸撑=拆除）
* 工序(step): 全局递增且必须连续（1,2,3…）；每步一种类型，内含一个或多个原子动作。
  校验在内存候选状态上完成，任何动作失败 => 整步拒绝，数据库无任何变更。

两类图校验（每步施加动作后）
============================
* 施工态（remove/install/unshore 后）：每个在位承重构件必须能经
  「在位榫卯连接 ∪ 在位临时支撑」到达某个固定支承点。
* 卸撑态（unshore 特有）：每个在位承重构件必须能仅凭「在位榫卯连接」
  到达固定支承点，且持有在【当前永久连接版本】下做出的、
  载荷 >= 自身最低值的通过试载。

永久连接版本(perm_version) 仅在永久结构变化时 +1
（断榫卯/接榫卯、拆下/装入构件或替换件）；试载与撑立/卸撑不改变版本，
因此在最终永久状态下做的试载在随后卸撑时仍有效。
"""

import json
import math
import os
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DB_PATH = os.environ.get("DB_PATH", "/data/repair.db")
PORT = int(os.environ.get("PORT", "8080"))

# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    id INTEGER PRIMARY KEY CHECK (id = 1),
    perm_version INTEGER NOT NULL,
    next_seq INTEGER NOT NULL
);
INSERT OR IGNORE INTO meta(id, perm_version, next_seq) VALUES(1, 0, 1);

CREATE TABLE IF NOT EXISTS nodes(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,     -- 登记次序，决定“首个”构件
    code TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('support','member','part')),
    name TEXT NOT NULL,
    min_load REAL,                             -- 固定支承点为 NULL
    replaces TEXT,
    in_place INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS endpoints(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    node_code TEXT NOT NULL REFERENCES nodes(code),
    name TEXT NOT NULL,
    UNIQUE(node_code, name)
);
CREATE TABLE IF NOT EXISTS links(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('joint','shore')),
    a_node TEXT NOT NULL, a_ep TEXT NOT NULL,
    b_node TEXT NOT NULL, b_ep TEXT NOT NULL,
    in_place INTEGER NOT NULL,
    UNIQUE(a_node, a_ep, b_node, b_ep)
);
CREATE TABLE IF NOT EXISTS steps(
    seq INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    perm_version_after INTEGER NOT NULL,
    actions TEXT NOT NULL,
    result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS load_tests(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    step_seq INTEGER NOT NULL REFERENCES steps(seq),
    node_code TEXT NOT NULL,
    load REAL NOT NULL,
    passed INTEGER NOT NULL,
    perm_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    label TEXT,
    after_step INTEGER NOT NULL,
    perm_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    body TEXT NOT NULL
);
"""


def db_connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# --------------------------------------------------------------------------- #
# 错误
# --------------------------------------------------------------------------- #


class ApiError(Exception):
    def __init__(self, status, code, message, extra=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}

    def body(self):
        d = {"ok": False, "error": self.code, "message": self.message}
        d.update(self.extra)
        return d


# --------------------------------------------------------------------------- #
# 内存候选状态
# --------------------------------------------------------------------------- #


class State:
    def __init__(self, conn):
        self.nodes = {}
        for r in conn.execute("SELECT * FROM nodes"):
            self.nodes[r["code"]] = {
                "seq": r["seq"],
                "code": r["code"],
                "kind": r["kind"],
                "name": r["name"],
                "min_load": r["min_load"],
                "replaces": r["replaces"],
                "in_place": bool(r["in_place"]),
            }
        self.endpoints = {}  # (node, ep) -> 1
        for r in conn.execute("SELECT node_code, name FROM endpoints"):
            self.endpoints[(r["node_code"], r["name"])] = 1
        self.links = {}
        for r in conn.execute("SELECT * FROM links"):
            self.links[r["code"]] = {
                "seq": r["seq"],
                "code": r["code"],
                "kind": r["kind"],
                "a_node": r["a_node"],
                "a_ep": r["a_ep"],
                "b_node": r["b_node"],
                "b_ep": r["b_ep"],
                "in_place": bool(r["in_place"]),
            }
        row = conn.execute(
            "SELECT perm_version, next_seq FROM meta WHERE id=1"
        ).fetchone()
        self.perm_version = row["perm_version"]
        self.next_seq = row["next_seq"]
        # 各构件“当前版本下最新试载”（仅保留每个节点最新一条）
        self.tests = {}  # node -> {load, passed, version, step}
        for r in conn.execute(
            """SELECT t.* FROM load_tests t
               JOIN (SELECT node_code, MAX(seq) m FROM load_tests GROUP BY node_code) x
                 ON x.node_code = t.node_code AND x.m = t.seq"""
        ):
            self.tests[r["node_code"]] = {
                "load": r["load"],
                "passed": bool(r["passed"]),
                "version": r["perm_version"],
                "step": r["step_seq"],
            }
        # 待持久化的变更
        self.link_updates = {}   # code -> in_place(bool)
        self.node_updates = {}   # code -> in_place(bool)
        self.version_bumps = 0
        self.new_tests = []      # (node, load, passed)

    # -- 基础查询 ---------------------------------------------------------- #

    def require_node(self, code):
        n = self.nodes.get(code)
        if not n:
            raise ApiError(404, "unknown_node", f"未登记的节点: {code}", {"node": code})
        return n

    def require_link(self, code):
        lk = self.links.get(code)
        if not lk:
            raise ApiError(404, "unknown_link", f"未登记的连接: {code}", {"link": code})
        return lk

    def require_endpoint(self, ref, field):
        if not isinstance(ref, str) or ":" not in ref:
            raise ApiError(400, "bad_endpoint", f"{field} 必须形如 '节点编码:端点名'", {field: ref})
        node, ep = ref.split(":", 1)
        if (node, ep) not in self.endpoints:
            raise ApiError(
                400, "endpoint_not_registered",
                f"端点未登记: {ref}", {field: ref},
            )
        return node, ep

    def placed_members(self):
        return [n for n in self.nodes.values()
                if n["in_place"] and n["kind"] in ("member", "part")]

    # -- 连接图 ------------------------------------------------------------ #

    def _adj(self, include_joints, include_shores):
        adj = {c: set() for c in self.nodes}
        for lk in self.links.values():
            if not lk["in_place"]:
                continue
            if lk["kind"] == "joint" and not include_joints:
                continue
            if lk["kind"] == "shore" and not include_shores:
                continue
            a, b = lk["a_node"], lk["b_node"]
            adj[a].add(b)
            adj[b].add(a)
        return adj

    def reachable_supports(self, include_shores):
        """从任一固定支承点出发，返回能到达的节点集合。"""
        adj = self._adj(True, include_shores)
        seen = set()
        stack = [c for c, n in self.nodes.items() if n["kind"] == "support"]
        while stack:
            c = stack.pop()
            if c in seen:
                continue
            seen.add(c)
            stack.extend(adj[c] - seen)
        return seen

    def first_bad_member(self, include_shores, check_tests):
        """按登记次序返回首个不满足条件的承重构件，无则 None。

        条件:
          1) 在位；
          2) 能到达固定支承点（是否允许临时支撑由 include_shores 决定）；
          3) check_tests 时：存在当前永久版本下、载荷>=最低值的通过试载。
        """
        reached = self.reachable_supports(include_shores)
        members = sorted(self.placed_members(), key=lambda n: n["seq"])
        for n in members:
            if n["code"] not in reached:
                return n, "unreachable_support", "无法抵达固定支承点"
            if check_tests:
                t = self.tests.get(n["code"])
                ok = (
                    t is not None
                    and t["passed"]
                    and t["version"] == self.perm_version
                    and t["load"] + 1e-9 >= (n["min_load"] or 0)
                )
                if not ok:
                    reason = "无当前连接状态下的合格试载"
                    if t is None:
                        reason = "尚无试载记录"
                    elif not t["passed"]:
                        reason = "最近一次试载未通过"
                    elif t["version"] != self.perm_version:
                        reason = f"试载在旧连接状态(v{t['version']})下完成，当前为 v{self.perm_version}"
                    elif t["load"] + 1e-9 < (n["min_load"] or 0):
                        reason = f"试载 {t['load']} 低于最低试载值 {n['min_load']}"
                    return n, "missing_valid_load_test", reason
        return None

    # -- 状态变更（先在内存中累积）---------------------------------------- #

    def set_link(self, code, in_place):
        cur = self.links[code]["in_place"]
        if cur != in_place:
            self.link_updates[code] = in_place
            self.links[code]["in_place"] = in_place
            if self.links[code]["kind"] == "joint":
                self.version_bumps += 1

    def set_node(self, code, in_place):
        cur = self.nodes[code]["in_place"]
        if cur != in_place:
            self.node_updates[code] = in_place
            self.nodes[code]["in_place"] = in_place
            self.version_bumps += 1

    def add_test(self, node, load, passed):
        self.new_tests.append((node, load, passed))
        self.tests[node] = {
            "load": load,
            "passed": passed,
            "version": self.perm_version,
            "step": None,
        }


# --------------------------------------------------------------------------- #
# 登记接口（开工后锁定）
# --------------------------------------------------------------------------- #


def registration_open(st):
    if st.next_seq != 1:
        raise ApiError(
            409, "registration_locked",
            "已有工序回执，登记已锁定；后续修复不得补登端点/构件/连接",
        )


def register_support(st, body):
    registration_open(st)
    code = require_str(body, "code")
    name = require_str(body, "name", allow_empty=True)
    if code in st.nodes:
        raise ApiError(409, "duplicate_code", f"编码已存在: {code}", {"code": code})
    eps = require_endpoint_list(body)
    return {
        "node": ("support", code, name or code, None, None),
        "endpoints": [(code, e) for e in eps],
    }


def register_member(st, body, kind):
    registration_open(st)
    code = require_str(body, "code")
    name = require_str(body, "name", allow_empty=True)
    min_load = body.get("min_load")
    if not isinstance(min_load, (int, float)) or isinstance(min_load, bool) \
            or not math.isfinite(float(min_load)) or float(min_load) < 0:
        raise ApiError(400, "bad_min_load", "min_load 必须为非负数字")
    replaces = body.get("replaces")
    if replaces is not None:
        if not isinstance(replaces, str) or not replaces:
            raise ApiError(400, "bad_replaces", "replaces 必须为非空字符串")
        if replaces not in st.nodes or st.nodes[replaces]["kind"] != "member":
            raise ApiError(400, "replaces_unknown",
                           f"replaces 必须指向已登记承重构件: {replaces}",
                           {"replaces": replaces})
    if code in st.nodes:
        raise ApiError(409, "duplicate_code", f"编码已存在: {code}", {"code": code})
    eps = require_endpoint_list(body)
    in_place = kind == "member"  # 原构件开工时在位；替换件安装后才在位
    return {
        "node": (kind, code, name or code, float(min_load), replaces),
        "endpoints": [(code, e) for e in eps],
        "in_place": in_place,
    }


def register_link(st, body, kind):
    registration_open(st)
    code = require_str(body, "code")
    a = st.require_endpoint(body.get("a"), "a")
    b = st.require_endpoint(body.get("b"), "b")
    if a == b:
        raise ApiError(400, "self_link", "不允许端点自连")
    na, nb = st.nodes[a[0]], st.nodes[b[0]]
    if na["kind"] == "support" and nb["kind"] == "support":
        raise ApiError(400, "support_to_support", "不允许固定支承点之间直接连接")
    pair = tuple(sorted([a, b]))
    for lk in st.links.values():
        if tuple(sorted([(lk["a_node"], lk["a_ep"]), (lk["b_node"], lk["b_ep"])])) == pair:
            raise ApiError(409, "duplicate_endpoint_pair",
                           f"该端点组合已登记为连接 {lk['code']}",
                           {"existing_link": lk["code"]})
    if code in st.links:
        raise ApiError(409, "duplicate_code", f"编码已存在: {code}", {"code": code})
    # 榫卯开工即在位，但涉及替换件（尚未安装）的连接须等安装后才能接上；
    # 临时支撑必须经 install 工序撑立
    in_place = False
    if kind == "joint":
        in_place = st.nodes[a[0]]["in_place"] and st.nodes[b[0]]["in_place"]
    return {"link": (kind, code, a[0], a[1], b[0], b[1], in_place)}


def require_str(body, field, allow_empty=False):
    v = body.get(field)
    if not isinstance(v, str) or (not v and not allow_empty):
        raise ApiError(400, "bad_field", f"字段 {field} 必须为非空字符串", {"field": field})
    return v


def require_endpoint_list(body):
    eps = body.get("endpoints")
    if not isinstance(eps, list) or not eps:
        raise ApiError(400, "bad_endpoints", "endpoints 必须为非空字符串数组")
    out = []
    for e in eps:
        if not isinstance(e, str) or not e:
            raise ApiError(400, "bad_endpoints", "端点名必须为非空字符串")
        if e in out:
            raise ApiError(400, "duplicate_endpoint", f"端点重复: {e}", {"endpoint": e})
        out.append(e)
    return out


# --------------------------------------------------------------------------- #
# 工序处理
# --------------------------------------------------------------------------- #


def step_actions(body):
    seq = body.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise ApiError(400, "bad_seq", "seq 必须为整数工序号")
    actions = body.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ApiError(400, "bad_actions", "actions 必须为非空数组")
    return seq, actions


def act_str(a, field):
    v = a.get(field)
    if not isinstance(v, str) or not v:
        raise ApiError(400, "bad_action", f"动作缺少字符串字段 {field}", {"field": field})
    return v


def handle_remove(st, actions, present_node):
    """拆卸：断榫卯(sever) / 拆构件(member)。临时连接随构件脱离图。"""
    for i, a in enumerate(actions):
        op = act_str(a, "op")
        if op == "sever":
            lk = st.require_link(act_str(a, "link"))
            if lk["kind"] != "joint":
                raise ApiError(400, "wrong_link_kind",
                               f"{lk['code']} 是临时支撑，应使用 unshore 工序",
                               {"first_affected_member": present_node})
            if not lk["in_place"]:
                raise ApiError(409, "link_not_in_place",
                               f"榫卯连接 {lk['code']} 本就断开",
                               {"first_affected_member": present_node})
            st.set_link(lk["code"], False)
        elif op == "member":
            n = st.require_node(act_str(a, "code"))
            if n["kind"] == "support":
                raise ApiError(400, "cannot_remove_support",
                               "固定支承点不可拆卸", {"first_affected_member": present_node})
            if not n["in_place"]:
                raise ApiError(409, "member_not_in_place",
                               f"构件 {n['code']} 当前不在位",
                               {"first_affected_member": present_node})
            # 在位榫卯必须先显式断开（改接只能走已登记组合）
            live_joints = [
                lk["code"] for lk in st.links.values()
                if lk["in_place"] and lk["kind"] == "joint"
                and (lk["a_node"] == n["code"] or lk["b_node"] == n["code"])
            ]
            if live_joints:
                raise ApiError(
                    409, "joint_still_connected",
                    f"构件 {n['code']} 仍有在位榫卯连接，须先 sever: {', '.join(live_joints)}",
                    {"first_affected_member": n["code"], "joints": live_joints},
                )
            st.set_node(n["code"], False)
            # 仍挂在该构件上的临时支撑随之脱离连接图（不占卸撑指标），
            # 复装时须在 install 工序中显式重新 erect
            for lk in st.links.values():
                if lk["in_place"] and lk["kind"] == "shore" and (
                        lk["a_node"] == n["code"] or lk["b_node"] == n["code"]):
                    st.set_link(lk["code"], False)
        else:
            raise ApiError(400, "unknown_action",
                           f"remove 工序不支持动作 {op}",
                           {"first_affected_member": present_node})

    bad = st.first_bad_member(include_shores=True, check_tests=False)
    if bad:
        n, code, reason = bad
        raise ApiError(409, code, f"拆卸后 {reason}: {n['code']}",
                       {"first_affected_member": n["code"]})


def handle_install(st, actions, present_node):
    """安装：装构件(member)/接榫卯(joint)/撑立临时支撑(shore)。"""
    for a in actions:
        op = act_str(a, "op")
        if op == "member":
            n = st.require_node(act_str(a, "code"))
            if n["kind"] == "support":
                raise ApiError(400, "cannot_install_support", "固定支承点无需安装",
                               {"first_affected_member": present_node})
            if n["in_place"]:
                raise ApiError(409, "member_already_in_place",
                               f"构件 {n['code']} 已在位",
                               {"first_affected_member": present_node})
            st.set_node(n["code"], True)
        elif op == "joint":
            lk = st.require_link(act_str(a, "link"))
            if lk["kind"] != "joint":
                raise ApiError(400, "wrong_link_kind",
                               f"{lk['code']} 是临时支撑，应使用 erect 动作",
                               {"first_affected_member": present_node})
            if lk["in_place"]:
                raise ApiError(409, "link_already_in_place",
                               f"榫卯连接 {lk['code']} 已在位",
                               {"first_affected_member": present_node})
            # 端点必须属于已登记组合（link 本身即事先登记的组合）且当前在位
            for ref in (f"{lk['a_node']}:{lk['a_ep']}", f"{lk['b_node']}:{lk['b_ep']}"):
                node, ep = ref.split(":", 1)
                if not st.nodes[node]["in_place"]:
                    raise ApiError(
                        409, "endpoint_not_in_place",
                        f"端点不在位，无法改接: {ref}",
                        {"first_affected_member": present_node, "endpoint": ref})
            st.set_link(lk["code"], True)
        elif op == "shore":
            lk = st.require_link(act_str(a, "link"))
            if lk["kind"] != "shore":
                raise ApiError(400, "wrong_link_kind",
                               f"{lk['code']} 是榫卯连接，应使用 joint 动作",
                               {"first_affected_member": present_node})
            if lk["in_place"]:
                raise ApiError(409, "shore_already_erected",
                               f"临时支撑 {lk['code']} 已撑立",
                               {"first_affected_member": present_node})
            for ref in (f"{lk['a_node']}:{lk['a_ep']}", f"{lk['b_node']}:{lk['b_ep']}"):
                node, ep = ref.split(":", 1)
                if not st.nodes[node]["in_place"]:
                    raise ApiError(
                        409, "endpoint_not_in_place",
                        f"端点不在位，无法撑立: {ref}",
                        {"first_affected_member": present_node, "endpoint": ref})
            st.set_link(lk["code"], True)
        else:
            raise ApiError(400, "unknown_action",
                           f"install 工序不支持动作 {op}",
                           {"first_affected_member": present_node})

    bad = st.first_bad_member(include_shores=True, check_tests=False)
    if bad:
        n, code, reason = bad
        raise ApiError(409, code, f"安装后 {reason}: {n['code']}",
                       {"first_affected_member": n["code"]})


def handle_test(st, actions, present_node):
    """试载：记录每个动作的结果；载荷须 >= 构件最低试载值才算通过。"""
    for a in actions:
        if act_str(a, "op") != "test":
            raise ApiError(400, "unknown_action", "test 工序仅支持 op=test",
                           {"first_affected_member": present_node})
        n = st.require_node(act_str(a, "code"))
        if n["kind"] not in ("member", "part"):
            raise ApiError(400, "not_a_member",
                           f"固定支承点 {n['code']} 不做试载",
                           {"first_affected_member": present_node})
        if not n["in_place"]:
            raise ApiError(409, "member_not_in_place",
                           f"构件 {n['code']} 不在位，无法试载",
                           {"first_affected_member": n["code"]})
        load = a.get("load")
        if not isinstance(load, (int, float)) or isinstance(load, bool) \
                or not math.isfinite(float(load)):
            raise ApiError(400, "bad_load", "load 必须为有限数字",
                           {"first_affected_member": n["code"]})
        load = float(load)
        passed = load + 1e-9 >= (n["min_load"] or 0)
        st.add_test(n["code"], load, passed)
    # 试载不改变连接图，但施工态连通性仍须成立（永久+临时）
    bad = st.first_bad_member(include_shores=True, check_tests=False)
    if bad:
        n, code, reason = bad
        raise ApiError(409, code, f"试载时 {reason}: {n['code']}",
                       {"first_affected_member": n["code"]})


def handle_unshore(st, actions, present_node):
    """卸撑：逐根拆除临时支撑；每根拆完都必须仅凭永久连接成立。"""
    removed = []
    for a in actions:
        if act_str(a, "op") != "shore":
            raise ApiError(400, "unknown_action", "unshore 工序仅支持 op=shore",
                           {"first_affected_member": present_node})
        lk = st.require_link(act_str(a, "link"))
        if lk["kind"] != "shore":
            raise ApiError(400, "wrong_link_kind",
                           f"{lk['code']} 是榫卯连接，不能卸撑",
                           {"first_affected_member": present_node})
        if not lk["in_place"]:
            raise ApiError(409, "shore_not_erected",
                           f"临时支撑 {lk['code']} 未撑立或已拆除",
                           {"first_affected_member": present_node})
        st.set_link(lk["code"], False)
        removed.append(lk["code"])
        # 卸撑条件：仅凭永久连接到达支承点 + 每构件有当前状态下达标试载
        bad = st.first_bad_member(include_shores=False, check_tests=True)
        if bad:
            n, err_code, reason = bad
            raise ApiError(
                409, "unshore_condition_failed",
                f"拆除支撑 {lk['code']} 后卸撑条件不满足：{n['code']} {reason}",
                {"first_affected_member": n["code"], "reason": err_code,
                 "removed_in_step": removed},
            )


HANDLERS = {
    "remove": handle_remove,
    "install": handle_install,
    "test": handle_test,
    "unshore": handle_unshore,
}


def process_step(conn, body):
    if not isinstance(body, dict):
        raise ApiError(400, "bad_json", "请求体必须为 JSON 对象")
    st = State(conn)
    seq, actions = step_actions(body)
    stype = body.get("type")
    if stype not in HANDLERS:
        raise ApiError(400, "bad_type",
                       f"type 必须为 {sorted(HANDLERS)}")

    # 工序号：必须 == 下一个应到序号（拒绝跳号与重号/补号）
    if seq != st.next_seq:
        raise ApiError(
            409, "seq_out_of_order",
            f"工序跳号/重号：收到 {seq}，下一个应为 {st.next_seq}",
            {"received": seq, "expected": st.next_seq},
        )

    # “首个受影响构件”的稳定基准：动作发生时按登记次序最早的在位承重构件
    present_node = min(
        (n["code"] for n in st.placed_members()),
        key=lambda c: st.nodes[c]["seq"],
        default=None,
    )

    # 在内存候选状态上施加全部动作（失败即抛错，尚未写库）
    HANDLERS[stype](st, actions, present_node)

    # 全部通过 => 事务落盘
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    new_version = st.perm_version + st.version_bumps
    cur = conn.cursor()
    cur.execute("UPDATE meta SET perm_version=?, next_seq=? WHERE id=1",
                (new_version, seq + 1))
    summary = build_summary(st, stype)
    cur.execute(
        "INSERT INTO steps(seq,type,accepted_at,perm_version_after,actions,result)"
        " VALUES(?,?,?,?,?,?)",
        (seq, stype, now, new_version,
         json.dumps(actions, ensure_ascii=False),
         json.dumps(summary, ensure_ascii=False)),
    )
    for code, in_place in st.link_updates.items():
        cur.execute("UPDATE links SET in_place=? WHERE code=?", (1 if in_place else 0, code))
    for code, in_place in st.node_updates.items():
        cur.execute("UPDATE nodes SET in_place=? WHERE code=?", (1 if in_place else 0, code))
    for node, load, passed in st.new_tests:
        cur.execute(
            "INSERT INTO load_tests(step_seq,node_code,load,passed,perm_version)"
            " VALUES(?,?,?,?,?)",
            (seq, node, load, 1 if passed else 0, new_version),
        )
    conn.commit()
    return {
        "ok": True,
        "accepted_seq": seq,
        "type": stype,
        "next_expected_seq": seq + 1,
        "perm_version": new_version,
        "result": summary,
    }


def build_summary(st, stype):
    bad = st.first_bad_member(include_shores=False, check_tests=False)
    shores = [lk["code"] for lk in st.links.values() if lk["in_place"] and lk["kind"] == "shore"]
    return {
        "placed_members": sorted(n["code"] for n in st.placed_members()),
        "erected_shores": sorted(shores),
        "permanent_only_reachable": bad is None,
        "can_finalize": (bad is None and not shores
                         and st.first_bad_member(False, True) is None),
    }


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #


def snapshot_body(conn, after_step, version):
    st = State(conn)
    return {
        "after_step": after_step,
        "perm_version": version,
        "nodes": [
            {k: (bool(v) if k == "in_place" else v)
             for k, v in n.items() if k != "seq"}
            for n in sorted(st.nodes.values(), key=lambda x: x["seq"])
        ],
        "links": [
            {k: (bool(v) if k == "in_place" else v)
             for k, v in lk.items() if k != "seq"}
            for lk in sorted(st.links.values(), key=lambda x: x["seq"])
        ],
        "steps": [
            {"seq": r["seq"], "type": r["type"], "accepted_at": r["accepted_at"],
             "perm_version_after": r["perm_version_after"],
             "actions": json.loads(r["actions"]), "result": json.loads(r["result"])}
            for r in conn.execute("SELECT * FROM steps ORDER BY seq")
        ],
        "load_tests": [
            {"step_seq": r["step_seq"], "node_code": r["node_code"],
             "load": r["load"], "passed": bool(r["passed"]),
             "perm_version": r["perm_version"]}
            for r in conn.execute("SELECT * FROM load_tests ORDER BY seq")
        ],
    }


def state_view(conn):
    st = State(conn)
    per_bad = st.first_bad_member(False, False)
    test_bad = st.first_bad_member(False, True)
    shores = sorted(lk["code"] for lk in st.links.values()
                    if lk["in_place"] and lk["kind"] == "shore")
    return {
        "next_expected_seq": st.next_seq,
        "registration_open": st.next_seq == 1,
        "perm_version": st.perm_version,
        "nodes": sorted(st.nodes.values(), key=lambda n: n["seq"]),
        "links": [
            {k: (bool(v) if k == "in_place" else v)
             for k, v in lk.items() if k != "seq"}
            for lk in sorted(st.links.values(), key=lambda x: x["seq"])
        ],
        "latest_tests": st.tests,
        "construction_ok": st.first_bad_member(True, False) is None,
        "permanent_ok": per_bad is None,
        "erected_shores": shores,
        "can_finalize": per_bad is None and not shores and test_bad is None,
    }


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    server_version = "TimberRepair/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > 1_048_576:
            raise ApiError(400, "bad_body", "需要 JSON 请求体（<=1MB）")
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError(400, "bad_json", "请求体不是合法 JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "bad_json", "请求体必须为 JSON 对象")
        return body

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        with self.server.lock:
            conn = db_connect()
            try:
                if path == "/health":
                    self._send(200, {"ok": True})
                elif path == "/state":
                    self._send(200, state_view(conn))
                elif path == "/steps":
                    rows = conn.execute("SELECT * FROM steps ORDER BY seq").fetchall()
                    self._send(200, {"steps": [
                        {"seq": r["seq"], "type": r["type"],
                         "accepted_at": r["accepted_at"],
                         "perm_version_after": r["perm_version_after"],
                         "actions": json.loads(r["actions"]),
                         "result": json.loads(r["result"])} for r in rows]})
                elif path.startswith("/steps/"):
                    try:
                        seq = int(path.split("/")[-1])
                    except ValueError:
                        raise ApiError(404, "not_found", "未知路径")
                    r = conn.execute("SELECT * FROM steps WHERE seq=?", (seq,)).fetchone()
                    if not r:
                        raise ApiError(404, "step_not_found", f"工序 {seq} 不存在")
                    self._send(200, {"seq": r["seq"], "type": r["type"],
                                     "accepted_at": r["accepted_at"],
                                     "perm_version_after": r["perm_version_after"],
                                     "actions": json.loads(r["actions"]),
                                     "result": json.loads(r["result"])})
                elif path == "/snapshots":
                    rows = conn.execute("SELECT * FROM snapshots ORDER BY seq").fetchall()
                    self._send(200, {"snapshots": [
                        {"code": r["code"], "label": r["label"],
                         "after_step": r["after_step"],
                         "perm_version": r["perm_version"],
                         "created_at": r["created_at"]} for r in rows]})
                elif path.startswith("/snapshots/"):
                    code = path.split("/", 2)[-1]
                    r = conn.execute("SELECT * FROM snapshots WHERE code=?", (code,)).fetchone()
                    if not r:
                        raise ApiError(404, "snapshot_not_found", f"快照 {code} 不存在")
                    self._send(200, json.loads(r["body"]))
                elif path == "/registry":
                    self._send(200, {
                        "nodes": [dict(n) for n in conn.execute("SELECT * FROM nodes ORDER BY seq")],
                        "endpoints": [dict(e) for e in conn.execute("SELECT * FROM endpoints ORDER BY seq")],
                        "links": [dict(l) for l in conn.execute("SELECT * FROM links ORDER BY seq")],
                    })
                else:
                    raise ApiError(404, "not_found", f"未知路径: {path}")
            finally:
                conn.close()

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        try:
            body = self._read_json()
            with self.server.lock:
                conn = db_connect()
                try:
                    if path == "/register/support":
                        plan = register_support(State(conn), body)
                        self._persist_registration(conn, body, plan, is_link=False)
                        self._send(201, {"ok": True, "registered": body["code"]})
                    elif path == "/register/member":
                        plan = register_member(State(conn), body, "member")
                        self._persist_registration(conn, body, plan, is_link=False)
                        self._send(201, {"ok": True, "registered": body["code"]})
                    elif path == "/register/part":
                        plan = register_member(State(conn), body, "part")
                        self._persist_registration(conn, body, plan, is_link=False)
                        self._send(201, {"ok": True, "registered": body["code"]})
                    elif path == "/register/joint":
                        plan = register_link(State(conn), body, "joint")
                        self._persist_registration(conn, body, plan, is_link=True)
                        self._send(201, {"ok": True, "registered": body["code"]})
                    elif path == "/register/shore":
                        plan = register_link(State(conn), body, "shore")
                        self._persist_registration(conn, body, plan, is_link=True)
                        self._send(201, {"ok": True, "registered": body["code"]})
                    elif path == "/steps":
                        self._send(201, process_step(conn, body))
                    elif path == "/snapshots":
                        self._send(201, self._finalize(conn, body))
                    else:
                        raise ApiError(404, "not_found", f"未知路径: {path}")
                finally:
                    conn.close()
        except ApiError as e:
            self._send(e.status, e.body())

    @staticmethod
    def _persist_registration(conn, body, plan, is_link):
        cur = conn.cursor()
        if is_link:
            kind, code, an, ae, bn, be, in_place = plan["link"]
            cur.execute(
                "INSERT INTO links(code,kind,a_node,a_ep,b_node,b_ep,in_place)"
                " VALUES(?,?,?,?,?,?,?)",
                (code, kind, an, ae, bn, be, 1 if in_place else 0))
        else:
            kind, code, name, min_load, replaces = plan["node"]
            cur.execute(
                "INSERT INTO nodes(code,kind,name,min_load,replaces,in_place)"
                " VALUES(?,?,?,?,?,?)",
                (code, kind, name, min_load, replaces,
                 1 if plan.get("in_place", kind in ("support", "member")) else 0))
            for node_code, ep in plan["endpoints"]:
                cur.execute("INSERT INTO endpoints(node_code,name) VALUES(?,?)",
                            (node_code, ep))
        conn.commit()

    @staticmethod
    def _finalize(conn, body):
        st = State(conn)
        if st.next_seq == 1:
            raise ApiError(409, "no_steps", "尚无任何工序回执，不能固化快照")
        bad = st.first_bad_member(False, True)
        if bad:
            n, code, reason = bad
            raise ApiError(409, "finalize_condition_failed",
                           f"固化条件不满足：{n['code']} {reason}",
                           {"first_affected_member": n["code"], "reason": code})
        erected = [lk["code"] for lk in st.links.values()
                   if lk["in_place"] and lk["kind"] == "shore"]
        if erected:
            raise ApiError(409, "shores_remaining",
                           f"仍有未卸除的临时支撑: {', '.join(sorted(erected))}")
        code = body.get("code") or f"snap-{uuid.uuid4().hex[:12]}"
        if not isinstance(code, str) or not code:
            raise ApiError(400, "bad_code", "snapshot code 必须为非空字符串")
        label = body.get("label")
        if label is not None and not isinstance(label, str):
            raise ApiError(400, "bad_label", "label 必须为字符串")
        if conn.execute("SELECT 1 FROM snapshots WHERE code=?", (code,)).fetchone():
            raise ApiError(409, "snapshot_exists", f"快照编码已存在: {code}")
        after_step = st.next_seq - 1
        snap = snapshot_body(conn, after_step, st.perm_version)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute(
            "INSERT INTO snapshots(code,label,after_step,perm_version,created_at,body)"
            " VALUES(?,?,?,?,?,?)",
            (code, label, after_step, st.perm_version, now,
             json.dumps(snap, ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "snapshot": code, "after_step": after_step,
                "perm_version": st.perm_version, "created_at": now}


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.lock = threading.Lock()
    # 启动即初始化库
    conn = db_connect()
    conn.close()
    print(f"木构件修复验收 API listening on 0.0.0.0:{PORT} (db={DB_PATH})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
