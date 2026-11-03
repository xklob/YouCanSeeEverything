from dataclasses import dataclass, field
from pathlib import Path
import os
import json

PROJECT_ROOT = Path(__file__).resolve().parents[1]

def load_profile():
    """An optional private deployment profile, outside the application checkout."""
    path = os.environ.get("BIOGRAPHY_PROFILE")
    if not path:
        return {}
    value = json.loads(Path(path).expanduser().read_text())
    if not isinstance(value, dict):
        raise ValueError("BIOGRAPHY_PROFILE must contain a JSON object")
    return value

PROFILE = load_profile()
STATE_DIR = Path(os.environ.get("BIOGRAPHY_STATE_DIR", PROFILE.get("state_dir", str(PROJECT_ROOT / ".local")))).expanduser()

@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    state_dir: Path = STATE_DIR
    data_dir: Path = Path(os.environ.get("BIOGRAPHY_DATA_DIR", PROFILE.get("data_dir", str(STATE_DIR / "vault")))).expanduser()
    export_root: Path = Path(os.environ.get("BIOGRAPHY_EXPORT_ROOT", PROFILE.get("export_root", str(Path.home() / "Data Exports")))).expanduser()
    account_key: str = str(PROFILE.get("account_key", "owner"))
    excluded_directory_names: tuple[str, ...] = tuple(str(x).casefold() for x in PROFILE.get("excluded_directory_names", []))
    reserve_bytes: int = int(os.environ.get("BIOGRAPHY_RESERVE_BYTES", str(20 * 1024**3)))
    # Browser-restricted key stays outside profiles, source control and builds.
    google_maps_api_key: str = field(default=os.environ.get("BIOGRAPHY_GOOGLE_MAPS_API_KEY", "").strip(), repr=False)

    @property
    def database(self):
        return self.data_dir / "catalog.sqlite3"

    @property
    def revisions_dir(self):
        return self.state_dir / "revisions"

settings = Settings()
