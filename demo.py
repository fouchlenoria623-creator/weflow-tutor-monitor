from __future__ import annotations

import argparse
import json
from pathlib import Path

import monitor
import ranker


ROOT = Path(__file__).resolve().parent
DEMO_DATE = "2026-01-01"
DEMO_CONFIG = {
    **monitor.DEFAULT_CONFIG,
    "map_routing_enabled": False,
    "map_provider": "baidu",
    "origin_name": "合成出发地",
    "origin_coord": "",
    "subject_weights": {"数学物理": 44, "数学": 35, "物理": 33, "英语": 14},
    "tutor_profile": {"gender": "", "school_tags": [], "school_names": []},
    "online_only_default": False,
    "priority_only_default": False,
}

SYNTHETIC_ROUTES = {
    "DEMO1002": {
        "status": "ok",
        "route_provider": "synthetic",
        "one_km": 5.2,
        "round_km": 10.4,
        "one_min": 22,
        "round_min": 44,
        "one_taxi": 21,
        "round_taxi": 42,
        "taxi_estimated": True,
    },
    "DEMO1003": {
        "status": "ok",
        "route_provider": "synthetic",
        "one_km": 11.8,
        "round_km": 23.6,
        "one_min": 38,
        "round_min": 76,
        "one_taxi": 42,
        "round_taxi": 84,
        "taxi_estimated": True,
    },
}


def build_demo(output_dir: Path):
    ranker.TODAY = DEMO_DATE
    ranker.configure_runtime(DEMO_CONFIG)
    messages = ranker.read_messages(str(ROOT / "examples" / "synthetic_messages.md"))
    orders = []
    for message_index, message in enumerate(messages, 1):
        for block_index, block in enumerate(ranker.split_blocks(message), 1):
            order = ranker.make_order(message, block)
            key = f"synthetic:{message_index}:{block_index}"
            order["source_message_key"] = key
            order["groups"] = ["合成演示群"]
            order["group"] = "合成演示群"
            orders.append(order)

    orders = ranker.dedupe_orders(orders)
    new_keys = {order["source_message_key"] for order in orders}
    for order in orders:
        if ranker.is_online_order(order):
            order["route"] = {
                "status": "online",
                "one_km": 0,
                "round_km": 0,
                "one_min": 0,
                "round_min": 0,
                "one_taxi": 0,
                "round_taxi": 0,
            }
        elif order["id"] in SYNTHETIC_ROUTES:
            order["route"] = dict(SYNTHETIC_ROUTES[order["id"]])
        ranker.final_score(order)
        monitor.apply_user_background_fit(order, DEMO_CONFIG)
        monitor.apply_online_priority(order, DEMO_CONFIG)

    orders.sort(
        key=lambda order: (
            1 if order.get("delivery_mode") == "online" and not order.get("hard_reasons") else 0,
            order.get("score", -9999),
            order.get("rough_score", -9999),
        ),
        reverse=True,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / "latest.html"
    json_path = output_dir / "demo-orders.json"
    csv_path = output_dir / "demo-orders.csv"
    stats = {
        "groups": 1,
        "messages": len(messages),
        "new_orders": len(orders),
        "total_orders": len(orders),
        "report_date": DEMO_DATE,
        "report_title": "合成数据家教单演示",
        "online_only_default": False,
        "priority_only_default": False,
    }
    monitor.dashboard(orders, new_keys, stats, output_path=html_path, include_hard=True)
    json_path.write_text(
        json.dumps({"stats": stats, "orders": orders}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    ranker.write_csv(csv_path, orders)
    return {"html": str(html_path), "json": str(json_path), "csv": str(csv_path)}


def main():
    parser = argparse.ArgumentParser(description="使用纯合成消息生成正式样式的家教单仪表盘")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "demo-output")
    args = parser.parse_args()
    outputs = build_demo(args.out_dir)
    print(json.dumps({"status": "ok", "outputs": outputs}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
