# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Backups implementation."""

import logging
import os
import tempfile
from contextlib import nullcontext
from datetime import datetime
from io import BytesIO
from subprocess import run
from typing import Any

from boto3.session import Session
from botocore import loaders
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError
from botocore.regions import EndpointResolver
from charms.data_platform_libs.v0.s3 import (
    CredentialsChangedEvent,
    S3Requirer,
)
from ops.charm import ActionEvent
from ops.framework import Object
from ops.model import ActiveStatus, MaintenanceStatus

logger = logging.getLogger(__name__)

BACKUP_ID_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ANOTHER_CLUSTER_REPOSITORY_ERROR_MESSAGE = "the S3 repository has backups from another cluster"
FAILED_TO_ACCESS_CREATE_BUCKET_ERROR_MESSAGE = (
    "failed to access/create the bucket, check your S3 settings"
)
S3_BLOCK_MESSAGES = [
    FAILED_TO_ACCESS_CREATE_BUCKET_ERROR_MESSAGE,
]


class MAASBackups(Object):
    """In this class, we manage MAASBackups backups."""

    def __init__(self, charm, relation_name: str):
        """Manage MAASBackups backups."""
        super().__init__(charm, "backup")
        self.charm = charm
        self.relation_name = relation_name

        # s3 relation handles the config options for s3 backups
        self.s3_client = S3Requirer(self.charm, self.relation_name)
        self.framework.observe(
            self.s3_client.on.credentials_changed, self._on_s3_credential_changed
        )
        # When the leader unit is being removed, s3_client.on.credentials_gone is performed on it (and only on it).
        # After a new leader is elected, the S3 connection must be reinitialized.
        self.framework.observe(self.charm.on.leader_elected, self._on_s3_credential_changed)
        self.framework.observe(self.s3_client.on.credentials_gone, self._on_s3_credential_gone)
        self.framework.observe(self.charm.on.create_backup_action, self._on_create_backup_action)
        self.framework.observe(self.charm.on.list_backups_action, self._on_list_backups_action)
        self.framework.observe(
            self.charm.on.restore_from_backup_action, self._on_restore_from_backup_action
        )

    def _get_s3_session_resource(self, s3_parameters: dict, ca_file: Any) -> Any:
        session = Session(
            aws_access_key_id=s3_parameters["access-key"],
            aws_secret_access_key=s3_parameters["secret-key"],
            region_name=s3_parameters["region"],
        )
        return session.resource(
            "s3",
            endpoint_url=self._construct_endpoint(s3_parameters),
            verify=(ca_file),
            config=Config(
                # https://github.com/boto/boto3/issues/4400#issuecomment-2600742103
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def _get_s3_session_client(self, s3_parameters: dict, ca_file: Any) -> Any:
        session = Session(
            aws_access_key_id=s3_parameters["access-key"],
            aws_secret_access_key=s3_parameters["secret-key"],
            region_name=s3_parameters["region"],
        )
        return session.client(
            "s3",
            endpoint_url=self._construct_endpoint(s3_parameters),
            verify=(ca_file),
            config=Config(
                # https://github.com/boto/boto3/issues/4400#issuecomment-2600742103
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def _are_backup_settings_ok(self) -> tuple[bool, str]:
        """Validate whether backup settings are OK."""
        if self.model.get_relation(self.relation_name) is None:
            return (
                False,
                "Relation with s3-integrator charm missing, cannot create/restore backup.",
            )

        _, missing_parameters = self._retrieve_s3_parameters()
        if missing_parameters:
            return False, f"Missing S3 parameters: {missing_parameters}"

        return True, ""

    def _can_unit_perform_backup(self) -> tuple[bool, str | None]:
        """Validate whether this unit can perform a backup."""
        if not self.charm.unit.is_leader():
            return False, "Unit is not the leader"

        if self.charm.is_blocked:
            return False, "Unit is in a blocking state"

        return self._are_backup_settings_ok()

    def can_use_s3_repository(self) -> tuple[bool, str | None]:
        """Return whether the charm was configured to use another cluster repository."""
        # Check model uuid
        s3_parameters, _ = self._retrieve_s3_parameters()
        s3_model_uuid = self._read_content_from_s3(
            "model-uuid.txt",
            s3_parameters,
        )

        if s3_model_uuid and s3_model_uuid.strip() != self.model.uuid:
            logger.debug(
                f"can_use_s3_repository: incompatible model-uuid s3={s3_model_uuid.strip()}, local={self.model.uuid}"
            )
            return False, ANOTHER_CLUSTER_REPOSITORY_ERROR_MESSAGE

        return True, None

    def _construct_endpoint(self, s3_parameters: dict) -> str:
        """Construct the S3 service endpoint using the region.

        This is needed when the provided endpoint is from AWS, and it doesn't contain the region.
        """
        # Use the provided endpoint if a region is not needed.
        endpoint = s3_parameters["endpoint"]

        # Load endpoints data.
        loader = loaders.create_loader()
        data = loader.load_data("endpoints")

        # Construct the endpoint using the region.
        resolver = EndpointResolver(data)
        endpoint_data = resolver.construct_endpoint("s3", s3_parameters["region"])

        # Use the built endpoint if it is an AWS endpoint.
        if endpoint_data and endpoint.endswith(endpoint_data["dnsSuffix"]):
            endpoint = f"{endpoint.split('://')[0]}://{endpoint_data['hostname']}"

        return endpoint

    def _execute_command(
        self,
        command: list[str],
        command_input: bytes | None = None,
        timeout: int | None = None,
    ) -> tuple[int, str, str]:
        """Execute a command in the workload container."""
        # Input is generated by the charm
        process = run(
            command,
            input=command_input,
            capture_output=True,
            timeout=timeout,
        )
        return process.returncode, process.stdout.decode(), process.stderr.decode()

    def _format_backup_list(self, backup_list, s3_bucket, s3_path) -> str:
        """Format provided list of backups as a table."""
        backups = [
            f"Storage bucket name: {s3_bucket:s}",
            f"Backups base path: {s3_path:s}/backup/\n",
            "{:<20s} | {:<19s} | {:<8s} | {:s}".format(
                "backup-id",
                "action",
                "status",
                "backup-path",
            ),
        ]
        backups.append("-" * len(backups[2]))
        for (
            backup_id,
            backup_action,
            backup_status,
            path,
        ) in backup_list:
            backups.append(
                f"{backup_id:<20s} | {backup_action:<19s} | {backup_status:<8s} | {path:s}"
            )
        return "\n".join(backups)

    def _generate_backup_list_output(self) -> str:
        """Generate a list of backups in a formatted table.

        List contains successful and failed backups in order of ascending time.
        """
        s3_parameters, _ = self._retrieve_s3_parameters()
        backups = self._list_backups(s3_parameters)
        backup_list = []
        for backup in backups:
            backup_action = "full backup"
            backup_path = f"/{s3_parameters['path'].lstrip('/')}/backup/{backup['id']}"
            backup_status = "finished"
            backup_list.append(
                (
                    backup["id"],
                    backup_action,
                    backup_status,
                    backup_path,
                )
            )

        backup_list.sort(key=lambda x: x[0])

        return self._format_backup_list(
            backup_list, s3_parameters["bucket"], s3_parameters["path"]
        )

    def _list_backups(self, s3_parameters) -> list[dict]:
        """Retrieve the list of backups.

        Returns:
            a list of previously created backups or an empty list if there are no backups in the S3
                bucket.
        """
        backups = []
        ca_chain = s3_parameters.get("tls-ca-chain", [])
        with tempfile.NamedTemporaryFile() if ca_chain else nullcontext() as ca_file:
            if ca_file:
                ca = "\n".join(ca_chain)
                ca_file.write(ca.encode())
                ca_file.flush()

                s3 = self._get_s3_session_client(s3_parameters, ca_file.name)
            else:
                s3 = self._get_s3_session_client(s3_parameters, None)

            paginator = s3.get_paginator("list_objects_v2")
            page_iterator = paginator.paginate(
                Bucket=s3_parameters["bucket"],
                Prefix=f"{s3_parameters['path'].lstrip('/')}/backup/",
                Delimiter="/",
            )
            for page in page_iterator:
                for common_prefix in page.get("CommonPrefixes", []):
                    backups.append(
                        {
                            "id": common_prefix["Prefix"]
                            .lstrip(f"{s3_parameters['path'].lstrip('/')}/backup/")
                            .rstrip("/")
                        }
                    )

        return backups

    def _on_s3_credential_changed(self, event: CredentialsChangedEvent) -> None:
        if not self.charm.unit.is_leader():
            return

        s3_parameters, _ = self._retrieve_s3_parameters()
        if not s3_parameters:
            event.defer()
            return

        self._upload_content_to_s3(
            self.model.uuid,
            "model-uuid.txt",
            s3_parameters,
        )

    def _on_s3_credential_gone(self, event) -> None:
        if self.charm.is_blocked and self.charm.unit.status.message in S3_BLOCK_MESSAGES:
            self.charm.unit.status(ActiveStatus())

    def _on_create_backup_action(self, event) -> None:
        logger.info("A backup has been requested on unit")
        can_unit_perform_backup, validation_message = self._can_unit_perform_backup()
        if not can_unit_perform_backup:
            logger.error(f"Backup failed: {validation_message}")
            event.fail(validation_message)
            return

        # Retrieve the S3 Parameters to use when uploading the backup logs to S3.
        s3_parameters, _ = self._retrieve_s3_parameters()

        # Test uploading metadata to S3 to test credentials before backup.
        datetime_backup_requested = datetime.now().strftime(BACKUP_ID_FORMAT)
        metadata = f"""Date Backup Requested: {datetime_backup_requested}
Model Name: {self.model.name}
Application Name: {self.model.app.name}
Unit Name: {self.charm.unit.name}
Juju Version: {self.charm.model.juju_version!s}
"""

        if not self._upload_content_to_s3(
            metadata,
            "backup/latest",
            s3_parameters,
        ):
            error_message = "Failed to upload metadata to provided S3"
            logger.error(f"Backup failed: {error_message}")
            event.fail(error_message)
            return

        self.charm.unit.status(MaintenanceStatus("creating backup"))

        self._run_backup(event, s3_parameters, datetime_backup_requested)

        self.charm.unit.status(ActiveStatus())

    def _run_backup(
        self,
        event: ActionEvent,
        s3_parameters: dict,
        datetime_backup_requested: str,
    ) -> None:
        command = [
            # TODO: fill in the details
            "ls -l",
        ]
        return_code, stdout, stderr = self._execute_command(command)
        if return_code != 0:
            error_message = f"Failed to backup MAAS with error: {stderr}"
            logger.error(f"Backup failed: {error_message}")
            event.fail(error_message)
        else:
            backup_id = datetime_backup_requested

            # Upload the logs to S3 and fail the action if it doesn't succeed.
            logs = f"""Stdout:
{stdout}

Stderr:
{stderr}
"""

            if not self._upload_content_to_s3(
                logs,
                f"backup/{backup_id}/backup.log",
                s3_parameters["bucket"],
            ):
                error_message = "Error uploading logs to S3"
                logger.error(f"Backup failed: {error_message}")
                event.fail(error_message)
            else:
                logger.info(f"Backup succeeded: with backup-id {backup_id}")
                event.set_results({"backup-status": "backup created"})

    def _on_list_backups_action(self, event) -> None:
        """List the previously created backups."""
        are_backup_settings_ok, validation_message = self._are_backup_settings_ok()
        if not are_backup_settings_ok:
            logger.warning(validation_message)
            event.fail(validation_message)
            return

        try:
            formatted_list = self._generate_backup_list_output()
            event.set_results({"backups": formatted_list})
        except BotoCoreError as e:
            logger.exception(e)
            event.fail(f"Failed to list MAAS backups with error: {e!s}")

    def _on_restore_from_backup_action(self, event):
        if not self._pre_restore_checks(event):
            return

        backup_id = event.params.get("backup-id")
        logger.info(f"A restore with backup-id {backup_id} has been requested on the unit")

        # Validate the provided backup id
        logger.info("Validating provided backup-id")
        try:
            s3_parameters, _ = self._retrieve_s3_parameters()
            backups = [b["id"] for b in self._list_backups(s3_parameters)]
            is_backup_id_real = backup_id and backup_id in backups
            if backup_id and not is_backup_id_real:
                error_message = f"Invalid backup-id: {backup_id}"
                logger.error(f"Restore failed: {error_message}")
                event.fail(error_message)
                return
        except BotoCoreError as e:
            logger.exception(e)
            error_message = "Failed to retrieve backups list"
            logger.error(f"Restore failed: {error_message}")
            event.fail(error_message)
            return

        self.charm.unit.status(MaintenanceStatus("restoring backup"))

        # Step 1
        logger.info("Step 1")
        # ...
        # ...
        # Step N
        logger.info("Step N")

        event.set_results({"restore-status": "restore finished"})

    def _pre_restore_checks(self, event: ActionEvent) -> bool:
        """Run some checks before starting the restore.

        Returns:
            a boolean indicating whether restore should be run.
        """
        are_backup_settings_ok, validation_message = self._are_backup_settings_ok()
        if not are_backup_settings_ok:
            logger.error(f"Restore failed: {validation_message}")
            event.fail(validation_message)
            return False

        if not event.params.get("backup-id"):
            error_message = "Backup-id parameter need to be provided to be able to do restore"
            logger.error(f"Restore failed: {error_message}")
            event.fail(error_message)
            return False

        logger.info("Checking if cluster is in blocked state")
        if self.charm.is_blocked:
            error_message = "Cluster or unit is in a blocking state"
            logger.error(f"Restore failed: {error_message}")
            event.fail(error_message)
            return False

        logger.info("Checking that this unit was already elected the leader unit")
        if not self.charm.unit.is_leader():
            error_message = "Unit cannot restore backup as it was not elected the leader unit yet"
            logger.error(f"Restore failed: {error_message}")
            event.fail(error_message)
            return False

        return True

    def _retrieve_s3_parameters(self) -> tuple[dict, list[str]]:
        """Retrieve S3 parameters from the S3 integrator relation."""
        s3_parameters = self.s3_client.get_s3_connection_info()
        required_parameters = [
            "bucket",
            "access-key",
            "secret-key",
        ]
        missing_required_parameters = [
            param for param in required_parameters if param not in s3_parameters
        ]
        if missing_required_parameters:
            logger.warning(
                f"Missing required S3 parameters in relation with S3 integrator: {missing_required_parameters}"
            )
            return {}, missing_required_parameters

        # Add some sensible defaults (as expected by the code) for missing optional parameters
        s3_parameters.setdefault("endpoint", "https://s3.amazonaws.com")
        s3_parameters.setdefault("region", "")
        s3_parameters.setdefault("path", "")
        s3_parameters.setdefault("s3-uri-style", "host")
        s3_parameters.setdefault("delete-older-than-days", "9999999")

        # Strip whitespaces from all parameters.
        for key, value in s3_parameters.items():
            if isinstance(value, str):
                s3_parameters[key] = value.strip()

        # Clean up extra slash symbols to avoid issues on 3rd-party storages
        # like Ceph Object Gateway (RadosGW).
        s3_parameters["endpoint"] = s3_parameters["endpoint"].rstrip("/")
        s3_parameters["path"] = f"/{s3_parameters['path'].strip('/')}"
        s3_parameters["bucket"] = s3_parameters["bucket"].strip("/")

        return s3_parameters, []

    def _upload_content_to_s3(self, content: str, s3_path: str, s3_parameters: dict) -> bool:
        """Upload the provided contents to the provided S3 bucket relative to the path from the S3 config.

        Args:
            content: The content to upload to S3
            s3_path: The S3 path from which download the content
            s3_parameters: The S3 parameters needed to perform the request

        Returns:
            a boolean indicating success.
        """
        ca_chain = s3_parameters.get("tls-ca-chain", [])
        with tempfile.NamedTemporaryFile() if ca_chain else nullcontext() as ca_file:
            if ca_file:
                ca = "\n".join(ca_chain)
                ca_file.write(ca.encode())
                ca_file.flush()

                s3 = self._get_s3_session_resource(s3_parameters, ca_file.name)
            else:
                s3 = self._get_s3_session_resource(s3_parameters, None)

            path = os.path.join(s3_parameters["path"], s3_path).lstrip("/")

            try:
                logger.info(f"Uploading content to bucket={s3_parameters['bucket']}, path={path}")
                bucket = s3.Bucket(s3_parameters["bucket"])

                with tempfile.NamedTemporaryFile() as temp_file:
                    temp_file.write(content.encode("utf-8"))
                    temp_file.flush()
                    bucket.upload_file(temp_file.name, path)
            except Exception as e:
                logger.exception(
                    f"Failed to upload content to S3 bucket={s3_parameters['bucket']}, path={path}",
                    exc_info=e,
                )
                return False

        return True

    def _read_content_from_s3(self, s3_path: str, s3_parameters: dict) -> str | None:
        """Read specified content from the provided S3 bucket relative to the path from the S3 config.

        Args:
            s3_path: The S3 path from which download the content
            s3_parameters: The S3 parameters needed to perform the request

        Returns:
            a string with the content if object is successfully downloaded and None if file is not existing or error
            occurred during download.
        """
        ca_chain = s3_parameters.get("tls-ca-chain", [])
        with tempfile.NamedTemporaryFile() if ca_chain else nullcontext() as ca_file:
            if ca_file:
                ca = "\n".join(ca_chain)
                ca_file.write(ca.encode())
                ca_file.flush()

                s3 = self._get_s3_session_resource(s3_parameters, ca_file.name)
            else:
                s3 = self._get_s3_session_resource(s3_parameters, None)

            path = os.path.join(s3_parameters["path"], s3_path).lstrip("/")

            try:
                logger.info(f"Reading content from bucket={s3_parameters['bucket']}, path={path}")
                bucket = s3.Bucket(s3_parameters["bucket"])
                with BytesIO() as buf:
                    bucket.download_fileobj(path, buf)
                    return buf.getvalue().decode("utf-8")
            except ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    logger.info(
                        f"No such object to read from S3 bucket={s3_parameters['bucket']}, path={path}"
                    )
                else:
                    logger.exception(
                        f"Failed to read content from S3 bucket={s3_parameters['bucket']}, path={path}",
                        exc_info=e,
                    )
            except Exception as e:
                logger.exception(
                    f"Failed to read content from S3 bucket={s3_parameters['bucket']}, path={path}",
                    exc_info=e,
                )

        return None
