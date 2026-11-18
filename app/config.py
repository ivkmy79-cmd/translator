"""설정과 언어 레지스트리.

언어를 추가할 때는 LANGUAGES 에 항목을 넣고 ACTIVE_LANGUAGES 에 코드를 추가하면 된다.
중국어/베트남어는 PoC 에서 비활성이지만 레지스트리에는 미리 정의해 둔다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

from dotenv import load_dotenv

load_dotenv()

KST_OFFSET_HOURS = 9


@dataclass(frozen=True)
class Language:
    code: str           # 앱 내부 코드
    label: str          # UI 표시명
    speech_locale: str  # Azure 음성 인식 로케일 (source 로 쓸 때)
    translate_code: str # Azure 번역 대상 코드 (target 으로 쓸 때)


LANGUAGES: Dict[str, Language] = {
    "ko": Language("ko", "한국어", "ko-KR", "ko"),
    "en": Language("en", "English", "en-US", "en"),
    "vi": Language("vi", "Tiếng Việt", "vi-VN", "vi"),
    # PoC 비활성 - 후속 확장용
    "zh": Language("zh", "中文(简体)", "zh-CN", "zh-Hans"),
}

ACTIVE_LANGUAGES: Tuple[str, ...] = ("ko", "en", "vi")


def active_languages() -> Tuple[Language, ...]:
    return tuple(LANGUAGES[code] for code in ACTIVE_LANGUAGES)


def is_active(code: str) -> bool:
    return code in ACTIVE_LANGUAGES


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        # 잘못된 설정을 조용히 기본값으로 넘기지 않고 알린다.
        raise ValueError(f"{name} 는 정수여야 합니다. 현재 값: {raw!r}")


def read_credentials_file(path: str) -> Tuple[Dict[str, str], str]:
    """`KEY=VALUE` 형식의 외부 자격증명 파일을 읽는다.

    돌려주는 값은 (값 사전, 오류 메시지)이다. 오류 메시지는 UI 에 그대로 노출되므로
    파일 경로까지만 담고 값은 절대 담지 않는다.
    """
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        return {}, f"자격증명 파일을 찾을 수 없습니다: {file_path}"

    values: Dict[str, str] = {}
    try:
        content = file_path.read_text(encoding="utf-8-sig")
    except OSError as error:
        return {}, f"자격증명 파일을 읽지 못했습니다: {file_path} ({error.strerror})"

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values, ""


@dataclass(frozen=True)
class Settings:
    speech_key: str
    speech_region: str
    floor_limit_seconds: int
    floor_warning_seconds: int
    room_passcode: str = ""  # 비어 있으면 코드 없이 입장 (사내망 전용 운영)
    deepseek_api_key: str = ""
    summary_model: str = "deepseek-flash"
    credentials_path: str = ""
    credentials_error: str = ""

    @property
    def azure_configured(self) -> bool:
        return bool(self.speech_key) and bool(self.speech_region)

    def azure_status(self) -> dict:
        """UI 로 그대로 내보내는 Azure 상태. 미설정을 성공으로 표시하지 않는다."""
        if self.azure_configured:
            return {"state": "configured", "label": "연결됨", "reason": ""}

        if self.credentials_error:
            reason = self.credentials_error
        else:
            missing = []
            if not self.speech_key:
                missing.append("AZURE_SPEECH_KEY")
            if not self.speech_region:
                missing.append("AZURE_SPEECH_REGION")
            source = self.credentials_path or "서버 .env"
            reason = f"{source} 에 " + ", ".join(missing) + " 값이 비어 있습니다."

        return {"state": "unconfigured", "label": "Azure 설정 필요", "reason": reason}

    def summary_status(self) -> dict:
        """회의 정리(DeepSeek) 설정 상태. 미설정을 성공으로 표시하지 않는다."""
        if self.deepseek_api_key:
            return {"state": "configured", "label": "회의 정리 가능", "reason": ""}
        source = self.credentials_path or "서버 .env"
        return {
            "state": "unconfigured",
            "label": "회의 정리 설정 필요",
            "reason": f"{source} 에 DEEPSEEK_API_KEY 값이 비어 있습니다.",
        }


def load_settings() -> Settings:
    limit = _int_env("FLOOR_LIMIT_SECONDS", 30)
    warning = _int_env("FLOOR_WARNING_SECONDS", 25)
    if warning >= limit:
        raise ValueError("FLOOR_WARNING_SECONDS 는 FLOOR_LIMIT_SECONDS 보다 작아야 합니다.")

    # .env 가 가리키는 외부 자격증명 파일. 키 자체는 저장소 밖에만 둔다.
    credentials_path = os.getenv("AZURE_CREDENTIALS_FILE", "").strip()
    credentials: Dict[str, str] = {}
    credentials_error = ""
    if credentials_path:
        credentials, credentials_error = read_credentials_file(credentials_path)

    # 환경변수를 직접 지정했으면 그쪽이 우선하고, 없을 때 자격증명 파일을 쓴다.
    key = os.getenv("AZURE_SPEECH_KEY", "").strip() or credentials.get("AZURE_SPEECH_KEY", "")
    region = (
        os.getenv("AZURE_SPEECH_REGION", "").strip()
        or credentials.get("AZURE_SPEECH_REGION", "")
    )

    deepseek_key = (
        os.getenv("DEEPSEEK_API_KEY", "").strip() or credentials.get("DEEPSEEK_API_KEY", "")
    )

    return Settings(
        speech_key=key,
        speech_region=region,
        floor_limit_seconds=limit,
        floor_warning_seconds=warning,
        room_passcode=os.getenv("ROOM_PASSCODE", "").strip(),
        deepseek_api_key=deepseek_key,
        summary_model=os.getenv("DEEPSEEK_MODEL", "").strip() or "deepseek-flash",
        credentials_path=credentials_path,
        credentials_error=credentials_error,
    )
