import unittest

import ranker


class SchoolConstraintTests(unittest.TestCase):
    def setUp(self):
        ranker.configure_runtime({
            "tutor_profile": {
                "gender": "",
                "school_tags": ["双一流"],
                "school_names": ["示例大学"],
            }
        })

    def tearDown(self):
        ranker.configure_runtime({"tutor_profile": {"gender": "", "school_tags": [], "school_names": []}})

    def test_explicit_school_identity_requirement_is_hard_when_profile_does_not_match(self):
        hard, notes = ranker.analyze_constraints("老师要求：身份要求为北师大在校生，师范专业优先")
        self.assertIn("北师大指定/强偏好", hard)
        self.assertNotIn("北师大偏好", notes)

    def test_school_preference_remains_soft(self):
        hard, notes = ranker.analyze_constraints("老师要求：北师大优先，其他学校有经验也可以")
        self.assertNotIn("北师大指定/强偏好", hard)
        self.assertIn("北师大偏好", notes)

    def test_unknown_school_profile_is_flagged_for_review(self):
        ranker.configure_runtime({"tutor_profile": {"gender": "", "school_tags": [], "school_names": []}})
        hard, notes = ranker.analyze_constraints("老师要求：身份要求为北师大在校生")
        self.assertNotIn("北师大指定/强偏好", hard)
        self.assertIn("需核对北师大指定要求", notes)

    def test_repeated_synthetic_locations_mark_a_merged_order(self):
        hard, _ = ranker.analyze_constraints(
            "【上课区域】海淀区测试路甲\n【预算范围】177-233/小时\n"
            "【上课区域】朝阳区样例街乙\n【预算范围】4321/月"
        )
        self.assertIn("多单合并需拆分", hard)

    def test_repeated_synthetic_order_ids_mark_a_merged_order(self):
        hard, _ = ranker.analyze_constraints(
            "DEMO400001 家教\n教学地址：海淀区测试路甲\n辅导科目：数学\n"
            "DEMO400002 家教\n教学地址：丰台区样例街乙\n辅导科目：语文和数学"
        )
        self.assertIn("多单合并需拆分", hard)


if __name__ == "__main__":
    unittest.main()
