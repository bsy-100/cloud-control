"""앱 런타임 설정. 서버 프로세스가 실행될 때만 프로젝트 루트 .env를 읽는다."""

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT_DIR / ".env"
FRONTEND_DIR = ROOT_DIR / "frontend"

REQUIRED_VARS = ("AWS_PROFILE", "AWS_REGION", "EXPECTED_ACCOUNT_ID", "ADMIN_API_TOKEN")
ALLOWED_REGION = "ap-northeast-2"
INSTANCE_ID_PATTERN = re.compile(r"i-[0-9a-f]{8}(?:[0-9a-f]{9})?")


class ConfigError(Exception):
    """설정 오류. 메시지에는 변수 이름만 담고 값은 담지 않는다."""


@dataclass(frozen=True)
class Settings:
    aws_profile: str
    aws_region: str
    expected_account_id: str
    # repr=False: 설정 객체가 로그나 오류에 찍혀도 토큰이 드러나지 않게 한다.
    admin_api_token: str = field(repr=False)
    allowed_instance_ids: frozenset[str] = frozenset()


def parse_instance_ids(raw: str) -> frozenset[str]:
    """쉼표로 나누고 앞뒤 공백과 빈 항목을 제거한 집합을 만든다."""
    ids = frozenset(part.strip() for part in raw.split(",") if part.strip())
    if any(not INSTANCE_ID_PATTERN.fullmatch(i) for i in ids):
        raise ConfigError("ALLOWED_INSTANCE_IDS에 EC2 인스턴스 ID 형식이 아닌 항목이 있습니다.")
    return ids


@lru_cache
def get_settings() -> Settings:
    load_dotenv(ENV_FILE, override=False)

    values = {name: os.getenv(name, "").strip() for name in REQUIRED_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(f"필수 설정이 없습니다: {', '.join(missing)}")

    if values["AWS_REGION"] != ALLOWED_REGION:
        raise ConfigError(f"AWS_REGION은 {ALLOWED_REGION}만 허용합니다.")

    if not re.fullmatch(r"\d{12}", values["EXPECTED_ACCOUNT_ID"]):
        raise ConfigError("EXPECTED_ACCOUNT_ID는 12자리 숫자여야 합니다.")

    return Settings(
        aws_profile=values["AWS_PROFILE"],
        aws_region=values["AWS_REGION"],
        expected_account_id=values["EXPECTED_ACCOUNT_ID"],
        admin_api_token=values["ADMIN_API_TOKEN"],
        allowed_instance_ids=parse_instance_ids(os.getenv("ALLOWED_INSTANCE_IDS", "")),
    )


DB_REQUIRED_VARS = ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME")


@dataclass(frozen=True)
class DbSettings:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    name: str


@lru_cache
def get_db_settings() -> DbSettings:
    """DB 설정. AWS 설정과 분리해 init_db 명령에서도 단독으로 쓸 수 있게 한다."""
    load_dotenv(ENV_FILE, override=False)

    values = {name: os.getenv(name, "").strip() for name in DB_REQUIRED_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(f"필수 설정이 없습니다: {', '.join(missing)}")

    if not values["DB_PORT"].isdigit() or not 0 < int(values["DB_PORT"]) < 65536:
        raise ConfigError("DB_PORT는 1~65535 사이의 숫자여야 합니다.")

    return DbSettings(
        host=values["DB_HOST"],
        port=int(values["DB_PORT"]),
        user=values["DB_USER"],
        password=values["DB_PASSWORD"],
        name=values["DB_NAME"],
    )
