"""中心抽查网页（Flask 薄层）。

页面流程：
  1. 首页选月份、上传抽查 CSV（站点、月报版号、combo_id、凭证序号）建单；
     建单前按表中版号向站点点查申报次数校验，任一约束不满足整单退回；
  2. 订单页逐项列出工单号；站点超时/不可达的项保持“待取回”，
     点“取数/续取”按钮在原单上续取；订单落盘，服务重启后仍可继续；
  3. 原版无对应记录的项标“缺凭证”；原版已撤下的项标“原版已撤下”，
     页面提示重新发起抽查；已确认项保留原结果，迟到响应不覆盖。

取数严格通过站点的两个受控点查接口（/api/declared、/api/voucher），
中心只索取抽中的明细，不拉取站点全量数据。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

import core

MONTHS = [m.strip() for m in os.environ.get("AVAILABLE_MONTHS", "2026-01,2026-02,2026-03").split(",") if m.strip()]
STATIONS = {
    "a": {
        "url": os.environ.get("STATION_A_URL", "http://station-a:8000"),
        "label": os.environ.get("STATION_A_LABEL", "维修站A"),
    },
    "b": {
        "url": os.environ.get("STATION_B_URL", "http://station-b:8000"),
        "label": os.environ.get("STATION_B_LABEL", "维修站B"),
    },
}
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "5"))
DATA_DIR = Path(os.environ.get("WEB_DATA_DIR", str(Path(__file__).resolve().parent / "data")))
MAX_UPLOAD_BYTES = 1 * 1024 * 1024  # 抽查 CSV 不会超过 1MB


class _NotFound(Exception):
    """站点返回 404：月份或版号不存在（未申报或已撤下）。"""


class _FetchFailed(Exception):
    """站点不可达、超时或响应异常。"""


def _http_get_json(base_url: str, path: str, params: dict[str, Any]) -> dict:
    url = base_url.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise _NotFound(str(exc)) from exc
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:
            detail = f"HTTP {exc.code}"
        raise _FetchFailed(detail or f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _FetchFailed(f"无法连接站点: {exc}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise _FetchFailed("响应不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise _FetchFailed("响应格式不正确")
    return payload


def declared_fetcher(station_key: str, month: str, version: str, combo: str) -> int:
    """建单前校验：向站点点查某组合在该版月报上的申报次数。"""
    station = STATIONS[station_key]
    try:
        payload = _http_get_json(station["url"], "/api/declared", {
            "month": month, "version": version, "combo": combo,
        })
    except _NotFound as exc:
        raise core.DeclaredNotFound(f"{month}/{version}") from exc
    except _FetchFailed as exc:
        raise core.StationUnavailable(str(exc)) from exc
    try:
        return int(payload["declared"])
    except (KeyError, TypeError, ValueError) as exc:
        raise core.StationUnavailable("申报次数响应格式不正确") from exc


def voucher_fetcher(station_key: str, month: str, version: str, combo: str, seq: int) -> core.FetchResult:
    """取数：向站点点查某组合第 seq 号凭证的工单号。"""
    station = STATIONS[station_key]
    label = station["label"]
    try:
        payload = _http_get_json(station["url"], "/api/voucher", {
            "month": month, "version": version, "combo": combo, "seq": seq,
        })
    except _NotFound:
        return core.FetchResult(
            "withdrawn",
            detail=f"{label} 已查不到 {month} 的 {version} 版月报（原版已撤下）",
        )
    except _FetchFailed as exc:
        return core.FetchResult("failed", detail=f"{label} 取数失败：{exc}")
    if payload.get("found") and payload.get("work_order"):
        return core.FetchResult("confirmed", work_order=str(payload["work_order"]))
    return core.FetchResult("missing")


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "spotcheck-dev-key")
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    store = core.OrderStore(DATA_DIR / "orders.json")
    app.extensions["order_store"] = store

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/")
    def index():
        orders = [
            {**order, "summary": core.summarize(order)}
            for order in store.list_orders()
        ]
        return render_template(
            "index.html",
            months=MONTHS,
            orders=orders,
            problems=None,
            selected_month=request.args.get("month", ""),
        )

    @app.post("/orders")
    def create_order():
        month = (request.form.get("month") or "").strip()
        upload = request.files.get("file")

        def reject(problems: list[str]):
            orders = [
                {**order, "summary": core.summarize(order)}
                for order in store.list_orders()
            ]
            return render_template(
                "index.html",
                months=MONTHS,
                orders=orders,
                problems=problems,
                selected_month=month,
            ), 400

        if month not in MONTHS:
            return reject(["请选择有效的月份。"])
        if upload is None or not upload.filename:
            return reject(["请上传抽查 CSV 文件。"])
        try:
            text = upload.stream.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return reject(["CSV 不是合法的 UTF-8 文本。"])

        try:
            rows = core.parse_upload_csv(text)
            items = core.validate_and_build_items(
                rows, month,
                {key: {"label": s["label"]} for key, s in STATIONS.items()},
                declared_fetcher,
            )
        except core.UploadError as exc:
            return reject(exc.problems)  # 整单退回，不落单

        order = store.create(month, items)
        flash(f"抽查单 {order['id']} 已建（{len(items)} 项），请在订单页取数。")
        return redirect(url_for("order_page", order_id=order["id"]))

    @app.get("/orders/<order_id>")
    def order_page(order_id: str):
        order = store.get(order_id)
        if order is None:
            abort(404)
        return render_template(
            "order.html",
            order=order,
            summary=core.summarize(order),
            status_labels=core.STATUS_LABELS,
        )

    @app.post("/orders/<order_id>/fetch")
    def fetch_order(order_id: str):
        # 同一订单串行取数；锁内重读最新订单，避免并发续取互相覆盖
        with store.order_lock(order_id):
            order = store.get(order_id)
            if order is None:
                abort(404)
            stats = core.run_fetch_pass(order, voucher_fetcher)
            store.save(order)
        flash(
            "本次取数：新确认 {confirmed} 项，缺凭证 {missing} 项，"
            "仍待取回 {failed} 项，原版已撤下 {withdrawn} 项。".format(**stats)
        )
        return redirect(url_for("order_page", order_id=order_id))

    @app.get("/orders/<order_id>.json")
    def order_json(order_id: str):
        order = store.get(order_id)
        if order is None:
            abort(404)
        return jsonify({**order, "summary": core.summarize(order)})

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
