import unittest

import monitor
import ranker


class AddressParserTests(unittest.TestCase):
    def test_bracketed_hourly_label_keeps_clean_pay_text(self):
        hourly, raw, kind = ranker.parse_pay("【课时费】：180元/小时", 2.0)

        self.assertEqual(180.0, hourly)
        self.assertEqual("180元/小时", raw)
        self.assertEqual("hourly", kind)

    def test_inline_address_label_is_extracted(self):
        text = "合成测试消息：需要数学家教，地点：北京示例街。\n科目：微积分"
        self.assertEqual("示例街", ranker.extract_address(text, ""))

    def test_pay_range_per_hours_uses_the_low_end(self):
        hourly, raw, kind = ranker.parse_pay("课时费：360-520/2小时", 2.0)
        self.assertEqual(180.0, hourly)
        self.assertEqual("360-520/2小时", raw)
        self.assertEqual("range_per_session", kind)

    def test_requirement_wording_is_not_an_address(self):
        text = "学科：数学物理\n年级：高三\n老师要求：熟悉本地考试特点"
        self.assertEqual("", ranker.extract_address(text, ""))

    def test_bracketed_number_cards_are_split_and_keep_synthetic_ids(self):
        body = """【编号】：DEMO200001
【地址】：线上
【年级】：新高三
【科目】：数学、英语
【报价】：190/h

【编号】：示例转介绍 示例桥
【地址】：海淀区测试路地铁站附近
【年级】：新高一
【科目】：数学、物理
【报价】：175/h"""
        blocks = ranker.split_blocks({"body": body})
        self.assertEqual(2, len(blocks))
        self.assertEqual(
            ["DEMO200001", "示例转介绍 示例桥"],
            [ranker.extract_id(block)[0] for block in blocks],
        )

    def test_information_fee_is_spread_over_the_course(self):
        raw = """【编号】：DEMO200002
【地址】：线上
【科目】：数学
【一次几个小时】：一次2小时
【一周打算上几次课】：共12次课
【课时费】：150/h
信息费：720元"""
        order = ranker.make_order(
            {"group": "合成线上群", "date": "2026-01-01", "time": "12:00:00", "sender": "演示发送者", "file": "synthetic"},
            raw,
        )
        order["route"] = {"status": "online", "round_min": 0, "round_taxi": 0}
        ranker.final_score(order)
        self.assertEqual(12, order["total_lessons"])
        self.assertEqual(720.0, order["information_fee"])
        self.assertEqual(120.0, order["net_hourly"])


class SearchKeywordTests(unittest.TestCase):
    def test_year_only_header_uses_structured_synthetic_keyword(self):
        order = {
            "id": "AUTO-DEMO0001",
            "raw": "2031\n地址：海淀区测试路或朝阳区样例街\n科目：数学\n时薪：321/小时",
            "address": "海淀区测试路或朝阳区样例街",
            "subject": "数学",
            "pay_raw": "时薪：321",
        }
        keyword = monitor.extract_search_keyword(order)
        self.assertNotEqual("2031", keyword)
        self.assertIn("海淀区测试路", keyword)
        self.assertIn("数学", keyword)
        self.assertIn("321", keyword)

    def test_distinctive_synthetic_first_line_remains_the_keyword(self):
        order = {
            "id": "AUTO-DEMO0002",
            "raw": "课程助理示例-线上-ALPHA42\n【科目和年级】高一数学",
        }
        self.assertEqual("课程助理示例-线上-ALPHA42", monitor.extract_search_keyword(order))


if __name__ == "__main__":
    unittest.main()
