import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import monitor
import ranker


def synthetic_auto_order(**overrides):
    order = {
        "id": "AUTO-example",
        "posted_at": "2026-01-01 10:00:00",
        "sender": "示例发送者",
        "group": "示例群甲",
        "source_file": "synthetic",
        "address": "海淀区测试路",
        "grade": "初二",
        "subject": "数学",
        "schedule": "每周二、周六晚上",
        "duration_h": 2.0,
        "frequency": 2,
        "total_lessons": 12,
        "information_fee": 0,
        "hourly": 160.0,
        "pay_raw": "160元/小时",
        "pay_kind": "hourly",
        "hard_reasons": [],
        "notes": [],
        "raw": "要求：耐心负责，帮助整理错题",
        "rough_score": 80,
    }
    order.update(overrides)
    return order


class AutoDedupeTests(unittest.TestCase):
    def test_cross_group_repost_on_same_day_is_merged(self):
        first = synthetic_auto_order()
        repost = synthetic_auto_order(
            id="AUTO-repost",
            group="示例群乙",
            posted_at="2026-01-01 10:30:00",
        )

        result = ranker.dedupe_orders([first, repost])

        self.assertEqual(1, len(result))
        self.assertEqual(["示例群乙", "示例群甲"], result[0]["groups"])

    def test_different_schedule_or_frequency_is_not_merged(self):
        first = synthetic_auto_order()
        different = synthetic_auto_order(
            id="AUTO-different",
            schedule="每周一、周三、周五晚上",
            frequency=3,
        )

        self.assertNotEqual(ranker.auto_dedupe_key(first), ranker.auto_dedupe_key(different))
        self.assertEqual(2, len(ranker.dedupe_orders([first, different])))

    def test_same_shape_on_another_day_is_not_merged(self):
        first = synthetic_auto_order()
        next_day = synthetic_auto_order(id="AUTO-next-day", posted_at="2026-01-02 10:00:00")

        self.assertEqual(2, len(ranker.dedupe_orders([first, next_day])))


class RouteCacheContextTests(unittest.TestCase):
    def setUp(self):
        ranker._route_attempt_results.clear()
        ranker.ORIGIN_COORD = "116.4,39.9"

    def test_cache_key_changes_with_origin_and_provider(self):
        baidu_key = ranker.route_cache_key("测试地址", "baidu")
        alternate_provider_key = ranker.route_cache_key("测试地址", "alternate")
        ranker.ORIGIN_COORD = "116.5,39.95"
        moved_key = ranker.route_cache_key("测试地址", "baidu")

        self.assertNotEqual(baidu_key, alternate_provider_key)
        self.assertNotEqual(baidu_key, moved_key)

    def test_route_from_previous_origin_is_not_reused(self):
        cached_route = ranker.with_route_context(
            {"status": "ok", "one_km": 3.0, "round_km": 6.0},
            "baidu",
        )
        original_key = ranker.route_cache_key("测试地址", "baidu")
        candidate = {
            "address": "测试地址",
            "raw": "",
            "group": "示例群",
            "rough_score": 1,
            "hard_reasons": [],
        }

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "routes.json"
            cache_path.write_text(json.dumps({original_key: cached_route}), encoding="utf-8")
            ranker.ORIGIN_COORD = "116.5,39.95"
            with mock.patch.dict(os.environ, {"BAIDU_MAP_AK": ""}, clear=False):
                routed = ranker.route_orders([candidate], 0, cache_path)

        self.assertEqual(0, routed)
        self.assertNotIn("route", candidate)


class CsvSafetyTests(unittest.TestCase):
    def test_untrusted_cells_are_escaped_before_csv_export(self):
        order = synthetic_auto_order(
            sender="=HYPERLINK(\"https://invalid.example\")",
            groups=[" +SUM(1,1)"],
            address="\t-CMD",
            pay_raw="@SUM(1,1)",
            raw="=1+1",
            hard_reasons=["+SUM(1,1)"],
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.csv"
            ranker.write_csv(path, [order])
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))

        for field in ("sender", "groups", "address", "pay_raw", "raw", "hard_reasons", "reason"):
            self.assertTrue(row[field].startswith("'"), (field, row[field]))
        self.assertEqual("160.0", row["hourly"])


class ProfileDefaultTests(unittest.TestCase):
    def tearDown(self):
        ranker.configure_runtime({"tutor_profile": {"gender": "", "school_tags": [], "school_names": []}})

    def test_male_profile_accepts_male_requirement(self):
        ranker.configure_runtime({"tutor_profile": {"gender": "male", "school_tags": [], "school_names": []}})
        hard, notes = ranker.analyze_constraints("老师要求：仅限男老师，有耐心")

        self.assertNotIn("明确男老师要求", hard)
        self.assertNotIn("需核对男老师要求", notes)

    def test_blank_profile_flags_both_gender_requirements_for_review(self):
        ranker.configure_runtime({"tutor_profile": {"gender": "", "school_tags": [], "school_names": []}})
        female_hard, female_notes = ranker.analyze_constraints("老师要求：仅限女老师，有经验")
        male_hard, male_notes = ranker.analyze_constraints("老师要求：仅限男老师，有经验")

        self.assertNotIn("明确女老师要求", female_hard)
        self.assertIn("需核对女老师要求", female_notes)
        self.assertNotIn("明确男老师要求", male_hard)
        self.assertIn("需核对男老师要求", male_notes)


class DashboardColumnTests(unittest.TestCase):
    def test_show_all_keeps_forced_hidden_columns_hidden(self):
        stats = {
            "groups": 0,
            "messages": 0,
            "new_orders": 0,
            "total_orders": 0,
            "online_only_default": False,
            "priority_only_default": False,
            "report_date": "2026-01-01",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            monitor.dashboard([], set(), stats, output_path=path)
            report = path.read_text(encoding="utf-8")

        self.assertIn(
            "hiddenColumns.clear();forceHideColumns.forEach(name=>hiddenColumns.add(name));",
            report,
        )

    def test_manual_html_is_labelled_private_not_synthetic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manual.html"
            ranker.write_html(path, [])
            report = path.read_text(encoding="utf-8")

        self.assertIn("本地私有报告，请勿直接公开分享", report)
        self.assertNotIn("合成演示", report)


if __name__ == "__main__":
    unittest.main()
