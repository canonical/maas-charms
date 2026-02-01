from pathlib import Path

import yaml

METADATA = yaml.safe_load(Path("./charmcraft.yaml").read_text())
APP_NAME = METADATA["name"]
POSTGRESQL_CHANNEL = "16/stable"
MAAS_PEER_NAME = METADATA["peers"]["maas-cluster"]["interface"]
MAAS_INIT_RELATION = METADATA["peers"]["maas_init"]["interface"]
