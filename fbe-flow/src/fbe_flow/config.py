import os
from dataclasses import dataclass
from pathlib import Path


def default_data_dir() -> Path:
    if override := os.environ.get("FBE_FLOW_DATA_DIR"):
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "FBE Flow"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "fbe-flow"


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    worker_enabled: bool = True

    @property
    def database_path(self) -> Path:
        return self.data_dir / "flow.sqlite3"
