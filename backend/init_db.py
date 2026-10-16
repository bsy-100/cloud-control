"""없는 테이블만 생성한다. 기존 테이블과 데이터는 변경·삭제하지 않는다.

실행: python -m backend.init_db
"""

import sys

from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

import backend.models  # noqa: F401  모든 모델을 Base.metadata에 등록한다.
from backend.config import ConfigError
from backend.db import Base, get_engine


def describe_db_error(exc: SQLAlchemyError) -> str:
    """드라이버 오류 코드와 메시지만 보여 준다. URL·암호는 포함하지 않는다."""
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", ())
    if len(args) >= 2 and isinstance(args[0], int):
        return f"[{args[0]}] {args[1]}"
    return type(orig or exc).__name__


def main() -> int:
    try:
        engine = get_engine()
        before = set(inspect(engine).get_table_names())
        Base.metadata.create_all(engine, checkfirst=True)
        after = set(inspect(engine).get_table_names())
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(f"DB 오류: {describe_db_error(exc)}", file=sys.stderr)
        return 1

    for name in Base.metadata.tables:
        state = "생성" if name in after - before else "이미 있음"
        print(f"{name}: {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
