import unittest

import monitor


class AthleteProfileScoringTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "athlete_profile": {
                "level": "示例运动员等级",
                "general_bonus": 54,
                "nearby_bonus": 8,
                "nearby_km": 5,
                "general_subjects": ["体育", "体能", "体测", "跳绳"],
                "specific_subjects": ["游泳", "轮滑", "足球", "篮球"],
            }
        }

    def test_general_nearby_fitness_order_is_promoted(self):
        order = {
            "subject": "体能",
            "raw": "主要是体能和跳绳，要求体育生",
            "score": 17,
            "tier": "不优先",
            "hard_reasons": [],
            "notes": [],
            "route": {"one_km": 0.2},
        }

        monitor.apply_user_background_fit(order, self.config)

        self.assertEqual(79, order["score"])
        self.assertEqual("优先投", order["tier"])
        self.assertIn("示例运动员等级背景匹配通用体能", order["notes"])
        self.assertIn("投递时说明示例运动员等级资质", order["notes"])

    def test_specific_sport_is_not_promoted_without_project_match(self):
        order = {
            "subject": "轮滑/体能",
            "raw": "轮滑训练",
            "score": 30,
            "tier": "不优先",
            "hard_reasons": [],
            "notes": [],
            "route": {"one_km": 2},
        }

        monitor.apply_user_background_fit(order, self.config)

        self.assertEqual(30, order["score"])
        self.assertEqual("不优先", order["tier"])
        self.assertEqual([], order["notes"])

    def test_hard_excluded_order_is_never_promoted(self):
        order = {
            "subject": "体育体测",
            "raw": "要求女老师",
            "score": -9999,
            "tier": "硬排除",
            "hard_reasons": ["明确女老师要求"],
            "notes": [],
            "route": {"one_km": 1},
        }

        monitor.apply_user_background_fit(order, self.config)

        self.assertEqual(-9999, order["score"])
        self.assertEqual("硬排除", order["tier"])


if __name__ == "__main__":
    unittest.main()
