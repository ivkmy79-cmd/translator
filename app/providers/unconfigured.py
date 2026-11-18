"""Azure 키가 없을 때 쓰는 Provider.

가짜 번역을 만들어 성공처럼 보이게 하지 않는다. 항상 거부하고 이유를 남긴다.
"""
from __future__ import annotations

from typing import Sequence

from .base import (
    FinalCallback,
    InterimCallback,
    ProviderUnavailable,
    SpeechTranslationProvider,
    TranslationSession,
)


class UnconfiguredProvider(SpeechTranslationProvider):
    def __init__(self, reason: str) -> None:
        self.reason = reason

    @property
    def available(self) -> bool:
        return False

    async def start_session(
        self,
        source_lang: str,
        target_langs: Sequence[str],
        on_interim: InterimCallback,
        on_final: FinalCallback,
    ) -> TranslationSession:
        raise ProviderUnavailable(self.reason)
