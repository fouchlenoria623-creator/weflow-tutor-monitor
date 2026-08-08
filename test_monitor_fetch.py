import unittest
from unittest import mock

import monitor


def group(latest=200):
    return {
        "username": "demo-group@chatroom",
        "displayName": "合成家教群",
        "type": 2,
        "lastTimestamp": latest,
    }


class IncrementalFetchTests(unittest.TestCase):
    def setUp(self):
        self.config = {"first_run_lookback_hours": 24}

    def test_empty_message_response_does_not_advance_cursor(self):
        state = {"last_seen": {"demo-group@chatroom": 100}, "processed_messages": []}

        with mock.patch.object(monitor, "request_json", return_value={"messages": []}):
            rows, changed = monitor.fetch_changed_messages([group()], state, self.config)

        self.assertEqual(rows, [])
        self.assertEqual(changed, 1)
        self.assertEqual(state["last_seen"]["demo-group@chatroom"], 100)

    def test_content_compatibility_fields_are_supported(self):
        self.assertEqual("合成显示正文", monitor.content_of({"displayContent": "合成显示正文"}))
        self.assertEqual("合成文本正文", monitor.content_of({"text": "合成文本正文"}))

    def test_cursor_advances_only_to_observed_message_time(self):
        state = {"last_seen": {"demo-group@chatroom": 100}, "processed_messages": []}
        payload = {
            "messages": [
                {"serverId": "demo-1", "createTime": 120, "content": "合成消息一"},
                {"serverId": "demo-2", "createTime": 150, "content": "合成消息二"},
            ]
        }

        with mock.patch.object(monitor, "request_json", return_value=payload):
            rows, _ = monitor.fetch_changed_messages([group(latest=200)], state, self.config)

        self.assertEqual(len(rows), 2)
        self.assertEqual(state["last_seen"]["demo-group@chatroom"], 150)

    def test_limit_saturation_fails_without_advancing_cursor(self):
        state = {"last_seen": {"demo-group@chatroom": 100}, "processed_messages": []}
        payload = {"messages": [{} for _ in range(10_000)]}

        with mock.patch.object(monitor, "request_json", return_value=payload):
            with self.assertRaises(RuntimeError):
                monitor.fetch_changed_messages([group()], state, self.config)

        self.assertEqual(state["last_seen"]["demo-group@chatroom"], 100)


if __name__ == "__main__":
    unittest.main()
