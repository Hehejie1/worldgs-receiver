import json
import logging
import re
import threading
import uuid
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger("worldgs.security")


@dataclass(frozen=True)
class SecurityRule:
    attack_label: str
    severity: str
    pattern: re.Pattern[str]
    reason: str


@dataclass(frozen=True)
class AlertRule:
    threshold: int
    window_seconds: int
    reason: str


_SUSPICIOUS_PATH_RULES = (
    SecurityRule(
        attack_label="wordpress_probe",
        severity="high",
        pattern=re.compile(r"(?i)/(wp-admin|wp-login\.php|xmlrpc\.php)(?:/|$)"),
        reason="命中了常见 WordPress 扫描路径。",
    ),
    SecurityRule(
        attack_label="env_leak_probe",
        severity="high",
        pattern=re.compile(r"(?i)(?:^|/)\.(env|git)(?:/|$)"),
        reason="命中了环境变量或 Git 目录泄露探测路径。",
    ),
    SecurityRule(
        attack_label="phpmyadmin_probe",
        severity="high",
        pattern=re.compile(r"(?i)/(phpmyadmin|pma|mysql-admin)(?:/|$)"),
        reason="命中了常见数据库管理后台扫描路径。",
    ),
    SecurityRule(
        attack_label="infra_probe",
        severity="medium",
        pattern=re.compile(r"(?i)/(server-status|nginx_status|actuator|metrics|swagger|openapi)(?:/|$)"),
        reason="命中了基础设施探测路径。",
    ),
    SecurityRule(
        attack_label="device_probe",
        severity="medium",
        pattern=re.compile(r"(?i)/(cgi-bin|boaform|hnap1|shell|login\.cgi)(?:/|$)"),
        reason="命中了常见摄像头或 IoT 设备扫描路径。",
    ),
    SecurityRule(
        attack_label="backup_probe",
        severity="high",
        pattern=re.compile(r"(?i)\.(sql|bak|old|zip|tar|gz)$"),
        reason="命中了备份或数据库文件探测路径。",
    ),
    SecurityRule(
        attack_label="php_probe",
        severity="medium",
        pattern=re.compile(r"(?i)\.(php|asp|aspx|jsp|cgi)$"),
        reason="命中了动态脚本文件探测路径。",
    ),
)

_ALERT_RULES = {
    "wordpress_probe": AlertRule(threshold=3, window_seconds=600, reason="WordPress 扫描命中阈值"),
    "env_leak_probe": AlertRule(threshold=2, window_seconds=600, reason="配置泄露探测命中阈值"),
    "phpmyadmin_probe": AlertRule(threshold=3, window_seconds=600, reason="数据库后台扫描命中阈值"),
    "infra_probe": AlertRule(threshold=4, window_seconds=600, reason="基础设施探测命中阈值"),
    "device_probe": AlertRule(threshold=4, window_seconds=600, reason="设备面板扫描命中阈值"),
    "backup_probe": AlertRule(threshold=2, window_seconds=600, reason="备份文件探测命中阈值"),
    "php_probe": AlertRule(threshold=4, window_seconds=600, reason="脚本文件探测命中阈值"),
    "dashboard_auth_probe": AlertRule(threshold=5, window_seconds=600, reason="后台口令爆破疑似命中阈值"),
    "management_nonce_probe": AlertRule(threshold=4, window_seconds=600, reason="管理接口 nonce 爆破疑似命中阈值"),
    "sync_token_probe": AlertRule(threshold=5, window_seconds=600, reason="上传或同步 token 爆破疑似命中阈值"),
    "oversized_upload_probe": AlertRule(threshold=2, window_seconds=600, reason="超大上传疑似施压命中阈值"),
}

_PROTECTED_EXACT_PATHS = {
    "/api/analytics/summary",
    "/api/diag/v1/summary",
    "/api/security/summary",
}
_PROTECTED_PREFIX_PATHS = ("/api/diag/v1/issues/",)
_MANAGEMENT_PREFIX_PATHS = (
    "/api/open-path",
    "/api/uploads/",
    "/api/local-training/",
    "/api/automation/",
)
_SYNC_AUTH_PATHS = ("/upload", "/api/sync/sessions")


@dataclass
class SecurityMonitor:
    root_dir: Path

    def __post_init__(self) -> None:
        self.events_log = self.root_dir / "events.jsonl"
        self.alerts_log = self.root_dir / "alerts.jsonl"
        self._lock = threading.Lock()
        self._windows: dict[str, deque[float]] = defaultdict(deque)
        self._alert_buckets: dict[str, int] = {}

    def inspect_response(
        self,
        *,
        method: str,
        path: str,
        status_code: int,
        client_host: str,
        user_agent: str,
        query_keys: list[str],
    ) -> None:
        assessment = _assess_security_event(method=method, path=path, status_code=status_code)
        if assessment is None:
            return
        now = datetime.now(timezone.utc)
        record = {
            "id": f"sec_evt_{uuid.uuid4().hex[:12]}",
            "detected_at": now.isoformat(),
            "event_type": assessment["event_type"],
            "attack_label": assessment["attack_label"],
            "severity": assessment["severity"],
            "reason": assessment["reason"],
            "method": method,
            "path": path,
            "status_code": status_code,
            "client_host": _clean_text(client_host, "unknown", 80),
            "user_agent": _clean_text(user_agent, "", 300),
            "user_agent_family": _user_agent_family(user_agent),
            "query_keys": query_keys[:20],
        }
        alert = None
        with self._lock:
            self._append_jsonl(self.events_log, record)
            alert = self._maybe_create_alert(record, now.timestamp())
            if alert is not None:
                self._append_jsonl(self.alerts_log, alert)
        if alert is not None:
            logger.warning("worldgs_security_alert %s", json.dumps(alert, ensure_ascii=False, separators=(",", ":")))

    def summary(self, days: int = 7) -> dict[str, Any]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        last_24h = datetime.now(timezone.utc) - timedelta(hours=24)
        events = [event for event in self._read_jsonl(self.events_log) if _parse_ts(event.get("detected_at")) >= since]
        alerts = [alert for alert in self._read_jsonl(self.alerts_log) if _parse_ts(alert.get("detected_at")) >= since]
        top_attackers = self._top_attackers(events)
        top_signatures = self._top_signatures(events)
        return {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "days": days,
            "totals": {
                "events": len(events),
                "alerts": len(alerts),
                "unique_ips": len({event.get("client_host") for event in events if event.get("client_host")}),
                "high_severity_events": sum(1 for event in events if event.get("severity") == "high"),
                "medium_severity_events": sum(1 for event in events if event.get("severity") == "medium"),
            },
            "last_24_hours": {
                "events": sum(1 for event in events if _parse_ts(event.get("detected_at")) >= last_24h),
                "alerts": sum(1 for alert in alerts if _parse_ts(alert.get("detected_at")) >= last_24h),
                "unique_ips": len(
                    {event.get("client_host") for event in events if _parse_ts(event.get("detected_at")) >= last_24h}
                ),
            },
            "top_attackers": top_attackers,
            "top_signatures": top_signatures,
            "recent_alerts": alerts[-20:][::-1],
            "recent_events": events[-50:][::-1],
        }

    def _top_attackers(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            client_host = str(event.get("client_host") or "")
            if client_host:
                grouped[client_host].append(event)
        ranked: list[dict[str, Any]] = []
        for client_host, items in grouped.items():
            labels = Counter(str(item.get("attack_label") or "") for item in items if item.get("attack_label"))
            ranked.append(
                {
                    "client_host": client_host,
                    "count": len(items),
                    "top_attack_label": labels.most_common(1)[0][0] if labels else "",
                    "last_seen": max(str(item.get("detected_at") or "") for item in items),
                }
            )
        ranked.sort(key=lambda item: (int(item["count"]), str(item["last_seen"])), reverse=True)
        return ranked[:10]

    def _top_signatures(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            label = str(event.get("attack_label") or "")
            if label:
                grouped[label].append(event)
        ranked: list[dict[str, Any]] = []
        for label, items in grouped.items():
            ranked.append(
                {
                    "attack_label": label,
                    "severity": max((str(item.get("severity") or "low") for item in items), default="low"),
                    "count": len(items),
                    "last_seen": max(str(item.get("detected_at") or "") for item in items),
                }
            )
        ranked.sort(key=lambda item: (int(item["count"]), str(item["last_seen"])), reverse=True)
        return ranked[:10]

    def _maybe_create_alert(self, record: dict[str, Any], now_ts: float) -> dict[str, Any] | None:
        attack_label = str(record.get("attack_label") or "")
        rule = _ALERT_RULES.get(attack_label)
        if rule is None:
            return None
        key = f"{attack_label}:{record.get('client_host') or 'unknown'}"
        window = self._windows[key]
        cutoff = now_ts - rule.window_seconds
        while window and window[0] < cutoff:
            window.popleft()
        window.append(now_ts)
        count_in_window = len(window)
        if count_in_window < rule.threshold:
            return None
        bucket = int(now_ts // rule.window_seconds)
        if self._alert_buckets.get(key) == bucket:
            return None
        self._alert_buckets[key] = bucket
        return {
            "id": f"sec_alert_{uuid.uuid4().hex[:12]}",
            "detected_at": record["detected_at"],
            "severity": record["severity"],
            "attack_label": attack_label,
            "reason": rule.reason,
            "client_host": record["client_host"],
            "count_in_window": count_in_window,
            "window_seconds": rule.window_seconds,
            "latest_path": record["path"],
            "latest_status_code": record["status_code"],
            "event_type": record["event_type"],
        }

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
        return rows


def _assess_security_event(method: str, path: str, status_code: int) -> dict[str, str] | None:
    normalized_method = (method or "GET").upper()
    normalized_path = _normalize_path(path)
    for rule in _SUSPICIOUS_PATH_RULES:
        if rule.pattern.search(normalized_path):
            return {
                "event_type": "path_probe",
                "attack_label": rule.attack_label,
                "severity": rule.severity,
                "reason": rule.reason,
            }
    if status_code == 401 and _is_basic_auth_path(normalized_method, normalized_path):
        return {
            "event_type": "auth_failure",
            "attack_label": "dashboard_auth_probe",
            "severity": "high",
            "reason": "受保护后台接口发生鉴权失败。",
        }
    if status_code == 403 and _starts_with_any(normalized_path, _MANAGEMENT_PREFIX_PATHS):
        return {
            "event_type": "nonce_failure",
            "attack_label": "management_nonce_probe",
            "severity": "high",
            "reason": "管理接口 nonce 校验失败。",
        }
    if status_code == 403 and _starts_with_any(normalized_path, _SYNC_AUTH_PATHS):
        return {
            "event_type": "token_failure",
            "attack_label": "sync_token_probe",
            "severity": "medium",
            "reason": "上传或文件同步 token 校验失败。",
        }
    if status_code == 413 and _starts_with_any(normalized_path, _SYNC_AUTH_PATHS):
        return {
            "event_type": "payload_rejected",
            "attack_label": "oversized_upload_probe",
            "severity": "medium",
            "reason": "上传请求超过服务端限制。",
        }
    return None


def _is_basic_auth_path(method: str, path: str) -> bool:
    if path in _PROTECTED_EXACT_PATHS:
        return True
    if _starts_with_any(path, _PROTECTED_PREFIX_PATHS):
        return True
    return path == "/api/website/showcase-config" and method != "GET"


def _starts_with_any(path: str, prefixes: tuple[str, ...] | set[str]) -> bool:
    return any(path.startswith(prefix) for prefix in prefixes)


def _normalize_path(path: str) -> str:
    value = str(path or "/").strip()
    if not value.startswith("/"):
        value = f"/{value}"
    return value.lower()


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _clean_text(value: str, fallback: str, max_length: int) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    return text[:max_length]


def _user_agent_family(user_agent: str) -> str:
    value = (user_agent or "").lower()
    if "curl" in value:
        return "curl"
    if "python-requests" in value or "python/" in value:
        return "python"
    if "go-http-client" in value:
        return "go-http-client"
    if "mozilla/" in value:
        return "browser"
    if not value:
        return "empty"
    return "other"
