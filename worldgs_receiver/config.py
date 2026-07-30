from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ReceiverConfig:
    output_dir: Path
    host: str = "0.0.0.0"
    port: int = 8787
    token_ttl_seconds: int = 1800
    max_upload_bytes: int = 10 * 1024 * 1024 * 1024
    max_sync_file_bytes: int = 512 * 1024 * 1024
    max_sync_total_bytes: int = 20 * 1024 * 1024 * 1024
    max_sync_files: int = 5000
    analytics_dir: Optional[Path] = None
    dashboard_username: str = "worldgs"
    dashboard_password: str = "worldgs-admin"
    pointcosm_base_url: str = "https://3d.explorerglobal.cn/compute"
    default_automation_platform: str = "explorerglobal"
    explorerglobal_base_url: str = "https://3d.explorerglobal.cn/compute"
    local_training_enabled: bool = False
    local_training_command: tuple[str, ...] = ()
    local_training_cwd: Optional[Path] = None
    local_training_env: tuple[tuple[str, str], ...] = ()
