"""站点服务（两个维修站各部署一个实例）。

数据布局（STATION_DATA_DIR 下）：
  <month>/<version>.report.csv     月报：combo_id,count（申报次数）
  <month>/<version>.vouchers.csv   凭证：combo_id,seq,work_order（工单号）
同一版的月报与凭证按版配对、版内内容固定；撤版 = 撤下该版的两个文件。

对外只暴露两个受控点查接口（中心只能索取抽中的明细，拿不到全量数据）：
  1. GET /api/declared?month=YYYY-MM&version=版号&combo=编号
     查某组合在该版月报上的申报次数；组合缺号计 0；月份或版号不存在返回 404。
  2. GET /api/voucher?month=YYYY-MM&version=版号&combo=编号&seq=序号
     查某组合第 seq 号凭证的工单号；无对应记录返回 found=false（缺凭证），
     月份或版号不存在（已撤下）返回 404。
"""
from __future__ import annotations

import csv
import os
import re
from pathlib import Path

from flask import Flask, jsonify, request

DATA_DIR = Path(os.environ.get("STATION_DATA_DIR", "/data"))
STATION_NAME = os.environ.get("STATION_NAME", "station")

# month/version 会拼进文件路径，必须限制字符集，防路径穿越
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _parse_report(path: Path) -> dict[str, int]:
    """月报 CSV -> {combo_id: 申报次数}；同版内组合唯一。"""
    report: dict[str, int] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "combo_id" not in reader.fieldnames or "count" not in reader.fieldnames:
            raise ValueError(f"{path.name} 表头必须包含 combo_id,count")
        for line_no, row in enumerate(reader, start=2):
            combo = (row.get("combo_id") or "").strip()
            raw_count = (row.get("count") or "").strip()
            if not combo:
                raise ValueError(f"{path.name} 第 {line_no} 行组合编号为空")
            if combo in report:
                raise ValueError(f"{path.name} 组合 {combo} 重复出现")
            try:
                count = int(raw_count)
            except ValueError as exc:
                raise ValueError(f"{path.name} 第 {line_no} 行次数不是整数: {raw_count!r}") from exc
            if count < 0:
                raise ValueError(f"{path.name} 第 {line_no} 行次数为负数")
            report[combo] = count
    return report


def _parse_vouchers(path: Path) -> dict[tuple[str, int], str]:
    """凭证 CSV -> {(combo_id, seq): work_order}；同版内 (组合,序号) 唯一。"""
    vouchers: dict[tuple[str, int], str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        names = reader.fieldnames or []
        for col in ("combo_id", "seq", "work_order"):
            if col not in names:
                raise ValueError(f"{path.name} 表头必须包含 combo_id,seq,work_order")
        for line_no, row in enumerate(reader, start=2):
            combo = (row.get("combo_id") or "").strip()
            raw_seq = (row.get("seq") or "").strip()
            work_order = (row.get("work_order") or "").strip()
            if not combo:
                raise ValueError(f"{path.name} 第 {line_no} 行组合编号为空")
            try:
                seq = int(raw_seq)
                if seq < 1:
                    raise ValueError
            except ValueError as exc:
                raise ValueError(f"{path.name} 第 {line_no} 行序号不是 >=1 的整数: {raw_seq!r}") from exc
            if not work_order:
                raise ValueError(f"{path.name} 第 {line_no} 行工单号为空")
            key = (combo, seq)
            if key in vouchers:
                raise ValueError(f"{path.name} 组合 {combo} 序号 {seq} 重复出现")
            vouchers[key] = work_order
    return vouchers


def _load_version(month: str, version: str) -> tuple[dict[str, int], dict[tuple[str, int], str]]:
    """读取某版的月报+凭证配对文件；任一个缺失都视为该版不存在（已撤下）。"""
    base = DATA_DIR / month
    report_path = base / f"{version}.report.csv"
    vouchers_path = base / f"{version}.vouchers.csv"
    if not report_path.is_file() or not vouchers_path.is_file():
        raise FileNotFoundError(f"{month}/{version}")
    return _parse_report(report_path), _parse_vouchers(vouchers_path)


def _bad_request(message: str):
    return jsonify({"error": message}), 400


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["STATION_NAME"] = STATION_NAME

    def _parse_args(require_seq: bool):
        """解析并校验公共查询参数；返回 (month, version, combo, seq, error_response)。"""
        month = (request.args.get("month") or "").strip()
        version = (request.args.get("version") or "").strip()
        combo = (request.args.get("combo") or "").strip()
        if not MONTH_RE.match(month):
            return None, None, None, None, _bad_request("month 必须是 YYYY-MM 格式")
        if not VERSION_RE.match(version):
            return None, None, None, None, _bad_request("version 缺失或含非法字符")
        if not combo:
            return None, None, None, None, _bad_request("缺少 combo 参数")
        seq = None
        if require_seq:
            raw_seq = (request.args.get("seq") or "").strip()
            try:
                seq = int(raw_seq)
                if seq < 1:
                    raise ValueError
            except ValueError:
                return None, None, None, None, _bad_request("seq 必须是 >=1 的整数")
        return month, version, combo, seq, None

    def _load_or_404(month: str, version: str):
        """加载版本数据；不存在返回 (None, None, 404响应)，数据异常返回 500。"""
        try:
            report, vouchers = _load_version(month, version)
        except FileNotFoundError:
            return None, None, (jsonify({
                "error": f"站点 {STATION_NAME} 没有 {month} 的 {version} 版月报（不存在或已撤下）"
            }), 404)
        except ValueError as exc:
            return None, None, (jsonify({
                "error": f"站点 {STATION_NAME} 数据文件异常: {exc}"
            }), 500)
        return report, vouchers, None

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "station": STATION_NAME})

    @app.get("/api/declared")
    def declared():
        month, version, combo, _, error = _parse_args(require_seq=False)
        if error is not None:
            return error
        report, _, error = _load_or_404(month, version)
        if error is not None:
            return error
        return jsonify({
            "station": STATION_NAME,
            "month": month,
            "version": version,
            "combo_id": combo,
            "declared": report.get(combo, 0),  # 组合缺号计 0
        })

    @app.get("/api/voucher")
    def voucher():
        month, version, combo, seq, error = _parse_args(require_seq=True)
        if error is not None:
            return error
        _, vouchers, error = _load_or_404(month, version)
        if error is not None:
            return error
        work_order = vouchers.get((combo, seq))
        payload = {
            "station": STATION_NAME,
            "month": month,
            "version": version,
            "combo_id": combo,
            "seq": seq,
            "found": work_order is not None,
        }
        if work_order is not None:
            payload["work_order"] = work_order
        return jsonify(payload)

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
