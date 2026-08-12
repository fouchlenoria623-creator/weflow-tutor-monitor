import argparse
import csv
import hashlib
import html
import json
import math
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


TODAY = datetime.now().strftime("%Y-%m-%d")
ORIGIN_NAME = os.environ.get("TUTOR_ORIGIN_NAME", "未配置出发地")
ORIGIN_COORD = os.environ.get("TUTOR_ORIGIN_COORD", "")
MAP_PROVIDER = "baidu"
SUBJECT_SCORE_OVERRIDES = {}
TUTOR_PROFILE = {
    "gender": "",
    "school_tags": [],
    "school_names": [],
}
_route_attempt_results = {}
_map_thread_lock = threading.Lock()
_map_mutex_timeout_ms = 120_000
_map_request_cooldown_seconds = 0.35
MAP_KEY_ENV = {"baidu": "BAIDU_MAP_AK", "amap": "AMAP_KEY"}

FILES = []


def configure_runtime(config):
    """Apply local profile settings without persisting private values in source."""
    global ORIGIN_NAME, ORIGIN_COORD, MAP_PROVIDER, SUBJECT_SCORE_OVERRIDES, TUTOR_PROFILE

    ORIGIN_NAME = str(config.get("origin_name") or ORIGIN_NAME)
    ORIGIN_COORD = str(config.get("origin_coord") or ORIGIN_COORD)
    if "map_provider" in config:
        MAP_PROVIDER = str(config.get("map_provider") or "baidu").strip().lower()
    raw_weights = config.get("subject_weights") or {}
    SUBJECT_SCORE_OVERRIDES = {
        str(name): float(weight)
        for name, weight in raw_weights.items()
        if str(name).strip() and isinstance(weight, (int, float))
    }
    profile = config.get("tutor_profile") or {}
    TUTOR_PROFILE = {
        "gender": str(profile["gender"] if "gender" in profile else TUTOR_PROFILE.get("gender", "")).lower(),
        "school_tags": [str(item) for item in profile.get("school_tags", TUTOR_PROFILE.get("school_tags", []))],
        "school_names": [str(item) for item in profile.get("school_names", TUTOR_PROFILE.get("school_names", []))],
    }


DISTRICTS = (
    "海淀", "朝阳", "东城", "西城", "丰台", "大兴", "昌平", "通州", "顺义", "石景山",
    "房山", "怀柔", "门头沟", "密云", "平谷", "延庆", "北京", "北京市",
)
SUBJECT_WORDS = [
    "数学", "物理", "英语", "化学", "语文", "生物", "历史", "地理", "政治", "全科",
    "数理化", "数理", "数物", "物化生", "语数英", "语数", "数学物理", "奥数", "微积分",
    "高等数学", "高数", "AP Calculus", "Calculus", "IB数学", "A-Level数学", "编程", "体能",
    "书法", "硬笔", "钢琴",
]
GRADE_PAT = re.compile(
    r"(新?高[一二三123]|高一|高二|高三|准?初[一二三123]|初一|初二|初三|"
    r"[一二三四五六七八九]升[一二三四五六七八九]|小升初|小学[一二三四五六]年级|"
    r"[一二三四五六]年级|幼儿园|大班|5岁\+?|8升9|7升8|六升初一|初二升初三|初二刚上完)"
)


def md_unescape(text: str) -> str:
    replacements = {
        r"\-": "-",
        r"\#": "#",
        r"\+": "+",
        r"\.": ".",
        r"\[": "[",
        r"\]": "]",
        r"\(": "(",
        r"\)": ")",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def clean_spaces(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = text.replace("\x0b", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def normalize_key(text: str) -> str:
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，,。；;：:（）()\[\]【】#🔥🕊️🌾❗️\-—_]+", "", text)
    return text.lower()


def read_messages(path: str):
    p = Path(path)
    raw = md_unescape(p.read_text(encoding="utf-8", errors="replace"))
    lines = raw.splitlines()
    messages = []
    current = None
    heading = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s*(.*)$")
    for line in lines:
        m = heading.match(line)
        if m:
            if current:
                current["body"] = "\n".join(current.pop("_body")).strip()
                messages.append(current)
            current = {
                "file": str(p),
                "group": p.stem,
                "date": m.group(1),
                "time": m.group(2),
                "sender": clean_spaces(m.group(3)),
                "_body": [],
            }
        elif current:
            current["_body"].append(line)
    if current:
        current["body"] = "\n".join(current.pop("_body")).strip()
        messages.append(current)
    return [m for m in messages if m["date"] == TODAY]


START_PATTERN = re.compile(
    r"""(?mx)
    (?=^ \s* (
        (?:暑假单❗️|北京线上单❗️|机构单❗️)?L\d{4}\b |
        北京?Sg\d{8}\b |
        XM[^\d\n]{0,8}\d{8}\b |
        (?:\#?(?:暑假单|线上单)[^\n]{0,12})?XH\d{8}\b |
        [【\[]?\s*(?:订单编号|家教编号|家教编单|编号)\s*[】\]]?\s*[:：] |
        YH\s*:\s*\d{8,} |
        北京TT\d+ |
        YY家教\d+ |
        Ai【[^】]+】号信息 |
        [🌺]?\s*北京\d{6,} |
        (?:加急)?S\d{6,}\b |
        🔥?\#?北京线下NY\d+ |
        (?:暑假\+开学|暑假|长期)?0?7\d{4}号家教 |
        XS\s*\d{8,}\b |
        WH\d+[A-Z0-9]*\b |
        A\d{5,}\b |
        [A-Z]{1,4}\s*\d{5,}[A-Z0-9-]*\b |
        X\d{4,}[A-Z0-9-]*\b |
        【街道】 |
        学科\s*[:：]
    ))
    """
)


def strip_noise(body: str) -> str:
    body = md_unescape(body)
    body = re.sub(r"^\s*\d+@openim:\s*$", "", body, flags=re.M)
    body = re.sub(r"^\s*\[小程序\].*$", "", body, flags=re.M)
    body = body.replace("————", "\n————\n").replace("----", "\n----\n")
    body = body.replace("========", "\n========\n")
    return clean_spaces(body)


def split_blocks(message):
    body = strip_noise(message["body"])
    if not body:
        return []
    pieces = [p.strip(" \n\r\t-—") for p in START_PATTERN.split(body) if p.strip(" \n\r\t-—")]
    # re.split with a zero-width pattern returns the whole text if there is only one obvious card.
    if len(pieces) == 1:
        raw_pieces = re.split(r"\n\s*(?:[—-]{3,}|={4,})\s*\n", pieces[0])
        if len(raw_pieces) > 1:
            pieces = [p.strip() for p in raw_pieces if p.strip()]
    blocks = []
    for piece in pieces:
        if is_orderlike(piece):
            blocks.append(piece)
    if not blocks and is_orderlike(body):
        blocks.append(body)
    return blocks


def is_orderlike(text: str) -> bool:
    if len(text) < 18:
        return False
    if "小程序" in text and len(text) < 80:
        return False
    markers = 0
    for word in ("地址", "位置", "街道", "年级", "科目", "学科", "课时费", "薪酬", "报价", "薪资", "时薪", "时间", "每周", "一周", "补习"):
        if word in text:
            markers += 1
    if any(d in text for d in DISTRICTS):
        markers += 1
    if extract_id(text)[0]:
        markers += 1
    return markers >= 3


def clean_labelled_id(value: str) -> str:
    value = clean_spaces(value).strip(" #：:，,；;。")
    return re.sub(r"\s*[（(][^）)]*(?:单)?[）)]\s*$", "", value).strip()


def extract_id(text: str):
    pats = [
        (
            r"(?m)^\s*[【\[]?\s*(?:订单编号|家教编号|家教编单|编号)\s*[】\]]?\s*[:：]\s*"
            r"([A-Za-z](?:[A-Za-z0-9-]{2,}[A-Za-z0-9]))(?=\s*(?:$|[#，,；;。]))",
            lambda m: m.group(1),
        ),
        (
            r"(?m)^\s*[【\[]?\s*(?:订单编号|家教编号|家教编单|编号)\s*[】\]]?\s*[:：]\s*([^\n]{2,40})",
            lambda m: clean_labelled_id(m.group(1)),
        ),
        (r"(?:北京星辰)?家教\s*(\d{5,})\s*号家教", lambda m: m.group(1)),
        (r"(?:加急)?(S\d{6,})\b", lambda m: m.group(1)),
        (r"YH\s*:\s*(\d{8,})", lambda m: f"YH{m.group(1)}"),
        (r"(?:暑假\+开学|暑假|长期)?\s*(0?7\d{4})号家教", lambda m: m.group(1)),
        (r"\b(XH\d{8})\b", lambda m: m.group(1)),
        (r"XM[^\d\n]{0,8}(\d{8})\b", lambda m: f"XM{m.group(1)}"),
        (r"\b(北京?Sg\d{8})\b", lambda m: m.group(1).replace("北京", "")),
        (r"订单编号\s*[:：]\s*([A-Za-z\u4e00-\u9fff]*\d{4,}[A-Za-z0-9-]*)", lambda m: m.group(1)),
        (r"家教编单\s*[:：]\s*([A-Za-z\u4e00-\u9fff]*\d{4,}[A-Za-z0-9-]*)", lambda m: m.group(1)),
        (r"编号\s*[:：]\s*[^\w\u4e00-\u9fff\n]{0,5}([A-Za-z]{0,4}\d{4,}[A-Za-z0-9-]*)", lambda m: m.group(1)),
        (r"(北京TT\d+)", lambda m: m.group(1)),
        (r"(YY家教\d+)", lambda m: m.group(1)),
        (r"(Ai【[^】]+】号信息)", lambda m: m.group(1)),
        (r"家教编号\s*[:：]\s*([A-Za-z0-9-]+)", lambda m: m.group(1)),
        (r"编号\s*[:：]\s*([A-Za-z0-9-]+)", lambda m: m.group(1)),
        (r"北京线下(NY\d+)", lambda m: m.group(1)),
        (r"(?:^|\n)\s*((?:暑假单❗️|北京线上单❗️|机构单❗️)?L\d{4})", lambda m: re.sub(r"^\D*(L\d+)$", r"\1", m.group(1))),
        (r"(?:^|\n)\s*(X\d{4,}[A-Z0-9-]*)\b", lambda m: m.group(1)),
        (r"(?:^|\n)\s*((?:XS|WH|A|QS|WY|LL|XC|Zg|bj)[A-Za-z0-9\s-]{4,24})", lambda m: re.sub(r"\s+", "", m.group(1))),
        (r"\b([A-Z]{1,4}\d{6,}[A-Z0-9-]*)\b", lambda m: m.group(1)),
        (r"(?:^|\n)\s*([A-Z]{1,4}\s*\d{5,}[A-Z0-9-]*)", lambda m: re.sub(r"\s+", "", m.group(1))),
    ]
    for pat, fmt in pats:
        m = re.search(pat, text, flags=re.I)
        if m:
            return clean_spaces(fmt(m)), pat
    return "", ""


def field_value(text: str, labels):
    label_pat = "|".join(map(re.escape, labels))
    patterns = [
        rf"[「【]({label_pat})[」】]\s*[:：]?\s*([^\n]+)",
        rf"^\s*({label_pat})\s*[:：]\s*([^\n]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.M)
        if m:
            return clean_spaces(m.group(2))
    return ""


def line_after_id_address(text: str, oid: str) -> str:
    lines = [clean_spaces(x) for x in text.splitlines() if clean_spaces(x)]
    skip = {"————", "----"}
    lines = [x for x in lines if x not in skip]
    if len(lines) >= 2:
        first = normalize_key(lines[0])
        second = lines[1]
        looks_like_address = any(d in second for d in DISTRICTS) or any(x in second for x in ("地铁", "小区", "街", "路", "院", "号", "园", "附近", "站"))
        if oid and normalize_key(oid) in first and len(second) <= 40 and looks_like_address:
            return lines[1]
        if re.match(r"^[A-Za-z]{0,4}\d{4,}", first) and len(second) <= 40 and looks_like_address:
            return lines[1]
    return ""


def extract_address(text: str, oid: str):
    value = field_value(text, ["所在地址", "地址", "位置", "街道", "上课地点", "授课地点", "辅导地点", "补习地址", "学员地址", "补课地址", "地点", "站点", "省市区"])
    if value and "/" in value and "北京市" in value:
        # For YH cards, province field is followed by a separate address line.
        addr2 = field_value(text, ["地址"])
        if addr2:
            value = addr2
    if not value:
        inline = re.search(r"(?:地点|位置|地址)\s*[:：]\s*([^\n，,；;。]{2,40})", text)
        if inline:
            value = inline.group(1)
    if not value:
        value = line_after_id_address(text, oid)
    if not value:
        m = re.search(r"(?:L\d{4})([^，,\n]{3,35})(?:，|,)", text)
        if m:
            value = m.group(1)
    if not value:
        for d in DISTRICTS:
            m = re.search(rf"((?:北京市?)?{d}[^，,。\n；;:：]{{1,32}})", text)
            if m:
                candidate = m.group(1)
                if d in ("北京", "北京市"):
                    location_tail = candidate[len(d):]
                    if location_tail.startswith("地区"):
                        location_tail = location_tail[2:]
                    if not re.search(
                        r"区|县|街|路|道|巷|胡同|村|镇|乡|小区|社区|家园|花园|公寓|大厦|广场|中心|学校|大学|学院|地铁|站|桥|门|院|号|园|里|城|庄",
                        location_tail,
                    ):
                        continue
                value = candidate
                break
    value = clean_address(value)
    return value


def clean_address(value: str):
    value = clean_spaces(value)
    value = value.replace("#", " ")
    value = re.sub(r"^【?[^】\n]{0,8}(?:地址|地点|站点)】?\s*[:：]?\s*", "", value)
    value = re.sub(r"^[^\u4e00-\u9fffA-Za-z0-9]+", "", value)
    value = value.replace("北京市/市辖区/", "").replace("北京市/市辖区", "")
    value = re.sub(r"^(北京市?|市辖区)[/ ]*", "", value)
    value = re.split(r"[，,；;。]", value)[0]
    value = re.sub(r"(附近|地铁站附近|这边).*", lambda m: m.group(0), value)
    value = clean_spaces(value)
    return value[:60]


def extract_subject(text: str):
    value = field_value(text, ["科目", "学科", "补习科目", "辅导科目", "授课科目", "上课内容", "年级科目", "信息"])
    if not value:
        m = re.search(r"补习([^，,。\n；;]{1,18})", text)
        if m:
            value = m.group(1)
    if not value:
        hits = [w for w in SUBJECT_WORDS if w in text]
        value = "、".join(hits[:4])
    return clean_spaces(value)[:50]


def extract_grade(text: str):
    value = field_value(text, ["年级", "学生年级", "学员情况", "年级性别", "年级科目", "信息"])
    if not value:
        m = GRADE_PAT.search(text)
        if m:
            value = m.group(1)
    return clean_spaces(value)[:50]


def extract_schedule(text: str):
    value = field_value(text, ["时间", "补习时间", "每周频次", "课次", "周/次", "可选时段", "时间次数", "每周次数", "学习频率"])
    if not value:
        for line in text.splitlines():
            if any(x in line for x in ("每周", "一周", "周一", "周二", "周三", "周四", "周五", "周六", "周日", "单次", "每次")):
                value = line
                break
    return clean_spaces(value)[:90]


def chinese_num_to_float(s: str):
    table = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if s in table:
        return float(table[s])
    try:
        return float(s)
    except Exception:
        return None


def extract_duration_hours(text: str):
    pats = [
        r"每次\s*([0-9]+(?:\.[0-9]+)?)\s*(?:个)?\s*(?:小时|h)",
        r"一次\s*([0-9]+(?:\.[0-9]+)?)\s*(?:个)?\s*(?:小时|h)",
        r"单次\s*([0-9]+(?:\.[0-9]+)?)\s*(?:个)?\s*(?:小时|h)",
        r"每次\s*([一二两三四五六七八九十])\s*(?:个)?\s*小时",
        r"一次\s*([一二两三四五六七八九十])\s*(?:个)?\s*小时",
        r"([0-9]+(?:\.[0-9]+)?)\s*/\s*(?:次|节)",
        r"(?:至少|每天|全天|早九晚六|早上八点)\D{0,10}([0-9]+(?:\.[0-9]+)?)\s*(?:个)?\s*(?:小时|h)",
    ]
    for pat in pats:
        m = re.search(pat, text, flags=re.I)
        if m:
            v = chinese_num_to_float(m.group(1))
            if v and 0 < v <= 8:
                return v
    if re.search(r"(?:两|二|2)\s*(?:个)?\s*(?:小时|h)", text, flags=re.I):
        return 2.0
    if re.search(r"3\s*(?:个)?\s*(?:小时|h)|三\s*(?:个)?\s*小时", text, flags=re.I):
        return 3.0
    if re.search(r"1\.5\s*(?:个)?\s*(?:小时|h)", text, flags=re.I):
        return 1.5
    if re.search(r"早九晚六|9点到晚上8点|早上9点到晚上8点|全天", text):
        return 8.0
    return 2.0


def pay_context(text: str):
    labels = ("课时费", "薪酬", "报价", "期望时薪", "费用", "薪资", "酬", "时薪", "每小时", "课时价格", "大学生一次", "专职在职")
    lines = []
    for line in text.splitlines():
        if any(label in line for label in labels):
            lines.append(line)
    return "\n".join(lines) if lines else text


def parse_pay(text: str, duration: float):
    ctx = pay_context(text)
    ctx = ctx.replace("－", "-").replace("—", "-").replace("到", "-").replace("～", "~")
    candidates = []

    def add(value, raw, kind):
        try:
            value = float(value)
        except Exception:
            return
        if 30 <= value <= 800:
            candidates.append((value, clean_spaces(raw), kind))

    for m in re.finditer(
        r"(\d{2,4})\s*[~-]\s*(\d{2,4})\s*/\s*([0-9]+(?:\.[0-9]+)?)\s*(?:h|(?:个)?小时)",
        ctx,
        flags=re.I,
    ):
        add(float(m.group(1)) / float(m.group(3)), m.group(0), "range_per_session")
    for m in re.finditer(r"(\d{2,4})\s*(?:元)?\s*/\s*([0-9]+(?:\.[0-9]+)?)\s*h", ctx, flags=re.I):
        add(float(m.group(1)) / float(m.group(2)), m.group(0), "per_session")
    for m in re.finditer(r"(\d{2,4})\s*(?:元)?\s*/\s*([0-9]+(?:\.[0-9]+)?)\s*(?:个)?\s*小时", ctx, flags=re.I):
        add(float(m.group(1)) / float(m.group(2)), m.group(0), "per_hours")
    for m in re.finditer(r"(\d{2,4})\s*[~-]\s*(\d{2,4})\s*元?\s*(?:2|两|二)\s*(?:个)?\s*(?:小时|h)", ctx, flags=re.I):
        add(float(m.group(1)) / 2.0, m.group(0), "range_two_hours")
    for m in re.finditer(r"(\d{2,4})\s*元?\s*(?:2|两|二)\s*(?:个)?\s*(?:小时|h)", ctx, flags=re.I):
        add(float(m.group(1)) / 2.0, m.group(0), "two_hours")
    for m in re.finditer(r"(\d{2,4})\s*[~-]\s*(\d{2,4})\s*元?\s*/\s*次", ctx):
        add(float(m.group(1)) / max(duration, 1), m.group(0), "range_yuan_per_lesson")
    for m in re.finditer(r"(\d{2,4})\s*元?\s*/\s*次", ctx):
        add(float(m.group(1)) / max(duration, 1), m.group(0), "yuan_per_lesson")
    for m in re.finditer(r"(\d{2,4})\s*[~-]\s*(\d{2,4})\s*(?:元)?\s*(?:/h|/小时|每小时|一小时|时薪|每一小时|h\b)", ctx, flags=re.I):
        add(m.group(1), m.group(0), "hourly_range")
    for m in re.finditer(r"(?:时薪|薪资|课时费|期望时薪|费用|报价)\D{0,8}(\d{2,4})\+?", ctx, flags=re.I):
        raw = m.group(0)
        if re.search(r"2h|2小时|两小时|二小时", raw, flags=re.I):
            add(float(m.group(1)) / 2.0, raw, "label_two_hours")
        elif re.search(r"/次|每次|一次|一节", raw, flags=re.I):
            add(float(m.group(1)) / max(duration, 1), raw, "label_per_lesson")
        else:
            add(m.group(1), raw, "label_hourly")
    for m in re.finditer(r"(?:薪资|薪酬|课费|费用)\s*[:：]?\s*(\d{2,4})\s*[~-]\s*(\d{2,4})\s*$", ctx, flags=re.I | re.M):
        add(float(m.group(1)) / max(duration, 1), m.group(0), "unqualified_range_per_session")
    for m in re.finditer(r"(\d{2,4})\s*(?:元)?\s*(?:/h|/小时|每小时|一小时|时薪|h\b)", ctx, flags=re.I):
        add(m.group(1), m.group(0), "hourly")
    for m in re.finditer(r"(\d{2,4})\s*(?:一次|一节|/次)", ctx):
        add(float(m.group(1)) / max(duration, 1), m.group(0), "per_lesson")
    for m in re.finditer(r"(?:每次课|一次课|一节课)\D{0,4}(\d{2,4})", ctx):
        add(float(m.group(1)) / max(duration, 1), m.group(0), "per_lesson_label")
    for m in re.finditer(r"(\d{2,4})\s*[~-]\s*(\d{2,4})\s*/\s*天", ctx):
        add(float(m.group(1)) / max(duration, 6.0), m.group(0), "per_day_range")
    for m in re.finditer(r"(\d{2,4})\s*(?:元)?\s*/\s*天", ctx):
        add(float(m.group(1)) / max(duration, 6.0), m.group(0), "per_day")
    for m in re.finditer(r"(\d{2,4})\s*(?:元)?\s*(?:一天|每天)", ctx):
        add(float(m.group(1)) / max(duration, 6.0), m.group(0), "per_day_words")
    for m in re.finditer(r"(?:一天|每天)\s*(\d{2,4})", ctx):
        add(float(m.group(1)) / max(duration, 6.0), m.group(0), "per_day_prefix")
    for m in re.finditer(r"全天\s*(\d{2,4})", ctx):
        add(float(m.group(1)) / 8.0, m.group(0), "full_day")
    for m in re.finditer(r"一次\s*(\d{2,4})\s*[~-]\s*(\d{2,4})\s*(?:三|3)\s*(?:个)?\s*小时", ctx):
        add(float(m.group(1)) / 3.0, m.group(0), "three_hour_lesson")
    if re.search(r"酬\s*2h|酬2h|2h", ctx, flags=re.I):
        for m in re.finditer(r"(\d{2,4})\s*左右", ctx):
            add(float(m.group(1)) / 2.0, m.group(0), "around_two_hours")

    if not candidates:
        return None, "", ""
    # At the same conservative hourly value, prefer a match that includes the
    # actual unit over the generic labelled fallback. This keeps report text
    # such as "180元/小时" instead of "课时费】：180".
    candidates.sort(key=lambda item: (item[0], item[2].startswith("label_")))
    return candidates[0]


def extract_frequency(text: str):
    pats = [
        r"(?:一周|每周)\s*(?:最少|至少|约)?\s*([0-9一二两三四五六七八九十]+)\s*(?:到|-|~)?\s*([0-9一二两三四五六七八九十]*)\s*次",
        r"(?:周一到周五|周一至周五)",
        r"每天",
    ]
    m = re.search(pats[0], text)
    if m:
        low = chinese_num_to_float(m.group(1))
        high = chinese_num_to_float(m.group(2)) if m.group(2) else low
        if low:
            return (low + (high or low)) / 2.0
    if re.search(pats[1], text):
        return 5.0
    if re.search(pats[2], text):
        return 7.0
    return None


def extract_total_lessons(text: str):
    for pat in (
        r"(?:共|合计|总计|暑假)?\s*(\d{1,3})\s*(?:次课|节课)",
        r"(?:上|安排|集中上)\s*(\d{1,3})\s*次",
    ):
        match = re.search(pat, text)
        if match:
            value = int(match.group(1))
            if 1 <= value <= 200:
                return value
    return None


def extract_information_fee(text: str):
    lines = [clean_spaces(line) for line in str(text or "").splitlines() if clean_spaces(line)]
    for index, line in enumerate(lines):
        if not re.search(r"信息费|中介费|服务费", line):
            continue
        values = []
        for candidate in lines[index:index + 3]:
            values.extend(float(value) for value in re.findall(r"=\s*(\d{2,6}(?:\.\d+)?)", candidate))
            direct = re.search(r"(?:信息费|中介费|服务费)\s*[:：]\s*(\d{2,6}(?:\.\d+)?)\s*(?:元)?\s*$", candidate)
            if direct:
                values.append(float(direct.group(1)))
        if values:
            return values[-1]
    return None


def req_text(text: str):
    lines = []
    for line in text.splitlines():
        if any(k in line for k in ("要求", "老师", "特殊备注", "家教需求", "大概要求", "对老师要求")):
            lines.append(line)
    return "\n".join(lines) if lines else text


FEMALE_TUTOR_ROLE_RE = re.compile(
    r"(?<!男)女(?:性)?\s*"
    r"(?:(?:在读|在校|全日制|应届|优秀|年轻|本地|北京籍|在职|专职|兼职|机构|专业)\s*){0,3}"
    r"(?:(?:(?:师范类|学前|英语|数学|物理|理科|文科)(?:专业)?)(?:或|或者|、|/)?\s*){0,3}"
    r"(?:老师|教师|教员|家教|外教|同学|学生|大学生|本科生|研究生|硕士(?:生)?|博士(?:生)?|大|生老师)"
)
UNAMBIGUOUS_FEMALE_TEACHER_RE = re.compile(
    r"(?<!男)女(?:性)?\s*(?:(?:在读|在校|全日制|优秀|年轻|在职|专职|兼职|机构|专业)\s*){0,2}"
    r"(?:老师|教师|教员|家教|外教|生老师|(?:大学生|本科生|研究生|硕士生?|博士生?)\s*老师)"
)
MALE_TUTOR_ALLOWED_RE = re.compile(
    r"男女(?:老师|教师|教员|大学生|研究生|硕士生?)?"
    r"(?:不限|均可|都可|皆可|都可以|均可以|皆可以|都行)?"
    r"|不限性别|性别不限|无性别要求|性别无要求|可男可女"
    r"|男(?:生|老师|教师|教员|大学生|研究生|硕士生?)\s*(?:也|都|均)?\s*(?:可|可以|行|接受|优先)"
)


def _female_requirement_scope(text: str) -> str:
    label = re.compile(
        r"(?:对(?:老师|教师|教员|家教)(?:的)?要求"
        r"|(?:老师|教师|教员|家教)(?:要求|条件|资质|性别)"
        r"|[【\[]?(?:老师|教师|教员|家教)[】\]]"
        r"|(?:老师|教师|教员|家教)\s*[:：]"
        r"|师资要求|大概要求|其他要求|特殊备注|家教需求|性别要求|要求)"
        r"\s*[】}\]]?\s*[:：]?"
    )
    fragments = []
    for raw_line in str(text or "").splitlines() or [str(text or "")]:
        line = raw_line.strip()
        if not line:
            continue
        match = label.search(line)
        if match:
            fragments.append(line[match.start():])
        elif re.match(r"^\s*(?:5|五)\s*[、.．)：:]", line) and re.search(r"女|女性", line):
            fragments.append(line)
        elif UNAMBIGUOUS_FEMALE_TEACHER_RE.search(line):
            fragments.append(line)
        elif re.search(r"(?:只要|仅限|限|必须|务必|指定|要|找|招)\s*(?:是|为)?\s*(?:女性|女生|女的)", line):
            fragments.append(line)
        elif re.search(r"(?:高考|中考|考研|四六级|雅思|托福|成绩|分数)[^\n]{0,24}(?:女生|女性|女同学)", line):
            fragments.append(line)
    return "\n".join(dict.fromkeys(fragments))


def _female_role_is_preference(clause: str, match: re.Match) -> bool:
    before = clause[max(0, match.start() - 14):match.start()]
    after = clause[match.end():match.end() + 10]
    return bool(
        re.search(
            r"(?:优先|最好|尽量|倾向|偏向|希望|更希望)(?:是|找|考虑|选择)?\s*#?\s*"
            r"(?:(?:985|211|92|[\u4e00-\u9fffA-Za-z0-9/]{1,8}(?:大学|院校|专业|大))\s*)?$",
            before,
        )
        or re.match(r"\s*(?:优先|最好|更好|为佳|更合适)(?=[，,。；;\s#）)]|$)", after)
    )


def _female_standalone_matches(clause: str):
    return re.finditer(
        r"女性|女生|女孩子|女孩|女的|(?<![\u4e00-\u9fffA-Za-z0-9])女(?=[，,。；;\s#）)]|$)",
        clause,
    )


def _female_standalone_is_student_context(clause: str, match: re.Match) -> bool:
    before = clause[max(0, match.start() - 16):match.start()]
    after = clause[match.end():match.end() + 16]
    degree_role = bool(re.search(r"(?:大学生|本科生|研究生|硕士生?|博士生?)\s*$", before))
    return bool(
        (
            re.search(r"(?:学生|学员|孩子|小孩|女儿|宝宝|宝贝|年级|学生性别|学员性别|年级性别)\s*(?:是|为|[:：])?\s*$", before)
            and not degree_role
        )
        or re.search(r"家里是\s*$", before)
        or re.search(r"(?:双胞胎|姐妹|兄妹|姐弟|两个|两名|两位)\s*$", before)
        or (
            re.search(r"^[【\[]?其他要求", clause.strip())
            and (
                re.search(r"(?:一对[一二2]|[一二两\d]+个)\s*$", before)
                or re.match(r"\s*(?:上课|学生|学员|孩子|小孩|宝宝|宝贝)", after)
                or re.search(r"(?:女老师|女教师|女教员|老师|教师|教员)", after)
            )
        )
    )


def _female_standalone_is_preference(clause: str, match: re.Match) -> bool:
    before = clause[max(0, match.start() - 14):match.start()]
    after = clause[match.end():match.end() + 10]
    return bool(
        re.search(r"(?:优先|最好|尽量|倾向|偏向|希望|更希望)(?:是|找|考虑|选择)?\s*#?\s*$", before)
        or re.match(r"\s*(?:优先|最好|更好|为佳|更合适)(?=[，,。；;\s#）)]|$)", after)
    )


def female_tutor_constraint(text: str):
    """Return hard/preferred only for tutor gender, never student gender."""
    scope = _female_requirement_scope(text)
    if not scope:
        return None

    preferred = False
    for clause in (line.strip() for line in scope.splitlines() if line.strip()):
        allows_male = bool(MALE_TUTOR_ALLOWED_RE.search(clause))
        role_matches = list(FEMALE_TUTOR_ROLE_RE.finditer(clause))
        for match in role_matches:
            if _female_role_is_preference(clause, match):
                preferred = True
            elif not allows_male:
                return "hard"

        hard_standalone = bool(
            re.search(r"(?:只要|仅限|限|必须|务必|指定|要|找|招)\s*(?:是|为|一名|一个|1名|#|\(|（)*\s*(?:女性|女生|女孩子|女孩|女的)(?=[，,。；;\s）)#]|来就过|直接过|$)", clause)
            or re.search(r"(?:老师|教师|教员|家教)\s*性别(?:要求|选择)?\s*[:：]?\s*(?:女|女性|女生)(?=[，,。；;\s）)]|$)", clause)
            or re.search(r"性别(?:要求|选择)\s*[:：]?\s*(?:女|女性|女生)(?=[，,。；;\s）)]|$)", clause)
        )
        if hard_standalone and not allows_male:
            return "hard"

        for match in _female_standalone_matches(clause):
            if _female_standalone_is_student_context(clause, match):
                continue
            if _female_standalone_is_preference(clause, match):
                preferred = True
            elif not allows_male:
                return "hard"
    return "preferred" if preferred else None


MALE_TUTOR_ROLE_RE = re.compile(
    r"(?<!女)男(?:性)?\s*"
    r"(?:(?:在读|在校|全日制|应届|优秀|年轻|本地|在职|专职|兼职|机构|专业)\s*){0,3}"
    r"(?:老师|教师|教员|家教|外教|同学|大学生|本科生|研究生|硕士(?:生)?|博士(?:生)?)"
)
FEMALE_TUTOR_ALLOWED_RE = re.compile(
    r"男女(?:老师|教师|教员|大学生|研究生|硕士生?)?"
    r"(?:不限|均可|都可|皆可|都可以|均可以|皆可以|都行)?"
    r"|不限性别|性别不限|无性别要求|性别无要求|可男可女"
    r"|女(?:生|老师|教师|教员|大学生|研究生|硕士生?)\s*(?:也|都|均)?\s*(?:可|可以|行|接受|优先)"
)


def male_tutor_constraint(text: str):
    """Return hard/preferred for explicit male-tutor wording."""
    label = re.compile(
        r"(?:对(?:老师|教师|教员|家教)(?:的)?要求|(?:老师|教师|教员|家教)(?:要求|条件|资质|性别)"
        r"|师资要求|其他要求|特殊备注|家教需求|性别要求|要求)\s*[】}\]]?\s*[:：]?"
    )
    clauses = []
    for raw_line in str(text or "").splitlines() or [str(text or "")]:
        line = raw_line.strip()
        if not line:
            continue
        match = label.search(line)
        if match:
            clauses.append(line[match.start():])
        elif MALE_TUTOR_ROLE_RE.search(line):
            clauses.append(line)
    preferred = False
    for clause in dict.fromkeys(clauses):
        if FEMALE_TUTOR_ALLOWED_RE.search(clause):
            continue
        for match in MALE_TUTOR_ROLE_RE.finditer(clause):
            before = clause[max(0, match.start() - 14):match.start()]
            after = clause[match.end():match.end() + 10]
            if re.search(r"(?:优先|最好|尽量|倾向|偏向|希望|更希望)(?:是|找|考虑|选择)?\s*$", before) or re.match(
                r"\s*(?:优先|最好|更好|为佳|更合适)(?=[，,。；;\s）)]|$)", after
            ):
                preferred = True
            else:
                return "hard"
        if re.search(
            r"(?:只要|仅限|限|必须|务必|指定|要|找|招)\s*(?:是|为|一名|一个|1名)?\s*"
            r"(?:男性|男生|男老师|男教师|男教员)(?=[，,。；;\s）)]|$)",
            clause,
        ) or re.search(
            r"(?:老师|教师|教员|家教)?\s*性别(?:要求|选择)?\s*[:：]?\s*(?:男|男性|男生)(?=[，,。；;\s）)]|$)",
            clause,
        ):
            return "hard"
    return "preferred" if preferred else None


def analyze_constraints(text: str):
    req = req_text(text)
    scan = req + "\n" + text
    hard = []
    notes = []
    repeated_locations = max(
        (
            len(re.findall(rf"^\s*[【\[]?{label}[】\]]?\s*[:：]?", text, flags=re.M))
            for label in ("地点", "地址", "位置", "上课区域", "授课地点", "教学地址", "补习地址", "学员地址", "家教地址")
        ),
        default=0,
    )
    embedded_ids = set(re.findall(r"(?:S\d{6,}|[A-Za-z]{1,4}\d{6,}[A-Za-z0-9-]*)\b", text, flags=re.I))
    if repeated_locations >= 2 or len(embedded_ids) >= 2:
        hard.append("多单合并需拆分")
    school_tags = set(TUTOR_PROFILE.get("school_tags") or [])
    school_names = set(TUTOR_PROFILE.get("school_names") or [])
    school_profile_configured = bool(school_tags or school_names)
    if re.search(r"清北|清华|北大|只要清北", scan) and "清北可报价" not in scan:
        if not school_profile_configured:
            notes.append("需核对清北/清华北大要求")
        elif not school_names.intersection({"清华", "清华大学", "北大", "北京大学"}):
            hard.append("清北/清华北大要求")
    if re.search(r"(?<!/)(985)(?!/211)", scan) or re.search(r"\b985\b", scan):
        if "985/211" not in scan and "985、211" not in scan and "985 211" not in scan:
            if not school_profile_configured:
                notes.append("需核对985要求")
            elif "985" not in school_tags:
                hard.append("985硬要求")
    combined_school_requirement = bool(re.search(
        r"985\s*[/、 ]\s*211|(?<!\d)92(?!\d)[^\n]{0,4}(?:院校|高校|大学生|本科生|研究生|硕士生)",
        scan,
    ))
    if combined_school_requirement:
        if not school_profile_configured:
            notes.append("需核对985/211或92要求")
        elif school_tags.intersection({"985", "211", "92"}):
            notes.append("985/211或92要求")
        else:
            hard.append("985/211或92要求")
    female_constraint = female_tutor_constraint(text)
    if female_constraint == "hard":
        gender = TUTOR_PROFILE.get("gender")
        if gender in {"male", "男", "男性"}:
            hard.append("明确女老师要求")
        elif not gender:
            notes.append("需核对女老师要求")
    elif female_constraint == "preferred":
        notes.append("女老师偏好")
    male_constraint = male_tutor_constraint(text)
    if male_constraint == "hard":
        gender = TUTOR_PROFILE.get("gender")
        if gender in {"female", "女", "女性"}:
            hard.append("明确男老师要求")
        elif not gender:
            notes.append("需核对男老师要求")
    elif male_constraint == "preferred":
        notes.append("男老师偏好")
    if re.search(r"(在职|专职|机构|专业老师)", scan):
        if not re.search(r"大学生.*(?:也可|或|/)|大学生.*专职|专职.*大学生|大学生一次", scan):
            hard.append("在职/专职/机构要求")
        else:
            notes.append("有专职价格但大学生可投")
    if "住家" in scan:
        hard.append("住家要求")
    if re.search(r"全天|每天都要|周一到周五|一周7次", text):
        notes.append("频次/时长偏重")
    if re.search(r"清北可报价", scan):
        notes.append("清北可高报价但非唯一")
    for school in ("北航", "北理工", "人大", "北师大"):
        if school in scan:
            school_lines = "\n".join(line for line in scan.splitlines() if school in line)
            identity_required = bool(
                re.search(
                    rf"(?:身份|院校|学校)(?:要求)?\s*[:：]?\s*[^\n]{{0,8}}{school}"
                    rf"|(?:要求|只要|仅限|必须|指定)\s*[:：]?\s*{school}\s*(?:在校生|学生|本科生|研究生)"
                    rf"|{school}\s*(?:在校生|本科生|研究生)(?!\s*优先)",
                    school_lines,
                )
            )
            if identity_required:
                if not school_profile_configured:
                    notes.append(f"需核对{school}指定要求")
                elif school not in school_names:
                    hard.append(f"{school}指定/强偏好")
                else:
                    notes.append(f"{school}身份匹配")
            elif re.search(rf"{school}[^\n]{{0,12}}(?:优先|最好|喜欢)|(?:优先|最好|喜欢)[^\n]{{0,12}}{school}", school_lines):
                notes.append(f"{school}偏好")
    return hard, notes


def subject_score(subject: str, text: str):
    s = subject + " " + text[:120]
    if SUBJECT_SCORE_OVERRIDES:
        matches = [
            weight for keyword, weight in SUBJECT_SCORE_OVERRIDES.items()
            if keyword.lower() in s.lower()
        ]
        if matches:
            return max(matches)
    score = 0
    if re.search(
        r"微积分|高等数学|高数|calculus|ap\s*(?:calc|数学)|ib\s*(?:math|数学)|a[- ]?level\s*(?:math|数学)",
        s,
        flags=re.I,
    ):
        score += 46
    elif "数学物理" in s or "数物" in s or ("数学" in s and "物理" in s):
        score += 44
    elif "数理化" in s or "物化生" in s:
        score += 40
    elif "数学" in s or "奥数" in s:
        score += 35
    elif "物理" in s:
        score += 33
    elif "化学" in s:
        score += 24
    elif "全科" in s or "语数英" in s or "语数" in s:
        score += 22
    elif "英语" in s:
        score += 14
    elif "语文" in s:
        score += 7
    elif "生物" in s:
        score += 6
    elif "编程" in s:
        score += 2
    elif "体能" in s or "书法" in s or "钢琴" in s or "历史" in s:
        score -= 8
    return score


def grade_score(grade: str, text: str):
    g = grade + " " + text[:120]
    if re.search(r"高[一二三123]|新高|准高", g):
        return 14
    if re.search(r"初三|新初三|初二升初三|8升9", g):
        return 12
    if re.search(r"初二|初一|7升8|六升初一|小升初", g):
        return 7
    if re.search(r"[三四五六]年级|三升四|四升五|五升六", g):
        return 2
    if re.search(r"一年级|二年级|一升二|大班|5岁", g):
        return -4
    return 0


def rough_score(order):
    score = subject_score(order["subject"], order["raw"]) + grade_score(order["grade"], order["raw"])
    hourly = order.get("hourly")
    if hourly:
        score += min(max((hourly - 80) / 3.0, 0), 45)
    else:
        score -= 12
    freq = order.get("frequency")
    if freq:
        if 2 <= freq <= 4:
            score += 8
        elif freq == 1:
            score -= 2
        elif freq >= 5:
            score -= 3
    if any(x in order["raw"] for x in ("陪读", "写作业", "暑假作业", "督促作业", "检查作业", "作业陪读", "作业辅导", "辅导作业")):
        score += 15
    if any(x in order["raw"] for x in ("全天", "周一到周五", "每天")):
        score -= 8
    for d in ("房山", "怀柔", "顺义", "通州", "大兴", "昌平"):
        if d in order.get("address", ""):
            score -= 8
    if order["hard_reasons"]:
        score -= 1000
    return round(score, 2)


def make_order(message, block):
    oid, _ = extract_id(block)
    subject = extract_subject(block)
    grade = extract_grade(block)
    address = extract_address(block, oid)
    schedule = extract_schedule(block)
    duration = extract_duration_hours(block)
    hourly, pay_raw, pay_kind = parse_pay(block, duration)
    frequency = extract_frequency(block)
    total_lessons = extract_total_lessons(block)
    information_fee = extract_information_fee(block)
    hard, notes = analyze_constraints(block)
    if information_fee and total_lessons:
        notes.append(f"信息费{information_fee:g}元按{total_lessons}次摊销")
    if not oid:
        seed = f"{message['group']}|{message['date']} {message['time']}|{address}|{subject}|{grade}|{block[:80]}"
        oid = "AUTO-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
    order = {
        "id": oid,
        "posted_at": f"{message['date']} {message['time']}",
        "sender": message["sender"],
        "group": message["group"],
        "source_file": message["file"],
        "address": address,
        "grade": grade,
        "subject": subject,
        "schedule": schedule,
        "duration_h": duration,
        "frequency": frequency,
        "total_lessons": total_lessons,
        "information_fee": information_fee,
        "hourly": hourly,
        "pay_raw": pay_raw,
        "pay_kind": pay_kind,
        "hard_reasons": hard,
        "notes": notes,
        "raw": clean_spaces(block),
    }
    order["rough_score"] = rough_score(order)
    return order


def auto_dedupe_key(order):
    """Build a conservative key for unnumbered orders.

    Cross-posts usually keep the teaching schedule and requirements intact even
    when the sender or group changes. Including those fields, plus the posting
    day, avoids collapsing unrelated orders that merely share an address,
    subject, grade and hourly rate.
    """
    posted_day = str(order.get("posted_at") or "").split(" ", 1)[0]

    def canonical(value):
        if value is None:
            return ""
        if isinstance(value, float):
            return f"{value:g}"
        return normalize_key(str(value))

    raw = str(order.get("raw") or "")
    signature = {
        "posted_day": posted_day,
        "address": canonical(order.get("address")),
        "subject": canonical(order.get("subject")),
        "grade": canonical(order.get("grade")),
        "hourly": canonical(order.get("hourly")),
        "pay_kind": canonical(order.get("pay_kind")),
        "schedule": canonical(order.get("schedule")),
        "duration_h": canonical(order.get("duration_h")),
        "frequency": canonical(order.get("frequency")),
        "total_lessons": canonical(order.get("total_lessons")),
        "information_fee": canonical(order.get("information_fee")),
        "requirements": canonical(req_text(raw)),
    }
    payload = json.dumps(signature, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "auto:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()


def dedupe_orders(orders):
    kept = {}
    for order in orders:
        if order["id"].startswith("AUTO-"):
            key = auto_dedupe_key(order)
        else:
            key = normalize_key(order["id"])
        if key in kept:
            old = kept[key]
            old.setdefault("duplicate_sources", []).append({
                "group": order["group"],
                "posted_at": order["posted_at"],
                "source_file": order["source_file"],
            })
            old["groups"] = sorted(set(old.get("groups", [old["group"]]) + [order["group"]]))
            if order["rough_score"] > old["rough_score"] and len(order["raw"]) > len(old["raw"]) * 0.7:
                # Prefer the richer card while preserving duplicate source data.
                dup = old.get("duplicate_sources", [])
                groups = old.get("groups", [old["group"]])
                order["duplicate_sources"] = dup
                order["groups"] = groups
                kept[key] = order
        else:
            order["duplicate_sources"] = []
            order["groups"] = [order["group"]]
            kept[key] = order
    return list(kept.values())


def load_cache(path: Path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(path: Path, cache):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


@contextmanager
def _map_request_slot(provider):
    """Serialize live requests per provider across threads and Windows sessions."""
    provider_name = str(provider or "map").strip().title()
    with _map_thread_lock:
        if os.name != "nt":
            try:
                yield
            finally:
                time.sleep(_map_request_cooldown_seconds)
            return

        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        create_mutex.restype = ctypes.c_void_p
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        wait_for_single_object.restype = ctypes.c_uint32
        release_mutex = kernel32.ReleaseMutex
        release_mutex.argtypes = (ctypes.c_void_p,)
        release_mutex.restype = ctypes.c_bool
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_bool

        handle = create_mutex(None, False, rf"Global\WeFlowTutor{provider_name}Api")
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = False
        try:
            wait_status = wait_for_single_object(handle, _map_mutex_timeout_ms)
            if wait_status not in (0x00000000, 0x00000080):
                if wait_status == 0x00000102:
                    raise TimeoutError(f"Timed out waiting for the {provider_name} API request slot")
                raise OSError(f"WaitForSingleObject failed with status {wait_status}")
            acquired = True
            try:
                yield
            finally:
                time.sleep(_map_request_cooldown_seconds)
        finally:
            if acquired:
                release_mutex(handle)
            close_handle(handle)


@contextmanager
def _baidu_request_slot():
    with _map_request_slot("baidu"):
        yield


@contextmanager
def _amap_request_slot():
    with _map_request_slot("amap"):
        yield


def baidu_get(url, params):
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(url + "?" + query, headers={"User-Agent": "CodexTutorRank/1.0"})
    last_error = None
    for attempt in range(4):
        try:
            with _baidu_request_slot():
                with urllib.request.urlopen(req, timeout=12) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except OSError as exc:
            last_error = exc
            time.sleep(0.8 * (attempt + 1))
            continue
        try:
            status = int(data.get("status", -1))
        except (TypeError, ValueError):
            status = -1
        # Baidu uses 401 for QPS/concurrency pressure. Quota exhaustion (302)
        # and request/configuration errors must not be multiplied by retries.
        if status in (1, 401) and attempt < 3:
            time.sleep(0.8 * (attempt + 1))
            continue
        return data
    if last_error:
        raise last_error
    return data


def amap_get(url, params):
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(url + "?" + query, headers={"User-Agent": "CodexTutorRank/1.0"})
    last_error = None
    for attempt in range(4):
        try:
            with _amap_request_slot():
                with urllib.request.urlopen(req, timeout=12) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except OSError as exc:
            last_error = exc
            time.sleep(0.8 * (attempt + 1))
            continue
        # Do not retry API-level QPS, quota, permission or server-busy errors
        # within the same run. A tight retry loop can worsen throttling.
        return data
    if last_error:
        raise last_error
    return data


def estimate_one_way_taxi(km: float) -> float:
    if km <= 0:
        return 0.0
    base = 13.0
    if km <= 3:
        return base
    # Conservative Beijing daytime estimate used when the provider omits taxi fare.
    return round(base + (km - 3.0) * 2.3, 1)


def requested_district(address):
    for district in (
        "东城区", "西城区", "朝阳区", "海淀区", "丰台区", "石景山区",
        "门头沟区", "房山区", "通州区", "顺义区", "昌平区", "大兴区",
        "怀柔区", "平谷区", "密云区", "延庆区",
    ):
        if district in address or district.removesuffix("区") in address:
            return district
    return ""


def district_matches(expected, actual):
    if not expected or not actual:
        return True
    return expected.removesuffix("区") in str(actual).replace(" ", "")


def route_cache_plausible(address, route_data):
    minimum_by_district = {"房山区": 20, "大兴区": 18, "通州区": 20, "顺义区": 18}
    minimum = minimum_by_district.get(requested_district(address))
    return minimum is None or (route_data.get("one_km") or 0) >= minimum


def normalized_origin_coord():
    try:
        longitude, latitude = map(float, str(ORIGIN_COORD).split(",", 1))
        return f"{longitude:.6f},{latitude:.6f}"
    except (TypeError, ValueError):
        return str(ORIGIN_COORD).strip()


def route_cache_key(address, provider):
    provider_name = str(provider or "unknown").strip().lower()
    return f"route:{provider_name}:{normalized_origin_coord()}:{str(address).strip()}"


def geocode_cache_key(address, provider, field="location"):
    provider_name = str(provider or "unknown").strip().lower()
    return f"geo:{provider_name}:{normalized_origin_coord()}:{field}:{str(address).strip()}"


def clear_geocode_cache(cache, address, provider):
    for field in ("location", "source", "district", "mismatch", "confidence", "precise", "comprehension", "level"):
        cache.pop(geocode_cache_key(address, provider, field), None)


def with_route_context(route_data, provider):
    result = dict(route_data or {})
    result["cache_provider"] = str(provider or "unknown").strip().lower()
    result["cache_origin_coord"] = normalized_origin_coord()
    return result


def reusable_route(address, route_data, live_provider="", expected_provider=""):
    if not isinstance(route_data, dict) or route_data.get("status") not in ("ok", "estimated"):
        return False
    if expected_provider:
        if route_data.get("cache_provider") != str(expected_provider).strip().lower():
            return False
        if route_data.get("cache_origin_coord") != normalized_origin_coord():
            return False
    if not route_cache_plausible(address, route_data):
        return False
    # Refresh legacy or other-provider estimates once the selected provider is live.
    if live_provider and route_data.get("status") == "estimated":
        expected_route_provider = {
            "baidu": "baidu_directionlite",
            "amap": "amap_v5_driving",
        }.get(str(live_provider).strip().lower())
        return route_data.get("route_provider") == expected_route_provider
    return True


def _inside_beijing(longitude, latitude):
    # Loose municipality bounds, including Yanqing, Miyun and Pinggu.
    return 115.4 <= longitude <= 117.6 and 39.3 <= latitude <= 41.2


def _utf8_prefix(value, max_bytes):
    return str(value).encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def baidu_geocode(address, ak, cache):
    ckey = geocode_cache_key(address, "baidu")
    district_key = geocode_cache_key(address, "baidu", "district")
    expected_district = requested_district(address)
    if cache.get(ckey):
        cached_district = cache.get(district_key, "")
        if not cached_district or district_matches(expected_district, cached_district):
            return cache[ckey]
        cache.pop(ckey, None)

    lookup_address = re.split(r"[】#]|坐地铁|倒公交|公交路线|地铁路线", address, maxsplit=1)[0].strip(" ,，")
    structured = lookup_address if "北京" in lookup_address else "北京市" + lookup_address
    data = baidu_get("https://api.map.baidu.com/geocoding/v3/", {
        "address": _utf8_prefix(structured, 128),
        "city": "北京市",
        "output": "json",
        # Keep geocoder and route coordinates in the same coordinate system.
        "ret_coordtype": "gcj02ll",
        "extension_poi_infos": "true",
        "ak": ak,
    })
    try:
        status = int(data.get("status", -1))
    except (TypeError, ValueError):
        status = -1
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    location = result.get("location") if isinstance(result.get("location"), dict) else {}
    if status != 0 or location.get("lng") is None or location.get("lat") is None:
        return None

    returned_district = ""
    poi_infos = data.get("poi_infos") or result.get("poi_infos") or []
    if isinstance(poi_infos, list) and poi_infos and isinstance(poi_infos[0], dict):
        returned_district = str(poi_infos[0].get("district") or "")
    if returned_district and not district_matches(expected_district, returned_district):
        cache[geocode_cache_key(address, "baidu", "mismatch")] = returned_district
        return None

    try:
        longitude = float(location["lng"])
        latitude = float(location["lat"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return None
    if not _inside_beijing(longitude, latitude):
        cache[geocode_cache_key(address, "baidu", "mismatch")] = f"outside_beijing:{longitude:.6f},{latitude:.6f}"
        return None

    loc = f"{longitude:.6f},{latitude:.6f}"
    cache[ckey] = loc
    cache[geocode_cache_key(address, "baidu", "source")] = "baidu_geocode"
    cache[district_key] = returned_district or expected_district
    cache[geocode_cache_key(address, "baidu", "confidence")] = result.get("confidence")
    cache[geocode_cache_key(address, "baidu", "precise")] = result.get("precise")
    cache[geocode_cache_key(address, "baidu", "comprehension")] = result.get("comprehension")
    cache[geocode_cache_key(address, "baidu", "level")] = result.get("level")
    return loc


def _text_field(value):
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def amap_geocode(address, key, cache):
    ckey = geocode_cache_key(address, "amap")
    district_key = geocode_cache_key(address, "amap", "district")
    expected_district = requested_district(address)
    if cache.get(ckey):
        cached_district = cache.get(district_key, "")
        if not cached_district or district_matches(expected_district, cached_district):
            return cache[ckey]
        cache.pop(ckey, None)

    lookup_address = re.split(r"[】#]|坐地铁|倒公交|公交路线|地铁路线", address, maxsplit=1)[0].strip(" ,，")
    structured = lookup_address if "北京" in lookup_address else "北京市" + lookup_address
    data = amap_get("https://restapi.amap.com/v3/geocode/geo", {
        "key": key,
        "address": _utf8_prefix(structured, 128),
        "city": "北京市",
        "output": "JSON",
    })
    geocodes = data.get("geocodes") if isinstance(data.get("geocodes"), list) else []
    if str(data.get("status") or "") != "1" or not geocodes or not isinstance(geocodes[0], dict):
        return None

    geocode = geocodes[0]
    returned_district = _text_field(geocode.get("district"))
    if returned_district and not district_matches(expected_district, returned_district):
        cache[geocode_cache_key(address, "amap", "mismatch")] = returned_district
        return None
    try:
        longitude, latitude = map(float, str(geocode.get("location") or "").split(",", 1))
    except (TypeError, ValueError):
        return None
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return None
    if not _inside_beijing(longitude, latitude):
        cache[geocode_cache_key(address, "amap", "mismatch")] = f"outside_beijing:{longitude:.6f},{latitude:.6f}"
        return None

    loc = f"{longitude:.6f},{latitude:.6f}"
    cache[ckey] = loc
    cache[geocode_cache_key(address, "amap", "source")] = "amap_geocode_v3"
    cache[district_key] = returned_district or expected_district
    cache[geocode_cache_key(address, "amap", "level")] = _text_field(geocode.get("level"))
    return loc


def estimate_route_from_dest(dest, info=""):
    try:
        origin_lon, origin_lat = map(float, ORIGIN_COORD.split(","))
        dest_lon, dest_lat = map(float, dest.split(","))
    except (AttributeError, TypeError, ValueError):
        return None
    radius_km = 6371.0088
    lat1, lat2 = math.radians(origin_lat), math.radians(dest_lat)
    dlat = lat2 - lat1
    dlon = math.radians(dest_lon - origin_lon)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    straight_km = radius_km * 2 * math.asin(min(1.0, math.sqrt(value)))
    one_km = round(max(straight_km * 1.28, straight_km), 1)
    one_min = max(8, round(one_km / 25.0 * 60.0))
    one_taxi = estimate_one_way_taxi(one_km)
    return {
        "status": "estimated",
        "dest": dest,
        "one_km": one_km,
        "round_km": round(one_km * 2, 1),
        "one_min": one_min,
        "round_min": one_min * 2,
        "one_taxi": one_taxi,
        "round_taxi": round(one_taxi * 2, 1),
        "taxi_estimated": True,
        "route_estimated": True,
        "fallback_reason": info,
    }


def _baidu_direction_point(coord):
    longitude, latitude = map(float, str(coord).split(",", 1))
    # DirectionLite requires latitude,longitude, while both geocoders and the
    # internal cache store longitude,latitude.
    return f"{latitude:.6f},{longitude:.6f}"


def baidu_route(address, ak, cache):
    if not address:
        return {"status": "missing_address"}
    if "线上" in address:
        return {"status": "online", "one_km": 0, "round_km": 0, "one_min": 0, "round_min": 0, "one_taxi": 0, "round_taxi": 0}

    ckey = route_cache_key(address, "baidu")
    if ckey in cache and reusable_route(address, cache[ckey], "baidu", "baidu"):
        cached_route = cache[ckey]
        return cached_route
    if ckey in cache:
        cache.pop(ckey, None)
        clear_geocode_cache(cache, address, "baidu")

    dest = baidu_geocode(address, ak, cache)
    if not dest:
        return {"status": "geocode_failed", "route_provider": "baidu"}
    data = baidu_get("https://api.map.baidu.com/directionlite/v1/driving", {
        "origin": _baidu_direction_point(ORIGIN_COORD),
        "destination": _baidu_direction_point(dest),
        "coord_type": "gcj02",
        "ret_coordtype": "gcj02",
        "tactics": 0,
        "steps_info": 0,
        "ak": ak,
    })
    try:
        status = int(data.get("status", -1))
    except (TypeError, ValueError):
        status = -1
    result_data = data.get("result") if isinstance(data.get("result"), dict) else {}
    routes = result_data.get("routes") if isinstance(result_data.get("routes"), list) else []
    if status != 0 or not routes:
        info = str(data.get("message") or data.get("status") or "route_failed")
        fallback = estimate_route_from_dest(dest, info)
        if fallback:
            fallback["route_provider"] = "baidu_directionlite"
            fallback["geocode_source"] = cache.get(geocode_cache_key(address, "baidu", "source"), "baidu_cache")
            fallback = with_route_context(fallback, "baidu")
            cache[ckey] = fallback
            return fallback
        return {"status": "route_failed", "dest": dest, "info": info, "route_provider": "baidu"}

    selected = routes[0] if isinstance(routes[0], dict) else {}
    try:
        distance_m = float(selected.get("distance") or 0)
        duration_s = float(selected.get("duration") or 0)
    except (TypeError, ValueError):
        distance_m = 0
        duration_s = 0
    if distance_m <= 0 or duration_s <= 0:
        fallback = estimate_route_from_dest(dest, "invalid_route_metrics")
        if fallback:
            fallback["route_provider"] = "baidu_directionlite"
            fallback["geocode_source"] = cache.get(geocode_cache_key(address, "baidu", "source"), "baidu_cache")
            fallback = with_route_context(fallback, "baidu")
            cache[ckey] = fallback
            return fallback
        return {"status": "route_failed", "dest": dest, "info": "invalid_route_metrics", "route_provider": "baidu"}

    one_km = round(distance_m / 1000.0, 1)
    one_taxi = estimate_one_way_taxi(one_km)
    result = with_route_context({
        "status": "ok",
        "dest": dest,
        "one_km": one_km,
        "round_km": round(distance_m * 2 / 1000.0, 1),
        "one_min": round(duration_s / 60.0),
        "round_min": round(duration_s * 2 / 60.0),
        "one_taxi": one_taxi,
        "round_taxi": round(one_taxi * 2, 1),
        "taxi_estimated": True,
        "route_provider": "baidu_directionlite",
        "geocode_source": cache.get(geocode_cache_key(address, "baidu", "source"), "baidu_cache"),
    }, "baidu")
    cache[ckey] = result
    return result


def amap_route(address, key, cache):
    if not address:
        return {"status": "missing_address"}
    if "线上" in address:
        return {"status": "online", "one_km": 0, "round_km": 0, "one_min": 0, "round_min": 0, "one_taxi": 0, "round_taxi": 0}

    ckey = route_cache_key(address, "amap")
    if ckey in cache and reusable_route(address, cache[ckey], "amap", "amap"):
        return cache[ckey]
    if ckey in cache:
        cache.pop(ckey, None)
        clear_geocode_cache(cache, address, "amap")

    dest = amap_geocode(address, key, cache)
    if not dest:
        return {"status": "geocode_failed", "route_provider": "amap"}
    data = amap_get("https://restapi.amap.com/v5/direction/driving", {
        "key": key,
        "origin": normalized_origin_coord(),
        "destination": dest,
        "strategy": 32,
        "show_fields": "cost",
        "output": "JSON",
    })
    route = data.get("route") if isinstance(data.get("route"), dict) else {}
    paths = route.get("paths") if isinstance(route.get("paths"), list) else []
    if str(data.get("status") or "") != "1" or not paths:
        info = str(data.get("info") or data.get("infocode") or "route_failed")
        return {
            "status": "route_failed",
            "dest": dest,
            "info": info,
            "infocode": str(data.get("infocode") or ""),
            "route_provider": "amap",
        }

    selected = paths[0] if isinstance(paths[0], dict) else {}
    cost = selected.get("cost") if isinstance(selected.get("cost"), dict) else {}
    try:
        distance_m = float(selected.get("distance") or 0)
        duration_s = float(cost.get("duration") or selected.get("duration") or 0)
    except (TypeError, ValueError):
        distance_m = 0
        duration_s = 0
    if distance_m <= 0 or duration_s <= 0:
        return {
            "status": "route_failed",
            "dest": dest,
            "info": "invalid_route_metrics",
            "route_provider": "amap",
        }

    one_km = round(distance_m / 1000.0, 1)
    try:
        provider_taxi = float(route.get("taxi_cost") or 0)
    except (TypeError, ValueError):
        provider_taxi = 0
    taxi_estimated = provider_taxi <= 0
    one_taxi = round(provider_taxi, 1) if provider_taxi > 0 else estimate_one_way_taxi(one_km)
    result = with_route_context({
        "status": "ok",
        "dest": dest,
        "one_km": one_km,
        "round_km": round(distance_m * 2 / 1000.0, 1),
        "one_min": round(duration_s / 60.0),
        "round_min": round(duration_s * 2 / 60.0),
        "one_taxi": one_taxi,
        "round_taxi": round(one_taxi * 2, 1),
        "taxi_estimated": taxi_estimated,
        "route_provider": "amap_v5_driving",
        "geocode_source": cache.get(geocode_cache_key(address, "amap", "source"), "amap_cache"),
    }, "amap")
    cache[ckey] = result
    return result


def is_online_order(order):
    address = str(order.get("address", ""))
    raw = str(order.get("raw", ""))
    group = str(order.get("group", ""))
    return bool(
        re.search(r"线上|腾讯会议|网课|远程授课", address)
        or (
            not address
            and re.search(
                r"(?:上课方式|授课方式|教学方式)[】\]]?\s*[:：]?\s*线上|线上(?:单|授课|上课|教学|辅导|家教|课程)|网课|腾讯会议|远程授课",
                raw,
            )
        )
        or re.search(r"^线上网课群", group)
    )


def route_orders(orders, limit, cache_path):
    provider = str(MAP_PROVIDER or "baidu").strip().lower()
    if provider not in MAP_KEY_ENV:
        raise ValueError("map_provider 只能是 amap 或 baidu")
    key = str(os.environ.get(MAP_KEY_ENV[provider]) or "").strip()
    live_provider = provider if key else ""
    route_function = {"baidu": baidu_route, "amap": amap_route}[provider]
    cache = load_cache(cache_path)
    candidates = [o for o in orders if not o.get("hard_reasons") and (o.get("address") or is_online_order(o))]
    routed = 0
    address_groups = {}
    for order in candidates:
        if is_online_order(order):
            order["route"] = {"status": "online", "one_km": 0, "round_km": 0, "one_min": 0, "round_min": 0, "one_taxi": 0, "round_taxi": 0}
            routed += 1
            continue
        address_groups.setdefault(str(order["address"]).strip(), []).append(order)

    unresolved = []
    for address, group in address_groups.items():
        ckey = route_cache_key(address, provider)
        existing = next((
            order.get("route") for order in group
            if reusable_route(address, order.get("route"), live_provider, provider)
        ), None)
        cached = cache.get(ckey)
        if not reusable_route(address, cached, live_provider, provider):
            cached = None
        attempted = _route_attempt_results.get(ckey)
        result = existing or cached or attempted
        if result:
            result = with_route_context(result, provider)
            if result.get("status") in ("ok", "estimated"):
                cache[ckey] = result
            for order in group:
                order["route"] = dict(result)
            routed += len(group)
            continue
        unresolved.append((address, group))

    unresolved.sort(
        key=lambda item: max((order.get("rough_score", -9999) for order in item[1]), default=-9999),
        reverse=True,
    )
    if key and limit > 0:
        for address, group in unresolved[:limit]:
            ckey = route_cache_key(address, provider)
            try:
                result = route_function(address, key, cache)
            except Exception as exc:
                result = {"status": "error", "error": str(exc), "route_provider": provider}
            result = with_route_context(result, provider)
            _route_attempt_results[ckey] = result
            if result.get("status") in ("ok", "estimated"):
                cache[ckey] = result
            for order in group:
                order["route"] = dict(result)
            routed += len(group)
    save_cache(cache_path, cache)
    return routed


def final_score(order):
    if order["hard_reasons"]:
        order["tier"] = "硬排除"
        order["score"] = -9999
        return order["score"]
    score = subject_score(order["subject"], order["raw"]) + grade_score(order["grade"], order["raw"])
    hourly = order.get("hourly")
    duration = order.get("duration_h") or 2.0
    route_data = order.get("route") or {}
    online = is_online_order(order)
    if not order.get("address") and not online:
        score -= 35
    elif route_data.get("status") in ("geocode_failed", "route_failed", "error"):
        score -= 12
    elif not route_data and not online:
        score -= 10
    round_min = route_data.get("round_min")
    round_taxi = route_data.get("round_taxi")
    if hourly and route_data.get("status") in ("ok", "estimated", "online"):
        gross = hourly * duration
        total_h = duration + (round_min or 0) / 60.0
        information_fee = float(order.get("information_fee") or 0)
        lesson_count = order.get("total_lessons")
        allocated_fee = information_fee / float(lesson_count) if information_fee and lesson_count else 0
        net = (gross - (round_taxi or 0) - allocated_fee) / total_h if total_h else hourly
        order["net_hourly"] = round(net, 1)
        score += min(max((net - 60) / 2.4, 0), 55)
        if net < 30:
            score -= 35
        elif net < 45:
            score -= 22
        elif net < 60:
            score -= 10
        if round_min and round_min > 140:
            score -= 10
        elif round_min and round_min > 100:
            score -= 5
    elif hourly:
        order["net_hourly"] = None
        score += min(max((hourly - 80) / 3.0, 0), 45)
    else:
        order["net_hourly"] = None
        score -= 14
    freq = order.get("frequency")
    if freq:
        if 2 <= freq <= 4:
            score += 8
        elif freq == 1:
            score -= 3
        elif freq >= 5:
            score -= 4
    if any(x in order["raw"] for x in ("陪读", "写作业", "暑假作业", "督促作业", "检查作业", "作业陪读", "作业辅导", "辅导作业")):
        score += 15
    if any(x in order["raw"] for x in ("全天", "每天都要", "周一到周五", "一周7次")):
        score -= 10
    if order.get("notes"):
        score -= min(len(order["notes"]) * 2, 8)
    order["score"] = round(score, 2)
    if score >= 78:
        tier = "优先投"
    elif score >= 60:
        tier = "可投"
    elif score >= 40:
        tier = "备选"
    else:
        tier = "不优先"
    order["tier"] = tier
    return order["score"]


def compact_reason(order):
    parts = []
    if order["hard_reasons"]:
        return "；".join(order["hard_reasons"])
    if re.search(r"数学|物理|数理|微积分|高数|calculus", order["subject"], flags=re.I):
        parts.append("科目匹配")
    if order.get("net_hourly"):
        parts.append(f"净{order['net_hourly']}/h")
    elif order.get("hourly"):
        parts.append(f"表面{order['hourly']:.0f}/h")
    r = order.get("route") or {}
    if r.get("status") == "ok":
        parts.append(f"往返{r.get('round_km')}km/{r.get('round_min')}min")
    elif r.get("status") == "estimated":
        parts.append(f"通勤估算{r.get('round_km')}km/{r.get('round_min')}min")
    elif r.get("status") == "online":
        parts.append("线上无通勤")
    if any(x in order.get("raw", "") for x in ("陪读", "写作业", "暑假作业", "督促作业", "检查作业", "作业陪读", "作业辅导", "辅导作业")):
        parts.append("陪读/作业辅导优先")
    if order.get("notes"):
        parts.extend(order["notes"][:2])
    return "；".join(parts[:5])


def row_for_csv(order, rank=None):
    r = order.get("route") or {}
    return {
        "rank": rank or "",
        "tier": order.get("tier", ""),
        "score": order.get("score", ""),
        "id": order["id"],
        "posted_at": order["posted_at"],
        "groups": ",".join(order.get("groups", [order["group"]])),
        "sender": order["sender"],
        "grade": order["grade"],
        "subject": order["subject"],
        "address": order["address"],
        "schedule": order["schedule"],
        "hourly": "" if order.get("hourly") is None else round(order["hourly"], 1),
        "pay_raw": order["pay_raw"],
        "duration_h": order.get("duration_h"),
        "frequency": order.get("frequency") or "",
        "net_hourly": order.get("net_hourly") or "",
        "round_km": r.get("round_km", ""),
        "round_min": r.get("round_min", ""),
        "round_taxi": r.get("round_taxi", ""),
        "route_status": r.get("status", ""),
        "geocode_source": r.get("geocode_source", ""),
        "hard_reasons": "；".join(order["hard_reasons"]),
        "notes": "；".join(order["notes"]),
        "reason": compact_reason(order),
        "raw": order["raw"],
    }


def spreadsheet_safe(value):
    """Keep untrusted chat text from becoming a spreadsheet formula."""
    if isinstance(value, str) and re.match(r"^[\s\x00-\x1f]*[=+\-@]", value):
        return "'" + value
    return value


def write_csv(path: Path, orders):
    rows = [row_for_csv(o, i + 1) for i, o in enumerate(orders)]
    rows = [
        {key: spreadsheet_safe(value) for key, value in row.items()}
        for row in rows
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["rank"])
        writer.writeheader()
        writer.writerows(rows)


def md_table(orders, max_rows=30):
    headers = ["序", "级别", "单号", "群", "时间", "年级/科目", "地址", "报价", "净时薪", "距离/通勤/车费", "理由"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for i, o in enumerate(orders[:max_rows], 1):
        r = o.get("route") or {}
        commute = ""
        if r.get("status") == "ok":
            commute = f"{r.get('round_km')}km / {r.get('round_min')}min / {r.get('round_taxi')}元"
        elif r.get("status") == "estimated":
            commute = f"估 {r.get('round_km')}km / {r.get('round_min')}min / {r.get('round_taxi')}元"
        elif r.get("status") == "online":
            commute = "线上"
        elif r.get("status"):
            commute = r.get("status")
        pay = o["pay_raw"] or ("协商/未写" if o.get("hourly") is None else f"{o['hourly']:.0f}/h")
        grade_subject = clean_spaces((o["grade"] + " " + o["subject"]).strip())
        vals = [
            str(i),
            o.get("tier", ""),
            o["id"],
            ",".join(o.get("groups", [o["group"]])),
            o["posted_at"][11:16],
            grade_subject,
            o["address"],
            pay,
            "" if o.get("net_hourly") is None else f"{o['net_hourly']}/h",
            commute,
            compact_reason(o),
        ]
        vals = [str(v).replace("|", "/").replace("\n", " ") for v in vals]
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def write_markdown(path: Path, orders, stats):
    viable = [o for o in orders if o.get("tier") in ("优先投", "可投")]
    backup = [o for o in orders if o.get("tier") == "备选"]
    hard = [o for o in orders if o.get("tier") == "硬排除"]
    low = [o for o in orders if o.get("tier") == "不优先"]
    lines = [
        f"# {TODAY} 今日家教单优先级排序",
        "",
        f"- 今日消息数: {stats['messages']}",
        f"- 抽取订单块: {stats['blocks']}",
        f"- 去重后订单: {stats['unique']}",
        f"- 硬排除: {len(hard)}",
        f"- 已调用/标注通勤: {stats['routed']}",
        f"- 出发点: {ORIGIN_NAME} ({ORIGIN_COORD})",
        "",
        "## 优先投 / 可投",
        "",
        md_table(viable, 40) if viable else "无",
        "",
        "## 备选",
        "",
        md_table(backup, 30) if backup else "无",
        "",
        "## 不优先",
        "",
        md_table(low, 25) if low else "无",
        "",
        "## 硬排除摘要",
        "",
        md_table(hard, 40) if hard else "无",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_html(path: Path, orders):
    rows = []
    for i, o in enumerate(orders, 1):
        r = row_for_csv(o, i)
        rows.append(r)
    cols = ["rank", "tier", "id", "groups", "posted_at", "grade", "subject", "address", "hourly", "net_hourly", "round_km", "round_min", "round_taxi", "reason"]
    labels = {
        "rank": "序", "tier": "级别", "id": "单号/关键词", "groups": "群", "posted_at": "发布时间",
        "grade": "年级", "subject": "科目", "address": "地址", "hourly": "时薪", "net_hourly": "净时薪",
        "round_km": "往返 km", "round_min": "往返 min", "round_taxi": "往返车费", "reason": "判断",
    }
    thead = "".join(f"<th data-key='{c}'>{html.escape(labels[c])}</th>" for c in cols)
    body = []
    for row in rows:
        tds = "".join(f"<td>{html.escape(str(row.get(c, '')))}</td>" for c in cols)
        body.append(f"<tr data-tier='{html.escape(str(row.get('tier', '')), quote=True)}'>{tds}</tr>")
    doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{TODAY} 今日家教单排序</title>
<style>
:root {{ color-scheme:dark; --bg:#0f1210; --surface:#181d1a; --surface2:#232a25; --line:#39443d; --ink:#eef4ef; --muted:#9eada3; --green:#62cf9a; --amber:#e7b45b; --red:#ff8178; }}
* {{ box-sizing:border-box }}
body {{ font-family:system-ui,-apple-system,'Segoe UI','Microsoft YaHei',sans-serif; margin:0; background:var(--bg); color:var(--ink); }}
header {{ padding:18px 22px 12px; background:var(--surface); border-bottom:1px solid var(--line); }}
h1 {{ margin:0 0 4px; max-width:100%; font-size:20px; line-height:1.35; letter-spacing:0; white-space:normal; overflow-wrap:anywhere; }}
.meta {{ max-width:100%; color:var(--muted); font-size:13px; white-space:normal; overflow-wrap:anywhere; }}
.bar {{ position:sticky; top:0; z-index:3; display:flex; gap:8px; flex-wrap:wrap; padding:12px 22px; background:var(--surface); border-bottom:1px solid var(--line); }}
input,select {{ height:36px; padding:0 10px; color:var(--ink); background:#101512; border:1px solid #526057; border-radius:5px; }}
input {{ width:min(360px,100%); }}
main {{ padding:16px 22px 28px; overflow:auto; }}
table {{ border-collapse:collapse; width:100%; min-width:1320px; font-size:13px; background:var(--surface); }}
th,td {{ border-bottom:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; }}
th {{ background:var(--surface2); cursor:pointer; position:sticky; top:0; z-index:2; white-space:nowrap; }}
th[data-direction='asc']::after {{ content:' ↑'; color:var(--green); }}
th[data-direction='desc']::after {{ content:' ↓'; color:var(--green); }}
tbody tr:hover {{ background:#263029; }}
tr[data-tier='优先投'] {{ border-left:4px solid var(--green); }}
tr[data-tier='可投'] {{ border-left:4px solid #70b7c2; }}
tr[data-tier='备选'] {{ border-left:4px solid var(--amber); }}
tr[data-tier='硬排除'] {{ border-left:4px solid var(--red); opacity:.75; }}
@media(max-width:700px) {{ header,.bar,main {{ padding-left:12px; padding-right:12px; }} input {{ width:100%; }} }}
</style>
</head>
<body>
<header><h1>{TODAY} 今日家教单排序</h1><div class="meta">本地私有报告，请勿直接公开分享 · {len(rows)} 个去重订单 · 点击表头可升降序排序</div></header>
<div class="bar">
  <input id="q" placeholder="搜索单号、地址、科目、理由">
  <select id="tier"><option value="">全部级别</option><option>优先投</option><option>可投</option><option>备选</option><option>不优先</option><option>硬排除</option></select>
</div>
<main><table id="t"><thead><tr>{thead}</tr></thead><tbody>{''.join(body)}</tbody></table></main>
<script>
const table = document.querySelector('#t');
for (const th of table.tHead.rows[0].cells) {{
  th.addEventListener('click', () => {{
    const idx = th.cellIndex;
    const rows = Array.from(table.tBodies[0].rows);
    const numeric = ['rank','hourly','net_hourly','round_km','round_min','round_taxi'].includes(th.dataset.key);
    const direction = th.dataset.direction === 'asc' ? 'desc' : 'asc';
    for (const other of table.tHead.rows[0].cells) delete other.dataset.direction;
    th.dataset.direction = direction;
    rows.sort((a,b) => {{
      const av = a.cells[idx].textContent.trim();
      const bv = b.cells[idx].textContent.trim();
      const compared = numeric
        ? ((Number.isFinite(parseFloat(av)) ? parseFloat(av) : -Infinity) - (Number.isFinite(parseFloat(bv)) ? parseFloat(bv) : -Infinity))
        : av.localeCompare(bv, 'zh-Hans-CN');
      return direction === 'asc' ? compared : -compared;
    }});
    rows.forEach(r => table.tBodies[0].appendChild(r));
  }});
}}
function filter() {{
  const q = document.querySelector('#q').value.toLowerCase();
  const tier = document.querySelector('#tier').value;
  for (const tr of table.tBodies[0].rows) {{
    const text = tr.textContent.toLowerCase();
    const okq = !q || text.includes(q);
    const okt = !tier || tr.cells[1].textContent === tier;
    tr.style.display = okq && okt ? '' : 'none';
  }}
}}
document.querySelector('#q').addEventListener('input', filter);
document.querySelector('#tier').addEventListener('change', filter);
</script>
</body></html>"""
    path.write_text(doc, encoding="utf-8")


def main():
    global TODAY
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", default=[], help="你有权处理的 Markdown；可重复传入")
    parser.add_argument("--date", default=TODAY, help="只处理这个日期，格式 YYYY-MM-DD")
    parser.add_argument("--out-dir", default="outputs")
    parser.add_argument("--cache", default="work/route_cache.json")
    parser.add_argument(
        "--route-limit",
        type=int,
        default=0,
        help="新线下地址的地图上限；大于 0 且配置所选供应商 Key 时会向该地图服务发送地址",
    )
    parser.add_argument("--map-provider", choices=("amap", "baidu"), default="baidu")
    parser.add_argument("--origin-name", default=ORIGIN_NAME)
    parser.add_argument("--origin-coord", default=ORIGIN_COORD, help="GCJ-02 经度,纬度")
    args = parser.parse_args()

    TODAY = args.date
    configure_runtime({
        "origin_name": args.origin_name,
        "origin_coord": args.origin_coord,
        "map_provider": args.map_provider,
    })
    input_files = args.input or FILES
    if not input_files:
        parser.error("请至少提供一个 --input 文件；实时监控请运行 monitor.py")
    if args.route_limit > 0 and not ORIGIN_COORD:
        parser.error("启用通勤计算时必须提供 --origin-coord 经度,纬度")

    messages = []
    for f in input_files:
        p = Path(f)
        if p.exists():
            messages.extend(read_messages(f))
    blocks = []
    orders = []
    for msg in messages:
        for block in split_blocks(msg):
            blocks.append(block)
            orders.append(make_order(msg, block))
    unique = dedupe_orders(orders)
    routed = route_orders(unique, args.route_limit, Path(args.cache))
    for order in unique:
        final_score(order)
    unique.sort(key=lambda o: (o.get("score", -9999), o.get("rough_score", -9999)), reverse=True)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {"messages": len(messages), "blocks": len(blocks), "unique": len(unique), "routed": routed}
    json_path = out_dir / f"today_tutor_rank_{TODAY}.json"
    csv_path = out_dir / f"today_tutor_rank_{TODAY}.csv"
    md_path = out_dir / f"today_tutor_rank_{TODAY}.md"
    html_path = out_dir / f"today_tutor_rank_{TODAY}.html"
    json_path.write_text(json.dumps({"stats": stats, "orders": unique}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(csv_path, unique)
    write_markdown(md_path, unique, stats)
    write_html(html_path, unique)
    print(json.dumps({
        "stats": stats,
        "outputs": {
            "json": str(json_path),
            "csv": str(csv_path),
            "md": str(md_path),
            "html": str(html_path),
        },
        "top": [
            {
                "rank": i + 1,
                "id": o["id"],
                "tier": o.get("tier"),
                "subject": o["subject"],
                "grade": o["grade"],
                "address": o["address"],
                "hourly": o.get("hourly"),
                "net_hourly": o.get("net_hourly"),
                "route": o.get("route"),
                "reason": compact_reason(o),
            }
            for i, o in enumerate(unique[:12])
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
