"""앱 토큰 인증과 EC2 제어 대상 제한. 둘 다 AWS 호출 전에 실행된다."""

import secrets

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.config import INSTANCE_ID_PATTERN, Settings, get_settings

bearer_scheme = HTTPBearer(auto_error=False)


def api_error(status_code: int, code: str, message: str, headers: dict | None = None) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message}, headers=headers)


def require_app_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> None:
    """모든 /api 경로의 공통 인증. 누락·잘못된 scheme·빈 값·불일치를 모두 401로 처리한다."""
    supplied = credentials.credentials.strip() if credentials and credentials.credentials else ""
    # 바이트로 비교해 ASCII가 아닌 입력도 TypeError(500) 없이 처리한다.
    if not supplied or not secrets.compare_digest(
        supplied.encode("utf-8"), settings.admin_api_token.encode("utf-8")
    ):
        raise api_error(
            401,
            "APP_AUTH_REQUIRED",
            "앱 토큰이 없거나 올바르지 않습니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def is_controllable(instance_id: str, settings: Settings) -> bool:
    return instance_id in settings.allowed_instance_ids


def ensure_instance_allowed(instance_id: object, settings: Settings) -> str:
    """제어 요청 대상 확인. 허용 목록은 서버 설정에서만 읽으며 AWS 호출 전에 차단한다."""
    if not isinstance(instance_id, str) or not INSTANCE_ID_PATTERN.fullmatch(instance_id):
        raise api_error(400, "INVALID_INSTANCE_ID", "EC2 인스턴스 ID 형식이 올바르지 않습니다.")
    if not settings.allowed_instance_ids:
        raise api_error(403, "CONTROL_DISABLED", "제어 허용 목록이 비어 있어 시작·중지를 사용할 수 없습니다.")
    if not is_controllable(instance_id, settings):
        raise api_error(403, "INSTANCE_NOT_ALLOWED", "허용 목록에 없는 인스턴스는 제어할 수 없습니다.")
    return instance_id
