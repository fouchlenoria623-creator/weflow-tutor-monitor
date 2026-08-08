import unittest
from unittest import mock

import monitor


class NotificationPrivacyTests(unittest.TestCase):
    def order(self):
        return {
            "id": "DEMO-NOTICE-1",
            "grade": "初二",
            "subject": "数学",
            "address": "海淀区测试路",
            "tier": "优先投",
            "source_message_key": "demo-message-1",
            "delivery_mode": "offline",
        }

    def notify_body(self, include_address):
        config = {
            "notify_tiers": ["优先投"],
            "notify_online_only": False,
            "notification_include_address": include_address,
        }
        with mock.patch.object(monitor.subprocess, "Popen") as popen:
            count = monitor.notify([self.order()], {"demo-message-1"}, config)
        self.assertEqual(count, 1)
        command = popen.call_args.args[0]
        return command[command.index("-Body") + 1]

    def test_notification_hides_address_by_default(self):
        body = self.notify_body(False)
        self.assertNotIn("测试路", body)

    def test_notification_address_requires_explicit_opt_in(self):
        body = self.notify_body(True)
        self.assertIn("海淀区测试路", body)


if __name__ == "__main__":
    unittest.main()
