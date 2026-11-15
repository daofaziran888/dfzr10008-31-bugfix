"""跨源数据关联业务规则：状态流转、字段校验、关联结果计算与导出快照都收在这里。

链路口径：登记（保存全部字段）→ 启用（按序流转并计算关联结果）→ 变更（重算并归档旧结果）
→ 导出（冻结当前字段匹配快照，只含本规则数据）。列表页、详情页、导出入口读同一份结果，
避免旧属性残留。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any

from app.store import store

MODULE = "crossref"
RESULT_MODULE = "crossref_results"
REQUIRED_FIELDS = ["规则编号", "主数据源", "关联数据源"]
EDITABLE_FIELDS = ["规则编号", "主数据源", "关联数据源", "关联字段", "关联类型", "结果字段", "时效控制"]
# 这些字段一变，关联结果就必须重算，否则详情/列表/导出会继续显示旧属性
JOIN_CONFIG_FIELDS = ["主数据源", "关联数据源", "关联字段", "关联类型", "结果字段", "时效控制"]
STATUS_ORDER = ["草稿", "已启用", "已暂停", "已关闭"]
# 动作 -> (允许执行的当前状态, 推进到的目标状态)：启用/暂停/关闭必须按序推进
ACTION_RULES = {"启用关联": ("草稿", "已启用"), "暂停关联": ("已启用", "已暂停"), "关闭关联": ("已暂停", "已关闭")}
NEGATIVE_ACTIONS: list[str] = []
JOIN_TYPES = {"left join", "inner join"}

# 已上传的数据源登记表：导出前与启用前都要校验主/关联数据源在册
DATASOURCES: dict[str, list[dict[str, Any]]] = {
    "orders": [
        {"order_id": "O-1001", "store_id": "S01", "amount": 199.5, "channel": "线上"},
        {"order_id": "O-1002", "store_id": "S02", "amount": 58.0, "channel": "门店"},
        {"order_id": "O-1003", "store_id": "S01", "amount": 320.0, "channel": "线上"},
        {"order_id": "O-1004", "store_id": "S09", "amount": 76.5, "channel": "门店"},
    ],
    "stores": [
        {"store_id": "S01", "store_name": "朝阳旗舰店", "region": "华东"},
        {"store_id": "S02", "store_name": "高新体验店", "region": "西南"},
        {"store_id": "S03", "store_name": "滨江快闪店", "region": "华南"},
    ],
}

_FIELD_SPLIT = re.compile(r"[、,，;；\s]+")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _result_fields(entry: dict[str, Any]) -> list[str]:
    raw = str(entry.get("结果字段") or "")
    return [part for part in _FIELD_SPLIT.split(raw) if part]


def _join_keys(entry: dict[str, Any]) -> tuple[str, str]:
    """把「orders.store_id=stores.store_id」解析成（主数据源键, 关联数据源键）。

    带表名前缀时按主/关联数据源名归位，交换主次数据源后仍能正确取键；
    不带前缀时按书写顺序，左侧归主数据源。
    """
    raw = str(entry.get("关联字段") or "")
    if "=" not in raw:
        return "", ""
    left, _, right = raw.partition("=")
    parsed: list[tuple[str, str]] = []
    for side in (left.strip(), right.strip()):
        table, dot, column = side.partition(".")
        parsed.append((table.strip(), column.strip()) if dot else ("", table.strip()))
    primary_name = str(entry.get("主数据源") or "").strip()
    secondary_name = str(entry.get("关联数据源") or "").strip()
    for index, (table, _) in enumerate(parsed):
        if table and table == primary_name:
            return parsed[index][1], parsed[1 - index][1]
    for index, (table, _) in enumerate(parsed):
        if table and table == secondary_name:
            return parsed[1 - index][1], parsed[index][1]
    return parsed[0][1], parsed[1][1]


class CrossrefService:
    """跨源数据关联的规则、状态机与关联结果都走这里，保证两个页面和导出看到同一份数据。"""

    def __init__(self) -> None:
        self._locks_guard = threading.Lock()
        self._locks: dict[int, threading.Lock] = {}

    # ---- 列表与详情 -------------------------------------------------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None and entry.get("status") == "已启用":
            self._ensure_fresh_result(entry)
        return entry

    # ---- 登记与保存 -------------------------------------------------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, [f"缺少必填字段：{'、'.join(missing)}"]
        errors = self._validate_config(values)
        if errors:
            return None, errors
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        # 保存全部可登记字段：关联字段、关联类型、结果字段、时效控制一个都不能丢
        entry.update({field: values.get(field) for field in EDITABLE_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["规则状态"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, [f"关联规则 {entry_id} 不存在或已归档"]
        if entry.get("status") == STATUS_ORDER[-1]:
            return None, ["规则已关闭，不能再修改配置"]
        changed: dict[str, Any] = {}
        for field in EDITABLE_FIELDS:
            if field in values and values.get(field) != entry.get(field):
                changed[field] = values.get(field)
        if not changed:
            return entry, []
        errors = self._validate_config({**entry, **changed})
        if errors:
            return None, errors
        entry.update(changed)
        # 主/关联数据源、关联字段等一变就重算，旧属性（如旧的 region）不得残留
        if entry.get("status") == "已启用" and any(field in changed for field in JOIN_CONFIG_FIELDS):
            _, error = self._rematerialize(entry)
            if error:
                return None, [error]
        return entry, []

    # ---- 状态机：启用/暂停/关闭按序推进 ------------------------------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"关联规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于跨源数据关联可执行范围"
        required, target = ACTION_RULES[action]
        if action == "启用关联":
            return self._enable(entry, required, target)
        if entry.get("status") != required:
            return None, f"当前状态为「{entry.get('status')}」，「{action}」需要从「{required}」按序推进"
        self._transition(entry, target, action)
        return entry, f"关联规则已{action}"

    def _enable(self, entry: dict[str, Any], required: str, target: str) -> tuple[dict[str, Any] | None, str]:
        # 同一条规则的启用请求串行化：两个运营同时启用，只保留一个跨源关联结果
        lock = self._lock_for(int(entry.get("id", 0)))
        with lock:
            if entry.get("status") == target:
                return entry, "关联规则已处于已启用状态，保留当前跨源关联结果"
            if entry.get("status") != required:
                return None, f"当前状态为「{entry.get('status')}」，「启用关联」需要从「{required}」按序推进"
            _, error = self._rematerialize(entry)
            if error:
                return None, error
            self._transition(entry, target, "启用关联")
            return entry, "关联规则已启用关联，跨源关联结果已生成"

    def _transition(self, entry: dict[str, Any], target: str, action: str) -> None:
        entry["status"] = target
        entry["规则状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS

    # ---- 关联结果：计算、重算、追溯 ----------------------------------

    def list_results(self, entry_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """返回（当前结果, 历史结果）：旧时效结果留在历史里可追溯。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, []
        if entry.get("status") == "已启用":
            self._ensure_fresh_result(entry)
        records = [row for row in store.rows(RESULT_MODULE) if int(row.get("rule_id", 0)) == entry_id]
        records.sort(key=lambda row: int(row.get("version", 0)), reverse=True)
        current = next((row for row in records if row.get("结果状态") == "当前"), None)
        history = [row for row in records if row.get("结果状态") != "当前"]
        return current, history

    def build_export(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """冻结当前字段匹配快照打包导出；只取本规则的结果，不混入其他规则。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"关联规则 {entry_id} 不存在或已归档"
        missing = self._missing_sources(entry)
        if missing:
            return None, f"导出前校验未通过：数据源 {'、'.join(missing)} 未上传，请先上传主次数据源"
        if entry.get("status") == "已启用":
            result = self._ensure_fresh_result(entry)
        else:
            result, _ = self._results_for(entry_id)
        if result is None:
            return None, "规则尚未启用，没有可导出的跨源关联结果，请先启用关联"
        snapshot = dict(result["snapshot"])
        return {
            "module": MODULE,
            "规则编号": entry.get("规则编号"),
            "规则状态": entry.get("status"),
            "结果版本": result.get("version"),
            "导出时间": _now(),
            "字段匹配": snapshot,
            "结果行数": result.get("row_count", 0),
            "rows": list(result.get("rows", [])),
        }, ""

    def _ensure_fresh_result(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """启用中的规则必须持有与当前字段匹配一致的结果；不一致就重算，杜绝旧值。"""
        current, _ = self._results_for(int(entry.get("id", 0)))
        if current is not None and not self._snapshot_stale(entry, current):
            return current
        result, _ = self._rematerialize(entry)
        return result

    def _results_for(self, entry_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        records = [row for row in store.rows(RESULT_MODULE) if int(row.get("rule_id", 0)) == entry_id]
        records.sort(key=lambda row: int(row.get("version", 0)), reverse=True)
        current = next((row for row in records if row.get("结果状态") == "当前"), None)
        return current, [row for row in records if row.get("结果状态") != "当前"]

    def _snapshot_stale(self, entry: dict[str, Any], result: dict[str, Any]) -> bool:
        snapshot = result.get("snapshot", {})
        for field in JOIN_CONFIG_FIELDS:
            if field == "结果字段":
                if list(snapshot.get(field, [])) != _result_fields(entry):
                    return True
            elif str(snapshot.get(field) or "") != str(entry.get(field) or ""):
                return True
        return False

    def _rematerialize(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        missing = self._missing_sources(entry)
        if missing:
            return None, f"数据源 {'、'.join(missing)} 未上传，无法计算跨源关联结果"
        rule_id = int(entry.get("id", 0))
        records = store.rows(RESULT_MODULE)
        for record in records:
            if int(record.get("rule_id", 0)) == rule_id and record.get("结果状态") == "当前":
                record["结果状态"] = "历史"  # 旧时效结果归档，仍可追溯
        rows = self._compute_rows(entry)
        version = max((int(r.get("version", 0)) for r in records if int(r.get("rule_id", 0)) == rule_id), default=0) + 1
        result = {
            "id": max((int(r.get("id", 0)) for r in records), default=0) + 1,
            "rule_id": rule_id,
            "规则编号": entry.get("规则编号"),
            "version": version,
            "结果状态": "当前",
            "generated_at": _now(),
            "snapshot": {
                "主数据源": entry.get("主数据源"),
                "关联数据源": entry.get("关联数据源"),
                "关联字段": entry.get("关联字段"),
                "关联类型": entry.get("关联类型"),
                "结果字段": _result_fields(entry),
                "时效控制": entry.get("时效控制"),
            },
            "rows": rows,
            "row_count": len(rows),
        }
        records.append(result)
        return result, ""

    def _compute_rows(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        primary_name = str(entry.get("主数据源") or "").strip()
        secondary_name = str(entry.get("关联数据源") or "").strip()
        primary_rows = DATASOURCES.get(primary_name, [])
        secondary_rows = DATASOURCES.get(secondary_name, [])
        primary_key, secondary_key = _join_keys(entry)
        fields = _result_fields(entry)
        join_type = str(entry.get("关联类型") or "left join").strip().lower()
        index: dict[str, dict[str, Any]] = {}
        for row in secondary_rows:
            index.setdefault(str(row.get(secondary_key)), row)
        output: list[dict[str, Any]] = []
        for row in primary_rows:
            match = index.get(str(row.get(primary_key)))
            if match is None and join_type == "inner join":
                continue
            merged = dict(row)
            for field in fields:
                merged[field] = match.get(field) if match else None
            output.append(merged)
        return output

    # ---- 校验与锁 ----------------------------------------------------

    def _validate_config(self, values: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        join_type = str(values.get("关联类型") or "").strip().lower()
        if join_type and join_type not in JOIN_TYPES:
            errors.append(f"关联类型「{values.get('关联类型')}」不支持，可选：{'、'.join(sorted(JOIN_TYPES))}")
        errors.extend(f"数据源 {name} 未上传" for name in self._missing_sources(values))
        return errors

    def _missing_sources(self, values: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        for field in ("主数据源", "关联数据源"):
            name = str(values.get(field) or "").strip()
            if name and name not in DATASOURCES:
                missing.append(name)
        return missing

    def _lock_for(self, entry_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(entry_id, threading.Lock())
