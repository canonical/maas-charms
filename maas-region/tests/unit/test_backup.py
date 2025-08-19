# Copyright 2025 Canonical
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import logging
import subprocess
import unittest
from unittest.mock import MagicMock, PropertyMock, call, patch

import ops
import ops.testing
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import BotoCoreError, ClientError, ConnectTimeoutError, SSLError

from backups import (
    FAILED_TO_ACCESS_CREATE_BUCKET_ERROR_MESSAGE,
    DownloadProgressPercentage,
    RegionsNotAvailableError,
    UploadProgressPercentage,
    as_size,
)
from charm import MaasRegionCharm


class TestMAASBackups(unittest.TestCase):
    def setUp(self):
        self.harness = ops.testing.Harness(MaasRegionCharm)
        self.addCleanup(self.harness.cleanup)

    @patch("boto3.session.Session.resource")
    @patch("backups.Config")
    def test_get_s3_session_resource(self, _config, _resource):
        self.harness.begin()

        s3_parameters = {
            "bucket": "test-bucket",
            "access-key": "test-access-key",
            "secret-key": "test-secret-key",
            "endpoint": "https://s3.amazonaws.com",
            "path": "/test-path",
            "region": "us-east-1",
        }
        ca_file_path = "/path"

        self.harness.charm.backup._get_s3_session_resource(s3_parameters, ca_file_path)

        _resource.assert_called_once_with(
            "s3",
            endpoint_url="https://s3.us-east-1.amazonaws.com",
            verify=ca_file_path,
            config=_config.return_value,
        )
        _config.assert_called_once_with(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )

    @patch("boto3.session.Session.client")
    @patch("backups.Config")
    def test_get_s3_session_client(self, _config, _client):
        self.harness.begin()

        s3_parameters = {
            "bucket": "test-bucket",
            "access-key": "test-access-key",
            "secret-key": "test-secret-key",
            "endpoint": "https://s3.amazonaws.com",
            "path": "/test-path",
            "region": "us-east-1",
        }
        ca_file_path = "/path"

        self.harness.charm.backup._get_s3_session_client(s3_parameters, ca_file_path)

        _client.assert_called_once_with(
            "s3",
            endpoint_url="https://s3.us-east-1.amazonaws.com",
            verify=ca_file_path,
            config=_config.return_value,
        )
        _config.assert_called_once_with(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )

    @patch("backups.MAASBackups._retrieve_s3_parameters")
    def test_are_backup_settings_ok(self, s3_parameters):
        self.harness.begin()
        self.harness.add_relation("s3-parameters", "s3-integrator")
        s3_parameters.return_value = {}, []
        self.assertEqual(self.harness.charm.backup._are_backup_settings_ok(), (True, ""))

    @patch("backups.MAASBackups._retrieve_s3_parameters")
    def test_are_backup_settings_ok__missing_relation(self, s3_parameters):
        self.harness.begin()
        s3_parameters.return_value = {}, []
        self.assertEqual(
            self.harness.charm.backup._are_backup_settings_ok(),
            (False, "Relation with s3-integrator charm missing, cannot create/restore backup."),
        )

    @patch("backups.MAASBackups._retrieve_s3_parameters")
    def test_are_backup_settings__missing_parameters(self, s3_parameters):
        self.harness.begin()
        self.harness.add_relation("s3-parameters", "s3-integrator")
        s3_parameters.return_value = {}, ["bucket"]
        self.assertEqual(
            self.harness.charm.backup._are_backup_settings_ok(),
            (False, "Missing S3 parameters: ['bucket']"),
        )

    @patch("backups.MAASBackups._are_backup_settings_ok")
    def test_can_unit_perform_backup(self, backup_settings):
        self.harness.begin()
        self.harness.set_leader(True)
        backup_settings.return_value = (True, "")
        self.assertEqual(self.harness.charm.backup._can_unit_perform_backup(), (True, ""))

    def test_can_unit_perform_backup__no_leader(self):
        self.harness.begin()
        self.harness.set_leader(False)
        self.assertEqual(
            self.harness.charm.backup._can_unit_perform_backup(), (False, "Unit is not the leader")
        )

    def test_can_unit_perform_backup__blocker(self):
        self.harness.begin()
        self.harness.set_leader(True)
        self.harness.charm.unit.status = ops.BlockedStatus("fake blocked state")
        self.assertEqual(
            self.harness.charm.backup._can_unit_perform_backup(),
            (False, "Unit is in a blocking state"),
        )

    @patch("backups.MAASBackups._retrieve_s3_parameters")
    @patch("backups.MAASBackups._read_content_from_s3")
    def test_can_use_s3_repository(self, read_content, s3_parameters):
        s3_parameters.return_value = {}, []
        read_content.return_value = "123-456"
        self.harness.set_model_uuid("123-456")
        self.harness.begin()
        self.assertEqual(self.harness.charm.backup._can_use_s3_repository(), (True, ""))

    @patch("backups.MAASBackups._retrieve_s3_parameters")
    @patch("backups.MAASBackups._read_content_from_s3")
    def test_can_use_s3_repository__incompatible(self, read_content, s3_parameters):
        s3_parameters.return_value = {}, []
        read_content.return_value = "456-789"
        self.harness.set_model_uuid("123-456")
        self.harness.begin()
        self.assertEqual(
            self.harness.charm.backup._can_use_s3_repository(),
            (False, "the S3 repository has backups from another cluster"),
        )

    def test_construct_endpoint(self):
        s3_parameters = {"endpoint": "https://10.10.10.10:9000", "region": ""}
        self.harness.begin()
        self.assertEqual(
            self.harness.charm.backup._construct_endpoint(s3_parameters), s3_parameters["endpoint"]
        )

    def test_construct_endpoint__aws(self):
        s3_parameters = {"endpoint": "https://s3.amazonaws.com", "region": "us-east-1"}
        self.harness.begin()
        self.assertEqual(
            self.harness.charm.backup._construct_endpoint(s3_parameters),
            "https://s3.us-east-1.amazonaws.com",
        )

    @patch("backups.MAASBackups._get_s3_session_resource")
    def test_create_bucket_if_not_exists(self, _resource):
        s3_parameters = {
            "endpoint": "https://s3.amazonaws.com",
            "region": "us-east-1",
            "bucket": "maas",
        }
        self.harness.begin()
        _resource.side_effect = None

        # Test when the bucket already exists.
        head_bucket = _resource.return_value.Bucket.return_value.meta.client.head_bucket
        create = _resource.return_value.Bucket.return_value.create
        wait_until_exists = _resource.return_value.Bucket.return_value.wait_until_exists
        self.harness.charm.backup._create_bucket_if_not_exists(s3_parameters)
        head_bucket.assert_called_once()
        create.assert_not_called()
        wait_until_exists.assert_not_called()

        # Test when the bucket doesn't exist.
        s3_parameters["tls-ca-chain"] = ["one", "two"]
        head_bucket.reset_mock()
        head_bucket.side_effect = ClientError(
            error_response={"Error": {"Code": "SomeFakeException", "message": "fake error"}},
            operation_name="fake operation name",
        )
        self.harness.charm.backup._create_bucket_if_not_exists(s3_parameters)
        head_bucket.assert_called_once()
        create.assert_called_once()
        wait_until_exists.assert_called_once()

        # Test when the bucket creation fails.
        head_bucket.reset_mock()
        create.reset_mock()
        wait_until_exists.reset_mock()
        create.side_effect = ClientError(
            error_response={"Error": {"Code": "SomeFakeException", "message": "fake error"}},
            operation_name="fake operation name",
        )
        with self.assertRaises(ClientError):
            self.harness.charm.backup._create_bucket_if_not_exists(s3_parameters)
        head_bucket.assert_called_once()
        create.assert_called_once()
        wait_until_exists.assert_not_called()

        # Test when the bucket creation fails with InvalidLocationConstraint.
        head_bucket.reset_mock()
        create.reset_mock()
        wait_until_exists.reset_mock()
        create.side_effect = ClientError(
            error_response={
                "Error": {"Code": "InvalidLocationConstraint", "message": "fake error"}
            },
            operation_name="fake operation name",
        )
        with self.assertRaises(ClientError):
            self.harness.charm.backup._create_bucket_if_not_exists(s3_parameters)
        head_bucket.assert_called_once()
        want = [
            call(CreateBucketConfiguration={"LocationConstraint": "us-east-1"}),
            call(),
        ]
        create.assert_has_calls(want)
        wait_until_exists.assert_not_called()

        # Test when the bucket creation fails with InvalidLocationConstraint but second create succeeds.
        head_bucket.reset_mock()
        create.reset_mock()
        wait_until_exists.reset_mock()
        create.side_effect = [
            ClientError(
                error_response={
                    "Error": {"Code": "InvalidLocationConstraint", "message": "fake error"}
                },
                operation_name="fake operation name",
            ),
            None,
        ]
        self.harness.charm.backup._create_bucket_if_not_exists(s3_parameters)
        head_bucket.assert_called_once()
        want = [
            call(CreateBucketConfiguration={"LocationConstraint": "us-east-1"}),
            call(),
        ]
        create.assert_has_calls(want)
        wait_until_exists.assert_called_once()

        # Test when the bucket creation fails due to a timeout error.
        head_bucket.reset_mock()
        create.reset_mock()
        wait_until_exists.reset_mock()
        head_bucket.side_effect = ConnectTimeoutError(endpoint_url="fake endpoint URL")
        with self.assertRaises(ConnectTimeoutError):
            self.harness.charm.backup._create_bucket_if_not_exists(s3_parameters)
        head_bucket.assert_called_once()
        create.assert_not_called()
        wait_until_exists.assert_not_called()

        # Test when the bucket creation fails due to a SSL error.
        head_bucket.reset_mock()
        create.reset_mock()
        wait_until_exists.reset_mock()
        head_bucket.side_effect = SSLError(
            error="fake error",
            endpoint_url="fake endpoint URL",
        )
        with self.assertRaises(SSLError):
            self.harness.charm.backup._create_bucket_if_not_exists(s3_parameters)
        head_bucket.assert_called_once()
        create.assert_not_called()
        wait_until_exists.assert_not_called()

    def test_format_backup_list(self):
        self.harness.begin()

        # Test when there are no backups.
        self.assertEqual(
            self.harness.charm.backup._format_backup_list([], "test-bucket", "/test-path"),
            """Storage bucket name: test-bucket
Backups base path: /test-path/backup/

backup-id            | action      | status   | maas     | size       | controllers            | backup-path
------------------------------------------------------------------------------------------------------------""",
        )

        # Test when there are backups.
        backup_list = [
            (
                "2023-01-01T09:00:00Z",
                "full backup",
                "failed",
                "3.6.0",
                "772.56 MiB",
                "abc123, def456, ghi789",
                "a/b/c",
            ),
            (
                "2023-01-01T10:00:00Z",
                "full backup",
                "finished",
                "3.6.1",
                "123.56 MiB",
                "abc123, def456, ghi789",
                "a/b/d",
            ),
            (
                "2023-01-01T11:00:00Z",
                "full backup",
                "finished",
                "3.6.2",
                "42.42 GiB",
                "abc123, def456, ghi789",
                "n/a",
            ),
        ]
        self.assertEqual(
            self.harness.charm.backup._format_backup_list(
                backup_list, "test-bucket", "/test-path"
            ),
            """Storage bucket name: test-bucket
Backups base path: /test-path/backup/

backup-id            | action      | status   | maas     | size       | controllers            | backup-path
------------------------------------------------------------------------------------------------------------
2023-01-01T09:00:00Z | full backup | failed   | 3.6.0    | 772.56 MiB | abc123, def456, ghi789 | a/b/c
2023-01-01T10:00:00Z | full backup | finished | 3.6.1    | 123.56 MiB | abc123, def456, ghi789 | a/b/d
2023-01-01T11:00:00Z | full backup | finished | 3.6.2    | 42.42 GiB  | abc123, def456, ghi789 | n/a""",
        )

    @patch("backups.MAASBackups._retrieve_s3_parameters")
    @patch("backups.MAASBackups._list_backups")
    def test_generate_backup_list_output(self, list_backups, s3_parameters):
        self.harness.begin()

        s3_parameters.return_value = (
            {
                "bucket": "test-bucket",
                "access-key": " test-access-key ",
                "secret-key": " test-secret-key ",
                "path": "/test-path",
            },
            [],
        )
        list_backups.return_value = [
            {
                "id": "2024-10-14T20:27:32Z",
                "size": "772.56 MiB",
                "controller_ids": ["abc123", "def456", "ghi789"],
                "completed": False,
                "maas_version": "3.6.0",
            },
            {
                "id": "2024-10-14T20:27:32Z",
                "size": "42.42 GiB",
                "controller_ids": ["abc123", "def456", "ghi789"],
                "completed": True,
                "maas_version": "3.6.1",
            },
        ]

        self.assertEqual(
            self.harness.charm.backup._generate_backup_list_output(),
            """Storage bucket name: test-bucket
Backups base path: /test-path/backup/

backup-id            | action      | status   | maas     | size       | controllers            | backup-path
------------------------------------------------------------------------------------------------------------
2024-10-14T20:27:32Z | full backup | failed   | 3.6.0    | 772.56 MiB | abc123, def456, ghi789 | /test-path/backup/2024-10-14T20:27:32Z
2024-10-14T20:27:32Z | full backup | finished | 3.6.1    | 42.42 GiB  | abc123, def456, ghi789 | /test-path/backup/2024-10-14T20:27:32Z""",
        )

    @patch("backups.MAASBackups._get_backup_details")
    @patch("backups.MAASBackups._get_s3_session_client")
    def test_list_backups(self, _client, _backup_details):
        self.harness.begin()

        s3_parameters = {
            "bucket": "test-bucket",
            "access-key": " test-access-key ",
            "secret-key": " test-secret-key ",
            "path": "/test-path",
        }
        backup_details = {
            "id": "123-456",
            "size": "772.56 MiB",
            "controller_ids": ["abc123", "def456", "ghi789"],
            "completed": True,
            "maas_version": "3.6.1",
        }
        _client.return_value.get_paginator.return_value.paginate.return_value = [
            {"CommonPrefixes": [{"Prefix": "123-456"}]}
        ]
        _backup_details.return_value = backup_details
        self.assertEqual(self.harness.charm.backup._list_backups(s3_parameters), [backup_details])

        # Test listing backups with TLS CA chain
        s3_parameters["tls-ca-chain"] = ["one", "two"]
        self.assertEqual(self.harness.charm.backup._list_backups(s3_parameters), [backup_details])

    @patch("backups.MAASBackups._retrieve_s3_parameters")
    @patch("backups.MAASBackups._create_bucket_if_not_exists")
    @patch("backups.MAASBackups._can_use_s3_repository")
    @patch("backups.MAASBackups._upload_content_to_s3")
    def test_on_s3_credential_changed(
        self, upload_to_s3, can_use_s3, create_bucket, s3_parameters
    ):
        s3_parameters_dict = {
            "bucket": "test-bucket",
            "region": "test-region",
            "endpoint": "https://s3.amazonaws.com",
            "access-key": " test-access-key ",
            "secret-key": " test-secret-key ",
            "path": "/test-path",
        }
        s3_parameters.return_value = s3_parameters_dict, []
        can_use_s3.return_value = True, ""
        self.harness.set_model_uuid("123-456")
        self.harness.begin()
        self.harness.set_leader(True)
        self.harness.add_relation("s3-parameters", "s3-integrator")
        create_bucket.assert_called_once_with(s3_parameters_dict)
        upload_to_s3.assert_called_once_with("123-456", "model-uuid.txt", s3_parameters_dict)

    @patch("backups.MAASBackups._create_bucket_if_not_exists")
    def test_on_s3_credential_changed__no_leader(self, create_bucket):
        self.harness.begin()
        self.harness.set_leader(False)
        rel = self.harness.add_relation("s3-parameters", "s3-integrator")
        self.harness.update_relation_data(
            rel,
            "s3-integrator",
            {
                "access-key": "admin",
                "secret-key": "admin",
            },
        )
        create_bucket.assert_not_called()

    @patch("backups.MAASBackups._retrieve_s3_parameters")
    @patch("backups.MAASBackups._create_bucket_if_not_exists")
    def test_on_s3_credential_changed__bucket_error(self, create_bucket, s3_parameters):
        s3_parameters_dict = {
            "bucket": "test-bucket",
            "region": "test-region",
            "endpoint": "https://s3.amazonaws.com",
            "access-key": " test-access-key ",
            "secret-key": " test-secret-key ",
            "path": "/test-path",
        }
        s3_parameters.return_value = s3_parameters_dict, []
        create_bucket.side_effect = ValueError()
        self.harness.begin()
        self.harness.set_leader(True)
        self.harness.add_relation("s3-parameters", "s3-integrator")
        create_bucket.assert_called_once_with(s3_parameters_dict)
        self.assertEqual(
            self.harness.charm.unit.status,
            ops.BlockedStatus(FAILED_TO_ACCESS_CREATE_BUCKET_ERROR_MESSAGE),
        )

    @patch("backups.MAASBackups._retrieve_s3_parameters")
    @patch("backups.MAASBackups._create_bucket_if_not_exists")
    @patch("backups.MAASBackups._can_use_s3_repository")
    def test_on_s3_credential_changed__cannot_use_s3(
        self, can_use_s3, create_bucket, s3_parameters
    ):
        s3_parameters_dict = {
            "bucket": "test-bucket",
            "region": "test-region",
            "endpoint": "https://s3.amazonaws.com",
            "access-key": " test-access-key ",
            "secret-key": " test-secret-key ",
            "path": "/test-path",
        }
        s3_parameters.return_value = s3_parameters_dict, []
        can_use_s3.return_value = False, "validation"
        self.harness.begin()
        self.harness.set_leader(True)
        self.harness.add_relation("s3-parameters", "s3-integrator")
        create_bucket.assert_called_once_with(s3_parameters_dict)
        self.assertEqual(
            self.harness.charm.unit.status,
            ops.BlockedStatus("validation"),
        )

    def test_on_s3_credential_gone(self):
        rel = self.harness.add_relation("s3-parameters", "s3-integrator")
        self.harness.begin()
        self.harness.charm.unit.status = ops.ActiveStatus()
        self.harness.remove_relation(rel)
        self.assertEqual(
            self.harness.charm.unit.status,
            ops.ActiveStatus(),
        )

    def test_on_s3_credential_gone__set_active(self):
        rel = self.harness.add_relation("s3-parameters", "s3-integrator")
        self.harness.begin()
        self.harness.charm.unit.status = ops.BlockedStatus(
            FAILED_TO_ACCESS_CREATE_BUCKET_ERROR_MESSAGE
        )
        self.harness.remove_relation(rel)
        self.assertEqual(
            self.harness.charm.unit.status,
            ops.ActiveStatus(),
        )

    @patch("backups.MAASBackups._run_backup")
    @patch("backups.MAASBackups._upload_content_to_s3")
    @patch("backups.MAASBackups._retrieve_s3_parameters")
    @patch("backups.MAASBackups._can_unit_perform_backup")
    def test_on_create_backup_action(self, can_unit_backup, s3_params, upload_content, run_backup):
        can_unit_backup.return_value = True, ""
        s3_params.return_value = ({}, [])
        self.harness.begin()
        self.harness.run_action("create-backup")
        can_unit_backup.assert_called_once()
        s3_params.assert_called_once()
        run_backup.assert_called_once()
        upload_content.assert_called_once()
        self.assertIsInstance(self.harness.charm.unit.status, ops.ActiveStatus)

    @patch("backups.MAASBackups._can_unit_perform_backup")
    def test_on_create_backup_action_cannot_perform_backup(self, can_unit_backup):
        can_unit_backup.return_value = False, "error msg"
        self.harness.begin()
        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("create-backup")
        self.assertEqual(e.exception.message, "error msg")

    @patch("backups.MAASBackups._upload_content_to_s3")
    @patch("backups.MAASBackups._retrieve_s3_parameters")
    @patch("backups.MAASBackups._can_unit_perform_backup")
    def test_on_create_backup_action_upload_metadata_fail(
        self,
        can_unit_backup,
        s3_params,
        upload_content,
    ):
        can_unit_backup.return_value = True, ""
        upload_content.return_value = False
        s3_params.return_value = ({}, [])
        self.harness.begin()
        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("create-backup")
        self.assertEqual(
            e.exception.message,
            "Failed to upload metadata to provided S3. Please check the juju debug-log for more details.",
        )

    @patch("backups.MAASBackups._generate_backup_id")
    @patch("backups.MAASBackups._upload_backup_metadata")
    @patch("backups.MAASBackups._execute_backup_to_s3")
    def test_run_backup(self, execute_backup, upload_backup_metadata, generate_id):
        backup_id = "2025-01-01T10:10:10Z"
        self.harness.begin()

        # Test backup success
        generate_id.return_value = backup_id
        execute_backup.return_value = True
        upload_backup_metadata.return_value = True
        s3_parameters_dict = {
            "bucket": "test-bucket",
            "region": "test-region",
            "endpoint": "https://s3.amazonaws.com",
            "access-key": " test-access-key ",
            "secret-key": " test-secret-key ",
            "path": "/test-path",
        }
        action_event = MagicMock(spec=ops.ActionEvent)
        self.harness.charm.backup._run_backup(
            event=action_event,
            s3_parameters=s3_parameters_dict,
        )
        generate_id.assert_called_once()
        execute_backup.assert_called_once()
        action_event.set_results.assert_called_once_with(
            {"backups": f"backup created with id {backup_id}"}
        )
        action_event.fail.assert_not_called()

        # Test backup failure with metadata success
        action_event.reset_mock()
        execute_backup.reset_mock()
        generate_id.reset_mock()
        execute_backup.return_value = False
        upload_backup_metadata.return_value = True
        generate_id.return_value = backup_id
        self.harness.charm.backup._run_backup(
            event=action_event,
            s3_parameters=s3_parameters_dict,
        )
        generate_id.assert_called_once()
        execute_backup.assert_called_once()
        action_event.set_results.assert_not_called()
        action_event.fail.assert_called_once_with(
            "Failed to archive and upload MAAS files to S3. Please check the juju debug-log for more details."
        )

        # Test success with upload metadata failure
        action_event.reset_mock()
        execute_backup.reset_mock()
        generate_id.reset_mock()
        execute_backup.return_value = True
        upload_backup_metadata.return_value = False
        generate_id.return_value = backup_id
        self.harness.charm.backup._run_backup(
            event=action_event,
            s3_parameters=s3_parameters_dict,
        )
        generate_id.assert_called_once()
        execute_backup.assert_called_once()
        action_event.set_results.assert_not_called()
        action_event.fail.assert_called_once_with(
            f"Failed to upload backup metadata to S3 for backup-id {backup_id}. Please check the juju debug-log for more details."
        )

        # Test backup failure with upload metadata failure
        action_event.reset_mock()
        execute_backup.reset_mock()
        generate_id.reset_mock()
        execute_backup.return_value = False
        upload_backup_metadata.return_value = False
        generate_id.return_value = backup_id
        self.harness.charm.backup._run_backup(
            event=action_event,
            s3_parameters=s3_parameters_dict,
        )
        generate_id.assert_called_once()
        execute_backup.assert_called_once()
        action_event.set_results.assert_not_called()
        action_event.fail.assert_called_once_with(
            f"Failed to upload backup metadata to S3 for backup-id {backup_id}. Please check the juju debug-log for more details."
        )

    @patch("backups.MAASBackups._backup_maas_to_s3")
    @patch("tempfile.NamedTemporaryFile")
    @patch("backups.MAASBackups._get_s3_session_client")
    def test_execute_backup_to_s3(self, get_client, _named_temporary_file, backup_maas_to_s3):
        # Setup
        client = MagicMock()
        get_client.return_value = client
        s3_path = "/test-path/test-dir"
        s3_parameters = {
            "bucket": "test-bucket",
            "region": "test-region",
            "endpoint": "https://s3.amazonaws.com",
            "access-key": " test-access-key ",
            "secret-key": " test-secret-key ",
            "path": "/test-path",
        }
        _named_temporary_file.return_value.__enter__.return_value.name = "/tmp/test-file"
        action_event = MagicMock(spec=ops.ActionEvent)
        self.harness.begin()

        # Test failure without ca chain.
        backup_maas_to_s3.side_effect = S3UploadFailedError
        self.assertFalse(
            self.harness.charm.backup._execute_backup_to_s3(
                event=action_event,
                s3_parameters=s3_parameters,
                s3_path=s3_path,
            )
        )
        backup_maas_to_s3.assert_called_once_with(
            event=action_event,
            client=client,
            bucket_name=s3_parameters["bucket"],
            s3_path=s3_path,
        )
        _named_temporary_file.assert_not_called()
        get_client.assert_called_once_with(s3_parameters, None)

        action_event.fail.assert_called_once_with(
            f"Failed to backup to S3 bucket={s3_parameters['bucket']}, path={s3_path}. Please check the juju debug-log for more details."
        )

        # Test success with ca chain.
        get_client.reset_mock()
        _named_temporary_file.reset_mock()
        backup_maas_to_s3.reset_mock()
        backup_maas_to_s3.side_effect = None
        s3_parameters["tls-ca-chain"] = ["one", "two"]
        self.assertTrue(
            self.harness.charm.backup._execute_backup_to_s3(
                event=action_event,
                s3_parameters=s3_parameters,
                s3_path=s3_path,
            )
        )
        backup_maas_to_s3.assert_called_once_with(
            event=action_event,
            client=client,
            bucket_name=s3_parameters["bucket"],
            s3_path=s3_path,
        )
        get_client.assert_called_once_with(s3_parameters, "/tmp/test-file")
        _named_temporary_file.assert_called_once()

    @patch("tarfile.open")
    @patch("charm.MaasRegionCharm._get_region_system_ids")
    def test_backup_maas_to_s3(self, get_region_ids, _tar_open):
        event_mock = MagicMock(spec=ops.ActionEvent)
        client_mock = MagicMock()
        self.harness.begin()

        # Test fails to get region ids
        get_region_ids.side_effect = subprocess.CalledProcessError(1, "maas")
        with self.assertRaises(RegionsNotAvailableError):
            self.harness.charm.backup._backup_maas_to_s3(
                event=event_mock,
                client=client_mock,
                bucket_name="test-bucket",
                s3_path="/test-path/test-dir",
            )
        get_region_ids.assert_called_once()

        # Test fails to upload regions
        get_region_ids.side_effect = None
        get_region_ids.return_value = set()
        event_mock.reset_mock()
        client_mock.reset_mock()
        client_mock.upload_file.side_effect = S3UploadFailedError

        with self.assertRaises(S3UploadFailedError):
            self.harness.charm.backup._backup_maas_to_s3(
                event=event_mock,
                client=client_mock,
                bucket_name="test-bucket",
                s3_path="/test-path/test-dir",
            )

        # Test fails to upload
        get_region_ids.return_value = set()
        event_mock.reset_mock()
        client_mock.reset_mock()
        client_mock.upload_file.side_effect = [None, S3UploadFailedError("Failure")]
        with self.assertRaises(S3UploadFailedError):
            self.harness.charm.backup._backup_maas_to_s3(
                event=event_mock,
                client=client_mock,
                bucket_name="test-bucket",
                s3_path="/test-path/test-dir",
            )

        # Test successful backup
        get_region_ids.return_value = set()
        event_mock.reset_mock()
        client_mock.reset_mock()
        client_mock.upload_file.side_effect = None
        self.harness.charm.backup._backup_maas_to_s3(
            event=event_mock,
            client=client_mock,
            bucket_name="test-bucket",
            s3_path="/test-path/test-dir",
        )
        client_mock.upload_file.assert_called()

    @patch("tempfile.NamedTemporaryFile")
    @patch("backups.MAASBackups._get_s3_session_client")
    @patch("backups.MAASBackups._get_backup_metadata")
    def test_upload_backup_metadata(self, get_backup_metadata, get_client, _named_temporary_file):
        self.harness.begin()

        # Common mock values
        get_backup_metadata.return_value = {"metadata": "data"}
        s3_parameters = {
            "bucket": "test-bucket",
            "path": "/not-used",
            "tls-ca-chain": ["one", "two"],
        }
        s3_path = "/test-path/test-dir"
        _named_temporary_file.return_value.__enter__.return_value.name = "/tmp/test-file"
        client = MagicMock()
        get_client.return_value = client

        # Test success
        success = self.harness.charm.backup._upload_backup_metadata(s3_parameters, s3_path, True)
        self.assertTrue(success)
        get_backup_metadata.assert_called_once()
        client.upload_file.assert_called_once_with(
            "/tmp/test-file",
            "test-bucket",
            "/test-path/test-dir/backup_metadata.json",
        )

        # Test success with no ca chain
        get_backup_metadata.reset_mock()
        client.reset_mock()
        client.upload_file.side_effect = None
        del s3_parameters["tls-ca-chain"]
        success = self.harness.charm.backup._upload_backup_metadata(s3_parameters, s3_path, True)
        self.assertTrue(success)

        # Test failure
        get_backup_metadata.reset_mock()
        client.reset_mock()
        client.upload_file.side_effect = S3UploadFailedError("Failure")
        success = self.harness.charm.backup._upload_backup_metadata(s3_parameters, s3_path, True)
        self.assertFalse(success)
        get_backup_metadata.assert_called_once()
        client.upload_file.assert_called_once_with(
            "/tmp/test-file",
            "test-bucket",
            "/test-path/test-dir/backup_metadata.json",
        )

    @patch(
        "charm.MaasRegionCharm.version",
        new_callable=PropertyMock(return_value="3.6.0"),
    )
    @patch(
        "charm.MaasHelper.get_installed_channel",
        return_value="3.6/stable",
    )
    @patch(
        "charm.MaasRegionCharm.model",
        new_callable=PropertyMock(return_value=MagicMock(juju_version="1.0.0")),
    )
    def test_get_backup_metadata(self, _model, _snap_channel, _version):
        self.harness.begin()
        expected_metadata = {
            "maas_snap_version": "3.6.0",
            "maas_snap_channel": "3.6/stable",
            "unit_name": "maas-region/0",
            "juju_version": "1.0.0",
        }
        self.assertEqual(self.harness.charm.backup._get_backup_metadata(), expected_metadata)

    def test_generate_backup_id(self):
        self.harness.begin()
        result = self.harness.charm.backup._generate_backup_id()
        datetime_now_pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
        self.assertRegex(result, datetime_now_pattern)

    @patch("backups.MAASBackups._are_backup_settings_ok")
    @patch("backups.MAASBackups._generate_backup_list_output")
    def test_on_list_backups_action(self, list_output, settings_ok):
        settings_ok.return_value = True, ""
        list_output_value = "list output"
        list_output.return_value = list_output_value
        self.harness.begin()
        output = self.harness.run_action("list-backups")
        self.assertEqual(output.results["backups"], list_output_value)

    @patch("backups.MAASBackups._are_backup_settings_ok")
    def test_on_list_backups_action__settings_not_ok(self, settings_ok):
        settings_ok.return_value = False, "explanation"
        self.harness.begin()
        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("list-backups")
        err = e.exception
        self.assertEqual(err.message, "explanation")

    @patch("backups.MAASBackups._are_backup_settings_ok")
    @patch("backups.MAASBackups._generate_backup_list_output")
    def test_on_list_backups_action__boto_error(self, list_output, settings_ok):
        settings_ok.return_value = True, ""
        list_output.side_effect = BotoCoreError()
        self.harness.begin()
        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("list-backups")
        err = e.exception
        self.assertEqual(
            err.message, "Failed to list MAAS backups with error: An unspecified error occurred"
        )

    def test_on_restore_from_backup_action(self):
        # TODO: implement this
        pass

    def test_pre_restore_checks(self):
        # TODO: implement this
        pass

    @patch("charms.data_platform_libs.v0.s3.S3Requirer.get_s3_connection_info")
    def test_retrieve_s3_parameters(self, _get_s3_connection_info):
        self.harness.begin()

        # Test when there are missing S3 parameters.
        _get_s3_connection_info.return_value = {}
        self.assertEqual(
            self.harness.charm.backup._retrieve_s3_parameters(),
            (
                {},
                ["bucket", "access-key", "secret-key"],
            ),
        )

        # Test when only the required parameters are provided.
        _get_s3_connection_info.return_value = {
            "bucket": "test-bucket",
            "access-key": "test-access-key",
            "secret-key": "test-secret-key",
            # to check that the strip does not apply to values that are not type string
            "sample": 1,
        }
        self.assertEqual(
            self.harness.charm.backup._retrieve_s3_parameters(),
            (
                {
                    "access-key": "test-access-key",
                    "bucket": "test-bucket",
                    "delete-older-than-days": "9999999",
                    "endpoint": "https://s3.amazonaws.com",
                    "path": "/",
                    "region": "",
                    "s3-uri-style": "host",
                    "secret-key": "test-secret-key",
                    "sample": 1,
                },
                [],
            ),
        )

        # Test when all parameters are provided.
        _get_s3_connection_info.return_value = {
            "bucket": " /test-bucket/ ",
            "access-key": " test-access-key ",
            "secret-key": " test-secret-key ",
            "endpoint": " https://storage.googleapis.com// ",
            "path": " test-path/ ",
            "region": " us-east-1 ",
            "s3-uri-style": " path ",
            "delete-older-than-days": "30",
        }
        self.assertEqual(
            self.harness.charm.backup._retrieve_s3_parameters(),
            (
                {
                    "access-key": "test-access-key",
                    "bucket": "test-bucket",
                    "endpoint": "https://storage.googleapis.com",
                    "path": "/test-path",
                    "region": "us-east-1",
                    "s3-uri-style": "path",
                    "secret-key": "test-secret-key",
                    "delete-older-than-days": "30",
                },
                [],
            ),
        )

    @patch("tempfile.NamedTemporaryFile")
    @patch("backups.MAASBackups._get_s3_session_resource")
    def test_upload_content_to_s3(self, _resource, _named_temporary_file):
        self.harness.begin()

        # Set some parameters.
        content = "test-content"
        s3_path = "test-file."
        s3_parameters = {
            "bucket": "test-bucket",
            "region": "test-region",
            "endpoint": "https://s3.amazonaws.com",
            "access-key": " test-access-key ",
            "secret-key": " test-secret-key ",
            "path": "/test-path",
        }

        upload_file = _resource.return_value.Bucket.return_value.upload_file
        _named_temporary_file.return_value.__enter__.return_value.name = "/tmp/test-file"

        # Test when any exception happens.
        upload_file.side_effect = S3UploadFailedError
        self.assertFalse(
            self.harness.charm.backup._upload_content_to_s3(content, s3_path, s3_parameters)
        )
        _named_temporary_file.assert_called_once()
        upload_file.assert_called_once_with("/tmp/test-file", "test-path/test-file.")

        # Test when the upload succeeds
        s3_parameters["tls-ca-chain"] = ["one", "two"]
        _named_temporary_file.reset_mock()
        upload_file.reset_mock()
        upload_file.side_effect = None
        self.assertTrue(
            self.harness.charm.backup._upload_content_to_s3(content, s3_path, s3_parameters)
        )
        self.assertEqual(_named_temporary_file.call_count, 2)
        upload_file.assert_called_once_with("/tmp/test-file", "test-path/test-file.")

    @patch("tempfile.NamedTemporaryFile")
    @patch("backups.MAASBackups._get_s3_session_resource")
    @patch("backups.BytesIO")
    def test_read_content_from_s3(self, _buf, _resource, _named_temporary_file):
        self.harness.begin()

        # Set some parameters.
        content = "test-content"
        s3_path = "test-file"
        s3_parameters = {
            "bucket": "test-bucket",
            "region": "test-region",
            "endpoint": "https://s3.amazonaws.com",
            "access-key": " test-access-key ",
            "secret-key": " test-secret-key ",
            "path": "/test-path",
        }

        download_file = _resource.return_value.Bucket.return_value.download_fileobj
        _named_temporary_file.return_value.__enter__.return_value.name = "/tmp/test-file"
        buf_obj = _buf.return_value.__enter__.return_value

        # Test when any exception happens.
        download_file.side_effect = ClientError(
            error_response={"Error": {"Code": "404"}}, operation_name="read"
        )
        self.assertIsNone(self.harness.charm.backup._read_content_from_s3(s3_path, s3_parameters))
        download_file.assert_called_once_with("test-path/test-file", buf_obj)

        download_file.reset_mock()
        download_file.side_effect = ClientError(
            error_response={"Error": {"Code": "403"}}, operation_name="read"
        )
        self.assertIsNone(self.harness.charm.backup._read_content_from_s3(s3_path, s3_parameters))
        download_file.assert_called_once_with("test-path/test-file", buf_obj)

        download_file.reset_mock()
        download_file.side_effect = Exception()
        self.assertIsNone(self.harness.charm.backup._read_content_from_s3(s3_path, s3_parameters))
        download_file.assert_called_once_with("test-path/test-file", buf_obj)

        # Test when the upload succeeds
        s3_parameters["tls-ca-chain"] = ["one", "two"]
        download_file.reset_mock()
        download_file.side_effect = None
        buf_obj.getvalue.return_value = content.encode()
        self.assertEqual(
            self.harness.charm.backup._read_content_from_s3(s3_path, s3_parameters), content
        )
        _named_temporary_file.assert_called_once()
        download_file.assert_called_once_with("test-path/test-file", buf_obj)


class TestProgressPercentage(unittest.TestCase):
    @patch("backups.logger", spec=logging.Logger)
    @patch("backups.os.path.getsize")
    def test_upload_progress_percentage(self, _getsize, logger):
        _getsize.return_value = 50

        # Test creation and initial call
        progress_percentage = UploadProgressPercentage(
            "test-file", "test-label", update_interval=10
        )
        progress_percentage(25)
        self.assertEqual(progress_percentage._last_percentage, 50)
        logger.info.assert_called_once_with("uploading test-label to s3: 50.00%")

        # Test less than update interval
        logger.reset_mock()
        progress_percentage(1)
        self.assertEqual(progress_percentage._last_percentage, 50)
        logger.info.assert_not_called()

        # Test cumulative progress greater than update interval
        logger.reset_mock()
        progress_percentage(24)
        self.assertEqual(progress_percentage._last_percentage, 100)
        logger.info.assert_called_once_with("uploading test-label to s3: 100.00%")

        # Test over 100% - unlikely but possible!
        logger.reset_mock()
        progress_percentage(25)
        self.assertEqual(progress_percentage._last_percentage, 150)
        logger.info.assert_called_once_with("uploading test-label to s3: 150.00%")

    @patch("backups.logger", spec=logging.Logger)
    def test_download_progress_percentage(self, logger):
        # Test creation and initial call
        progress_percentage = DownloadProgressPercentage(
            "test-file", "test-label", size=50, update_interval=10
        )
        progress_percentage(25)
        self.assertEqual(progress_percentage._last_percentage, 50)
        logger.info.assert_called_once_with("downloading test-label from s3: 50.00%")

        # Test less than update interval
        logger.reset_mock()
        progress_percentage(1)
        self.assertEqual(progress_percentage._last_percentage, 50)
        logger.info.assert_not_called()

        # Test cumulative progress greater than update interval
        logger.reset_mock()
        progress_percentage(24)
        self.assertEqual(progress_percentage._last_percentage, 100)
        logger.info.assert_called_once_with("downloading test-label from s3: 100.00%")

        # Test over 100% - unlikely but possible!
        logger.reset_mock()
        progress_percentage(25)
        self.assertEqual(progress_percentage._last_percentage, 150)
        logger.info.assert_called_once_with("downloading test-label from s3: 150.00%")


class TestAsSize(unittest.TestCase):
    def test_as_size(self):
        self.assertEqual(as_size(1024 * 1024), "1.0MiB")
