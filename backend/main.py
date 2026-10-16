"""FastAPI 앱. 실행: uvicorn backend.main:app --host 127.0.0.1 --port 8001"""

from contextlib import asynccontextmanager
from dataclasses import dataclass

import boto3
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import aws_service
from backend.aws_service import AwsServiceError
from backend.config import FRONTEND_DIR, ConfigError, Settings, get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 설정을 검증한다. 누락 시 변수 이름만 담긴 오류로 실행을 멈춘다.
    get_settings()
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


@dataclass
class AwsContext:
    session: boto3.Session
    info: dict


def verified_aws(settings: Settings = Depends(get_settings)) -> AwsContext:
    """모든 조회 전에 STS로 계정을 확인한다."""
    session = aws_service.create_session(settings)
    info = aws_service.get_verified_context(session, settings)
    return AwsContext(session=session, info=info)


@app.get("/api/context")
def get_context(ctx: AwsContext = Depends(verified_aws)):
    return ctx.info


@app.get("/api/ec2")
def get_ec2(ctx: AwsContext = Depends(verified_aws)):
    items = aws_service.list_ec2_instances(ctx.session)
    return {"items": items, "count": len(items)}


@app.get("/api/s3")
def get_s3(ctx: AwsContext = Depends(verified_aws)):
    items = aws_service.list_s3_buckets(ctx.session)
    return {"items": items, "count": len(items)}


@app.get("/api/security-groups")
def get_security_groups(ctx: AwsContext = Depends(verified_aws)):
    items = aws_service.list_security_groups(ctx.session)
    return {"items": items, "count": len(items)}


# 루트 전체가 아니라 frontend 폴더만 공개한다. API 경로 등록 뒤에 마운트한다.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
