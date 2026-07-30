import json
import shutil
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Optional, Union


@dataclass(frozen=True)
class DiagStore:
    root_dir: Path

    @property
    def event_log(self) -> Path:
        return self.root_dir / "events.jsonl"

    @property
    def issues_dir(self) -> Path:
        return self.root_dir / "issues"

    def record_event(self, payload: dict[str, Any], client_host: str, user_agent: str) -> dict[str, Any]:
        event_id = f"evt_{uuid.uuid4().hex}"
        record = {
            "id": event_id,
            "received_at": _now_iso(),
            "client_host": client_host,
            "user_agent": user_agent[:300],
            "payload": _clean_payload(payload),
        }
        self.event_log.parent.mkdir(parents=True, exist_ok=True)
        with self.event_log.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        return {"ok": True, "id": event_id}

    def record_issue(self, meta: dict[str, Any], file: BinaryIO, filename: str) -> dict[str, Any]:
        issue_id = f"iss_{uuid.uuid4().hex}"
        self.issues_dir.mkdir(parents=True, exist_ok=True)
        target = self.issues_dir / f"{issue_id}.zip"
        with target.open("wb") as output:
            shutil.copyfileobj(file, output)

        record = {
            "id": issue_id,
            "received_at": _now_iso(),
            "sid": _text(meta.get("sid"), 160),
            "rid": _text(meta.get("rid"), 160),
            "app": _text(meta.get("app"), 80),
            "code": _text(meta.get("code"), 120),
            "filename": Path(filename or "issue.zip").name[:120],
            "size_bytes": target.stat().st_size,
            "download_url": f"/api/diag/v1/issues/{issue_id}/download",
        }
        with (self.issues_dir / "index.jsonl").open("a", encoding="utf-8") as index:
            index.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        return {"ok": True, "id": issue_id}

    def issue_file(self, issue_id: str) -> Optional[Path]:
        if not issue_id.startswith("iss_") or "/" in issue_id or "\\" in issue_id:
            return None
        path = self.issues_dir / f"{issue_id}.zip"
        if not path.is_file():
            return None
        return path

    def summary(self, days: int = 7) -> dict[str, Any]:
        days = max(1, min(days, 30))
        events = self._read_events()
        issues = self._read_issues()
        start_day = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
        filtered = [
            event for event in events
            if _parse_day(_payload(event).get("ts") or event.get("received_at")) >= start_day
        ]
        today = datetime.now(timezone.utc).date().isoformat()
        today_events = [
            event for event in filtered
            if str(_payload(event).get("ts") or event.get("received_at") or "")[:10] == today
        ]
        today_issues = [issue for issue in issues if str(issue.get("received_at", ""))[:10] == today]

        quick_totals = [
            _number(_payload(event).get("dur"), "total")
            for event in filtered
            if _payload(event).get("mode") == "quick" and _number(_payload(event).get("dur"), "total") is not None
        ]
        success_count = sum(1 for event in today_events if _payload(event).get("phase") == "done")
        failure_events = [
            event for event in filtered
            if _payload(event).get("phase") not in ("done", "cancelled") or _payload(event).get("err")
        ]
        success_events = [
            event for event in filtered
            if _payload(event).get("phase") == "done"
        ]

        recent_issues = [_issue_row(issue) for issue in issues[-20:][::-1]]
        return {
            "ok": True,
            "updated_at": _now_iso(),
            "today": {
                "runs": len(today_events),
                "success_rate": round(success_count / len(today_events), 4) if today_events else 0.0,
                "quick_p50_ms": _percentile(quick_totals, 50),
                "quick_p90_ms": _percentile(quick_totals, 90),
                "issues": len(today_issues),
            },
            "duration": {
                key: _duration_stats(filtered, key)
                for key in ("prep", "pose", "fit", "pack")
            },
            "quality": {
                "registered_ratio_avg": _average([
                    _number(_payload(event).get("solve"), "ratio") for event in filtered
                ]),
                "blur_avg": _average([
                    _number(_payload(event).get("in"), "blur") for event in filtered
                ]),
                "exposure_avg": _average([
                    _number(_payload(event).get("in"), "exposure") for event in filtered
                ]),
            },
            "risk_distribution": _distribution(filtered, "solve", "risk"),
            "failure_distribution": _failure_distribution(failure_events),
            "failure_reasons": _failure_reasons(failure_events),
            "mode_stats": _mode_stats(filtered, success_events),
            "frame_buckets": _frame_bucket_stats(success_events),
            "version_stats": _version_stats(filtered, success_events, failure_events),
            "recent_failures": [_failure_row(event, recent_issues) for event in failure_events[-20:][::-1]],
            "recent_issues": recent_issues,
        }

    def _read_events(self) -> list[dict[str, Any]]:
        if not self.event_log.exists():
            return []
        return _read_jsonl(self.event_log)

    def _read_issues(self) -> list[dict[str, Any]]:
        index = self.issues_dir / "index.jsonl"
        if not index.exists():
            return []
        return _read_jsonl(index)


def _clean_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"t", "sid", "rid", "ts", "app", "dev", "hw", "mode", "phase", "cfg", "dur", "in", "solve", "out", "err"}
    return {key: payload[key] for key in allowed if key in payload}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                values.append(value)
    return values


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _duration_stats(events: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [_number(_payload(event).get("dur"), key) for event in events]
    numbers = [value for value in values if value is not None]
    return {
        "p50": _percentile(numbers, 50),
        "p90": _percentile(numbers, 90),
        "max": max(numbers) if numbers else None,
    }


def _distribution(events: list[dict[str, Any]], section: str, key: str) -> list[dict[str, Any]]:
    counts = Counter(
        str(value)
        for event in events
        if (value := _value(_payload(event).get(section), key)) not in (None, "")
    )
    return [{key: name, "count": count} for name, count in counts.most_common()]


def _failure_distribution(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(str(_payload(event).get("phase") or "unknown") for event in events)
    return [{"phase": phase, "count": count} for phase, count in counts.most_common()]


def _failure_reasons(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        payload = _payload(event)
        error = payload.get("err") if isinstance(payload.get("err"), dict) else {}
        code = str(error.get("code") or payload.get("phase") or "unknown") if isinstance(error, dict) else "unknown"
        grouped.setdefault(code, []).append(event)
    rows = []
    for code, items in grouped.items():
        rows.append(
            {
                "code": code,
                "count": len(items),
                "phase": _most_common_payload_value(items, "phase"),
                "mode": _most_common_payload_value(items, "mode"),
            }
        )
    return sorted(rows, key=lambda item: (-int(item["count"]), str(item["code"])))


def _mode_stats(events: list[dict[str, Any]], success_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    modes = sorted({
        str(_payload(event).get("mode") or "unknown")
        for event in events
    })
    rows = []
    for mode in modes:
        mode_events = [event for event in events if (_payload(event).get("mode") or "unknown") == mode]
        mode_success = [event for event in success_events if (_payload(event).get("mode") or "unknown") == mode]
        totals = _duration_values(mode_success, "total")
        rows.append(
            {
                "mode": mode,
                "runs": len(mode_events),
                "success": len(mode_success),
                "failures": len(mode_events) - len(mode_success),
                "success_rate": round(len(mode_success) / len(mode_events), 4) if mode_events else 0.0,
                "avg_total_ms": _average(totals),
                "p50_total_ms": _percentile(totals, 50),
                "p90_total_ms": _percentile(totals, 90),
                "avg_total_per_frame_ms": _average(_per_frame_values(mode_success, "total")),
                "avg_fit_per_frame_ms": _average(_per_frame_values(mode_success, "fit")),
            }
        )
    return rows


def _frame_bucket_stats(success_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = [
        ("1-10", 1, 10),
        ("11-30", 11, 30),
        ("31-50", 31, 50),
        ("51-100", 51, 100),
        ("101-200", 101, 200),
        ("201+", 201, None),
    ]
    rows = []
    modes = sorted({
        str(_payload(event).get("mode") or "unknown")
        for event in success_events
    })
    for mode in modes:
        mode_rows = []
        mode_events = [event for event in success_events if (_payload(event).get("mode") or "unknown") == mode]
        for label, minimum, maximum in buckets:
            items = [
                event for event in mode_events
                if _frame_count(event) is not None
                and _frame_count(event) >= minimum
                and (maximum is None or _frame_count(event) <= maximum)
            ]
            if not items:
                continue
            mode_rows.append(
                {
                    "mode": mode,
                    "bucket": label,
                    "runs": len(items),
                    "avg_frames": _average([_frame_count(event) for event in items]),
                    "avg_total_ms": _average(_duration_values(items, "total")),
                    "avg_total_per_frame_ms": _average(_per_frame_values(items, "total")),
                    "avg_fit_per_frame_ms": _average(_per_frame_values(items, "fit")),
                }
            )
        baseline = next(
            (row["avg_total_per_frame_ms"] for row in mode_rows if row.get("avg_total_per_frame_ms")),
            None,
        )
        for row in mode_rows:
            row["trend"] = _trend(row.get("avg_total_per_frame_ms"), baseline)
        rows.extend(mode_rows)
    return rows


def _version_stats(
    events: list[dict[str, Any]],
    success_events: list[dict[str, Any]],
    failure_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    versions = sorted({_app_version(event) for event in events}, reverse=True)
    rows = []
    for version in versions:
        version_events = [event for event in events if _app_version(event) == version]
        version_success = [event for event in success_events if _app_version(event) == version]
        version_failures = [event for event in failure_events if _app_version(event) == version]
        rows.append(
            {
                "version": version,
                "runs": len(version_events),
                "success_rate": round(len(version_success) / len(version_events), 4) if version_events else 0.0,
                "avg_total_ms": _average(_duration_values(version_success, "total")),
                "avg_total_per_frame_ms": _average(_per_frame_values(version_success, "total")),
                "top_error": _top_error(version_failures),
            }
        )
    return rows


def _failure_row(event: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any]:
    payload = _payload(event)
    error = payload.get("err") if isinstance(payload.get("err"), dict) else {}
    app = payload.get("app") if isinstance(payload.get("app"), dict) else {}
    rid = payload.get("rid") or ""
    code = error.get("code", "") if isinstance(error, dict) else ""
    issue = _matching_issue(rid=rid, sid=payload.get("sid") or "", code=code, issues=issues)
    return {
        "ts": payload.get("ts") or event.get("received_at"),
        "sid": payload.get("sid") or "",
        "rid": rid,
        "app": app.get("v", "") if isinstance(app, dict) else "",
        "mode": payload.get("mode") or "",
        "phase": payload.get("phase") or "",
        "code": code,
        "download_url": issue.get("download_url", "") if issue else "",
    }


def _issue_row(issue: dict[str, Any]) -> dict[str, Any]:
    row = dict(issue)
    issue_id = str(row.get("id") or "")
    if issue_id and not row.get("download_url"):
        row["download_url"] = f"/api/diag/v1/issues/{issue_id}/download"
    return row


def _matching_issue(
    *,
    rid: Any,
    sid: Any,
    code: Any,
    issues: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    rid_text = str(rid or "")
    sid_text = str(sid or "")
    code_text = str(code or "")
    for issue in issues:
        if rid_text and str(issue.get("rid") or "") == rid_text:
            return issue
    for issue in issues:
        if (
            sid_text
            and code_text
            and str(issue.get("sid") or "") == sid_text
            and str(issue.get("code") or "") == code_text
        ):
            return issue
    return None


def _duration_values(events: list[dict[str, Any]], key: str) -> list[Union[float, int]]:
    values = [_number(_payload(event).get("dur"), key) for event in events]
    return [value for value in values if value is not None]


def _per_frame_values(events: list[dict[str, Any]], duration_key: str) -> list[float]:
    values: list[float] = []
    for event in events:
        frames = _frame_count(event)
        duration = _number(_payload(event).get("dur"), duration_key)
        if frames and frames > 0 and duration is not None:
            values.append(duration / frames)
    return values


def _frame_count(event: dict[str, Any]) -> Optional[int]:
    value = _number(_payload(event).get("in"), "frames")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _app_version(event: dict[str, Any]) -> str:
    app = _payload(event).get("app")
    version = _value(app, "v")
    return str(version or "unknown")


def _top_error(events: list[dict[str, Any]]) -> Optional[str]:
    codes = []
    for event in events:
        error = _payload(event).get("err")
        if isinstance(error, dict):
            code = error.get("code")
            if code:
                codes.append(str(code))
    if not codes:
        return None
    return Counter(codes).most_common(1)[0][0]


def _most_common_payload_value(events: list[dict[str, Any]], key: str) -> str:
    values = [str(_payload(event).get(key) or "unknown") for event in events]
    return Counter(values).most_common(1)[0][0] if values else "unknown"


def _trend(value: Any, baseline: Any) -> str:
    if not isinstance(value, (int, float)) or not isinstance(baseline, (int, float)) or baseline <= 0:
        return "unknown"
    if value >= baseline * 1.5:
        return "slower"
    if value <= baseline * 0.7:
        return "faster"
    return "baseline" if value == baseline else "similar"


def _number(section: Any, key: str) -> Optional[Union[float, int]]:
    value = _value(section, key)
    if isinstance(value, (int, float)):
        return value
    return None


def _value(section: Any, key: str) -> Any:
    if isinstance(section, dict):
        return section.get(key)
    return None


def _average(values: list[Optional[Union[float, int]]]) -> Optional[float]:
    numbers = [value for value in values if value is not None]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 4)


def _percentile(values: list[Union[float, int]], percentile: int) -> Optional[Union[float, int]]:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile / 100)
    return ordered[index]


def _parse_day(value: Any):
    text = str(value or "")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return datetime.now(timezone.utc).date()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, max_length: int) -> str:
    return str(value or "").strip()[:max_length]
