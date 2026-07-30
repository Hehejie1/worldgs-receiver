import json
import secrets
import time
import uuid
from pathlib import Path
from typing import Optional


class PairingStore:
    def __init__(self, ttl_seconds: int, devices_path: Optional[Path] = None) -> None:
        self.ttl_seconds = ttl_seconds
        self.devices_path = devices_path
        self._tokens: dict[str, float] = {}
        self._devices: dict[str, dict[str, str]] = self._read_devices()

    def create_token(self) -> str:
        token = secrets.token_urlsafe(24)
        self._tokens[token] = time.time() + self.ttl_seconds
        return token

    def consume_token(self, token: str) -> bool:
        expires_at = self._tokens.pop(token, None)
        if expires_at is None:
            return False
        return time.time() <= expires_at

    def expires_at(self, token: str) -> Optional[float]:
        return self._tokens.get(token)

    def exchange_token_for_device(self, token: str) -> Optional[dict[str, str]]:
        expires_at = self._tokens.pop(token, None)
        if expires_at is None or time.time() > expires_at:
            return None
        device = {
            "deviceId": uuid.uuid4().hex,
            "deviceToken": secrets.token_urlsafe(32),
        }
        self._devices[device["deviceToken"]] = device
        self._write_devices()
        return device

    def device_for_token(self, device_token: str) -> Optional[dict[str, str]]:
        if not device_token:
            return None
        return self._devices.get(device_token)

    def _read_devices(self) -> dict[str, dict[str, str]]:
        if self.devices_path is None or not self.devices_path.is_file():
            return {}
        try:
            payload = json.loads(self.devices_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        devices: dict[str, dict[str, str]] = {}
        for item in payload.get("devices", []):
            device_id = str(item.get("deviceId") or "")
            device_token = str(item.get("deviceToken") or "")
            if device_id and device_token:
                devices[device_token] = {"deviceId": device_id, "deviceToken": device_token}
        return devices

    def _write_devices(self) -> None:
        if self.devices_path is None:
            return
        self.devices_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"devices": list(self._devices.values())}
        self.devices_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
