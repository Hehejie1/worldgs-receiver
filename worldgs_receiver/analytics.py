import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AnalyticsStore:
    event_log: Path

    def record(self, payload: dict[str, Any], client_host: str, user_agent: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        event = _clean_text(payload.get("event"), "unknown", max_length=80)
        client_id = _clean_text(payload.get("client_id"), "anonymous", max_length=160)
        occurred_at = _clean_text(payload.get("occurred_at"), now, max_length=64)

        record = {
            "event": event,
            "client_id": client_id,
            "occurred_at": occurred_at,
            "received_at": now,
            "source": _clean_text(payload.get("source"), "", max_length=80),
            "path": _clean_text(payload.get("path"), "", max_length=200),
            "platform": _clean_text(payload.get("platform"), "", max_length=80),
            "asset": _clean_text(payload.get("asset"), "", max_length=80),
            "location": _clean_text(payload.get("location"), "", max_length=80),
            "utm_source": _clean_text(payload.get("utm_source"), "", max_length=120),
            "utm_medium": _clean_text(payload.get("utm_medium"), "", max_length=120),
            "utm_campaign": _clean_text(payload.get("utm_campaign"), "", max_length=120),
            "app_version": _clean_text(payload.get("app_version"), "", max_length=80),
            "os_version": _clean_text(payload.get("os_version"), "", max_length=80),
            "device_model": _clean_text(payload.get("device_model"), "", max_length=120),
            "referrer": _clean_text(payload.get("referrer"), "", max_length=300),
            "client_host": client_host,
            "user_agent": user_agent[:300],
        }

        self.event_log.parent.mkdir(parents=True, exist_ok=True)
        with self.event_log.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        return {"ok": True}

    def summary(self) -> dict[str, Any]:
        events = self._read_events()
        today = datetime.now(timezone.utc).date().isoformat()
        days = [(datetime.now(timezone.utc).date() - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]

        by_day: dict[str, list[dict[str, Any]]] = {day: [] for day in days}
        for event in events:
            day = _event_day(event)
            if day in by_day:
                by_day[day].append(event)

        today_events = by_day.get(today, [])
        event_counts = Counter(str(event.get("event", "")) for event in events)
        recent_events = events[-50:][::-1]

        return {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "today": {
                "date": today,
                "page_views": _count(today_events, "page_view"),
                "unique_visitors": _unique_clients(today_events, "page_view"),
                "downloads": _count(today_events, "download_click"),
                "android_dau": _unique_clients(today_events, "android_dau"),
            },
            "last_7_days": [
                {
                    "date": day,
                    "page_views": _count(day_events, "page_view"),
                    "unique_visitors": _unique_clients(day_events, "page_view"),
                    "downloads": _count(day_events, "download_click"),
                    "android_dau": _unique_clients(day_events, "android_dau"),
                }
                for day, day_events in by_day.items()
            ],
            "totals": {
                "events": len(events),
                "page_views": _count(events, "page_view"),
                "downloads": _count(events, "download_click"),
                "android_dau_events": _count(events, "android_dau"),
            },
            "top_events": [{"event": key, "count": count} for key, count in event_counts.most_common(12)],
            "recent_events": recent_events,
        }

    def _read_events(self) -> list[dict[str, Any]]:
        if not self.event_log.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.event_log.open("r", encoding="utf-8") as file:
            for line in file:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    events.append(value)
        return events


def _clean_text(value: Any, fallback: str, max_length: int) -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    return text[:max_length]


def _event_day(event: dict[str, Any]) -> str:
    occurred_at = str(event.get("occurred_at") or event.get("received_at") or "")
    return occurred_at[:10]


def _count(events: list[dict[str, Any]], event_name: str) -> int:
    return sum(1 for event in events if event.get("event") == event_name)


def _unique_clients(events: list[dict[str, Any]], event_name: str) -> int:
    return len({event.get("client_id") for event in events if event.get("event") == event_name and event.get("client_id")})
