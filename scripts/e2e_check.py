#!/usr/bin/env python3
"""端到端自测：抽查建单、取数、缺凭证、整单退回、超时续取、重启续取、撤版提示。

脚本自己启动两个站点服务和一个中心网页（临时目录 + 空闲端口，互不影响），
跑完自动清理。用法：

  python3 scripts/e2e_check.py

覆盖：
  1. 站点点查接口：申报次数（缺号计 0）、凭证（无记录 found=false）、撤版 404；
  2. 建单校验：合法单通过；序号越界 / 同站两版 / 查不到月报 / 未知站点 → 整单退回；
  3. 取数：逐项列出工单号，原版无记录标缺凭证；
  4. 站点超时（挂起不响应）的项保持待取回，站点恢复后可在原单上续取；
  5. 中心网页重启后订单仍在，可继续续取；
  6. 原版撤下后项标“原版已撤下”，页面提示重新发起抽查。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MONTH = "2026-01"

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        failures.append(name)


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_ready(url: str, proc: subprocess.Popen, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_request(url: str, data: bytes | None = None, headers: dict | None = None):
    """返回 (status, body, final_url)；4xx/5xx 不抛异常。"""
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8"), resp.geturl()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), url


def post_multipart(url: str, fields: dict, file_field: str, filename: str, content: bytes):
    boundary = "----e2e" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{filename}\"\r\nContent-Type: text/csv\r\n\r\n".encode()
        + content + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    return http_request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})


class HangServer:
    """假站点：接受连接但永不响应，用于模拟站点超时。"""

    def __init__(self, port: int):
        self.port = port
        self._stop = threading.Event()
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        held: list[socket.socket] = []
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
                held.append(conn)  # 握住连接不响应，让客户端读超时
            except socket.timeout:
                continue
            except OSError:
                break
        for conn in held:
            try:
                conn.close()
            except OSError:
                pass

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=3)


class Proc:
    """被测进程句柄，支持杀掉后在同端口重启。"""

    def __init__(self, name: str, script: Path, env: dict):
        self.name = name
        self.script = script
        self.env = env
        self.p: subprocess.Popen | None = None

    def start(self):
        self.p = subprocess.Popen(
            [sys.executable, str(self.script)],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return self

    def stop(self):
        if self.p and self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(timeout=5)

    def restart(self):
        self.stop()
        time.sleep(0.3)
        self.start()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="spotcheck-e2e-"))
    procs: list[Proc] = []
    try:
        # ---- 准备：两个站点数据目录（拷贝仓库演示数据）+ web 数据目录 ----
        data_a = tmp / "station_a"
        data_b = tmp / "station_b"
        shutil.copytree(REPO / "station" / "data" / "station_a", data_a)
        shutil.copytree(REPO / "station" / "data" / "station_b", data_b)
        web_data = tmp / "web"
        web_data.mkdir()

        port_a, port_b, port_web = free_port(), free_port(), free_port()
        url_a = f"http://127.0.0.1:{port_a}"
        url_b = f"http://127.0.0.1:{port_b}"
        url_web = f"http://127.0.0.1:{port_web}"

        def station_env(name: str, data_dir: Path, port: int) -> dict:
            env = dict(os.environ)
            env.update({
                "STATION_NAME": name,
                "STATION_DATA_DIR": str(data_dir),
                "PORT": str(port),
            })
            return env

        def web_env(port: int) -> dict:
            env = dict(os.environ)
            env.update({
                "AVAILABLE_MONTHS": MONTH,
                "STATION_A_URL": url_a,
                "STATION_B_URL": url_b,
                "STATION_A_LABEL": "维修站A",
                "STATION_B_LABEL": "维修站B",
                "HTTP_TIMEOUT": "1",  # 自测里把超时调小，跑得快
                "WEB_DATA_DIR": str(web_data),
                "PORT": str(port),
            })
            return env

        station_a = Proc("station-a", REPO / "station" / "app.py",
                         station_env("维修站A", data_a, port_a)).start()
        station_b = Proc("station-b", REPO / "station" / "app.py",
                         station_env("维修站B", data_b, port_b)).start()
        web = Proc("web", REPO / "web" / "app.py", web_env(port_web)).start()
        procs += [station_a, station_b, web]

        for name, url, proc in (("station-a", url_a, station_a.p),
                                ("station-b", url_b, station_b.p),
                                ("web", url_web, web.p)):
            if not wait_ready(f"{url}/health", proc):
                print(f"!! {name} 未能启动")
                return 1

        # ---- 1. 站点点查接口 ----
        d = get_json(f"{url_a}/api/declared?month={MONTH}&version=v1&combo=A01")
        check("站点 declared 命中", d["declared"] == 3, str(d))
        d = get_json(f"{url_a}/api/declared?month={MONTH}&version=v1&combo=NOPE")
        check("站点 declared 缺号计零", d["declared"] == 0, str(d))
        v = get_json(f"{url_a}/api/voucher?month={MONTH}&version=v1&combo=A01&seq=2")
        check("站点 voucher 命中", v["found"] is True and v["work_order"] == "WO-A-2601-002", str(v))
        v = get_json(f"{url_a}/api/voucher?month={MONTH}&version=v1&combo=A02&seq=2")
        check("站点 voucher 无记录 found=false", v["found"] is False, str(v))
        status, body, _ = http_request(f"{url_a}/api/voucher?month={MONTH}&version=v9&combo=A01&seq=1")
        check("站点 已撤版返回 404", status == 404, f"status={status}")

        # ---- 2. 合法建单 + 取数 ----
        csv_ok = (
            "station,version,combo_id,seq\n"
            "a,v1,A01,1\n"
            "a,v1,A02,2\n"   # v1 版 A02 缺第 2 号凭证 → 缺凭证
            "b,v1,B01,2\n"
        ).encode()
        status, _, final_url = post_multipart(
            f"{url_web}/orders", {"month": MONTH}, "file", "spotcheck.csv", csv_ok)
        m = re.search(r"/orders/([0-9a-f]+)$", final_url)
        check("建单成功并跳转订单页", status == 200 and m is not None,
              f"status={status} url={final_url}")
        order1 = m.group(1) if m else ""

        status, _, _ = http_request(f"{url_web}/orders/{order1}/fetch", data=b"")
        order = get_json(f"{url_web}/orders/{order1}.json")
        by_n = {item["n"]: item for item in order["items"]}
        check("取数 A01#1 工单号",
              by_n[1]["status"] == "confirmed" and by_n[1]["work_order"] == "WO-A-2601-001",
              str(by_n[1]))
        check("取数 A02#2 缺凭证",
              by_n[2]["status"] == "missing" and by_n[2]["work_order"] is None, str(by_n[2]))
        check("取数 B01#2 工单号",
              by_n[3]["status"] == "confirmed" and by_n[3]["work_order"] == "WO-B-2601-002",
              str(by_n[3]))
        _, page, _ = http_request(f"{url_web}/orders/{order1}")
        check("订单页逐项列出工单号与缺凭证",
              "WO-A-2601-001" in page and "缺凭证" in page and "WO-B-2601-002" in page)

        # ---- 3. 整单退回 ----
        def order_count() -> int:
            _, html, _ = http_request(f"{url_web}/")
            return len(set(re.findall(r'/orders/([0-9a-f]+)', html)))

        before = order_count()
        cases = [
            ("序号越界", "station,version,combo_id,seq\na,v1,A03,2\n", "不在 1 至 1 内"),
            ("同站两版", "station,version,combo_id,seq\na,v1,A01,1\na,v2,A01,2\n", "同一站须用同一版"),
            ("查不到月报", "station,version,combo_id,seq\na,v9,A01,1\n", "查不到"),
            ("未知站点", "station,version,combo_id,seq\nc,v1,A01,1\n", "未知站点"),
        ]
        for name, csv_text, hint in cases:
            status, body, _ = post_multipart(
                f"{url_web}/orders", {"month": MONTH}, "file", "bad.csv", csv_text.encode())
            check(f"整单退回：{name}", status == 400 and hint in body,
                  f"status={status} 提示含{hint!r}={hint in body}")
        check("退回后未新增订单", order_count() == before,
              f"{before} -> {order_count()}")

        # ---- 4. 站点超时 → 原单续取 ----
        csv_b = "station,version,combo_id,seq\nb,v1,B01,1\nb,v1,B02,1\n".encode()
        _, _, final_url = post_multipart(
            f"{url_web}/orders", {"month": MONTH}, "file", "b.csv", csv_b)
        order2 = re.search(r"/orders/([0-9a-f]+)$", final_url).group(1)

        station_b.stop()
        with HangServer(port_b):  # 挂起：连接得上但永不响应 → 客户端超时
            http_request(f"{url_web}/orders/{order2}/fetch", data=b"")
            order = get_json(f"{url_web}/orders/{order2}.json")
            pending = [i for i in order["items"] if i["status"] == "pending"]
            check("站点超时的项保持待取回", len(pending) == 2,
                  f"pending={len(pending)} note={pending[0]['note'] if pending else ''}")

        station_b.restart()
        if not wait_ready(f"{url_b}/health", station_b.p):
            print("!! station-b 重启失败")
            return 1
        http_request(f"{url_web}/orders/{order2}/fetch", data=b"")
        order = get_json(f"{url_web}/orders/{order2}.json")
        works = [i["work_order"] for i in order["items"]]
        check("站点恢复后原单续取成功",
              order["summary"]["confirmed"] == 2
              and works == ["WO-B-2601-001", "WO-B-2601-003"], str(works))

        # ---- 5. 中心重启后仍可续取 ----
        csv_c = "station,version,combo_id,seq\na,v1,A01,3\n".encode()
        _, _, final_url = post_multipart(
            f"{url_web}/orders", {"month": MONTH}, "file", "c.csv", csv_c)
        order3 = re.search(r"/orders/([0-9a-f]+)$", final_url).group(1)
        web.restart()
        if not wait_ready(f"{url_web}/health", web.p):
            print("!! web 重启失败")
            return 1
        order = get_json(f"{url_web}/orders/{order3}.json")
        check("重启后订单与待取状态仍在",
              order["items"][0]["status"] == "pending", str(order["items"][0]["status"]))
        order1_after = get_json(f"{url_web}/orders/{order1}.json")
        check("重启后已确认项保留原结果",
              order1_after["items"][0]["work_order"] == "WO-A-2601-001")
        http_request(f"{url_web}/orders/{order3}/fetch", data=b"")
        order = get_json(f"{url_web}/orders/{order3}.json")
        check("重启后续取成功",
              order["items"][0]["status"] == "confirmed"
              and order["items"][0]["work_order"] == "WO-A-2601-003",
              str(order["items"][0]))

        # ---- 6. 原版撤下 → 提示重新发起抽查 ----
        csv_v2 = "station,version,combo_id,seq\na,v2,A01,4\na,v2,A02,2\n".encode()
        _, _, final_url = post_multipart(
            f"{url_web}/orders", {"month": MONTH}, "file", "v2.csv", csv_v2)
        order4 = re.search(r"/orders/([0-9a-f]+)$", final_url).group(1)
        # 站点撤下 v2 版（删掉该版配对文件）
        for f in data_a.glob(f"{MONTH}/v2.*.csv"):
            f.unlink()
        http_request(f"{url_web}/orders/{order4}/fetch", data=b"")
        order = get_json(f"{url_web}/orders/{order4}.json")
        check("撤版后项标原版已撤下",
              all(i["status"] == "withdrawn" for i in order["items"]),
              str([i["status"] for i in order["items"]]))
        _, page, _ = http_request(f"{url_web}/orders/{order4}")
        check("页面提示重新发起抽查", "重新发起抽查" in page)
        # 已确认的老单不受撤版影响
        order1_final = get_json(f"{url_web}/orders/{order1}.json")
        check("撤版不影响其他单已确认项",
              order1_final["items"][0]["work_order"] == "WO-A-2601-001")

        # ---- 7. 首页订单列表 ----
        _, html, _ = http_request(f"{url_web}/")
        check("首页列出全部抽查单", order_count() == 4, f"orders={order_count()}")

        print()
        if failures:
            print(f"共 {len(failures)} 项失败: {failures}")
            return 1
        print("全部端到端检查通过。")
        return 0
    finally:
        for proc in procs:
            proc.stop()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
