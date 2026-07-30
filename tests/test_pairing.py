from pathlib import Path

from worldgs_receiver.config import ReceiverConfig
from worldgs_receiver.pairing import PairingStore


def test_receiver_config_defaults(tmp_path: Path) -> None:
    config = ReceiverConfig(output_dir=tmp_path)

    assert config.host == "0.0.0.0"
    assert config.port == 8787
    assert config.output_dir == tmp_path
    assert config.token_ttl_seconds == 1800


def test_pairing_token_is_accepted_once() -> None:
    store = PairingStore(ttl_seconds=60)
    token = store.create_token()

    assert store.consume_token(token) is True
    assert store.consume_token(token) is False


def test_unknown_pairing_token_is_rejected() -> None:
    store = PairingStore(ttl_seconds=60)

    assert store.consume_token("missing") is False


def test_device_credential_persists_across_store_instances(tmp_path: Path) -> None:
    devices_path = tmp_path / "paired_devices.json"
    store = PairingStore(ttl_seconds=60, devices_path=devices_path)
    token = store.create_token()
    device = store.exchange_token_for_device(token)

    assert device is not None

    restored = PairingStore(ttl_seconds=60, devices_path=devices_path)

    assert restored.device_for_token(device["deviceToken"]) == device
