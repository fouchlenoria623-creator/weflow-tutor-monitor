import json
import tempfile
import unittest
from pathlib import Path

import monitor


class ConfigTests(unittest.TestCase):
    def write_config(self, directory, data):
        path = Path(directory) / "config.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_valid_local_config_is_merged_and_applied(self):
        data = {
            "origin_name": "示例出发地",
            "origin_coord": "116.4074,39.9042",
            "include_name_patterns": ["家教"],
            "subject_weights": {"数学": 50},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            config = monitor.load_config(path)
        self.assertEqual(60, config["scan_interval_minutes"])
        self.assertEqual("示例出发地", config["origin_name"])
        self.assertFalse(config["map_routing_enabled"])
        self.assertEqual("baidu", config["map_provider"])
        self.assertFalse(config["notification_include_address"])

    def test_example_explicitly_recommends_amap_but_keeps_routing_disabled(self):
        example = json.loads((Path(__file__).resolve().parent / "config.example.json").read_text(encoding="utf-8"))

        self.assertEqual("amap", example["map_provider"])
        self.assertFalse(example["map_routing_enabled"])

    def test_invalid_map_provider_is_rejected(self):
        data = {
            "map_provider": "other",
            "map_routing_enabled": False,
            "include_name_patterns": ["家教"],
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "map_provider"):
                monitor.load_config(self.write_config(directory, data))

    def test_invalid_coordinate_is_rejected(self):
        data = {"origin_coord": "请填写", "include_name_patterns": ["家教"]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "origin_coord"):
                monitor.load_config(path)

    def test_origin_is_optional_when_map_routing_is_disabled(self):
        data = {
            "map_routing_enabled": False,
            "origin_coord": "",
            "include_name_patterns": ["家教"],
        }
        with tempfile.TemporaryDirectory() as directory:
            config = monitor.load_config(self.write_config(directory, data))
        self.assertFalse(config["map_routing_enabled"])

    def test_origin_is_required_when_map_routing_is_enabled(self):
        data = {
            "map_routing_enabled": True,
            "origin_coord": "",
            "include_name_patterns": ["家教"],
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "origin_coord"):
                monitor.load_config(self.write_config(directory, data))

    def test_weflow_api_must_be_loopback(self):
        with self.assertRaisesRegex(ValueError, "非本机"):
            monitor._require_loopback("http://192.0.2.10:5031")
        self.assertEqual("http://127.0.0.1:5031", monitor._require_loopback("http://127.0.0.1:5031/"))


if __name__ == "__main__":
    unittest.main()
