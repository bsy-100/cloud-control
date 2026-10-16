"""EC2 시작·중지 처리. 학습용 단일 프로세스 앱 기준으로 전역 lock 하나로 직렬화한다.

처리 순서: 허용 ID 검사 → STS 계정 확인 → (lock) 중복 키 → 미확정 이력 → 최신 EC2 상태
→ REQUESTED commit → AWS 변경 호출(1회) → ACCEPTED/FAILED/UNKNOWN 기록.
"""

import threading

from botocore.exceptions import ClientError
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend import aws_service
from backend.auth import api_error, ensure_instance_allowed
from backend.aws_service import LOGIN_ERRORS, AwsServiceError
from backend.config import Settings
from backend.db import get_db
from backend.models import ActionLog

REQUESTED = "REQUESTED"
ACCEPTED = "ACCEPTED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"
UNRESOLVED_STATUSES = (REQUESTED, UNKNOWN)

# 작업별로 허용되는 현재 상태. 그 외 상태(전환 중 포함)는 409.
REQUIRED_STATE = {"start": "stopped", "stop": "running"}

# 프로세스에서 한 번만 만든다. 모든 변경 진입점은 이 lock을 사용해야 한다.
CONTROL_LOCK = threading.Lock()


class DuplicateRequestError(Exception):
    """request_id UNIQUE 충돌."""


class ActionStore:
    """action_logs 저장 함수 모음. 테스트에서는 이 객체를 대체한다."""

    def __init__(self, db: Session):
        self.db = db

    def get_by_request_id(self, request_id: str) -> ActionLog | None:
        return self.db.scalar(select(ActionLog).where(ActionLog.request_id == request_id))

    def has_unresolved(self, instance_id: str) -> bool:
        stmt = (
            select(ActionLog.id)
            .where(ActionLog.instance_id == instance_id, ActionLog.status.in_(UNRESOLVED_STATUSES))
            .limit(1)
        )
        return self.db.scalar(stmt) is not None

    def create_requested(self, request_id: str, instance_id: str, action: str) -> ActionLog:
        log = ActionLog(request_id=request_id, instance_id=instance_id, action=action, status=REQUESTED)
        self.db.add(log)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise DuplicateRequestError() from None
        except SQLAlchemyError:
            self.db.rollback()
            raise
        return log

    def set_result(self, action_id: str, status: str, aws_request_id: str | None, message: str) -> None:
        try:
            log = self.db.get(ActionLog, action_id)
            log.status = status
            log.aws_request_id = aws_request_id
            log.message = message[:500]
            self.db.commit()
        except SQLAlchemyError:
            self.db.rollback()
            raise


def get_action_store(db: Session = Depends(get_db)) -> ActionStore:
    return ActionStore(db)


def db_unavailable():
    return api_error(503, "DB_UNAVAILABLE", "DB에 연결할 수 없어 요청을 처리하지 않았습니다.")


def _db_call(func, *args):
    try:
        return func(*args)
    except SQLAlchemyError:
        raise db_unavailable() from None


def _replay(existing: ActionLog, instance_id: str, action: str) -> tuple[dict, bool]:
    """같은 키 재전송. 내용이 같으면 기존 이력을 돌려주고, 다르면 409."""
    if existing.instance_id != instance_id or existing.action != action:
        raise api_error(409, "IDEMPOTENCY_CONFLICT", "같은 요청 키가 다른 대상이나 작업에 이미 사용되었습니다.")
    return {"action_id": existing.id, "request_id": existing.request_id, "status": existing.status}, False


def _classify_failure(exc: Exception) -> tuple[str, AwsServiceError]:
    """변경 호출 예외를 FAILED(명시적 거절) 또는 UNKNOWN(결과 불확실)으로 나눈다."""
    if isinstance(exc, ClientError):
        http_status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if http_status < 500:
            return FAILED, aws_service.to_service_error(exc)
    elif isinstance(exc, LOGIN_ERRORS):
        # 자격증명을 얻지 못하면 요청이 전송되지 않는다.
        return FAILED, aws_service.login_required()
    return UNKNOWN, AwsServiceError(
        504,
        "AWS_RESULT_UNKNOWN",
        "AWS 응답을 받지 못해 결과를 알 수 없습니다. 콘솔에서 상태를 확인하세요. 이 대상의 새 제어는 차단됩니다.",
    )


def _failure_message(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return f"AWS 거절: {exc.response.get('Error', {}).get('Code', 'Unknown')}"
    return f"AWS 호출 실패: {type(exc).__name__}"


def perform_action(
    instance_id: str, action: str, request_id: str, settings: Settings, store: ActionStore
) -> tuple[dict, bool]:
    """(응답 본문, 새로 접수했는지)를 반환한다. 실패는 예외로 알린다."""
    if action not in REQUIRED_STATE:
        raise api_error(422, "INVALID_ACTION", "action은 start 또는 stop만 허용합니다.")
    # 허용 목록 검사는 어떤 AWS 호출보다 먼저 한다.
    ensure_instance_allowed(instance_id, settings)

    session = aws_service.create_session(settings)
    aws_service.get_verified_context(session, settings)

    with CONTROL_LOCK:
        # lock 안에서 중복 키·미확정 이력·최신 상태를 다시 확인한다. 중복 판정이 상태 검사보다 먼저다.
        existing = _db_call(store.get_by_request_id, request_id)
        if existing is not None:
            return _replay(existing, instance_id, action)

        if _db_call(store.has_unresolved, instance_id):
            raise api_error(
                409,
                "ACTION_IN_PROGRESS",
                "이 대상에 결과가 확정되지 않은 요청(REQUESTED/UNKNOWN)이 있어 새 제어를 할 수 없습니다.",
            )

        state = aws_service.get_instance_state(session, instance_id)
        if state is None:
            raise api_error(404, "INSTANCE_NOT_FOUND", "EC2 인스턴스를 찾을 수 없습니다.")
        if state != REQUIRED_STATE[action]:
            raise api_error(
                409,
                "INVALID_INSTANCE_STATE",
                f"{action}은(는) {REQUIRED_STATE[action]} 상태에서만 가능합니다. (현재: {state})",
            )

        # REQUESTED를 commit한 뒤에만 AWS를 호출한다.
        try:
            log = store.create_requested(request_id, instance_id, action)
        except DuplicateRequestError:
            existing = _db_call(store.get_by_request_id, request_id)
            if existing is None:
                raise api_error(409, "IDEMPOTENCY_CONFLICT", "요청 키 충돌이 발생했습니다.") from None
            return _replay(existing, instance_id, action)
        except SQLAlchemyError:
            raise db_unavailable() from None
        action_id, saved_request_id = log.id, log.request_id

        try:
            response = aws_service.send_instance_action(session, instance_id, action)
        except Exception as exc:
            status, error = _classify_failure(exc)
            try:
                store.set_result(action_id, status, None, _failure_message(exc))
            except SQLAlchemyError:
                pass  # REQUESTED로 남겨 둔다. 삭제·재실행하지 않는다.
            raise error from None

        aws_request_id = response.get("ResponseMetadata", {}).get("RequestId")
        body = {"action_id": action_id, "request_id": saved_request_id, "status": ACCEPTED}
        try:
            store.set_result(action_id, ACCEPTED, aws_request_id, "AWS 접수")
        except SQLAlchemyError:
            # AWS는 접수했으므로 재실행하지 않는다. 이력은 REQUESTED로 남아 새 제어를 막는다.
            body["message"] = "AWS는 요청을 접수했지만 이력 갱신에 실패해 REQUESTED로 남았습니다."
        return body, True
