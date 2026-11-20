"""抽查单核心逻辑自测（不依赖网络与 Flask，用内存假取数函数模拟站点）。

覆盖：
  1. 上传 CSV 解析：表头别名、缺列、非法序号/版号、空文件；
  2. 建单前校验：同一站须同一版、未知站点、查不到月报、序号须在 1..申报次数、
     站点不可达无法校验、申报次数按组合缓存只查一次；
  3. 取数状态机：确认/缺凭证/撤版/失败保持待取、续取只碰待取项；
  4. 已确认项保留原结果：迟到的失败（或成功）响应都不能覆盖终态；
  5. 持久化：订单落盘，重建 OrderStore（模拟重启）后仍在，可继续续取。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

import core  # noqa: E402

STATIONS = {"a": {"label": "维修站A"}, "b": {"label": "维修站B"}}

# 假站点申报数据：{(station, version, combo): declared}
DECLARED = {
    ("a", "v1", "A01"): 3,
    ("a", "v1", "A02"): 2,
    ("a", "v2", "A01"): 4,
    ("b", "v1", "B01"): 2,
}


def make_declared_fetcher(calls=None, unavailable=()):
    def fetch(station, month, version, combo):
        if calls is not None:
            calls.append((station, version, combo))
        if station in unavailable:
            raise core.StationUnavailable("连接超时")
        key = (station, version, combo)
        if key not in DECLARED:
            raise core.DeclaredNotFound(f"{month}/{version}/{combo}")
        return DECLARED[key]
    return fetch


def make_order(items=None):
    return {
        "id": "t1",
        "month": "2026-01",
        "created_at": "2026-11-20T00:00:00+00:00",
        "items": items if items is not None else [
            {"n": 1, "station": "a", "station_label": "维修站A", "version": "v1",
             "combo_id": "A01", "seq": 1, "declared": 3,
             "status": core.PENDING, "work_order": None, "note": "", "updated_at": ""},
        ],
    }


class ParseUploadCsvTest(unittest.TestCase):
    def test_valid_canonical_headers(self):
        rows = core.parse_upload_csv("station,version,combo_id,seq\na,v1,A01,2\n")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["station_raw"], "a")
        self.assertEqual(rows[0]["version"], "v1")
        self.assertEqual(rows[0]["combo_id"], "A01")
        self.assertEqual(rows[0]["seq"], 2)

    def test_chinese_header_aliases(self):
        rows = core.parse_upload_csv("站点,月报版号,组合编号,凭证序号\n维修站A,v1,A01,1\n")
        self.assertEqual(rows[0]["station_raw"], "维修站A")

    def test_missing_column_rejected(self):
        with self.assertRaises(core.UploadError) as ctx:
            core.parse_upload_csv("station,combo_id,seq\na,A01,1\n")
        self.assertIn("version", str(ctx.exception))

    def test_bad_seq_rejected(self):
        for bad in ("0", "-1", "abc", ""):
            with self.assertRaises(core.UploadError, msg=f"seq={bad!r}"):
                core.parse_upload_csv(f"station,version,combo_id,seq\na,v1,A01,{bad}\n")

    def test_empty_fields_rejected(self):
        with self.assertRaises(core.UploadError):
            core.parse_upload_csv("station,version,combo_id,seq\n,v1,A01,1\n")
        with self.assertRaises(core.UploadError):
            core.parse_upload_csv("station,version,combo_id,seq\na,,A01,1\n")
        with self.assertRaises(core.UploadError):
            core.parse_upload_csv("station,version,combo_id,seq\na,v1,,1\n")

    def test_illegal_version_rejected(self):
        with self.assertRaises(core.UploadError):
            core.parse_upload_csv("station,version,combo_id,seq\na,../../etc,A01,1\n")

    def test_empty_csv_rejected(self):
        with self.assertRaises(core.UploadError):
            core.parse_upload_csv("")
        with self.assertRaises(core.UploadError):
            core.parse_upload_csv("station,version,combo_id,seq\n")


class ValidateTest(unittest.TestCase):
    def _rows(self, text):
        return core.parse_upload_csv(text)

    def test_valid_builds_pending_items_and_caches_declared(self):
        calls = []
        rows = self._rows(
            "station,version,combo_id,seq\n"
            "a,v1,A01,1\n"
            "a,v1,A01,3\n"   # 同组合第二行应命中缓存
            "维修站B,v1,B01,2\n"  # 站名也可识别
        )
        items = core.validate_and_build_items(rows, "2026-01", STATIONS,
                                              make_declared_fetcher(calls))
        self.assertEqual(len(items), 3)
        self.assertEqual([i["n"] for i in items], [1, 2, 3])
        self.assertTrue(all(i["status"] == core.PENDING for i in items))
        self.assertEqual(items[0]["declared"], 3)
        self.assertEqual(items[2]["station"], "b")
        # A01 只点查一次（缓存），B01 一次
        self.assertEqual(sorted(calls), [("a", "v1", "A01"), ("b", "v1", "B01")])

    def test_same_station_must_use_same_version(self):
        rows = self._rows(
            "station,version,combo_id,seq\n"
            "a,v1,A01,1\n"
            "a,v2,A01,1\n"
        )
        with self.assertRaises(core.UploadError) as ctx:
            core.validate_and_build_items(rows, "2026-01", STATIONS, make_declared_fetcher())
        self.assertIn("同一站须用同一版", str(ctx.exception))

    def test_different_stations_may_use_different_versions(self):
        rows = self._rows(
            "station,version,combo_id,seq\n"
            "a,v1,A01,1\n"
            "b,v1,B01,1\n"
        )
        items = core.validate_and_build_items(rows, "2026-01", STATIONS,
                                              make_declared_fetcher())
        self.assertEqual(len(items), 2)

    def test_unknown_station_rejected(self):
        rows = self._rows("station,version,combo_id,seq\nc,v1,A01,1\n")
        with self.assertRaises(core.UploadError) as ctx:
            core.validate_and_build_items(rows, "2026-01", STATIONS, make_declared_fetcher())
        self.assertIn("未知站点", str(ctx.exception))

    def test_missing_report_rejects_whole_order(self):
        # 该站没有 v9 版月报：查不到指定月报，整单退回
        rows = self._rows("station,version,combo_id,seq\na,v9,A01,1\n")
        with self.assertRaises(core.UploadError) as ctx:
            core.validate_and_build_items(rows, "2026-01", STATIONS, make_declared_fetcher())
        self.assertIn("查不到", str(ctx.exception))

    def test_seq_out_of_declared_range_rejected(self):
        # A01 申报 3 次：序号 4 越界
        rows = self._rows("station,version,combo_id,seq\na,v1,A01,4\n")
        with self.assertRaises(core.UploadError) as ctx:
            core.validate_and_build_items(rows, "2026-01", STATIONS, make_declared_fetcher())
        self.assertIn("不在 1 至 3 内", str(ctx.exception))

    def test_seq_equal_declared_accepted(self):
        rows = self._rows("station,version,combo_id,seq\na,v1,A01,3\n")
        items = core.validate_and_build_items(rows, "2026-01", STATIONS,
                                              make_declared_fetcher())
        self.assertEqual(items[0]["seq"], 3)

    def test_station_unavailable_rejects(self):
        rows = self._rows("station,version,combo_id,seq\na,v1,A01,1\n")
        with self.assertRaises(core.UploadError) as ctx:
            core.validate_and_build_items(
                rows, "2026-01", STATIONS, make_declared_fetcher(unavailable={"a"}))
        self.assertIn("不可达", str(ctx.exception))


class FetchPassTest(unittest.TestCase):
    def _fetcher(self, mapping, calls=None):
        def fetch(station, month, version, combo, seq):
            if calls is not None:
                calls.append((station, combo, seq))
            return mapping[(station, combo, seq)]
        return fetch

    def test_confirmed_missing_withdrawn(self):
        order = make_order([
            {"n": 1, "station": "a", "station_label": "维修站A", "version": "v1",
             "combo_id": "A01", "seq": 1, "declared": 3,
             "status": core.PENDING, "work_order": None, "note": "", "updated_at": ""},
            {"n": 2, "station": "a", "station_label": "维修站A", "version": "v1",
             "combo_id": "A02", "seq": 2, "declared": 2,
             "status": core.PENDING, "work_order": None, "note": "", "updated_at": ""},
            {"n": 3, "station": "a", "station_label": "维修站A", "version": "v1",
             "combo_id": "A03", "seq": 1, "declared": 1,
             "status": core.PENDING, "work_order": None, "note": "", "updated_at": ""},
        ])
        mapping = {
            ("a", "A01", 1): core.FetchResult("confirmed", work_order="WO-1"),
            ("a", "A02", 2): core.FetchResult("missing"),
            ("a", "A03", 1): core.FetchResult("withdrawn", detail="原版已撤下"),
        }
        stats = core.run_fetch_pass(order, self._fetcher(mapping))
        self.assertEqual((stats["confirmed"], stats["missing"], stats["withdrawn"]), (1, 1, 1))
        self.assertEqual(order["items"][0]["status"], core.CONFIRMED)
        self.assertEqual(order["items"][0]["work_order"], "WO-1")
        self.assertEqual(order["items"][1]["status"], core.MISSING)
        self.assertIsNone(order["items"][1]["work_order"])
        self.assertEqual(order["items"][2]["status"], core.WITHDRAWN)

    def test_failed_stays_pending_and_resume_only_fetches_pending(self):
        order = make_order([
            {"n": 1, "station": "a", "station_label": "维修站A", "version": "v1",
             "combo_id": "A01", "seq": 1, "declared": 3,
             "status": core.PENDING, "work_order": None, "note": "", "updated_at": ""},
            {"n": 2, "station": "a", "station_label": "维修站A", "version": "v1",
             "combo_id": "A01", "seq": 2, "declared": 3,
             "status": core.PENDING, "work_order": None, "note": "", "updated_at": ""},
        ])
        # 第一次：一项确认，一项超时
        mapping1 = {
            ("a", "A01", 1): core.FetchResult("confirmed", work_order="WO-1"),
            ("a", "A01", 2): core.FetchResult("failed", detail="站点超时"),
        }
        stats = core.run_fetch_pass(order, self._fetcher(mapping1))
        self.assertEqual((stats["confirmed"], stats["failed"]), (1, 1))
        self.assertEqual(order["items"][1]["status"], core.PENDING)
        self.assertIn("超时", order["items"][1]["note"])

        # 站点恢复后续取：只应对仍待取的项发起点查
        calls = []
        mapping2 = {("a", "A01", 2): core.FetchResult("confirmed", work_order="WO-2")}
        stats = core.run_fetch_pass(order, self._fetcher(mapping2, calls))
        self.assertEqual(calls, [("a", "A01", 2)])  # 已确认项不再取
        self.assertEqual(stats["confirmed"], 1)
        self.assertEqual(order["items"][1]["work_order"], "WO-2")

    def test_fetcher_exception_treated_as_failed(self):
        order = make_order()

        def boom(*args):
            raise RuntimeError("连接被重置")

        stats = core.run_fetch_pass(order, boom)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(order["items"][0]["status"], core.PENDING)


class LateResponseTest(unittest.TestCase):
    """已确认项保留原结果：迟到的响应（无论成败）一律不覆盖终态。"""

    def test_late_failure_does_not_overwrite_confirmed(self):
        item = {"status": core.CONFIRMED, "work_order": "WO-1", "note": "", "updated_at": "t0"}
        changed = core.apply_result(item, core.FetchResult("failed", detail="站点超时"), now="t1")
        self.assertFalse(changed)
        self.assertEqual(item["status"], core.CONFIRMED)
        self.assertEqual(item["work_order"], "WO-1")
        self.assertEqual(item["updated_at"], "t0")

    def test_late_success_does_not_overwrite_confirmed(self):
        item = {"status": core.CONFIRMED, "work_order": "WO-1", "note": "", "updated_at": "t0"}
        changed = core.apply_result(item, core.FetchResult("confirmed", work_order="WO-X"), now="t1")
        self.assertFalse(changed)
        self.assertEqual(item["work_order"], "WO-1")

    def test_late_response_does_not_overwrite_missing_or_withdrawn(self):
        for status in (core.MISSING, core.WITHDRAWN):
            item = {"status": status, "work_order": None, "note": "n", "updated_at": "t0"}
            changed = core.apply_result(item, core.FetchResult("confirmed", work_order="WO-1"))
            self.assertFalse(changed)
            self.assertEqual(item["status"], status)

    def test_pending_accepts_result(self):
        item = {"status": core.PENDING, "work_order": None, "note": "", "updated_at": ""}
        changed = core.apply_result(item, core.FetchResult("confirmed", work_order="WO-9"), now="t1")
        self.assertTrue(changed)
        self.assertEqual(item["status"], core.CONFIRMED)
        self.assertEqual(item["work_order"], "WO-9")


class OrderStoreTest(unittest.TestCase):
    def test_persist_and_reload_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.json"
            store = core.OrderStore(path)
            order = store.create("2026-01", make_order()["items"])
            # 取数确认一项后落盘
            core.run_fetch_pass(order, lambda *a: core.FetchResult("confirmed", work_order="WO-1"))
            store.save(order)

            # 模拟重启：新建 Store 实例，订单与状态必须还在
            store2 = core.OrderStore(path)
            loaded = store2.get(order["id"])
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["items"][0]["status"], core.CONFIRMED)
            self.assertEqual(loaded["items"][0]["work_order"], "WO-1")
            self.assertEqual(core.summarize(loaded)["confirmed"], 1)

    def test_pending_survives_restart_and_can_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.json"
            store = core.OrderStore(path)
            order = store.create("2026-01", make_order()["items"])
            # 站点超时：保持待取
            core.run_fetch_pass(order, lambda *a: core.FetchResult("failed", detail="超时"))
            store.save(order)

            store2 = core.OrderStore(path)  # 重启
            loaded = store2.get(order["id"])
            self.assertEqual(loaded["items"][0]["status"], core.PENDING)
            # 站点恢复后续取
            stats = core.run_fetch_pass(loaded, lambda *a: core.FetchResult("confirmed", work_order="WO-7"))
            store2.save(loaded)
            self.assertEqual(stats["confirmed"], 1)
            self.assertEqual(store2.get(order["id"])["items"][0]["work_order"], "WO-7")

    def test_corrupt_file_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.json"
            path.write_text("not-json", encoding="utf-8")
            store = core.OrderStore(path)
            self.assertEqual(store.list_orders(), [])
            self.assertTrue(list(Path(tmp).glob("orders.json.corrupt-*")))

    def test_list_orders_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = core.OrderStore(Path(tmp) / "orders.json")
            first = store.create("2026-01", make_order()["items"])
            second = store.create("2026-02", make_order()["items"])
            self.assertEqual([o["id"] for o in store.list_orders()], [second["id"], first["id"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
