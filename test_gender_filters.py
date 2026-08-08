import unittest

import ranker


class TutorGenderFilterTests(unittest.TestCase):
    def setUp(self):
        ranker.configure_runtime({"tutor_profile": {"gender": "male", "school_tags": [], "school_names": []}})

    def tearDown(self):
        ranker.configure_runtime({"tutor_profile": {"gender": "", "school_tags": [], "school_names": []}})

    def assert_female_hard(self, text):
        hard, _ = ranker.analyze_constraints(text)
        self.assertIn("明确女老师要求", hard)

    def assert_female_not_hard(self, text):
        hard, _ = ranker.analyze_constraints(text)
        self.assertNotIn("明确女老师要求", hard)

    def test_student_gender_is_not_tutor_gender(self):
        for text in (
            "年级：初二女孩\n科目：数学\n老师要求：大学生，有耐心",
            "学生性别：女\n老师要求：认真负责",
            "其他要求：一对二，两个女生一起上课",
            "要求：帮助一对双胞胎女孩巩固基础",
        ):
            self.assert_female_not_hard(text)

    def test_synthetic_female_tutor_requirements_are_hard_for_male_profile(self):
        for text in (
            "DEMO课程A\n老师要求：女在读大学生，讲解清晰",
            "教员要求：女在读研究生，耐心负责",
            "师资要求：限女性，有辅导经验",
            "老师要求：女专职老师，熟悉教材",
            "家教需求：需要一名女大学生老师",
            "要求：年轻的女外教，发音标准",
        ):
            self.assert_female_hard(text)

    def test_explicit_synthetic_gender_labels_are_hard(self):
        for text in (
            "老师性别：女\n科目：数学",
            "要求：只要女生，有教学经验",
            "性别要求：女性\n科目：物理",
            "老师要求：英语专业，要女同学",
        ):
            self.assert_female_hard(text)

    def test_female_preference_remains_soft(self):
        for text in (
            "老师要求：女老师优先，有经验即可",
            "对老师要求：最好是女大学生",
            "要求：优先女研究生，男女均可",
        ):
            hard, notes = ranker.analyze_constraints(text)
            self.assertNotIn("明确女老师要求", hard)
            self.assertIn("女老师偏好", notes)

    def test_gender_unrestricted_wording_is_not_hard(self):
        for text in (
            "教员要求：有经验的男女大学生",
            "老师性别：男女不限",
            "老师要求：女老师可以，男老师也可以",
        ):
            self.assert_female_not_hard(text)

    def test_synthetic_cards_split_before_filtering(self):
        cards = []
        for index, requirement in enumerate(("大学生", "有经验", "认真负责", "男女不限", "只要女生"), 1):
            cards.append(
                f"DEMO30{index:04d}#\n地址：海淀区测试路{index}号\n年级：初二\n科目：数学\n要求：{requirement}\n薪资：{120 + index}/h"
            )
        blocks = ranker.split_blocks({"body": "\n".join(cards)})
        self.assertEqual(5, len(blocks))
        self.assertEqual([f"DEMO30{index:04d}" for index in range(1, 6)], [ranker.extract_id(block)[0] for block in blocks])
        constraints = [ranker.female_tutor_constraint(block) for block in blocks]
        self.assertEqual([None, None, None, None, "hard"], constraints)

    def test_female_profile_accepts_female_requirement_and_rejects_male_requirement(self):
        ranker.configure_runtime({"tutor_profile": {"gender": "female", "school_tags": [], "school_names": []}})
        hard, _ = ranker.analyze_constraints("老师要求：仅限女老师，有经验")
        self.assertNotIn("明确女老师要求", hard)
        hard, _ = ranker.analyze_constraints("老师要求：仅限男老师，有经验")
        self.assertIn("明确男老师要求", hard)

    def test_unknown_gender_is_flagged_for_review_not_excluded(self):
        ranker.configure_runtime({"tutor_profile": {"gender": "", "school_tags": [], "school_names": []}})
        hard, notes = ranker.analyze_constraints("老师要求：仅限女老师，有经验")
        self.assertNotIn("明确女老师要求", hard)
        self.assertIn("需核对女老师要求", notes)


if __name__ == "__main__":
    unittest.main()
