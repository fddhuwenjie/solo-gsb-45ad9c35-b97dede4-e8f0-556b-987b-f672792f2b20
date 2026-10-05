#!/usr/bin/env python3
"""端到端验证：启动服务 -> 登记 -> 走完换梁修复流程 -> 校验拒绝与快照。

仅用标准库。用法: python3 test_client.py
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:18080"
PASS, FAIL = 0, 0


def call(method, path, body=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            code, obj = r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        code, obj = e.code, json.loads(e.read())
    if expect is not None:
        check(code == expect, f"{method} {path} -> HTTP {code} (期望 {expect}) {obj if code != expect else ''}")
    return code, obj


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {msg}")
    else:
        FAIL += 1
        print(f"  ✗ {msg}")


def step_ok(seq, stype, actions):
    _, o = call("POST", "/steps", {"seq": seq, "type": stype, "actions": actions}, 201)
    check(o["accepted_seq"] == seq and o["next_expected_seq"] == seq + 1,
          f"工序 {seq}({stype}) 接受")
    return o


def step_reject(seq, stype, actions, err_code, member=None):
    _, before = call("GET", "/steps")
    code, o = call("POST", "/steps", {"seq": seq, "type": stype, "actions": actions}, 409)
    check(o["error"] == err_code, f"工序 {seq} 被拒绝: {err_code}（实际 {o.get('error')}：{o.get('message')}）")
    if member is not None:
        check(o.get("first_affected_member") == member,
              f"首个受影响构件 = {member}（实际 {o.get('first_affected_member')}）")
    _, after = call("GET", "/steps")
    check(len(before["steps"]) == len(after["steps"]), "拒绝后回执数量不变（原子回滚）")
    return o


def main():
    tmp = tempfile.mkdtemp()
    env = dict(os.environ, DB_PATH=os.path.join(tmp, "t.db"), PORT="18080")
    proc = subprocess.Popen([sys.executable, "-u", os.path.join(os.path.dirname(__file__), "app.py")],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(50):
            try:
                call("GET", "/health")
                break
            except Exception:
                time.sleep(0.1)
        else:
            print(proc.stdout.read())
            sys.exit(1)

        print("== 登记 ==")
        # 未登记端点组合：拒绝
        _, o = call("POST", "/register/joint",
                    {"code": "JX", "a": "S99:b", "b": "B:e1"}, 400)
        check(o["error"] == "endpoint_not_registered", "未登记端点组合禁止登记连接")

        for body in (
            {"code": "S1", "name": "前檐柱顶", "endpoints": ["b", "sh"]},
            {"code": "S2", "name": "后檐柱顶", "endpoints": ["b"]},
        ):
            call("POST", "/register/support", body, 201)
        call("POST", "/register/member",
             {"code": "B", "name": "五架梁", "min_load": 100,
              "endpoints": ["e1", "e2", "mid"]}, 201)
        call("POST", "/register/part",
             {"code": "P", "name": "替换五架梁", "min_load": 100, "replaces": "B",
              "endpoints": ["f1", "f2", "fm"]}, 201)
        for body in (
            {"code": "J1", "a": "S1:b", "b": "B:e1"},
            {"code": "J2", "a": "S2:b", "b": "B:e2"},
            {"code": "J3", "a": "S1:b", "b": "P:f1"},
            {"code": "J4", "a": "S2:b", "b": "P:f2"},
        ):
            call("POST", "/register/joint", body, 201)
        for body in (
            {"code": "SH", "a": "S1:sh", "b": "B:mid"},
            {"code": "SH2", "a": "S1:sh", "b": "P:fm"},
        ):
            call("POST", "/register/shore", body, 201)

        _, st = call("GET", "/state")
        check(st["next_expected_seq"] == 1 and st["construction_ok"], "初始状态：B 经 J1/J2 抵达支承点")
        check(st["links"] and all(isinstance(l["in_place"], bool) for l in st["links"]), "连接状态正常")

        print("== 阶段一：原梁试载与卸撑 ==")
        step_ok(1, "install", [{"op": "shore", "link": "SH"}])

        # 无合格试载即卸撑 -> 拒绝，首个受影响构件 B
        step_reject(2, "unshore", [{"op": "shore", "link": "SH"}],
                    "unshore_condition_failed", "B")

        o = step_ok(2, "test", [{"op": "test", "code": "B", "load": 100}])
        check(o["perm_version"] == 0, "试载/撑立不推进永久连接版本")

        # 跳号
        step_reject(99, "unshore", [{"op": "shore", "link": "SH"}], "seq_out_of_order")
        # 重号
        step_reject(1, "test", [{"op": "test", "code": "B", "load": 100}], "seq_out_of_order")

        o = step_ok(3, "unshore", [{"op": "shore", "link": "SH"}])
        check(o["result"]["permanent_only_reachable"], "卸撑仅凭永久连接可达")

        print("== 固化第一张验收快照 ==")
        _, s1 = call("POST", "/snapshots",
                     {"code": "accept-original", "label": "原梁验收"}, 201)
        check(s1["after_step"] == 3, "快照定格于工序 3")

        # 开工后登记锁定
        _, o = call("POST", "/register/member",
                    {"code": "X", "name": "x", "min_load": 1, "endpoints": ["e"]}, 409)
        check(o["error"] == "registration_locked", "开工后补登被拒绝")

        print("== 阶段二：换梁 ==")
        # 未撑先断 => B 失去全部支承路径，整步拒绝
        step_reject(4, "remove",
                    [{"op": "sever", "link": "J1"}, {"op": "sever", "link": "J2"}],
                    "unreachable_support", "B")

        step_ok(4, "install", [{"op": "shore", "link": "SH"}])

        # 带未断榫卯直接拆构件 => 拒绝
        step_reject(5, "remove", [{"op": "member", "code": "B"}],
                    "joint_still_connected", "B")

        step_ok(5, "remove", [
            {"op": "sever", "link": "J1"},
            {"op": "sever", "link": "J2"},
            {"op": "member", "code": "B"},
        ])

        # J3 连接的 P 尚未安装：此时无在位承重构件，首构件为 null，但整步仍拒绝
        rej = step_reject(6, "install", [{"op": "joint", "link": "J3"}],
                          "endpoint_not_in_place")
        check(rej.get("first_affected_member") is None,
              f"无在位构件时首个受影响构件为 null（实际 {rej.get('first_affected_member')}）")

        # 未登记的连接编码 => 404 拒绝，回执不变
        _, before = call("GET", "/steps")
        code, o = call("POST", "/steps",
                       {"seq": 6, "type": "install",
                        "actions": [{"op": "member", "code": "P"},
                                    {"op": "joint", "link": "NOPE"}]}, 404)
        check(o["error"] == "unknown_link", "非事先登记的端点组合/连接被拒绝")
        _, after = call("GET", "/steps")
        check(len(before["steps"]) == len(after["steps"]), "拒绝后回执数量不变")

        step_ok(6, "install", [
            {"op": "member", "code": "P"},
            {"op": "joint", "link": "J3"},
            {"op": "joint", "link": "J4"},
        ])
        _, st = call("GET", "/state")
        check(sorted(n["code"] for n in st["nodes"] if n["in_place"]) == ["P", "S1", "S2"],
              "当前在位：P + 支承点")

        # 先提交一次载荷不足的失败试载（作为回执保留）
        o = step_ok(7, "test", [{"op": "test", "code": "P", "load": 50}])
        check(o["result"]["placed_members"] == ["P"], "试载回执可查")

        step_ok(8, "install", [{"op": "shore", "link": "SH2"}])

        # 最近试载未达标 => 卸撑拒绝
        step_reject(9, "unshore", [{"op": "shore", "link": "SH2"}],
                    "unshore_condition_failed", "P")

        # 同一永久连接状态下补做达标试载（撑立不改变版本，旧试载位置已解释原因）
        step_ok(9, "test", [{"op": "test", "code": "P", "load": 120}])
        step_ok(10, "unshore", [{"op": "shore", "link": "SH2"}])

        print("== 固化第二张快照并验证旧快照不可改写 ==")
        _, s2 = call("POST", "/snapshots",
                     {"code": "accept-repaired", "label": "换梁后验收"}, 201)
        check(s2["after_step"] == 10, "新快照定格于工序 10")
        _, o = call("POST", "/snapshots", {"code": "accept-original"}, 409)
        check(o["error"] == "snapshot_exists", "快照编码唯一")

        _, old = call("GET", "/snapshots/accept-original")
        check(old["after_step"] == 3 and old["perm_version"] == 0, "旧快照元数据不变")
        old_codes = [n["code"] for n in old["nodes"] if n["in_place"]]
        check("B" in old_codes and "P" not in old_codes, "旧快照仍是原梁在位状态")
        check(len(old["steps"]) == 3, "旧快照只含工序 1-3")

        _, listing = call("GET", "/snapshots")
        check([s["code"] for s in listing["snapshots"]] == ["accept-original", "accept-repaired"],
              "两张快照均可列出")

        _, steps = call("GET", "/steps")
        seqs = [s["seq"] for s in steps["steps"]]
        check(seqs == list(range(1, 11)), f"工序结果连续 1..10（实际 {seqs}）")
        _, s3 = call("GET", "/steps/7")
        check(s3["actions"][0]["load"] == 50, "可查询单工序（失败试载回执保留）")

        _, st = call("GET", "/state")
        check(st["can_finalize"] and not st["erected_shores"] and st["permanent_ok"],
              "最终状态：无支撑、永久连通、可验收")

        print(f"\n结果: {PASS} 通过, {FAIL} 失败")
        if FAIL:
            sys.exit(1)
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
