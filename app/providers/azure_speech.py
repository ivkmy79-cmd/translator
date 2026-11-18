"""Azure Speech Translation Provider.

SDK 콜백은 별도 스레드에서 오므로 asyncio 루프로 넘길 때 run_coroutine_threadsafe 를 쓴다.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Sequence

from .base import (
    AUDIO_BITS_PER_SAMPLE,
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    FinalCallback,
    InterimCallback,
    ProviderUnavailable,
    SpeechTranslationProvider,
    TranslationSession,
)

logger = logging.getLogger(__name__)


class AzureTranslationSession(TranslationSession):
    def __init__(self, recognizer, push_stream, loop: asyncio.AbstractEventLoop,
                 on_interim: InterimCallback, on_final: FinalCallback,
                 source_lang: str) -> None:
        self._recognizer = recognizer
        self._push_stream = push_stream
        self._loop = loop
        self._on_interim = on_interim
        self._on_final = on_final
        self._source_lang = source_lang
        self._closed = False
        # 조용한 결손 감시용 - 턴마다 건수를 남긴다.
        self.interim_count = 0
        self.final_count = 0
        self.audio_bytes = 0

    def _dispatch(self, coro) -> None:
        if self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def handle_recognizing(self, evt) -> None:
        text = (evt.result.text or "").strip()
        if not text:
            return
        self.interim_count += 1
        self._dispatch(self._on_interim(text))

    def handle_recognized(self, evt) -> None:
        text = (evt.result.text or "").strip()
        if not text:
            return
        translations: Dict[str, str] = {}
        raw = getattr(evt.result, "translations", None)
        if raw:
            for target, value in raw.items():
                translations[str(target)] = value
        self.final_count += 1
        self._dispatch(self._on_final(text, translations))

    def handle_canceled(self, evt) -> None:
        logger.warning("[azure] 인식 취소 - reason=%s detail=%s",
                       getattr(evt, "reason", "?"), getattr(evt, "error_details", ""))

    async def push_audio(self, chunk: bytes) -> None:
        if self._closed:
            return
        self.audio_bytes += len(chunk)
        self._push_stream.write(chunk)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._push_stream.close()
        await asyncio.to_thread(self._recognizer.stop_continuous_recognition)
        logger.info(
            "[azure] 턴 종료 - 언어=%s 오디오=%d바이트 interim=%d건 final=%d건",
            self._source_lang, self.audio_bytes, self.interim_count, self.final_count,
        )
        if self.audio_bytes > 0 and self.final_count == 0:
            logger.warning(
                "[azure] 오디오 %d바이트를 보냈으나 확정 인식 0건 - 마이크 입력 또는 언어 설정 확인 필요",
                self.audio_bytes,
            )


class AzureSpeechProvider(SpeechTranslationProvider):
    def __init__(self, key: str, region: str) -> None:
        self._key = key
        self._region = region

    @property
    def available(self) -> bool:
        return True

    async def start_session(
        self,
        source_lang: str,
        target_langs: Sequence[str],
        on_interim: InterimCallback,
        on_final: FinalCallback,
    ) -> TranslationSession:
        import azure.cognitiveservices.speech as speechsdk  # 지연 임포트

        from ..config import LANGUAGES

        source = LANGUAGES[source_lang]
        targets = [
            LANGUAGES[code].translate_code
            for code in target_langs
            if code in LANGUAGES and code != source_lang
        ]

        config = speechsdk.translation.SpeechTranslationConfig(
            subscription=self._key, region=self._region
        )
        config.speech_recognition_language = source.speech_locale
        for target in targets:
            config.add_target_language(target)

        stream_format = speechsdk.audio.AudioStreamFormat(
            samples_per_second=AUDIO_SAMPLE_RATE,
            bits_per_sample=AUDIO_BITS_PER_SAMPLE,
            channels=AUDIO_CHANNELS,
        )
        push_stream = speechsdk.audio.PushAudioInputStream(stream_format)
        recognizer = speechsdk.translation.TranslationRecognizer(
            translation_config=config,
            audio_config=speechsdk.audio.AudioConfig(stream=push_stream),
        )

        session = AzureTranslationSession(
            recognizer, push_stream, asyncio.get_running_loop(),
            on_interim, on_final, source_lang,
        )
        recognizer.recognizing.connect(session.handle_recognizing)
        recognizer.recognized.connect(session.handle_recognized)
        recognizer.canceled.connect(session.handle_canceled)
        recognizer.start_continuous_recognition()
        logger.info("[azure] 턴 시작 - source=%s targets=%s", source.speech_locale, targets)
        return session


def build_provider(settings) -> SpeechTranslationProvider:
    """설정 상태에 따라 실제 Provider 또는 미설정 Provider 를 돌려준다."""
    from .unconfigured import UnconfiguredProvider

    status = settings.azure_status()
    if status["state"] != "configured":
        return UnconfiguredProvider(status["reason"])
    try:
        import azure.cognitiveservices.speech  # noqa: F401
    except ImportError:
        return UnconfiguredProvider(
            "azure-cognitiveservices-speech 패키지가 설치되지 않았습니다. "
            "requirements.txt 를 설치하세요."
        )
    return AzureSpeechProvider(settings.speech_key, settings.speech_region)
