from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SHOWCASE_CONFIG: dict[str, Any] = {
    "sectionEyebrow": "Featured Cases",
    "sectionTitle": "精选案例",
    "sectionDescription": "用真实案例，直接展示 WorldGS 的生成与分享效果。",
    "showcases": [
        {
            "id": "sudongpo",
            "title": "苏东坡",
            "subtitle": "",
            "description": "苏堤南端的苏东坡石像，衣袂飘然，巍然矗立。",
            "shareUrl": "https://worldgs.notemeld.wiki/share/sh_vDXmrFEKSvsIBGrXvSzWFzPR",
            "imageUrl": "https://coresg-normal.trae.ai/api/ide/v1/text_to_image?prompt=realistic%20full-body%20statue%20of%20Su%20Dongpo%20at%20West%20Lake%20promenade%2C%20flowing%20robes%2C%20Chinese%20historical%20monument%2C%20daylight%2C%20high%20detail%2C%20cinematic%20photography%2C%20dark%20teal%20background%2C%20premium%20website%20hero%20image&image_size=landscape_4_3",
            "sortOrder": 10,
            "enabled": True,
        },
        {
            "id": "stone-lion",
            "title": "石狮",
            "subtitle": "",
            "description": "西湖苏堤桥上的石狮，蹲坐望柱，神态慵懒呆萌。",
            "shareUrl": "https://worldgs.notemeld.wiki/share/sh_3433q1YHxPjvxsnPoYw7gRTe",
            "imageUrl": "https://coresg-normal.trae.ai/api/ide/v1/text_to_image?prompt=realistic%20Chinese%20stone%20lion%20on%20West%20Lake%20bridge%2C%20carved%20stone%20guardian%20lion%2C%20close-up%2C%20soft%20daylight%2C%20detailed%20texture%2C%20cinematic%20photography%2C%20dark%20teal%20background%2C%20premium%20website%20hero%20image&image_size=landscape_4_3",
            "sortOrder": 20,
            "enabled": True,
        },
    ],
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_showcase_item(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("showcase item must be an object")
    title = _clean_text(item.get("title"))
    share_url = _clean_text(item.get("shareUrl"))
    if not title:
        raise ValueError("showcase title is required")
    if not share_url:
        raise ValueError("showcase shareUrl is required")
    sort_order_raw = item.get("sortOrder", index * 10)
    try:
        sort_order = int(sort_order_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("showcase sortOrder must be an integer") from exc
    return {
        "id": _clean_text(item.get("id")) or f"showcase-{index + 1}",
        "title": title,
        "subtitle": _clean_text(item.get("subtitle")),
        "description": _clean_text(item.get("description")),
        "shareUrl": share_url,
        "imageUrl": _clean_text(item.get("imageUrl")),
        "sortOrder": sort_order,
        "enabled": bool(item.get("enabled", True)),
    }


def normalize_showcase_config(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("showcase config must be an object")
    showcases = payload.get("showcases")
    if not isinstance(showcases, list):
        raise ValueError("showcases must be an array")
    normalized = {
        "sectionEyebrow": _clean_text(payload.get("sectionEyebrow")) or DEFAULT_SHOWCASE_CONFIG["sectionEyebrow"],
        "sectionTitle": _clean_text(payload.get("sectionTitle")) or DEFAULT_SHOWCASE_CONFIG["sectionTitle"],
        "sectionDescription": _clean_text(payload.get("sectionDescription")) or DEFAULT_SHOWCASE_CONFIG["sectionDescription"],
        "showcases": [_normalize_showcase_item(item, index) for index, item in enumerate(showcases)],
        "updatedAt": _clean_text(payload.get("updatedAt")) or _now_iso(),
    }
    normalized["showcases"].sort(key=lambda item: (item["sortOrder"], item["title"]))
    return normalized


class WebsiteShowcaseStore:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path

    def read(self) -> dict[str, Any]:
        if not self.config_path.is_file():
            return normalize_showcase_config(DEFAULT_SHOWCASE_CONFIG)
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return normalize_showcase_config(DEFAULT_SHOWCASE_CONFIG)
        try:
            return normalize_showcase_config(payload)
        except ValueError:
            return normalize_showcase_config(DEFAULT_SHOWCASE_CONFIG)

    def update(self, payload: Any) -> dict[str, Any]:
        next_config = normalize_showcase_config(payload)
        next_config["updatedAt"] = _now_iso()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(next_config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return next_config
