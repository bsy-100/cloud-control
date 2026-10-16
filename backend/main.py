"""FastAPI 앱. 실행: uvicorn backend.main:app --host 127.0.0.1 --port 8001"""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import boto3
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend import actions, aws_service
from backend.auth import is_controllable, require_app_token
from backend.aws_service import AwsServiceError
from backend.config import FRONTEND_DIR, ConfigError, Settings, get_db_settings, get_settings
from backend.db import get_db
from backend.models import ActionLog

ACTION_LOG_LIMIT = 50


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 설정을 검증한다. ADMIN_API_TOKEN·DB 설정 등이 없으면 변수 이름만 담긴 오류로 시작을 거절한다.
    get_settings()
    get_db_settings()
    yield


app = FastAPI(title="Cloud Control", lifespan=lifespan)


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": {"code": code, "message": message}})


@app.exception_handler(AwsServiceError)
def handle_aws_error(request: Request, exc: AwsServiceError):
    return error_response(exc.status_code, exc.code, exc.message)


@app.exception_handler(ConfigError)
def handle_config_error(request: Request, exc: ConfigError):
    return error_response(500, "CONFIG_ERROR", str(exc))


@app.exception_handler(RequestValidationError)
def handle_validation_error(request: Request, exc: RequestValidationError):
    # 입력값 원문은 응답에 넣지 않고 문제가 된 필드 위치만 안내한다.
    fields = sorted({".".join(str(p) for p in err.get("loc", ())[1:]) or "body" for err in exc.errors()})
    return error_response(422, "VALIDATION_ERROR", f"요청 형식이 올바르지 않습니다: {', '.join(fields)}")


# DB 오류 원문(SQL·접속 정보)은 응답에 넣지 않는다.
@app.exception_handler(OperationalError)
def handle_db_unavailable(request: Request, exc: OperationalError):
    return error_response(503, "DB_UNAVAILABLE", "DB에 연결할 수 없습니다. MariaDB 상태와 DB 설정을 확인하세요.")


@app.exception_handler(SQLAlchemyError)
def handle_db_error(request: Request, exc: SQLAlchemyError):
    return error_response(500, "DB_ERROR", "DB 처리 중 오류가 발생했습니다.")


@dataclass
class AwsContext:
    session: boto3.Session
    info: dict


def verified_aws(settings: Settings = Depends(get_settings)) -> AwsContext:
    """모든 조회 전에 STS로 계정을 확인한다. 계정·리전은 서버 설정에서만 가져온다."""
    session = aws_service.create_session(settings)
    info = aws_service.get_verified_context(session, settings)
    return AwsContext(session=session, info=info)


# 모든 /api 경로는 앱 토큰 인증을 먼저 통과해야 하며, 그 전에는 AWS를 호출하지 않는다.
api = APIRouter(prefix="/api", dependencies=[Depends(require_app_token)])


@api.get("/context")
def get_context(ctx: AwsContext = Depends(verified_aws)):
    return ctx.info


@api.get("/ec2")
def get_ec2(ctx: AwsContext = Depends(verified_aws), settings: Settings = Depends(get_settings)):
    items = aws_service.list_ec2_instances(ctx.session)
    for item in items:
        item["controllable"] = is_controllable(item["instance_id"], settings)
    return {"items": items, "count": len(items)}


@api.get("/s3")
def get_s3(ctx: AwsContext = Depends(verified_aws)):
    items = aws_service.list_s3_buckets(ctx.session)
    return {"items": items, "count": len(items)}


@api.get("/security-groups")
def get_security_groups(ctx: AwsContext = Depends(verified_aws)):
    items = aws_service.list_security_groups(ctx.session)
    return {"items": items, "count": len(items)}


class ActionRequest(BaseModel):
    # 추가 필드는 422로 거절한다. 계정·리전·허용 목록은 클라이언트가 보낼 수 없다.
    model_config = ConfigDict(extra="forbid")

    action: Literal["start", "stop"]
    request_id: UUID


@api.post("/ec2/{instance_id}/actions")
def post_ec2_action(
    instance_id: str,
    body: ActionRequest,
    settings: Settings = Depends(get_settings),
    store: actions.ActionStore = Depends(actions.get_action_store),
):
    result, created = actions.perform_action(instance_id, body.action, str(body.request_id), settings, store)
    return JSONResponse(status_code=202 if created else 200, content=result)


@api.get("/actions")
def get_actions(db: Session = Depends(get_db)):
    rows = db.scalars(
        select(ActionLog).order_by(ActionLog.created_at.desc(), ActionLog.id.desc()).limit(ACTION_LOG_LIMIT)
    ).all()
    items = [row.to_dict() for row in rows]
    return {"items": items, "count": len(items)}


app.include_router(api)

# 정적 파일은 인증 없이 제공해 토큰 입력 화면을 열 수 있게 한다.
# 루트 전체가 아니라 frontend 폴더만 공개하며, API 등록 뒤 마지막에 마운트한다.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
