"""회의 자막 요약 Provider - DeepSeek Chat Completions (OpenAI 호환 형식).

자막은 메모리에서만 읽어 API 로 보내며, 요약 결과도 서버에 저장하지 않는다.
키가 없으면 가짜 요약을 만들지 않고 SummaryUnavailable 로 거부한다.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import List

import httpx

from ..config import LANGUAGES

logger = logging.getLogger("translator.summary")

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

# 요약 구조는 여기 한 곳에서만 정한다. 결과는 «## 제목» + «- 항목» 형식의 텍스트다.
# 절 제목·라벨을 언어별로 직접 주어야 모델이 제목을 원문(한국어)으로 남기지 않는다.
SECTION_LABELS = {
    "ko": {
        "sections": ["회의 개요", "주요 논의 내용", "결정 사항", "할 일", "미결·후속 논의 사항"],
        "none": "없음", "owner": "담당", "due": "기한",
    },
    "en": {
        "sections": ["Meeting Overview", "Key Discussion Points", "Decisions", "Action Items",
                     "Open Issues and Follow-ups"],
        "none": "None", "owner": "Owner", "due": "Due",
    },
    "vi": {
        "sections": ["Tổng quan cuộc họp", "Nội dung thảo luận chính", "Quyết định", "Việc cần làm",
                     "Vấn đề còn bỏ ngỏ và theo dõi"],
        "none": "Không có", "owner": "Phụ trách", "due": "Hạn",
    },
}

SYSTEM_PROMPT = """당신은 다국어 회의의 서기입니다. 아래 회의 자막을 읽고 회의 정리 문서를 작성합니다.

규칙:
- 모든 문장을 반드시 {language} 로만 작성합니다. 자막의 원문 언어가 달라도 모두 {language} 로 옮깁니다.
  절 제목·라벨도 아래에 적힌 {language} 표기를 글자 그대로 사용하고, 한국어 제목을 남기지 않습니다.
- 자막에 없는 내용을 지어내지 않습니다. 확인되지 않은 수치·이름·기한을 만들지 않습니다.
- 아래 5개 절을 이 순서와 제목으로 작성합니다. 해당 내용이 없는 절은 «- {none}» 한 줄만 둡니다.
- 각 절은 «## 제목» 한 줄 뒤에 «- » 로 시작하는 항목을 씁니다. 다른 마크다운(굵게, 표, 코드)은 쓰지 않습니다.
- 네 번째 절(할 일)의 항목에는 담당자·기한이 자막에 언급된 경우에만 «({owner}: 이름, {due}: 날짜)» 를 붙입니다.

절 제목 (이 표기를 그대로 사용):
{sections}"""


def build_system_prompt(language: str) -> str:
    labels = SECTION_LABELS.get(language, SECTION_LABELS["en"])
    lang_label = LANGUAGES[language].label if language in LANGUAGES else language
    return SYSTEM_PROMPT.format(
        language=lang_label,
        none=labels["none"], owner=labels["owner"], due=labels["due"],
        sections="\n".join(f"## {title}" for title in labels["sections"]),
    )


class SummaryUnavailable(RuntimeError):
    pass


class SummaryProvider(ABC):
    @property
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    async def summarize(self, transcript: List[dict], language: str) -> str: ...


class UnconfiguredSummaryProvider(SummaryProvider):
    def __init__(self, reason: str) -> None:
        self.reason = reason

    @property
    def available(self) -> bool:
        return False

    async def summarize(self, transcript: List[dict], language: str) -> str:
        raise SummaryUnavailable(self.reason)


def transcript_to_text(transcript: List[dict]) -> str:
    """LLM 에 보낼 자막 원문. 번역본은 빼고 원문만 보내 토큰을 아낀다."""
    lines = []
    for entry in transcript:
        lang = LANGUAGES.get(entry.get("source_lang", ""), None)
        label = lang.label if lang else entry.get("source_lang", "?")
        lines.append(f"[{entry['time_kst']}] {entry['speaker']} ({label}): {entry['original']}")
    return "\n".join(lines)


class DeepSeekSummaryProvider(SummaryProvider):
    def __init__(self, api_key: str, model: str, timeout_seconds: float = 120.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._transport = transport  # 테스트에서 MockTransport 를 꽂는다

    @property
    def available(self) -> bool:
        return True

    async def summarize(self, transcript: List[dict], language: str) -> str:
        if not transcript:
            raise SummaryUnavailable("정리할 자막이 아직 없습니다.")
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": build_system_prompt(language)},
                {"role": "user", "content": transcript_to_text(transcript)},
            ],
            # 요약은 추론 모드가 필요 없고 응답 속도가 중요하다.
            "thinking": {"type": "disabled"},
            "temperature": 0.3,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.post(DEEPSEEK_URL, json=body, headers=headers)
        except httpx.HTTPError as error:
            logger.warning("[summary] 요청 실패 - %s", type(error).__name__)
            raise SummaryUnavailable("요약 서버에 연결하지 못했습니다.") from error

        if response.status_code != 200:
            # 오류 본문에 키가 섞일 수 있으므로 상태 코드만 남긴다.
            logger.warning("[summary] 응답 오류 - status=%s", response.status_code)
            if response.status_code in (401, 403):
                raise SummaryUnavailable("DeepSeek API 키가 유효하지 않습니다.")
            if response.status_code == 402:
                raise SummaryUnavailable("DeepSeek 잔액이 부족합니다.")
            raise SummaryUnavailable(f"요약 요청이 거부되었습니다 (HTTP {response.status_code}).")

        data = response.json()
        try:
            text = data["choices"][0]["message"]["content"].strip()
            usage = data.get("usage", {})
        except (KeyError, IndexError, AttributeError) as error:
            raise SummaryUnavailable("요약 응답 형식이 예상과 다릅니다.") from error
        if not text:
            raise SummaryUnavailable("요약 결과가 비어 있습니다.")
        logger.info(
            "[summary] 완료 - 언어=%s 자막=%d건 입력토큰=%s 출력토큰=%s",
            language, len(transcript), usage.get("prompt_tokens"), usage.get("completion_tokens"),
        )
        return text


def build_summary_provider(settings) -> SummaryProvider:
    status = settings.summary_status()
    if status["state"] != "configured":
        return UnconfiguredSummaryProvider(status["reason"])
    return DeepSeekSummaryProvider(settings.deepseek_api_key, settings.summary_model)
