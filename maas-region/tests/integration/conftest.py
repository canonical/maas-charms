from pathlib import Path

import yaml

METADATA = yaml.safe_load(Path("./charmcraft.yaml").read_text())
APP_NAME = METADATA["name"]
POSTGRESQL_CHANNEL = "16/stable"
HAPROXY_CHANNEL = "2.8/edge"
