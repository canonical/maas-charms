# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""File containing constants to be used in the charm."""

BACKUP_ID_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

SNAP_CURRENT_PATH = "/var/snap/charmed-postgresql/current"
SNAP_CONF_PATH = f"{SNAP_CURRENT_PATH}/etc"

PGBACKREST_BACKUP_ID_FORMAT = "%Y%m%d-%H%M%S"
PGBACKREST_CONF_PATH = f"{SNAP_CONF_PATH}/pgbackrest"
PGBACKREST_CONFIGURATION_FILE = f"--config={PGBACKREST_CONF_PATH}/pgbackrest.conf"
PGBACKREST_EXECUTABLE = "charmed-postgresql.pgbackrest"
