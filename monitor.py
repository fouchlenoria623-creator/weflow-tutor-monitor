from __future__ import annotations

import argparse
import csv
import hashlib
import html
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import ranker


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("TUTOR_MONITOR_DATA_DIR", ROOT))
STATE_DIR = DATA_DIR / "state"
REPORT_DIR = DATA_DIR / "reports"
CONFIG_PATH = Path(os.environ.get("TUTOR_MONITOR_CONFIG", ROOT / "config.local.json"))
WEFLOW_CONFIG = Path(os.environ.get(
    "WEFLOW_CONFIG_PATH",
    Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "weflow" / "WeFlow-config.json",
))
WEFLOW_EXE = Path(os.environ.get(
    "WEFLOW_EXE_PATH",
    Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Programs" / "WeFlow" / "WeFlow.exe",
))

DEFAULT_CONFIG = {
    "active_hours": {"start": 10, "end": 21},
    "scan_interval_minutes": 60,
    "first_run_lookback_hours": 24,
    "keep_leads_days": 7,
    "route_limit_per_run": 40,
    "map_routing_enabled": False,
    "map_provider": "baidu",
    "origin_name": "",
    "origin_coord": "",
    "subject_weights": {},
    "unsupported_subjects": [],
    "tutor_profile": {"gender": "", "school_tags": [], "school_names": []},
    "athlete_profile": {},
    "online_priority_bonus": 35,
    "online_only_default": False,
    "priority_only_default": True,
    "notify_online_only": False,
    "notification_include_address": False,
    "notify_tiers": ["线上优先", "优先投"],
    "include_name_patterns": ["家教", "接单"],
    "exclude_group_ids": [],
    "include_group_ids": [],
    "include_groups": {},
    "group_reports": [],
}


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_config(path: Path = CONFIG_PATH, *, require_origin=True):
    if not path.exists():
        raise FileNotFoundError(
            f"未找到本地配置：{path}。请复制 config.example.json 为 config.local.json 后填写。"
        )
    user = load_json(path, None)
    if not isinstance(user, dict):
        raise ValueError(f"配置不是有效的 JSON 对象：{path}")
    config = {**DEFAULT_CONFIG, **user}
    config["active_hours"] = {**DEFAULT_CONFIG["active_hours"], **(user.get("active_hours") or {})}
    config["tutor_profile"] = {**DEFAULT_CONFIG["tutor_profile"], **(user.get("tutor_profile") or {})}

    start = int(config["active_hours"]["start"])
    end = int(config["active_hours"]["end"])
    interval = int(config["scan_interval_minutes"])
    if not (0 <= start <= 23 and 0 <= end <= 23 and start <= end):
        raise ValueError("active_hours 必须满足 0 <= start <= end <= 23")
    if interval < 15 or interval > 1440:
        raise ValueError("scan_interval_minutes 必须在 15 到 1440 之间")
    if not isinstance(config.get("include_name_patterns"), list):
        raise ValueError("include_name_patterns 必须是 JSON 数组")
    try:
        for pattern in config["include_name_patterns"]:
            re.compile(str(pattern))
    except re.error as exc:
        raise ValueError(f"include_name_patterns 含无效正则：{exc}") from exc

    gender = str(config["tutor_profile"].get("gender") or "").lower()
    if gender not in {"", "male", "female"}:
        raise ValueError("tutor_profile.gender 只能是 male、female 或空字符串")
    if not isinstance(config.get("map_routing_enabled"), bool):
        raise ValueError("map_routing_enabled 必须是 true 或 false")
    provider = str(config.get("map_provider") or "baidu").strip().lower()
    if provider not in ranker.MAP_KEY_ENV:
        raise ValueError("map_provider 只能是 amap 或 baidu")
    config["map_provider"] = provider
    coord = str(config.get("origin_coord") or "").strip()
    if (require_origin and config.get("map_routing_enabled")) or coord:
        try:
            longitude, latitude = map(float, coord.split(",", 1))
        except (TypeError, ValueError):
            raise ValueError("origin_coord 必须填写为 GCJ-02 经度,纬度，例如 116.4074,39.9042") from None
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise ValueError("origin_coord 的经纬度超出有效范围")
    ranker.configure_runtime(config)
    return config


def _require_loopback(base: str):
    parsed = urllib.parse.urlparse(base)
    hostname = parsed.hostname or ""
    if parsed.scheme != "http" or not hostname:
        raise ValueError("WeFlow API 地址必须是本机 HTTP 地址")
    try:
        local = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        local = hostname.lower() == "localhost"
    if not local:
        raise ValueError("拒绝连接非本机 WeFlow API；请使用 127.0.0.1 或 localhost")
    return base.rstrip("/")


def weflow_settings():
    data = load_json(WEFLOW_CONFIG, {})
    configured_base = os.environ.get("WEFLOW_API_BASE")
    base = configured_base or f"http://{data.get('httpApiHost', '127.0.0.1')}:{data.get('httpApiPort', 5031)}"
    return {
        "base": _require_loopback(base),
        "token": os.environ.get("WEFLOW_API_TOKEN") or data.get("httpApiToken", ""),
    }


def request_json(path: str, params=None, timeout=90):
    settings = weflow_settings()
    query = urllib.parse.urlencode(params or {})
    url = settings["base"] + path + (("?" + query) if query else "")
    headers = {"Authorization": f"Bearer {settings['token']}"} if settings["token"] else {}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def ensure_api():
    try:
        return request_json("/health", timeout=4)
    except ValueError:
        raise
    except Exception:
        if WEFLOW_EXE.exists():
            subprocess.Popen([str(WEFLOW_EXE)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            time.sleep(1)
            try:
                return request_json("/health", timeout=3)
            except Exception:
                pass
    raise RuntimeError("WeFlow API 未启动。请打开 WeFlow，并在设置中确认 API 服务已开启。")


def is_tutor_group(session, config):
    gid = str(session.get("username", ""))
    name = str(session.get("displayName", "")).strip()
    if not gid.endswith("@chatroom") and session.get("type") != 2:
        return False
    if gid in config.get("exclude_group_ids", []):
        return False
    if gid in config.get("include_group_ids", []):
        return True
    return any(re.search(pattern, name, flags=re.I) for pattern in config["include_name_patterns"])


def local_label(name):
    name = str(name).strip()
    return f"家教-{int(name):02d}" if re.fullmatch(r"\d{1,2}", name) else name


def write_group_labels(groups):
    path = STATE_DIR / "group-labels.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["group_id", "weflow_name", "local_label", "last_timestamp"])
        writer.writeheader()
        for group in groups:
            row = {
                "group_id": group["username"],
                "weflow_name": group.get("displayName", ""),
                "local_label": local_label(group.get("displayName", "")),
                "last_timestamp": group.get("lastTimestamp", ""),
            }
            writer.writerow({key: ranker.spreadsheet_safe(value) for key, value in row.items()})


def write_group_catalog(sessions):
    path = STATE_DIR / "group-catalog.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    groups = [
        session for session in sessions
        if str(session.get("username", "")).endswith("@chatroom") or session.get("type") == 2
    ]
    groups.sort(key=lambda item: str(item.get("displayName", "")))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["index", "display_name", "group_id", "last_timestamp"])
        writer.writeheader()
        for index, group in enumerate(groups, 1):
            row = {
                "index": index,
                "display_name": group.get("displayName", ""),
                "group_id": group.get("username", ""),
                "last_timestamp": group.get("lastTimestamp", ""),
            }
            writer.writerow({key: ranker.spreadsheet_safe(value) for key, value in row.items()})
    return len(groups)


def content_of(message):
    for key in ("parsedContent", "content", "rawContent", "displayContent", "text"):
        value = message.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in {"[图片]", "[视频]", "[表情]"}:
            return value.strip()
    return ""


def message_key(group_id, message):
    raw_id = message.get("serverId") or message.get("localId")
    if raw_id:
        return f"{group_id}:{raw_id}"
    seed = f"{group_id}|{message.get('createTime')}|{content_of(message)}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def fetch_changed_messages(groups, state, config):
    first_since = int((datetime.now() - timedelta(hours=config["first_run_lookback_hours"])).timestamp())
    messages = []
    changed_groups = 0
    for group in groups:
        gid = group["username"]
        latest = int(group.get("lastTimestamp") or 0)
        previous = int(state.get("last_seen", {}).get(gid) or first_since)
        if latest and latest <= previous:
            continue
        changed_groups += 1
        request_limit = 10000
        data = request_json("/api/v1/messages", {
            "talker": gid,
            "start": max(first_since, previous - 2),
            "limit": request_limit,
        }, timeout=120)
        items = data.get("messages")
        if not isinstance(items, list):
            raise RuntimeError("WeFlow messages 响应缺少 messages 数组，未推进增量游标")
        if len(items) >= request_limit:
            raise RuntimeError(f"群消息达到单次上限 {request_limit}，为避免漏单已停止且未推进游标")
        if latest > previous and not items:
            # The session says there are new messages but the message endpoint
            # has not delivered them yet. Retry next run without skipping.
            continue
        observed = []
        for item in items:
            created = int(item.get("createTime") or 0)
            if created:
                observed.append(created)
            key = message_key(gid, item)
            if created >= previous and key not in state.get("processed_messages", []):
                messages.append((group, item, key))
        if observed:
            state.setdefault("last_seen", {})[gid] = max(previous, max(observed))
    return messages, changed_groups


def extract_orders(message_rows):
    orders = []
    processed = []
    for group, item, key in message_rows:
        content = content_of(item)
        if not content:
            processed.append(key)
            continue
        stamp = datetime.fromtimestamp(int(item.get("createTime") or time.time()))
        envelope = {
            "file": "WeFlow API",
            "group": local_label(group.get("displayName", "")),
            "date": stamp.strftime("%Y-%m-%d"),
            "time": stamp.strftime("%H:%M:%S"),
            "sender": item.get("senderUsername", ""),
            "body": content,
        }
        for block in ranker.split_blocks(envelope):
            order = ranker.make_order(envelope, block)
            order["source_message_key"] = key
            order["source_group_id"] = group.get("username", "")
            order["source_group_ids"] = [group.get("username", "")]
            order["sender_username"] = item.get("senderUsername", "")
            order["groups"] = [envelope["group"]]
            orders.append(order)
        processed.append(key)
    return orders, processed


def order_key(order):
    if not order["id"].startswith("AUTO-"):
        return ranker.normalize_key(order["id"])
    return ranker.auto_dedupe_key(order)


def merge_history(old_orders, new_orders, keep_days):
    cutoff = datetime.now() - timedelta(days=keep_days)
    merged = {}
    for order in old_orders + new_orders:
        try:
            posted = datetime.strptime(order["posted_at"], "%Y-%m-%d %H:%M:%S")
        except (KeyError, ValueError):
            continue
        if posted < cutoff:
            continue
        key = order_key(order)
        current = merged.get(key)
        if current is None:
            order.setdefault("groups", [order.get("group", "")])
            order.setdefault("source_group_ids", [order.get("source_group_id", "")])
            merged[key] = order
            continue
        groups = sorted({x for x in current.get("groups", [current.get("group", "")]) + order.get("groups", [order.get("group", "")]) if x})
        group_ids = sorted({x for x in current.get("source_group_ids", [current.get("source_group_id", "")]) + order.get("source_group_ids", [order.get("source_group_id", "")]) if x})
        chosen = order if order.get("posted_at", "") >= current.get("posted_at", "") else current
        chosen["groups"] = groups
        chosen["source_group_ids"] = group_ids
        merged[key] = chosen
    return list(merged.values())


def score_orders(orders, config):
    ranker.TODAY = datetime.now().strftime("%Y-%m-%d")
    ranker.configure_runtime(config)
    # Rebuild normalized fields so parser/filter improvements also repair saved history.
    rebuilt = []
    for order in orders:
        try:
            stamp = datetime.strptime(order["posted_at"], "%Y-%m-%d %H:%M:%S")
        except (KeyError, ValueError):
            stamp = datetime.now()
        envelope = {
            "file": order.get("source_file", "WeFlow API"),
            "group": order.get("group", ""),
            "date": stamp.strftime("%Y-%m-%d"),
            "time": stamp.strftime("%H:%M:%S"),
            "sender": order.get("sender", ""),
            "body": order.get("raw", ""),
        }
        blocks = ranker.split_blocks(envelope) or [order.get("raw", "")]
        for block in blocks:
            fresh = ranker.make_order(envelope, block)
            for key in ("source_message_key", "source_group_id", "source_group_ids", "sender_username", "duplicate_sources", "groups"):
                if key in order:
                    fresh[key] = order[key]
            apply_user_subject_fit(fresh, config)
            rebuilt.append(fresh)
    orders[:] = merge_history([], rebuilt, config.get("keep_leads_days", 7))
    today_orders = [order for order in orders if str(order.get("posted_at", "")).startswith(ranker.TODAY)]
    route_limit = int(config["route_limit_per_run"]) if config.get("map_routing_enabled") else 0
    ranker.route_orders(today_orders, route_limit, STATE_DIR / "route_cache.json")
    for order in orders:
        ranker.final_score(order)
        apply_user_background_fit(order, config)
        apply_online_priority(order, config)
    orders.sort(
        key=lambda o: (
            1 if o.get("delivery_mode") == "online" and not o.get("hard_reasons") else 0,
            o.get("score", -9999),
            o.get("rough_score", -9999),
        ),
        reverse=True,
    )


def apply_user_subject_fit(order, config):
    subject = order.get("subject", "")
    raw = order.get("raw", "")
    subject_scope = subject + "\n" + raw
    for unsupported in config.get("unsupported_subjects", []):
        aliases = {
            "化学": ("化学", "数理化", "物化生", "物化地", "物化", "理化"),
        }.get(unsupported, (unsupported,))
        if not any(alias in subject_scope for alias in aliases):
            continue
        can_split = re.search(r"(?:可分开|分开找|分别找|各找|各一位|分科)", raw)
        supported = [
            str(name) for name, weight in (config.get("subject_weights") or {}).items()
            if name != unsupported and isinstance(weight, (int, float)) and weight > 0
        ]
        has_supported_part = any(name in subject for name in supported)
        if can_split and has_supported_part:
            order.setdefault("notes", []).append(f"仅投非{unsupported}科目")
        else:
            reason = f"包含{unsupported}（不教）"
            if reason not in order.setdefault("hard_reasons", []):
                order["hard_reasons"].append(reason)


def apply_user_background_fit(order, config):
    profile = config.get("athlete_profile") or {}
    if not profile or order.get("hard_reasons"):
        return

    subject = str(order.get("subject") or "")
    general_subjects = tuple(str(item) for item in profile.get("general_subjects", []))
    if not general_subjects or not any(item in subject for item in general_subjects):
        return

    specific_subjects = tuple(str(item) for item in profile.get("specific_subjects", []))
    if any(item in subject for item in specific_subjects):
        return

    bonus = float(profile.get("general_bonus", 0))
    one_km = (order.get("route") or {}).get("one_km")
    if one_km is not None and float(one_km) <= float(profile.get("nearby_km", 0)):
        bonus += float(profile.get("nearby_bonus", 0))

    order["score"] = round(float(order.get("score", 0)) + bonus, 2)
    level = str(profile.get("level") or "运动员")
    note = f"{level}背景匹配通用体能"
    if note not in order.setdefault("notes", []):
        order["notes"].append(note)
    if "体育生" in str(order.get("raw") or ""):
        requirement_note = f"投递时说明{level}资质"
        if requirement_note not in order["notes"]:
            order["notes"].append(requirement_note)

    score = order["score"]
    if score >= 78:
        order["tier"] = "优先投"
    elif score >= 60:
        order["tier"] = "可投"
    elif score >= 40:
        order["tier"] = "备选"
    else:
        order["tier"] = "不优先"


def is_online_order(order):
    route = order.get("route") or {}
    if route.get("status") == "online":
        return True
    return ranker.is_online_order(order)


def apply_online_priority(order, config):
    online = is_online_order(order)
    order["delivery_mode"] = "online" if online else "offline"
    if not online or order.get("hard_reasons"):
        return
    if order.get("hourly") is not None and order.get("net_hourly") is None:
        order["net_hourly"] = round(float(order["hourly"]), 1)
    if not order.get("route"):
        order["route"] = {"status": "online", "round_km": 0, "round_min": 0, "round_taxi": 0}
    score = float(order.get("score", 0)) + float(config.get("online_priority_bonus", 35))
    order["score"] = round(score, 2)
    if "线上单优先" not in order.setdefault("notes", []):
        order["notes"].append("线上单优先")
    if score >= 78:
        order["tier"] = "线上优先"
    elif score >= 60:
        order["tier"] = "线上可投"
    else:
        order["tier"] = "线上备选"


def one_way(order, field):
    value = (order.get("route") or {}).get(field)
    return round(float(value) / 2, 1) if value not in (None, "") else ""


def resolve_sender_names(orders):
    cache_path = STATE_DIR / "sender_names.json"
    cache = load_json(cache_path, {"names": {}})
    names = cache.setdefault("names", {})
    pending = {}
    for order in orders:
        sender = str(order.get("sender_username") or order.get("sender") or "")
        group_id = str(order.get("source_group_id") or "")
        if not group_id:
            match = re.match(r"([^:]+@chatroom):", str(order.get("source_message_key") or ""))
            group_id = match.group(1) if match else ""
        if not sender or not group_id:
            continue
        order["sender_username"] = sender
        order["source_group_id"] = group_id
        key = f"{group_id}|{sender}"
        if key not in names:
            pending.setdefault(group_id, set()).add(sender)

    for group_id in pending:
        try:
            data = request_json("/api/v1/group-members", {"chatroomId": group_id}, timeout=45)
        except Exception:
            continue
        for member in data.get("members", []):
            username = str(member.get("wxid") or "")
            if not username:
                continue
            display = next((str(member.get(field) or "").strip() for field in (
                "groupNickname", "displayName", "remark", "nickname", "alias"
            ) if str(member.get(field) or "").strip()), username)
            names[f"{group_id}|{username}"] = display

    for order in orders:
        sender = str(order.get("sender_username") or order.get("sender") or "")
        group_id = str(order.get("source_group_id") or "")
        display = names.get(f"{group_id}|{sender}")
        if display:
            order["sender"] = display
    cache["updated_at"] = datetime.now().isoformat()
    save_json(cache_path, cache)


def wechat_search_keyword(order):
    raw = str(order.get("raw") or "").strip()
    order_id = str(order.get("id") or "").strip()
    if order_id and not order_id.startswith("AUTO-") and order_id in raw:
        return order_id
    fallback_id = re.search(r"\b([A-Z]{1,4}\d{6,}[A-Z0-9-]*)\b", raw, flags=re.I)
    if fallback_id:
        return fallback_id.group(1)
    match = re.search(r"(?:线上利智|利智)\s*\d{3,}", raw)
    if match:
        return clean_search_text(match.group(0))
    lines = [clean_search_text(line) for line in raw.splitlines() if clean_search_text(line)]
    if lines and not re.match(r"^(?:学科|地址|位置|年级|科目|时间|费用|薪资)\s*[:：]", lines[0]):
        return lines[0][:48]
    for line in lines:
        if re.match(r"^(?:地址|位置|补习地址)\s*[:：]", line) and len(line) >= 8:
            return line[:48]
    return (lines[0] if lines else f"{order.get('grade', '')} {order.get('subject', '')} {order.get('pay_raw', '')}").strip()[:48]


def extract_search_keyword(order):
    raw = str(order.get("raw") or "").strip()
    if not raw:
        return ""

    order_id = str(order.get("id") or "").strip()
    if order_id and not order_id.startswith("AUTO-"):
        return order_id

    labelled_match = re.search(
        r"(?:订单编号|订单号|单号|编号|编号[:：]\s*|订单编号[:：]\s*)\s*([A-Za-z0-9][A-Za-z0-9_-]{4,})",
        raw,
        flags=re.I,
    )
    if labelled_match:
        return clean_search_text(labelled_match.group(1))

    extracted_id = ranker.extract_id(raw)[0]
    if extracted_id and not extracted_id.startswith("AUTO-"):
        return clean_search_text(extracted_id)

    fallback_id = re.search(r"\b([A-Z]{1,4}\d{3,}[A-Z0-9-]*)\b", raw, flags=re.I)
    if fallback_id:
        return clean_search_text(fallback_id.group(1))

    lines = [clean_search_text(line) for line in raw.splitlines() if clean_search_text(line)]
    for line in lines[:3]:
        if (
            len(line) > 0
            and not is_low_signal_search_line(line)
            and not re.match(r"^(?:科目|地址|地点|年级|课时|报酬|时长|要求|职位)\s*[:：]", line)
        ):
            return line[:48]

    structured_parts = []
    for value in (order.get("address"), order.get("subject"), order.get("pay_raw")):
        part = clean_search_text(value or "")
        if part and part not in structured_parts:
            structured_parts.append(part)
    if structured_parts:
        return " ".join(structured_parts)[:48]

    for line in lines:
        if re.match(r"^(?:地址|地点|上门地址)\s*[:：]", line) and len(line) >= 8:
            return line[:48]
    for line in lines:
        if not is_low_signal_search_line(line):
            return line[:48]

    return f"{order.get('grade', '')} {order.get('subject', '')} {order.get('pay_raw', '')}".strip()[:48]


def clean_search_text(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def is_low_signal_search_line(value):
    compact = re.sub(r"\s+", "", str(value))
    return bool(re.fullmatch(r"(?:19|20)\d{2}|\d{1,4}", compact))


def dashboard(orders, new_keys, stats, output_path=None, include_hard=False):
    visible = list(orders) if include_hard else [o for o in orders if o.get("tier") != "硬排除"]
    visible_note = f" · 页面保留 {len(visible)} 单" if len(visible) != stats.get("total_orders") else ""
    source_notes = []
    if any((order.get("route") or {}).get("status") == "estimated" for order in visible):
        source_notes.append("路线 API 不可用时以直线距离折算通勤，页面会明确标注估算")
    source_note_html = f"<div class='source-note'>{' · '.join(source_notes)}</div>" if source_notes else ""
    rows = []
    for index, order in enumerate(visible, 1):
        route = order.get("route") or {}
        is_new = order.get("source_message_key") in new_keys
        cells = {
            "rank": index, "tier": order.get("tier", ""), "new": "新" if is_new else "",
            "mode": "线上" if order.get("delivery_mode") == "online" else "线下",
            "id": order.get("id", ""), "group": ", ".join(order.get("groups", [order.get("group", "")])),
            "sender": order.get("sender", ""), "search": extract_search_keyword(order),
            "time": order.get("posted_at", ""), "grade": order.get("grade", ""),
            "subject": order.get("subject", ""), "address": order.get("address", ""),
            "pay": order.get("pay_raw") or (f"{order.get('hourly')}/h" if order.get("hourly") else "未写"),
            "net": order.get("net_hourly") if order.get("net_hourly") is not None else "",
            "km": one_way(order, "round_km"),
            "minutes": one_way(order, "round_min"),
            "taxi": (
                f"{route.get('round_taxi')}（估）" if route.get("taxi_estimated") and route.get("round_taxi") not in (None, "")
                else route.get("round_taxi", "")
            ),
            "reason": ranker.compact_reason(order),
            "raw": order.get("raw", ""), "score": order.get("score", ""),
        }
        attrs = f'data-tier="{html.escape(str(cells["tier"]))}" data-mode="{order.get("delivery_mode", "offline")}" data-new="{1 if is_new else 0}"'
        before_search = "".join(f"<td>{html.escape(str(cells[k]))}</td>" for k in ["rank", "new", "mode", "tier", "id", "group", "sender"])
        keyword = html.escape(str(cells["search"]), quote=True)
        search_cell = f"<td class='search-key'><span>{keyword}</span><button type='button' data-keyword='{keyword}' title='复制单号/关键词'>复制</button></td>"
        after_search = "".join(f"<td>{html.escape(str(cells[k]))}</td>" for k in ["time", "grade", "subject", "address", "pay", "net", "km", "minutes", "taxi", "reason"])
        tds = before_search + search_cell + after_search
        detail = html.escape(str(cells["raw"]))
        rows.append(f"<tr {attrs}>{tds}</tr><tr class='detail'><td colspan='18'>{detail}</td></tr>")
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    doc = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>家教单监控</title><style>
:root{{--ink:#e7edf2;--muted:#98a8b5;--line:#33414c;--paper:#11171c;--surface:#182128;--surface-2:#202c34;--green:#58c594;--amber:#e2aa52;--red:#ff7b72}}
*{{box-sizing:border-box}}body{{margin:0;color:var(--ink);background:var(--paper);font:14px/1.45 system-ui,'Microsoft YaHei',sans-serif;color-scheme:dark}}
header{{background:var(--surface);border-bottom:1px solid var(--line);padding:18px 24px}}h1{{font-size:22px;margin:0 0 5px}}.sub{{color:var(--muted)}}
.bar{{display:flex;gap:10px;flex-wrap:wrap;padding:12px 24px;background:var(--surface);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:3}}
 input,select,button{{height:36px;color:var(--ink);border:1px solid #50616e;background:#121a20;padding:0 10px;border-radius:5px}}input{{min-width:280px}}input::placeholder{{color:#7f909d}}button{{cursor:pointer}}button:hover{{background:#26343e}}.source-note{{margin-top:5px;color:#d0b46b;font-size:12px}}
 .column-picker{{position:relative}}.column-menu{{position:absolute;right:0;top:42px;width:230px;max-height:420px;overflow:auto;padding:10px;background:#121a20;border:1px solid #50616e;border-radius:5px;box-shadow:0 12px 30px #0008;z-index:10}}.column-menu[hidden]{{display:none}}.column-list{{display:grid;gap:2px}}.column-menu label{{display:flex;align-items:center;gap:9px;padding:7px 6px;border-radius:4px;cursor:pointer}}.column-menu label:hover{{background:#26343e}}.column-menu input{{width:16px;height:16px;min-width:16px;margin:0}}.column-actions{{display:flex;justify-content:flex-end;padding-top:8px;margin-top:8px;border-top:1px solid var(--line)}}.column-actions button{{height:30px}}
 main{{padding:18px 24px;overflow:visible}}table{{border-collapse:collapse;width:100%;min-width:1500px;background:var(--surface)}}th,td{{border-bottom:1px solid var(--line);padding:8px;text-align:left;vertical-align:top}}th{{position:sticky;top:var(--toolbar-height,61px);z-index:2;background:var(--surface-2);color:#f2f6f8;cursor:pointer;white-space:nowrap;box-shadow:0 1px 0 var(--line)}}th[data-direction='asc']::after{{content:' ↑';color:#58c594}}th[data-direction='desc']::after{{content:' ↓';color:#58c594}}tbody tr:not(.detail):hover{{background:#223039}}tr[data-tier='线上优先']{{border-left:4px solid #53d7ff;background:#16262e}}tr[data-tier='线上可投']{{border-left:4px solid #53b9d7}}tr[data-tier='线上备选']{{border-left:4px solid #6f8fa1}}tr[data-tier='优先投']{{border-left:4px solid var(--green)}}tr[data-tier='可投']{{border-left:4px solid #62a98e}}tr[data-tier='备选']{{border-left:4px solid var(--amber)}}tr[data-tier='硬排除']{{border-left:4px solid var(--red);opacity:.72}}tr[data-new='1'] td:nth-child(2){{color:var(--red);font-weight:700}}.search-key{{white-space:nowrap}}.search-key button{{height:28px;margin-left:7px;padding:0 8px}}
.detail{{display:none;background:#141d23;color:#b9c7d0}}.detail td{{white-space:pre-wrap}}tr:hover+.detail{{display:table-row}}@media(max-width:700px){{header,.bar,main{{padding-left:12px;padding-right:12px}}input{{min-width:100%}}}}
 </style></head><body><header><h1>{stats.get('report_title') or stats.get('report_date', generated[:10]) + ' 家教单'}</h1><div class='sub'>更新于 {generated} · 仅显示当天 · 监控 {stats['groups']} 个群 · 本轮读取消息 {stats['messages']} 条 · 解析候选 {stats['new_orders']} 个 · 去重后 {stats['total_orders']} 单{visible_note}</div>{source_note_html}</header>
<div class='bar'><input id='q' placeholder='搜索单号、地址、科目、群'><select id='mode'><option value='online'{' selected' if stats.get('online_only_default') else ''}>只看线上</option><option value=''{' selected' if not stats.get('online_only_default') else ''}>全部模式</option><option value='offline'>只看线下</option></select><select id='tier'><option value=''{'' if stats.get('priority_only_default') else ' selected'}>全部单子</option><option value='priority'{' selected' if stats.get('priority_only_default') else ''}>高优先级</option><option value='viable'>可投</option><option value='backup'>备选</option><option value='rejected'>不优先</option></select><label><input id='newOnly' type='checkbox' style='min-width:auto;height:auto'> 只看本轮新增</label><div class='column-picker'><button id='columnButton' type='button' aria-expanded='false'>显示列</button><div id='columnMenu' class='column-menu' hidden><div id='columnList' class='column-list'></div><div class='column-actions'><button id='showAllColumns' type='button'>全部显示</button></div></div></div><button id='refresh'>刷新页面</button></div>
        <main><table id='orders'><thead><tr>{''.join(f'<th>{x}</th>' for x in ['序','新','模式','级别','单号','群标签','发送人','单号/关键词','发布时间','年级','科目','地址','报价','净时薪','单程km','单程min','往返车费','判断'])}</tr></thead><tbody>{''.join(rows)}</tbody></table></main>
<script>
const table=document.querySelector('#orders');
const toolbar=document.querySelector('.bar');
const columnButton=document.querySelector('#columnButton');
const columnMenu=document.querySelector('#columnMenu');
const columnList=document.querySelector('#columnList');
const headers=[...table.tHead.rows[0].cells];
const columnStorageKey='weflow-hidden-columns-v1';
let hiddenColumns=new Set();
function syncToolbarHeight(){{document.documentElement.style.setProperty('--toolbar-height',`${{Math.ceil(toolbar.getBoundingClientRect().height)}}px`)}}
syncToolbarHeight();
new ResizeObserver(syncToolbarHeight).observe(toolbar);
window.addEventListener('resize',syncToolbarHeight);
const forceHideColumns = new Set(['发送人', '专职老师']);
const forceShowColumns = new Set(['单号/关键词', '微信搜索词']);
try{{hiddenColumns=new Set(JSON.parse(localStorage.getItem(columnStorageKey)||'[]'))}}catch(e){{hiddenColumns=new Set()}}
forceHideColumns.forEach((name)=>hiddenColumns.add(name));
forceShowColumns.forEach((name)=>hiddenColumns.delete(name));
function saveHiddenColumns(){{try{{localStorage.setItem(columnStorageKey,JSON.stringify([...hiddenColumns]))}}catch(e){{}}}}
function applyColumnVisibility(){{
  let visibleCount=0;
  headers.forEach((th,index)=>{{
    const name=th.textContent.trim(),hidden=hiddenColumns.has(name);
    th.style.display=hidden?'none':'';
    table.tBodies[0].querySelectorAll('tr:not(.detail)').forEach(row=>{{if(row.cells[index])row.cells[index].style.display=hidden?'none':''}});
    if(!hidden)visibleCount++;
  }});
  table.querySelectorAll('tr.detail td').forEach(cell=>cell.colSpan=Math.max(visibleCount,1));
  columnButton.textContent='显示列 ('+visibleCount+'/'+headers.length+')';
}}
function buildColumnMenu(){{
  columnList.replaceChildren();
  headers.forEach(th=>{{
    const name=th.textContent.trim(),label=document.createElement('label'),checkbox=document.createElement('input');
    checkbox.type='checkbox';
    checkbox.checked=!hiddenColumns.has(name);
    checkbox.disabled=forceHideColumns.has(name) || forceShowColumns.has(name);
    checkbox.onchange=()=>{{checkbox.checked?hiddenColumns.delete(name):hiddenColumns.add(name);saveHiddenColumns();applyColumnVisibility()}};
    label.append(checkbox,document.createTextNode(name));columnList.append(label);
  }});
}}
function filter(){{const q=document.querySelector('#q').value.toLowerCase(),mode=document.querySelector('#mode').value,tier=document.querySelector('#tier').value,only=document.querySelector('#newOnly').checked;const tierGroups={{priority:['线上优先','优先投'],viable:['线上可投','可投'],backup:['线上备选','备选'],rejected:['不优先','硬排除']}};for(const tr of table.tBodies[0].querySelectorAll('tr:not(.detail)')){{const tierOk=!tier||(tierGroups[tier]||[tier]).includes(tr.dataset.tier);const ok=(!q||tr.innerText.toLowerCase().includes(q))&&(!mode||tr.dataset.mode===mode)&&tierOk&&(!only||tr.dataset.new==='1');tr.style.display=ok?'':'none';tr.nextElementSibling.style.display='none'}}}}
async function copyText(text,button){{try{{await navigator.clipboard.writeText(text)}}catch(e){{const area=document.createElement('textarea');area.value=text;document.body.appendChild(area);area.select();document.execCommand('copy');area.remove()}}const old=button.textContent;button.textContent='已复制';setTimeout(()=>button.textContent=old,1000)}}
document.querySelectorAll('.search-key button').forEach(button=>button.onclick=event=>{{event.stopPropagation();copyText(button.dataset.keyword,button)}});
document.querySelectorAll('#q,#mode,#tier,#newOnly').forEach(x=>x.addEventListener('input',filter));
document.querySelector('#refresh').onclick=()=>location.reload();
columnButton.onclick=()=>{{const opening=columnMenu.hidden;columnMenu.hidden=!opening;columnButton.setAttribute('aria-expanded',String(opening));if(opening)buildColumnMenu()}};
document.querySelector('#showAllColumns').onclick=()=>{{hiddenColumns.clear();forceHideColumns.forEach(name=>hiddenColumns.add(name));saveHiddenColumns();buildColumnMenu();applyColumnVisibility()}};
document.addEventListener('click',event=>{{if(!event.target.closest('.column-picker')){{columnMenu.hidden=true;columnButton.setAttribute('aria-expanded','false')}}}});
document.addEventListener('keydown',event=>{{if(event.key==='Escape'){{columnMenu.hidden=true;columnButton.setAttribute('aria-expanded','false')}}}});
function rowPairs(){{return [...table.tBodies[0].querySelectorAll('tr:not(.detail)')].map(row=>[row,row.nextElementSibling])}}
function numberValue(text){{const value=String(text||'').replaceAll(',','').trim();if(!value||value==='None')return null;const match=value.match(/-?\d+(?:\.\d+)?/);return match?Number(match[0]):null}}
function sortByHeader(th,direction){{
  const index=th.cellIndex,name=th.textContent.trim(),factor=direction==='asc'?1:-1;
  const numericColumns=new Set(['序','净时薪','单程km','单程min','往返车费']);
  const pairs=rowPairs();
  pairs.sort((a,b)=>{{
    const aText=a[0].cells[index]?.innerText||'',bText=b[0].cells[index]?.innerText||'';
    if(numericColumns.has(name)){{
      const av=numberValue(aText),bv=numberValue(bText);
      if(av===null&&bv===null)return 0;
      if(av===null)return 1;
      if(bv===null)return -1;
      return(av-bv)*factor;
    }}
    return aText.localeCompare(bText,'zh-CN')*factor;
  }});
  pairs.forEach(([row,detail])=>table.tBodies[0].append(row,detail));
  headers.forEach(header=>{{delete header.dataset.direction;header.removeAttribute('aria-sort')}});
  th.dataset.direction=direction;
  th.setAttribute('aria-sort',direction==='asc'?'ascending':'descending');
}}
filter();applyColumnVisibility();
headers.forEach(th=>{{
  const name=th.textContent.trim(),firstDirection=name==='净时薪'?'desc':'asc';
  th.title=name==='单程km'?'点击排序：首次从近到远':name==='净时薪'?'点击排序：首次从高到低':'点击排序';
  th.onclick=()=>{{const direction=th.dataset.direction?(th.dataset.direction==='asc'?'desc':'asc'):firstDirection;sortByHeader(th,direction)}};
}});
</script></body></html>"""
    (output_path or (REPORT_DIR / "latest.html")).write_text(doc, encoding="utf-8")


def notify(orders, new_keys, config):
    good = [o for o in orders if o.get("source_message_key") in new_keys and o.get("tier") in config["notify_tiers"]]
    if config.get("notify_online_only"):
        good = [o for o in good if o.get("delivery_mode") == "online"]
    if not good:
        return 0
    top = good[0]
    title = f"发现 {len(good)} 个值得投的家教单"
    body = f"{top['id']} · {top.get('grade','')} {top.get('subject','')}"
    if config.get("notification_include_address"):
        body += f" · {top.get('address','')}"
    subprocess.Popen([
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "notify.ps1"),
        "-Title", title, "-Body", body, "-Report", str(REPORT_DIR / "latest.html"),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return len(good)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="通过 WeFlow 本地 API 汇总并排序家教群消息")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="本地 JSON 配置路径")
    parser.add_argument("--check", action="store_true", help="检查配置与 API 响应结构，不生成报告")
    parser.add_argument("--list-groups", action="store_true", help="把全部群名和群 ID 写入本地私有 CSV")
    parser.add_argument("--no-notify", action="store_true", help="本轮不发送桌面通知")
    args = parser.parse_args(argv)

    config = load_config(args.config, require_origin=not args.list_groups)
    if args.no_notify:
        config["notify_tiers"] = []
    if args.check or args.list_groups:
        ensure_api()
        settings = weflow_settings()
        session_payload = request_json("/api/v1/sessions", {"limit": 1000}, timeout=30)
        sessions = session_payload.get("sessions")
        if not isinstance(sessions, list):
            raise RuntimeError("WeFlow sessions 响应缺少 sessions 数组")
        if args.list_groups:
            count = write_group_catalog(sessions)
            print(json.dumps({
                "status": "ok",
                "group_count": count,
                "catalog": "state/group-catalog.csv",
            }, ensure_ascii=False, indent=2))
            return 0
        matched = [session for session in sessions if is_tutor_group(session, config)]
        message_probe_ok = None
        if matched:
            probe = request_json("/api/v1/messages", {
                "talker": matched[0].get("username", ""),
                "start": int((datetime.now() - timedelta(hours=24)).timestamp()),
                "limit": 1,
            }, timeout=20)
            message_probe_ok = isinstance(probe.get("messages"), list)
            if not message_probe_ok:
                raise RuntimeError("WeFlow messages 响应缺少 messages 数组")
        print(json.dumps({
            "status": "ok",
            "config_loaded": True,
            "weflow_config_found": WEFLOW_CONFIG.exists(),
            "api_base": settings["base"],
            "api_token_configured": bool(settings["token"]),
            "api_session_count": len(sessions),
            "matched_group_count": len(matched),
            "message_schema_probe_ok": message_probe_ok,
            "output_directories_ready": True,
            "map_routing_enabled": bool(config.get("map_routing_enabled")),
            "map_provider": config["map_provider"],
            "map_key_configured": bool(os.environ.get(ranker.MAP_KEY_ENV[config["map_provider"]])),
        }, ensure_ascii=False, indent=2))
        return 0

    state = load_json(STATE_DIR / "state.json", {"last_seen": {}, "processed_messages": []})
    ensure_api()
    sessions = request_json("/api/v1/sessions", {"limit": 1000}, timeout=120).get("sessions", [])
    known_ids = {str(session.get("username", "")) for session in sessions}
    for group_id, display_name in config.get("include_groups", {}).items():
        if group_id in known_ids:
            for session in sessions:
                if str(session.get("username", "")) == group_id:
                    session["displayName"] = display_name
                    break
        else:
            sessions.append({
                "username": group_id,
                "displayName": display_name,
                "type": 2,
                "lastTimestamp": int(time.time()),
            })
    groups = sorted((s for s in sessions if is_tutor_group(s, config)), key=lambda s: str(s.get("displayName", "")))
    write_group_labels(groups)
    rows, changed_groups = fetch_changed_messages(groups, state, config)
    new_orders, processed = extract_orders(rows)
    history = load_json(STATE_DIR / "leads.json", {"orders": []}).get("orders", [])
    orders = merge_history(history, new_orders, config["keep_leads_days"])
    score_orders(orders, config)
    new_keys = {o.get("source_message_key") for o in new_orders}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    report_orders = [o for o in orders if str(o.get("posted_at", "")).startswith(today)]
    resolve_sender_names([o for o in report_orders if o.get("delivery_mode") == "online" and o.get("tier") != "硬排除"])
    stats = {"groups": len(groups), "changed_groups": changed_groups, "messages": len(rows), "new_orders": len(new_orders), "total_orders": len(report_orders), "online_only_default": config.get("online_only_default", True), "priority_only_default": config.get("priority_only_default", True), "report_date": today}
    group_report_paths = []
    for definition in config.get("group_reports", []):
        group_id = str(definition.get("group_id") or "")
        group_label = str(definition.get("group") or "")
        all_group_orders = [
            order for order in report_orders
            if (group_id and group_id in order.get("source_group_ids", [order.get("source_group_id", "")]))
            or (group_label and group_label in order.get("groups", [order.get("group", "")]))
        ]
        allowed_tiers = set(definition.get("tiers") or [])
        if definition.get("route_selected"):
            route_candidates = [order for order in all_group_orders if not allowed_tiers or order.get("tier") in allowed_tiers]
            # Main scoring owns the per-run live API budget. Group reports may
            # reuse cached/online routes but never create extra live calls.
            ranker.route_orders(route_candidates, 0, STATE_DIR / "route_cache.json")
            for order in route_candidates:
                order["notes"] = [note for note in order.get("notes", []) if note != "线上单优先"]
                ranker.final_score(order)
                apply_online_priority(order, config)
        group_orders = [dict(order) for order in all_group_orders if not allowed_tiers or order.get("tier") in allowed_tiers]
        allowed_modes = set(definition.get("modes") or [])
        if allowed_modes:
            group_orders = [order for order in group_orders if order.get("delivery_mode") in allowed_modes]
        # A deduplicated order can be cross-posted to several groups. Keep an
        # individual group report visually scoped to the group it represents.
        if group_label:
            for order in group_orders:
                order["group"] = group_label
                order["groups"] = [group_label]
        output_tiers = set(definition.get("output_tiers") or [])
        if output_tiers:
            group_orders = [order for order in group_orders if order.get("tier") in output_tiers]
        if definition.get("sort") == "score":
            group_orders.sort(key=lambda order: (order.get("score", -9999), order.get("posted_at", "")), reverse=True)
        else:
            group_orders.sort(key=lambda order: order.get("posted_at", ""), reverse=True)
        resolve_sender_names(group_orders)
        filename = str(definition.get("filename") or f"{group_label or group_id}-最新单子")
        base = REPORT_DIR / filename
        group_new_keys = {o.get("source_message_key") for o in group_orders if o.get("source_message_key") in new_keys}
        group_stats = {
            "groups": 1,
            "changed_groups": 1 if group_new_keys else 0,
            "messages": len(group_new_keys),
            "new_orders": len(group_new_keys),
            "total_orders": len(group_orders),
            "online_only_default": False,
            "report_date": today,
            "report_title": f"{today} {definition.get('report_title') or (group_label or group_id) + '最新单子'}",
        }
        save_json(base.with_suffix(".json"), {"stats": group_stats, "orders": group_orders})
        ranker.write_csv(base.with_suffix(".csv"), group_orders)
        dashboard(
            group_orders,
            group_new_keys,
            group_stats,
            output_path=base.with_suffix(".html"),
            include_hard=bool(definition.get("include_hard", True)),
        )
        group_report_paths.append(str(base.with_suffix(".html")))
    report_orders.sort(
        key=lambda order: (
            1 if order.get("delivery_mode") == "online" and not order.get("hard_reasons") else 0,
            order.get("score", -9999),
            order.get("rough_score", -9999),
        ),
        reverse=True,
    )
    save_json(STATE_DIR / "leads.json", {"updated_at": datetime.now().isoformat(), "orders": orders})
    save_json(REPORT_DIR / "latest.json", {"stats": stats, "orders": report_orders})
    ranker.write_csv(REPORT_DIR / "latest.csv", report_orders)
    dashboard(report_orders, new_keys, stats)
    compatible_orders = [order for order in report_orders if order.get("tier") != "硬排除"]
    compatible_stats = {
        **stats,
        "total_orders": len(compatible_orders),
        "online_only_default": False,
        "priority_only_default": False,
        "report_title": f"{today} 今日全部兼容家教单",
    }
    save_json(REPORT_DIR / "今日全部兼容单.json", {"stats": compatible_stats, "orders": compatible_orders})
    ranker.write_csv(REPORT_DIR / "今日全部兼容单.csv", compatible_orders)
    dashboard(
        compatible_orders,
        new_keys,
        compatible_stats,
        output_path=REPORT_DIR / "今日全部兼容单.html",
        include_hard=False,
    )
    online_orders = [
        order
        for order in report_orders
        if order.get("delivery_mode") == "online" and not order.get("hard_reasons")
    ]
    online_stats = {
        **stats,
        "total_orders": len(online_orders),
        "online_only_default": False,
        "priority_only_default": False,
        "report_title": f"{today} 今日线上家教单",
    }
    save_json(REPORT_DIR / "今日线上单子.json", {"stats": online_stats, "orders": online_orders})
    ranker.write_csv(REPORT_DIR / "今日线上单子.csv", online_orders)
    dashboard(online_orders, new_keys, online_stats, output_path=REPORT_DIR / "今日线上单子.html", include_hard=False)
    priority_orders = [order for order in report_orders if order.get("tier") in {"线上优先", "优先投"}]
    priority_stats = {
        **stats,
        "total_orders": len(priority_orders),
        "online_only_default": False,
        "priority_only_default": False,
        "report_title": f"{today} 优先投家教单",
    }
    save_json(REPORT_DIR / "今日优先投.json", {"stats": priority_stats, "orders": priority_orders})
    ranker.write_csv(REPORT_DIR / "今日优先投.csv", priority_orders)
    dashboard(priority_orders, new_keys, priority_stats, output_path=REPORT_DIR / "今日优先投.html")
    state["processed_messages"] = (state.get("processed_messages", []) + processed)[-50000:]
    state["last_run"] = datetime.now().isoformat()
    save_json(STATE_DIR / "state.json", state)
    notified = notify(report_orders, new_keys, config)
    print(json.dumps({
        **stats,
        "notified": notified,
        "report": str(REPORT_DIR / "latest.html"),
        "online_report": str(REPORT_DIR / "今日线上单子.html"),
        "group_reports": group_report_paths,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
