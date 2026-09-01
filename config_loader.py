from pathlib import Path
import yaml


LABS_DIR = Path(__file__).parent / "labs"


def load_lab_config(lab_id):
    config_path = LABS_DIR / lab_id / "config.yaml"

    if not config_path.exists():
        raise ValueError(f"Unknown lab: {lab_id}")

    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def list_labs():
    return sorted(
        path.name
        for path in LABS_DIR.iterdir()
        if path.is_dir() and (path / "config.yaml").exists()
    )