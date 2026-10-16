"""EC2 제어 API 테스트. 실제 AWS·실습 DB·.env는 사용하지 않는다.

실행: python -m unittest discover -s tests -v
"""

import asyncio
import json
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from botocore.exceptions import ClientError, ReadTimeoutError
from sqlalchemy.exc import IntegrityError, OperationalError

import backend.config as config

# 테스트가 실수로 실제 .env를 읽지 않게 한다.
config.ENV_FILE = Path("/nonexistent/.env")

from backend import actions, aws_service  # noqa: E402
from backend.config import Settings, get_settings  # noqa: E402
from backend.main import app  # noqa: E402

TOKEN = "test-token"
ACCOUNT = "111111111111"
ALLOWED = "i-0123456789abcdef0"
OTHER = "i-0fedcba9876543210"


def db_error():
    return OperationalError("SQL", {}, Exception("db down"))


class FakeStore:
    """ActionStore 대체. 메모리에 이력을 저장하고 실패를 흉내 낼 수 있다."""

    def __init__(self):
        self.rows = {}
        self.fail_reads = False
        self.fail_create = False
        self.fail_set_result = False
        self.integrity_on_create = None  # create 직전에 다른 요청이 같은 키를 넣은 상황

    def get_by_request_id(self, request_id):
        if self.fail_reads:
            raise db_error()
        return next((r for r in self.rows.values() if r.request_id == request_id), None)

    def has_unresolved(self, instance_id):
        if self.fail_reads:
            raise db_error()
        return any(
            r.instance_id == instance_id and r.status in actions.UNRESOLVED_STATUSES for r in self.rows.values()
        )

    def create_requested(self, request_id, instance_id, action):
        if self.fail_create:
            raise db_error()
        if self.integrity_on_create:
            self.add(**self.integrity_on_create)
            self.integrity_on_create = None
            raise actions.DuplicateRequestError()
        return self.add(request_id=request_id, instance_id=instance_id, action=action, status=actions.REQUESTED)

    def set_result(self, action_id, status, aws_request_id, message):
        if self.fail_set_result:
            raise db_error()
        row = self.rows[action_id]
        row.status, row.aws_request_id, row.message = status, aws_request_id, message

    def add(self, request_id, instance_id, action, status):
        row = SimpleNamespace(
            id=str(uuid.uuid4()),
            request_id=request_id,
            instance_id=instance_id,
            action=action,
            status=status,
            aws_request_id=None,
            message=None,
        )
        self.rows[row.id] = row
        return row


def call(method, path, body=None, token=TOKEN):
    """httpx 없이 ASGI 앱을 직접 호출한다."""

    async def run():
        raw = json.dumps(body).encode() if body is not None else b""
        headers = [(b"content-type", b"application/json")]
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8001),
        }
        messages, sent = [], False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        data = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return status, json.loads(data) if data else None

    return asyncio.run(run())


class ActionApiTest(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            aws_profile="test",
            aws_region="ap-northeast-2",
            expected_account_id=ACCOUNT,
            admin_api_token=TOKEN,
            allowed_instance_ids=frozenset({ALLOWED}),
        )
        self.store = FakeStore()
        app.dependency_overrides[get_settings] = lambda: self.settings
        app.dependency_overrides[actions.get_action_store] = lambda: self.store

        self.sts = mock.Mock()
        self.sts.get_caller_identity.return_value = {"Account": ACCOUNT, "Arn": "arn:aws:iam::111111111111:user/t"}
        self.ec2 = mock.Mock()
        self.set_state("stopped")
        self.ec2.start_instances.return_value = {"ResponseMetadata": {"RequestId": "aws-req-1"}}
        self.ec2.stop_instances.return_value = {"ResponseMetadata": {"RequestId": "aws-req-2"}}
        self.session = mock.Mock()
        self.session.client.side_effect = lambda name, **kw: self.sts if name == "sts" else self.ec2
        patcher = mock.patch.object(aws_service, "create_session", return_value=self.session)
        self.create_session = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(app.dependency_overrides.clear)

    def set_state(self, state, instance_id=ALLOWED):
        self.ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{"InstanceId": instance_id, "State": {"Name": state}}]}]
        }

    def change_calls(self):
        return self.ec2.start_instances.call_count + self.ec2.stop_instances.call_count

    def post(self, action="start", request_id=None, instance_id=ALLOWED, token=TOKEN, extra=None):
        body = {"action": action, "request_id": request_id or str(uuid.uuid4())}
        body.update(extra or {})
        return call("POST", f"/api/ec2/{instance_id}/actions", body, token=token)

    def assert_error(self, result, status, code):
        self.assertEqual(result[0], status, result[1])
        self.assertEqual(result[1]["detail"]["code"], code)
        self.assertIn("message", result[1]["detail"])

    # ---------- 변경 호출 0회여야 하는 경우 ----------

    def test_invalid_token_makes_no_aws_calls(self):
        for token in (None, "", "wrong"):
            self.assert_error(self.post(token=token), 401, "APP_AUTH_REQUIRED")
        self.assertEqual(self.change_calls(), 0)
        self.create_session.assert_not_called()

    def test_not_allowed_instance_rejected_before_aws(self):
        self.assert_error(self.post(instance_id=OTHER), 403, "INSTANCE_NOT_ALLOWED")
        self.assertEqual(self.change_calls(), 0)
        self.create_session.assert_not_called()

    def test_empty_allowlist_rejects_control(self):
        self.settings = Settings("test", "ap-northeast-2", ACCOUNT, TOKEN, frozenset())
        self.assert_error(self.post(), 403, "CONTROL_DISABLED")
        self.assertEqual(self.change_calls(), 0)

    def test_db_failure_on_requested_makes_no_change_call(self):
        self.store.fail_create = True
        self.assert_error(self.post(), 503, "DB_UNAVAILABLE")
        self.assertEqual(self.change_calls(), 0)

    def test_db_failure_on_read_makes_no_change_call(self):
        self.store.fail_reads = True
        self.assert_error(self.post(), 503, "DB_UNAVAILABLE")
        self.assertEqual(self.change_calls(), 0)

    def test_account_mismatch_is_403(self):
        self.sts.get_caller_identity.return_value = {"Account": "999999999999", "Arn": "x"}
        self.assert_error(self.post(), 403, "AWS_ACCOUNT_MISMATCH")
        self.assertEqual(self.change_calls(), 0)

    def test_extra_field_and_bad_uuid_are_422(self):
        self.assert_error(self.post(extra={"region": "us-east-1"}), 422, "VALIDATION_ERROR")
        self.assert_error(self.post(request_id="not-a-uuid"), 422, "VALIDATION_ERROR")
        self.assert_error(self.post(action="terminate"), 422, "VALIDATION_ERROR")
        self.assertEqual(self.change_calls(), 0)

    def test_state_mismatch_is_409(self):
        self.set_state("running")
        self.assert_error(self.post(action="start"), 409, "INVALID_INSTANCE_STATE")
        for state in ("pending", "stopping"):
            self.set_state(state)
            self.assert_error(self.post(action="start"), 409, "INVALID_INSTANCE_STATE")
            self.assert_error(self.post(action="stop"), 409, "INVALID_INSTANCE_STATE")
        self.assertEqual(self.change_calls(), 0)
        self.assertEqual(self.store.rows, {})

    def test_existing_requested_or_unknown_blocks_new_request(self):
        for status in (actions.REQUESTED, actions.UNKNOWN):
            with self.subTest(status=status):
                self.store.rows.clear()
                self.store.add(str(uuid.uuid4()), ALLOWED, "stop", status)
                self.assert_error(self.post(action="start"), 409, "ACTION_IN_PROGRESS")
                self.assertEqual(self.change_calls(), 0)
        self.ec2.describe_instances.assert_not_called()

    # ---------- 접수와 멱등성 ----------

    def test_accept_then_same_key_returns_existing(self):
        key = str(uuid.uuid4())
        status, body = self.post(request_id=key)
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "ACCEPTED")
        self.assertEqual(body["request_id"], key)
        self.ec2.start_instances.assert_called_once_with(InstanceIds=[ALLOWED])
        row = self.store.rows[body["action_id"]]
        self.assertEqual((row.status, row.aws_request_id), ("ACCEPTED", "aws-req-1"))

        # 상태가 바뀌어도 중복 판정이 먼저이므로 기존 결과를 돌려준다.
        self.set_state("pending")
        status2, body2 = self.post(request_id=key)
        self.assertEqual(status2, 200)
        self.assertEqual(body2, {"action_id": body["action_id"], "request_id": key, "status": "ACCEPTED"})
        self.assertEqual(self.change_calls(), 1)

    def test_same_key_different_content_is_409(self):
        key = str(uuid.uuid4())
        self.assertEqual(self.post(request_id=key)[0], 202)
        self.set_state("running")
        self.assert_error(self.post(action="stop", request_id=key), 409, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.change_calls(), 1)

    def test_unique_conflict_on_insert_is_treated_as_duplicate(self):
        key = str(uuid.uuid4())
        self.store.integrity_on_create = dict(
            request_id=key, instance_id=ALLOWED, action="start", status=actions.ACCEPTED
        )
        status, body = self.post(request_id=key)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ACCEPTED")
        self.assertEqual(self.change_calls(), 0)

    # ---------- AWS 결과 분류 ----------

    def test_timeout_records_unknown_and_never_retries(self):
        self.ec2.start_instances.side_effect = ReadTimeoutError(endpoint_url="https://ec2")
        key = str(uuid.uuid4())
        self.assert_error(self.post(request_id=key), 504, "AWS_RESULT_UNKNOWN")
        self.assertEqual(self.change_calls(), 1)
        (row,) = self.store.rows.values()
        self.assertEqual(row.status, "UNKNOWN")

        # 같은 키 재전송은 기존 UNKNOWN을 돌려주고 다시 호출하지 않는다.
        status, body = self.post(request_id=key)
        self.assertEqual((status, body["status"]), (200, "UNKNOWN"))
        # 새 키도 UNKNOWN 이력 때문에 차단된다.
        self.assert_error(self.post(), 409, "ACTION_IN_PROGRESS")
        self.assertEqual(self.change_calls(), 1)

    def test_explicit_rejection_records_failed(self):
        self.ec2.start_instances.side_effect = ClientError(
            {"Error": {"Code": "UnauthorizedOperation"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
            "StartInstances",
        )
        self.assert_error(self.post(), 403, "AWS_ACCESS_DENIED")
        (row,) = self.store.rows.values()
        self.assertEqual(row.status, "FAILED")
        # FAILED는 확정 상태이므로 새 요청은 막지 않는다.
        self.ec2.start_instances.side_effect = None
        self.assertEqual(self.post()[0], 202)

    def test_server_error_from_aws_is_unknown(self):
        self.ec2.stop_instances.side_effect = ClientError(
            {"Error": {"Code": "InternalError"}, "ResponseMetadata": {"HTTPStatusCode": 500}}, "StopInstances"
        )
        self.set_state("running")
        self.assert_error(self.post(action="stop"), 504, "AWS_RESULT_UNKNOWN")
        (row,) = self.store.rows.values()
        self.assertEqual(row.status, "UNKNOWN")

    def test_db_update_failure_after_accept_keeps_requested(self):
        self.store.fail_set_result = True
        key = str(uuid.uuid4())
        status, body = self.post(request_id=key)
        self.assertEqual((status, body["status"]), (202, "ACCEPTED"))
        (row,) = self.store.rows.values()
        self.assertEqual(row.status, "REQUESTED")
        self.store.fail_set_result = False
        self.assertEqual(self.post(request_id=key)[0], 200)
        self.assertEqual(self.change_calls(), 1)

    def test_change_client_has_single_attempt_and_timeouts(self):
        cfg = aws_service.CONTROL_CONFIG
        self.assertEqual(cfg.retries["total_max_attempts"], 1)
        self.assertTrue(cfg.connect_timeout and cfg.read_timeout)
        self.post()
        control_calls = [c for c in self.session.client.call_args_list if c.kwargs.get("config") is cfg]
        self.assertEqual(len(control_calls), 1)

    def test_lock_is_single_module_level_lock(self):
        lock = actions.CONTROL_LOCK
        self.assertIsInstance(lock, type(threading.Lock()))
        self.post()
        self.assertIs(actions.CONTROL_LOCK, lock)
        self.assertFalse(lock.locked())


class ActionStoreTest(unittest.TestCase):
    """실제 ActionStore의 UNIQUE 충돌 변환만 확인한다(DB 세션은 mock)."""

    def test_integrity_error_becomes_duplicate(self):
        db = mock.Mock()
        db.commit.side_effect = IntegrityError("INSERT", {}, Exception("dup"))
        with self.assertRaises(actions.DuplicateRequestError):
            actions.ActionStore(db).create_requested(str(uuid.uuid4()), ALLOWED, "start")
        db.rollback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
