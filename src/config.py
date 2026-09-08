"""
설정 파일 로더
YAML 설정 파일을 읽고 검증하는 모듈
"""

import os
import sys
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
EXAMPLE_CONFIG_PATH = Path(__file__).parent.parent / "config.example.yaml"


def resolve_notify_from_date(value: str | date | None, today: date | None = None) -> str:
    """CGV 알림 시작일을 YYYYMMDD로 변환합니다.

    today는 한국 시간의 오늘을 사용합니다. 고정 날짜는 YYYYMMDD 또는
    YYYY-MM-DD 형식으로 지정할 수 있고, 빈 값은 날짜 제한이 없습니다.
    """
    if value is None or str(value).strip() == "":
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")

    value = str(value).strip()
    if value.lower() == "today":
        return (today or datetime.now(ZoneInfo("Asia/Seoul")).date()).strftime("%Y%m%d")

    normalized = value.replace("-", "")
    if len(normalized) != 8 or not normalized.isdigit():
        raise ValueError("notify_from_date는 today, YYYYMMDD 또는 YYYY-MM-DD 형식이어야 합니다.")
    return datetime.strptime(normalized, "%Y%m%d").strftime("%Y%m%d")


def _resolve_cgv_dates(config: dict) -> None:
    """설정을 로드할 때 상대 날짜를 한국 시간 기준으로 확정합니다."""
    for watcher in config.get("watchers", []):
        if watcher.get("type") != "cgv":
            continue
        settings = watcher.setdefault("settings", {})
        configured = settings.get("notify_from_date", "today")
        try:
            resolved = resolve_notify_from_date(configured)
        except ValueError as exc:
            raise ValueError(f"{watcher.get('name', 'CGV')}: {exc}") from exc
        settings["notify_from_date"] = resolved
        logger.info("[%s] 알림 시작일: %s", watcher.get("name", "CGV"), resolved or "제한없음")


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """
    YAML 설정 파일을 로드합니다.
    환경변수 CONFIG_PATH 또는 인자로 경로를 지정할 수 있습니다.
    """
    config_path = Path(
        path or os.environ.get("CONFIG_PATH", str(DEFAULT_CONFIG_PATH))
    )

    if not config_path.exists():
        logger.error(
            f"설정 파일을 찾을 수 없습니다: {config_path}\n"
            f"  config.example.yaml을 config.yaml로 복사한 뒤 수정하세요:\n"
            f"  cp config.example.yaml config.yaml"
        )
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    _validate_config(config)
    _apply_env_overrides(config)
    _resolve_cgv_dates(config)

    return config


def _validate_config(config: dict) -> None:
    """필수 설정값 검증"""
    # 텔레그램 설정 검증
    tg = config.get("telegram", {})
    token = tg.get("bot_token", "")
    if not token or token == "YOUR_BOT_TOKEN_HERE":
        logger.error(
            "텔레그램 봇 토큰이 설정되지 않았습니다. "
            "config.yaml의 telegram.bot_token을 확인하세요."
        )
        sys.exit(1)

    chat_ids = tg.get("chat_ids", [])
    if not chat_ids or chat_ids == ["YOUR_CHAT_ID_HERE"]:
        logger.warning(
            "텔레그램 채팅 ID가 설정되지 않았습니다. "
            "/start 명령어로 봇에 메시지를 보내면 자동 등록됩니다."
        )

    # watcher 설정 검증
    watchers = config.get("watchers", [])
    if not watchers:
        logger.warning("모니터링 대상(watchers)이 설정되지 않았습니다.")

    for i, w in enumerate(watchers):
        if "name" not in w:
            logger.error(f"watcher[{i}]에 name이 없습니다.")
            sys.exit(1)
        if "type" not in w:
            logger.error(f"watcher[{i}] '{w['name']}'에 type이 없습니다.")
            sys.exit(1)
        if w["type"] not in ("cgv", "webpage"):
            logger.error(
                f"watcher[{i}] '{w['name']}'의 type '{w['type']}'은 "
                f"지원되지 않습니다. (cgv, webpage 중 선택)"
            )
            sys.exit(1)


def _apply_env_overrides(config: dict) -> None:
    """환경변수로 설정값을 오버라이드합니다. (Docker/CI 환경 지원)"""
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if env_token:
        config["telegram"]["bot_token"] = env_token

    env_chat_ids = os.environ.get("TELEGRAM_CHAT_IDS")
    if env_chat_ids:
        config["telegram"]["chat_ids"] = [
            cid.strip() for cid in env_chat_ids.split(",")
        ]

    env_log_level = os.environ.get("LOG_LEVEL")
    if env_log_level:
        config.setdefault("advanced", {})["log_level"] = env_log_level


def get_enabled_watchers(config: dict) -> list[dict]:
    """활성화된 watcher 목록만 반환"""
    return [w for w in config.get("watchers", []) if w.get("enabled", True)]
