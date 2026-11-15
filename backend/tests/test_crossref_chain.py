"""跨源数据关联链路回归：登记 → 保存回显 → 按序启用 → 导出快照 → 交换重算 → 暂停/关闭。

只依赖标准库：临时起一个 uvicorn 进程跑真实 HTTP 链路，覆盖并发启用与历史追溯。
运行：cd backend && .venv/bin/python tests/test_crossref_chain.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8123"
ROOT = Path(__file__).resolve().parent.parent


def call(method: str, path: str, values: dict | None = None) -> tuple[int, dict]:
    body = json.dumps({"values": values}).encode() if values is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=body, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not condition:
        raise SystemExit(f"回归失败：{name} {detail}")


def main() -> None:
    proc = subprocess.Popen(
        [str(ROOT / ".venv/bin/uvicorn"), "app.main:app", "--host", "127.0.0.1", "--port", "8123"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                status, _ = call("GET", "/api/health")
                if status == 200:
                    break
            except urllib.error.URLError:
                time.sleep(0.2)
        # 1. 登记：保存全部字段
        values = {"规则编号": "XREF-0002", "主数据源": "orders", "关联数据源": "stores",
                  "关联字段": "orders.store_id=stores.store_id", "关联类型": "left join",
                  "结果字段": "store_name、region", "时效控制": "日级"}
        status, payload = call("POST", "/api/crossref", values)
        check("登记保存全部字段", status == 200 and payload["ok"]
              and all(payload["entry"].get(k) == v for k, v in values.items()))
        rule_id = payload["entry"]["id"]
        # 2. 详情回显
        status, detail = call("GET", f"/api/crossref/{rule_id}")
        check("详情回显字段匹配", all(detail.get(k) == v for k, v in values.items()))
        # 3. 列表导出不再 422，且带字段
        status, export = call("GET", "/api/crossref/export")
        check("清单导出可用", status == 200 and export["total"] >= 1
              and all("关联字段" in item for item in export["items"]))
        # 4. 状态机按序：草稿不能直接暂停/关闭
        for action in ("暂停关联", "关闭关联"):
            _, payload = call("POST", f"/api/crossref/{rule_id}/actions", {"action": action})
            check(f"草稿禁止{action}", not payload["ok"])
        # 5. 并发启用：只保留一个跨源关联结果
        barrier = threading.Barrier(2)
        results: list[dict] = []

        def enable() -> None:
            barrier.wait()
            _, payload = call("POST", f"/api/crossref/{rule_id}/actions", {"action": "启用关联"})
            results.append(payload)

        threads = [threading.Thread(target=enable) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        check("并发启用都成功", all(payload["ok"] for payload in results))
        _, results_payload = call("GET", f"/api/crossref/{rule_id}/results")
        check("并发启用只保留一个结果", results_payload["current"]["version"] == 1
              and len(results_payload["history"]) == 0)
        # 6. 导出冻结字段匹配快照，不混入其他规则
        status, bundle = call("GET", f"/api/crossref/{rule_id}/export")
        check("导出字段匹配非空", status == 200 and bundle["字段匹配"]["结果字段"] == ["store_name", "region"])
        check("导出只含本规则", bundle["规则编号"] == "XREF-0002"
              and all(row.get("order_id", "").startswith("O-") for row in bundle["rows"]))
        # 7. 交换主次数据源：保存后重算，旧属性不残留，旧时效结果可追溯
        swapped = {"主数据源": "stores", "关联数据源": "orders",
                   "关联字段": "stores.store_id=orders.store_id", "结果字段": "order_id、amount"}
        _, payload = call("PUT", f"/api/crossref/{rule_id}", swapped)
        check("交换主次数据源保存成功", payload["ok"])
        _, bundle = call("GET", f"/api/crossref/{rule_id}/export")
        check("导出为重算快照", bundle["字段匹配"]["主数据源"] == "stores"
              and bundle["字段匹配"]["结果字段"] == ["order_id", "amount"])
        check("旧 region 投影不残留", all("region" not in bundle["字段匹配"]["结果字段"]
              for _ in [0]) and all(set(["order_id", "amount"]) <= set(row) for row in bundle["rows"]))
        _, results_payload = call("GET", f"/api/crossref/{rule_id}/results")
        check("旧日级结果可追溯", len(results_payload["history"]) == 1
              and results_payload["history"][0]["snapshot"]["时效控制"] == "日级")
        # 8. 按序走完 暂停 → 关闭
        _, payload = call("POST", f"/api/crossref/{rule_id}/actions", {"action": "关闭关联"})
        check("已启用禁止直接关闭", not payload["ok"])
        _, payload = call("POST", f"/api/crossref/{rule_id}/actions", {"action": "暂停关联"})
        check("暂停关联", payload["ok"] and payload["entry"]["status"] == "已暂停")
        _, payload = call("POST", f"/api/crossref/{rule_id}/actions", {"action": "关闭关联"})
        check("关闭关联", payload["ok"] and payload["entry"]["status"] == "已关闭")
        _, payload = call("POST", f"/api/crossref/{rule_id}/actions", {"action": "启用关联"})
        check("已关闭禁止再启用", not payload["ok"])
        # 9. 未上传数据源：登记与导出前校验
        _, payload = call("POST", "/api/crossref", {"规则编号": "XREF-0099", "主数据源": "orders",
                                                    "关联数据源": "legacy_db"})
        check("未上传数据源登记被拦", not payload["ok"] and "legacy_db" in payload["message"])
        print("\n全部回归用例通过")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
