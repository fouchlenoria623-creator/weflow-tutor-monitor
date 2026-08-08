import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import monitor
import ranker


def order(address, rough_score=0, *, online=False, hard=False):
    return {
        "address": "线上" if online else address,
        "raw": "线上授课" if online else "",
        "group": "测试群",
        "rough_score": rough_score,
        "hard_reasons": ["不符合"] if hard else [],
    }


class BaiduRouteTests(unittest.TestCase):
    def setUp(self):
        ranker._route_attempt_results.clear()
        ranker.ORIGIN_NAME = "示例出发地"
        ranker.ORIGIN_COORD = "116,40"

    def test_baidu_get_uses_the_global_request_slot(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"status": 0, "result": {}}'
        slot = mock.MagicMock()

        with mock.patch.object(ranker, "_baidu_request_slot", return_value=slot) as request_slot:
            with mock.patch.object(ranker.urllib.request, "urlopen", return_value=response) as urlopen:
                result = ranker.baidu_get("https://api.map.baidu.com/test", {"ak": "test-ak"})

        request_slot.assert_called_once_with()
        slot.__enter__.assert_called_once_with()
        slot.__exit__.assert_called_once()
        urlopen.assert_called_once()
        self.assertEqual(result["status"], 0)

    def test_baidu_coordinates_and_response_parsing(self):
        responses = [
            {
                "status": 0,
                "result": {
                    "location": {"lng": 116.4, "lat": 39.9},
                    "confidence": 90,
                    "precise": 1,
                },
                "poi_infos": [{"district": "海淀区"}],
            },
            {
                "status": 0,
                "message": "ok",
                "result": {"routes": [{"distance": 12345, "duration": 1800}]},
            },
        ]
        calls = []

        def fake_get(url, params):
            calls.append((url, params))
            return responses.pop(0)

        cache = {}
        with mock.patch.object(ranker, "baidu_get", side_effect=fake_get):
            result = ranker.baidu_route("海淀区测试路10号", "test-ak", cache)

        self.assertEqual(calls[0][0], "https://api.map.baidu.com/geocoding/v3/")
        self.assertEqual(calls[0][1]["ret_coordtype"], "gcj02ll")
        self.assertEqual(calls[1][0], "https://api.map.baidu.com/directionlite/v1/driving")
        self.assertEqual(calls[1][1]["origin"], f"{40:.6f},{116:.6f}")
        self.assertEqual(calls[1][1]["destination"], f"{39.9:.6f},{116.4:.6f}")
        self.assertEqual(calls[1][1]["coord_type"], "gcj02")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["one_km"], 12.3)
        self.assertEqual(result["one_min"], 30)
        self.assertEqual(result["route_provider"], "baidu_directionlite")
        self.assertTrue(result["taxi_estimated"])

    def test_baidu_rejects_geocode_outside_beijing(self):
        response = {
            "status": 0,
            "result": {"location": {"lng": 114.30, "lat": 30.60}, "confidence": 90},
        }
        cache = {}
        with mock.patch.object(ranker, "baidu_get", return_value=response) as request:
            result = ranker.baidu_geocode("武汉市东西湖区", "test-ak", cache)

        self.assertIsNone(result)
        self.assertEqual(request.call_count, 1)
        self.assertIn("outside_beijing", cache["geo_mismatch:武汉市东西湖区"])

    def test_live_baidu_refreshes_legacy_estimate(self):
        old_estimate = {
            "status": "estimated",
            "one_km": 9.0,
            "round_km": 18.0,
            "one_min": 27,
            "round_min": 54,
        }
        fresh_route = {
            "status": "ok",
            "one_km": 8.1,
            "round_km": 16.2,
            "one_min": 22,
            "round_min": 44,
            "route_provider": "baidu_directionlite",
        }
        orders = [order("待刷新地址")]

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "routes.json"
            cache_path.write_text(json.dumps({"route:待刷新地址": old_estimate}, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {"BAIDU_MAP_AK": "test-ak"}, clear=False):
                with mock.patch.object(ranker, "baidu_route", return_value=fresh_route) as live:
                    ranker.route_orders(orders, 1, cache_path)

        live.assert_called_once()
        self.assertEqual(orders[0]["route"]["status"], "ok")

    def test_cache_first_unique_limit_and_same_run_retry_shield(self):
        cached_result = ranker.with_route_context({
            "status": "ok",
            "one_km": 4.2,
            "round_km": 8.4,
            "one_min": 12,
            "round_min": 24,
            "one_taxi": 15,
            "round_taxi": 30,
            "geocode_source": "baidu_geocode",
            "route_provider": "baidu_directionlite",
        }, "baidu")
        live_result = {
            "status": "ok",
            "one_km": 8.0,
            "round_km": 16.0,
            "one_min": 24,
            "round_min": 48,
            "one_taxi": 24,
            "round_taxi": 48,
            "route_provider": "baidu_directionlite",
        }
        orders = [
            order("缓存地址", 1),
            order("缓存地址", 2),
            order("新地址A", 50),
            order("新地址A", 40),
            order("新地址B", 30),
            order("", online=True),
            order("硬排除地址", 100, hard=True),
        ]

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "routes.json"
            cache_path.write_text(
                json.dumps({ranker.route_cache_key("缓存地址", "baidu"): cached_result}, ensure_ascii=False),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"BAIDU_MAP_AK": "test-ak"}, clear=False):
                with mock.patch.object(ranker, "baidu_route", return_value=live_result) as live:
                    routed = ranker.route_orders(orders, 1, cache_path)
                    followup = order("新地址A", 1)
                    ranker.route_orders([followup], 10, cache_path)

        self.assertEqual(live.call_count, 1)
        live.assert_called_once_with("新地址A", "test-ak", mock.ANY)
        self.assertEqual(routed, 5)
        self.assertEqual(orders[0]["route"]["geocode_source"], "baidu_geocode")
        self.assertEqual(orders[1]["route"]["one_km"], 4.2)
        self.assertEqual(orders[2]["route"]["one_km"], 8.0)
        self.assertEqual(orders[3]["route"]["one_km"], 8.0)
        self.assertNotIn("route", orders[4])
        self.assertEqual(orders[5]["route"]["status"], "online")
        self.assertNotIn("route", orders[6])
        self.assertEqual(followup["route"]["one_km"], 8.0)

    def test_cache_and_online_routes_work_without_a_live_key_or_budget(self):
        cached_result = ranker.with_route_context(
            {"status": "ok", "one_km": 3.0, "round_km": 6.0, "route_provider": "baidu_directionlite"},
            "baidu",
        )
        orders = [order("缓存地址"), order("", online=True), order("未缓存地址")]

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "routes.json"
            cache_path.write_text(
                json.dumps({ranker.route_cache_key("缓存地址", "baidu"): cached_result}, ensure_ascii=False),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"BAIDU_MAP_AK": ""}, clear=False):
                routed = ranker.route_orders(orders, 0, cache_path)

        self.assertEqual(routed, 2)
        self.assertEqual(orders[0]["route"]["one_km"], 3.0)
        self.assertEqual(orders[1]["route"]["status"], "online")
        self.assertNotIn("route", orders[2])

    def test_score_orders_only_routes_today(self):
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        orders = [
            {
                "posted_at": f"{today} 10:00:00",
                "raw": "学科：数学\n地址：海淀区测试路甲\n费用：100/小时",
                "group": "测试群",
                "sender": "a",
            },
            {
                "posted_at": f"{yesterday} 10:00:00",
                "raw": "学科：数学\n地址：海淀区样例街乙\n费用：100/小时",
                "group": "测试群",
                "sender": "b",
            },
        ]
        config = {
            "unsupported_subjects": ["化学"],
            "route_limit_per_run": 300,
            "online_priority_bonus": 35,
        }

        with mock.patch.object(ranker, "route_orders", return_value=0) as route_orders:
            monitor.score_orders(orders, config)

        routed_orders = route_orders.call_args.args[0]
        self.assertEqual(len(routed_orders), 1)
        self.assertEqual(route_orders.call_args.args[1], 0)
        self.assertTrue(routed_orders[0]["posted_at"].startswith(today))

if __name__ == "__main__":
    unittest.main()
