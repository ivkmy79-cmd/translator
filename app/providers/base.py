"""교체 가능한 음성인식·번역 Provider 인터페이스.

Azure Speech Translation 을 기본으로 쓰되, 나중에 Whisper + M2M100 같은 로컬 모델로
교체할 수 있도록 앱 나머지 부분은 이 인터페이스만 알고 있어야 한다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Dict, Sequence

# on_interim(text) / on_final(original, translations)
InterimCallback = Callable[[str], Awaitable[None]]
FinalCallback = Callable[[str, Dict[str, str]], Awaitable[None]]

# 클라이언트가 보내는 오디오 형식. Azure PushAudioInputStream 기본값과 동일하다.
AUDIO_SAMPLE_RATE = 16000
AUDIO_BITS_PER_SAMPLE = 16
AUDIO_CHANNELS = 1


class ProviderUnavailable(Exception):
    """Provider 를 쓸 수 없는 이유. 사용자에게 그대로 보여줄 수 있는 문구여야 한다."""


class TranslationSession(ABC):
    """한 번의 발화 턴(발언권 획득~해제)에 대응하는 세션."""

    @abstractmethod
    async def push_audio(self, chunk: bytes) -> None:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...


class SpeechTranslationProvider(ABC):
    @property
    @abstractmethod
    def available(self) -> bool:
        ...

    @abstractmethod
    async def start_session(
        self,
        source_lang: str,
        target_langs: Sequence[str],
        on_interim: InterimCallback,
        on_final: FinalCallback,
    ) -> TranslationSession:
        ...
