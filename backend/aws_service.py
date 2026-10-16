"""Boto3 조회 로직과 AWS 오류 분류. 모든 호출은 동기 방식이다."""

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionError as BotoConnectionError,
    CredentialRetrievalError,
    HTTPClientError,
    LoginError,
    NoCredentialsError,
    PartialCredentialsError,
    ProfileNotFound,
    RefreshWithMFAUnsupportedError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)

from backend.config import Settings

BOTO_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"max_attempts": 2, "mode": "standard"},
)

LOGIN_ERRORS = (
    LoginError,
    NoCredentialsError,
    PartialCredentialsError,
    CredentialRetrievalError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
    SSOTokenLoadError,
    RefreshWithMFAUnsupportedError,
    ProfileNotFound,
)
CONNECTION_ERRORS = (BotoConnectionError, HTTPClientError)

ACCESS_DENIED_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "Forbidden",
}
LOGIN_REQUIRED_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
    "RequestExpired",
    "AuthFailure",
    "InvalidToken",
}


class AwsServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def to_service_error(exc: Exception) -> AwsServiceError:
    """Boto3 예외를 API 오류로 변환한다. AWS 원문 메시지는 응답에 넣지 않는다."""
    if isinstance(exc, LOGIN_ERRORS):
        return login_required()
    if isinstance(exc, CONNECTION_ERRORS):
        return AwsServiceError(
            503, "AWS_CONNECTION_ERROR", "AWS와 통신하지 못했습니다. 네트워크 상태를 확인한 뒤 다시 시도하세요."
        )
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ACCESS_DENIED_CODES:
            return AwsServiceError(403, "AWS_ACCESS_DENIED", "이 AWS 작업을 수행할 권한이 없습니다.")
        if code in LOGIN_REQUIRED_CODES:
            return login_required()
        return AwsServiceError(502, "AWS_REQUEST_FAILED", f"AWS 요청이 실패했습니다. (오류 코드: {code or 'Unknown'})")
    if isinstance(exc, BotoCoreError):
        return AwsServiceError(502, "AWS_REQUEST_FAILED", "AWS 요청 처리 중 오류가 발생했습니다.")
    raise exc


def login_required() -> AwsServiceError:
    return AwsServiceError(
        503, "AWS_LOGIN_REQUIRED", "AWS 로그인이 필요하거나 만료되었습니다. 터미널에서 aws login을 다시 실행하세요."
    )


def create_session(settings: Settings) -> boto3.Session:
    try:
        return boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)
    except Exception as exc:
        raise to_service_error(exc) from None


def call_aws(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        raise to_service_error(exc) from None


def get_verified_context(session: boto3.Session, settings: Settings) -> dict:
    """STS로 현재 자격증명을 확인하고 기대 계정이 아니면 차단한다."""
    sts = session.client("sts", config=BOTO_CONFIG)
    identity = call_aws(sts.get_caller_identity)
    if identity["Account"] != settings.expected_account_id:
        raise AwsServiceError(
            403, "AWS_ACCOUNT_MISMATCH", "로그인한 AWS 계정이 설정된 EXPECTED_ACCOUNT_ID와 다릅니다."
        )
    return {
        "account_id": identity["Account"],
        "caller_arn": identity["Arn"],
        "region": settings.aws_region,
    }


def _paginate(client, operation: str, result_key: str, **params) -> list:
    def run():
        items = []
        for page in client.get_paginator(operation).paginate(**params):
            items.extend(page.get(result_key, []))
        return items

    return call_aws(run)


def list_ec2_instances(session: boto3.Session) -> list[dict]:
    ec2 = session.client("ec2", config=BOTO_CONFIG)
    reservations = _paginate(ec2, "describe_instances", "Reservations")
    items = []
    for reservation in reservations:
        for inst in reservation.get("Instances", []):
            tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
            items.append({
                "instance_id": inst["InstanceId"],
                "name": tags.get("Name") or inst["InstanceId"],
                "state": inst.get("State", {}).get("Name"),
                "instance_type": inst.get("InstanceType"),
                "private_ip": inst.get("PrivateIpAddress"),
                "public_ip": inst.get("PublicIpAddress"),
            })
    return items


def list_s3_buckets(session: boto3.Session) -> list[dict]:
    s3 = session.client("s3", config=BOTO_CONFIG)
    buckets = _paginate(s3, "list_buckets", "Buckets")
    return [{"name": b["Name"]} for b in buckets]


def list_security_groups(session: boto3.Session) -> list[dict]:
    ec2 = session.client("ec2", config=BOTO_CONFIG)
    groups = _paginate(ec2, "describe_security_groups", "SecurityGroups")
    return [
        {
            "GroupId": g["GroupId"],
            "GroupName": g.get("GroupName"),
            "IpPermissions": g.get("IpPermissions", []),
        }
        for g in groups
    ]
