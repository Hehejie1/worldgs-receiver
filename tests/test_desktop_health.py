from pathlib import Path

from fastapi.testclient import TestClient

from worldgs_receiver.app import create_app
from worldgs_receiver.config import ReceiverConfig


def test_healthz_returns_ready_status_and_output_dir(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path, port=8787)))

    response = client.get("/api/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "status": "ready",
        "port": 8787,
        "outputDir": str(tmp_path),
    }
