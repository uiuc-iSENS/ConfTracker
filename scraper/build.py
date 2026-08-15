"""Build site/data.json from the per-conference YAML files."""

import json
from datetime import datetime, timezone

import yaml

from . import config


def build(conf_dir=config.CONF_DIR, out_path=config.SITE_DATA) -> int:
    conferences = []
    for path in sorted(conf_dir.glob("*.yml")):
        conferences.append(yaml.safe_load(path.read_text())[0])

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "conferences": conferences,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return len(conferences)
