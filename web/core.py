"""抽查单核心逻辑（纯 Python，不依赖 Flask，便于自测）。

业务流程：
  1. 中心网页选月份、上传抽查 CSV（站点、月报版号、combo_id、凭证序号）；
  2. 建单前校验：同一站必须同一版；按表中版号向站点逐组合点查申报次数，
     凭证序号须落在 1..申报次数；查不到指定月报或序号越界 → 整单退回；
  3. 建单后中心只对抽中的明细逐条点查凭证（工单号），绝不拉取全量数据；
  4. 取数状态机：pending → confirmed / missing / withdrawn；
     站点超时或不可达时保持 pending，可在原单上续取；订单落盘，重启后仍可继续；
     已确认项保留原结果，迟到的响应（无论成败）一律不覆盖；
     原版已撤下的项标记 withdrawn，提示重新发起抽查。
"""
from __future__ import annotations

import copy
import csv
import io
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# ---- 订单项状态 ----
PENDING = "pending"        # 待取回（含上次取数超时/失败）
CONFIRMED = "confirmed"    # 已确认工单号
MISSING = "missing"        # 缺凭证：原版无对应记录
WITHDRAWN = "withdrawn"    # 原版已撤下，需重新发起抽查
TERMINAL = frozenset({CONFIRMED, MISSING, WITHDRAWN})

STATUS_LABELS = {
    PENDING: "待取回",
    CONFIRMED: "已确认",
    MISSING: "缺凭证",
    WITHDRAWN: "原版已撤下",
}

# 版号会拼进站点查询参数与文件路径，限制字符集
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# 上传 CSV 表头别名（第一行必须是表头）
HEADER_ALIASES = {
    "station": {"station", "站点"},
    "version": {"version", "report_version", "月报版号", "版号"},
    "combo_id": {"combo_id", "combo", "组合编号", "组合"},
    "seq": {"seq", "凭证序号", "序号"},
}


class UploadError(Exception):
    """整单退回。problems 为逐条问题描述（带行号）。"""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class DeclaredNotFound(Exception):
    """站点查不到指定月份+版号的月报。"""


class StationUnavailable(Exception):
    """站点不可达或响应异常，无法完成校验。"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- 1. 上传 CSV 解析 ----

def parse_upload_csv(text: str) -> list[dict]:
    """解析抽查 CSV，返回行列表 [{line, station_raw, version, combo_id, seq}]。

    表头必须能映射到 station/version/combo_id/seq 四列（支持别名）；
    任何一行有问题都抛 UploadError（整单退回）。
    """
    problems: list[str] = []
    try:
        reader = csv.reader(io.StringIO(text))
        rows_raw = [row for row in reader]
    except csv.Error as exc:
        raise UploadError([f"CSV 解析失败: {exc}"]) from exc

    # 跳过空行后再找表头
    rows_raw = [r for r in rows_raw if any(cell.strip() for cell in r)]
    if not rows_raw:
        raise UploadError(["CSV 为空或没有数据行"])

    header = [cell.strip().lower() for cell in rows_raw[0]]
    col_index: dict[str, int] = {}
    for canonical, aliases in HEADER_ALIASES.items():
        for idx, cell in enumerate(header):
            if cell in aliases:
                col_index[canonical] = idx
                break
    missing = [name for name in ("station", "version", "combo_id", "seq") if name not in col_index]
    if missing:
        raise UploadError([
            "CSV 表头缺少必需列: " + ", ".join(missing)
            + "（需要 station,version,combo_id,seq，支持中文别名）"
        ])

    rows: list[dict] = []
    for offset, raw in enumerate(rows_raw[1:], start=2):
        def cell(name: str) -> str:
            idx = col_index[name]
            return raw[idx].strip() if idx < len(raw) else ""

        station_raw = cell("station")
        version = cell("version")
        combo = cell("combo_id")
        raw_seq = cell("seq")

        if not station_raw:
            problems.append(f"第 {offset} 行：站点为空")
            continue
        if not version:
            problems.append(f"第 {offset} 行：月报版号为空")
            continue
        if not VERSION_RE.match(version):
            problems.append(f"第 {offset} 行：版号 {version!r} 含非法字符")
            continue
        if not combo:
            problems.append(f"第 {offset} 行：combo_id 为空")
            continue
        try:
            seq = int(raw_seq)
            if seq < 1:
                raise ValueError
        except ValueError:
            problems.append(f"第 {offset} 行：凭证序号 {raw_seq!r} 不是 >=1 的整数")
            continue
        rows.append({
            "line": offset,
            "station_raw": station_raw,
            "version": version,
            "combo_id": combo,
            "seq": seq,
        })

    if not rows and not problems:
        problems.append("CSV 没有数据行")
    if problems:
        raise UploadError(problems)
    return rows


# ---- 2. 建单前校验 ----

# declared_fetcher(station_key, month, version, combo_id) -> int
#   查不到月报抛 DeclaredNotFound；站点不可达抛 StationUnavailable
DeclaredFetcher = Callable[[str, str, str, str], int]


def _resolve_station(raw: str, stations: dict[str, dict]) -> Optional[str]:
    """站点列可填站点键（如 a/b，大小写不敏感）或站点名（如 维修站A）。"""
    text = raw.strip()
    for key, meta in stations.items():
        if text == meta["label"] or text.lower() == key.lower():
            return key
    return None


def validate_and_build_items(
    rows: list[dict],
    month: str,
    stations: dict[str, dict],
    declared_fetcher: DeclaredFetcher,
) -> list[dict]:
    """校验并生成订单项；任一约束不满足即抛 UploadError（整单退回）。

    约束：站点已知；同一站所有行必须同一版；按表中版号点查申报次数，
    凭证序号须在 1..申报次数 内；查不到指定月报整单退回。
    """
    problems: list[str] = []
    items: list[dict] = []
    version_by_station: dict[str, str] = {}
    declared_cache: dict[tuple[str, str, str], tuple[str, object]] = {}
    now = utcnow()

    for row in rows:
        line = row["line"]
        key = _resolve_station(row["station_raw"], stations)
        if key is None:
            known = "、".join(f"{k}/{m['label']}" for k, m in stations.items())
            problems.append(f"第 {line} 行：未知站点 {row['station_raw']!r}（可填 {known}）")
            continue
        label = stations[key]["label"]
        version = row["version"]
        combo = row["combo_id"]
        seq = row["seq"]

        # 同一站须用同一版
        if key in version_by_station and version_by_station[key] != version:
            problems.append(
                f"第 {line} 行：{label} 其他行用 {version_by_station[key]} 版，"
                f"本行却是 {version} 版（同一站须用同一版）"
            )
            continue
        version_by_station.setdefault(key, version)

        # 按表中版号点查申报次数（同组合只查一次；成功与失败都缓存）
        cache_key = (key, version, combo)
        if cache_key not in declared_cache:
            try:
                declared_cache[cache_key] = ("ok", declared_fetcher(key, month, version, combo))
            except (DeclaredNotFound, StationUnavailable) as exc:
                declared_cache[cache_key] = ("error", exc)
        kind, value = declared_cache[cache_key]
        if kind == "error":
            exc = value
            if isinstance(exc, DeclaredNotFound):
                problems.append(f"第 {line} 行：查不到 {label} {month} 的 {version} 版月报")
            else:
                problems.append(f"第 {line} 行：{label} 不可达（{exc}），无法校验申报次数")
            continue

        declared = int(value)  # type: ignore[arg-type]
        if not 1 <= seq <= declared:
            problems.append(
                f"第 {line} 行：{label} {version} 版 {combo} 申报次数为 {declared}，"
                f"凭证序号 {seq} 不在 1 至 {declared} 内"
            )
            continue

        items.append({
            "n": len(items) + 1,
            "station": key,
            "station_label": label,
            "version": version,
            "combo_id": combo,
            "seq": seq,
            "declared": declared,
            "status": PENDING,
            "work_order": None,
            "note": "",
            "updated_at": now,
        })

    if problems:
        raise UploadError(problems)
    return items


# ---- 3. 取数状态机 ----

@dataclass
class FetchResult:
    """一次凭证点查的结果。kind: confirmed / missing / withdrawn / failed。"""
    kind: str
    work_order: Optional[str] = None
    detail: str = ""


# voucher_fetcher(station_key, month, version, combo_id, seq) -> FetchResult
VoucherFetcher = Callable[[str, str, str, str, int], FetchResult]


def apply_result(item: dict, result: FetchResult, now: Optional[str] = None) -> bool:
    """把一次取数结果落到订单项上；只有 pending 项接受结果。

    已确认/缺凭证/已撤下都是终态：迟到的响应（无论成败）一律不覆盖，
    已确认项保留原结果。返回是否写入了该项。
    """
    if item["status"] != PENDING:
        return False
    now = now or utcnow()
    if result.kind == "confirmed":
        item["status"] = CONFIRMED
        item["work_order"] = result.work_order
        item["note"] = ""
    elif result.kind == "missing":
        item["status"] = MISSING
        item["work_order"] = None
        item["note"] = "原版无对应凭证记录"
    elif result.kind == "withdrawn":
        item["status"] = WITHDRAWN
        item["work_order"] = None
        item["note"] = result.detail or "原版已撤下"
    else:  # failed：保持 pending，记下原因，可在原单上续取
        item["note"] = result.detail or "站点取数失败"
    item["updated_at"] = now
    return True


def run_fetch_pass(order: dict, voucher_fetcher: VoucherFetcher, now: Optional[str] = None) -> dict:
    """对订单内全部 pending 项逐条点查凭证；返回本次统计。

    只碰 pending 项；终态项直接跳过（不会重复取数，也不会被覆盖）。
    """
    now = now or utcnow()
    stats = {"confirmed": 0, "missing": 0, "withdrawn": 0, "failed": 0, "skipped": 0}
    for item in order["items"]:
        if item["status"] != PENDING:
            stats["skipped"] += 1
            continue
        try:
            result = voucher_fetcher(
                item["station"], order["month"], item["version"],
                item["combo_id"], item["seq"],
            )
        except Exception as exc:  # 兜底：任何异常都视为本次取数失败，保持 pending
            result = FetchResult("failed", detail=str(exc))
        if not apply_result(item, result, now):
            stats["skipped"] += 1  # 并发下已被其他流程确认：迟到响应不覆盖
        elif result.kind in ("confirmed", "missing", "withdrawn"):
            stats[result.kind] += 1
        else:
            stats["failed"] += 1
    return stats


def summarize(order: dict) -> dict:
    """订单状态汇总（用于列表与页面展示）。"""
    counts = {PENDING: 0, CONFIRMED: 0, MISSING: 0, WITHDRAWN: 0}
    for item in order["items"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "total": len(order["items"]),
        "pending": counts[PENDING],
        "confirmed": counts[CONFIRMED],
        "missing": counts[MISSING],
        "withdrawn": counts[WITHDRAWN],
        "done": counts[PENDING] == 0,
    }


# ---- 4. 订单持久化 ----

class OrderStore:
    """订单落盘（单 JSON 文件 + 原子替换）。重启后订单仍在，可继续续取。"""

    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._order_locks: dict[str, threading.Lock] = {}
        self._orders: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            self._orders = []
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            orders = data.get("orders") if isinstance(data, dict) else data
            if not isinstance(orders, list):
                raise ValueError("orders 不是列表")
            self._orders = orders
        except Exception:
            # 文件损坏：备份后从空开始，避免服务起不来
            backup = self._path.with_name(f"{self._path.name}.corrupt-{int(time.time())}")
            os.replace(self._path, backup)
            self._orders = []

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(
            json.dumps({"orders": self._orders}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)  # 原子替换，崩溃不留半截文件

    def order_lock(self, order_id: str) -> threading.Lock:
        """每个订单一把锁：同一订单的取数/修改串行，防止并发覆盖。"""
        with self._lock:
            return self._order_locks.setdefault(order_id, threading.Lock())

    def list_orders(self) -> list[dict]:
        """全部订单的副本，新的在前。"""
        with self._lock:
            return copy.deepcopy(list(reversed(self._orders)))

    def get(self, order_id: str) -> Optional[dict]:
        """订单副本；不存在返回 None。"""
        with self._lock:
            for order in self._orders:
                if order["id"] == order_id:
                    return copy.deepcopy(order)
        return None

    def create(self, month: str, items: list[dict]) -> dict:
        with self._lock:
            order = {
                "id": uuid.uuid4().hex[:12],
                "month": month,
                "created_at": utcnow(),
                "items": items,
            }
            self._orders.append(order)
            self._save_locked()
            return copy.deepcopy(order)

    def save(self, order: dict) -> None:
        """把修改后的订单写回（按 id 替换）并落盘。"""
        with self._lock:
            for idx, existing in enumerate(self._orders):
                if existing["id"] == order["id"]:
                    self._orders[idx] = copy.deepcopy(order)
                    self._save_locked()
                    return
        raise KeyError(f"订单不存在: {order['id']}")
