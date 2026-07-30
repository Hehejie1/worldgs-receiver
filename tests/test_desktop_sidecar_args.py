from pathlib import Path

from desktop.sidecar.entrypoint import _argv_with_local_training_env
from worldgs_receiver.cli import main


def test_cli_accepts_loopback_host_port_and_output_for_desktop_mode(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls = {}

    def fake_run(app, host: str, port: int) -> None:
        calls["host"] = host
        calls["port"] = port

    monkeypatch.setattr("worldgs_receiver.cli.uvicorn.run", fake_run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "worldgs_receiver",
            "--host",
            "127.0.0.1",
            "--port",
            "8878",
            "--output",
            str(tmp_path),
        ],
    )

    main()

    assert calls == {"host": "127.0.0.1", "port": 8878}
    assert tmp_path.is_dir()


def test_sidecar_env_appends_local_training_command(monkeypatch) -> None:
    monkeypatch.setenv("WORLDGS_LOCAL_TRAINING_CWD", "/home/hehejie/difix3d_lab/Difix3D")
    monkeypatch.setenv(
        "WORLDGS_LOCAL_TRAINING_COMMAND",
        "python examples/gsplat/simple_trainer_difix3d.py --data_dir {dataset_dir} --result_dir {run_output_dir}",
    )

    argv = _argv_with_local_training_env(["receiver_sidecar", "--host", "127.0.0.1"])

    assert argv[:3] == ["receiver_sidecar", "--host", "127.0.0.1"]
    assert argv[-1] == "--local-training-cwd=/home/hehejie/difix3d_lab/Difix3D"
    command_args = [item for item in argv if item.startswith("--local-training-command=")]
    assert len(command_args) == 6
    assert "--local-training-command={dataset_dir}" in argv
    assert "--local-training-command={run_output_dir}" in argv


def test_sidecar_env_preserves_dash_prefixed_command_tokens(monkeypatch) -> None:
    monkeypatch.setenv("WORLDGS_LOCAL_TRAINING_COMMAND", "bash -lc 'echo ok'")

    argv = _argv_with_local_training_env(["receiver_sidecar"])

    assert "--local-training-command=bash" in argv
    assert "--local-training-command=-lc" in argv
    assert "--local-training-command=echo ok" in argv
