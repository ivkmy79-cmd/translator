import asyncio

import pytest

from app.config import Settings, is_active, load_settings
from app.providers import ProviderUnavailable, UnconfiguredProvider, build_provider


def test_missing_key_is_reported_as_unconfigured():
    settings = Settings("", "koreacentral", 30, 25)
    status = settings.azure_status()

    assert settings.azure_configured is False
    assert status["state"] == "unconfigured"
    assert status["label"] == "Azure 설정 필요"
    assert "AZURE_SPEECH_KEY" in status["reason"]


def test_configured_settings_report_connected():
    settings = Settings("dummy-key", "koreacentral", 30, 25)
    status = settings.azure_status()

    assert settings.azure_configured is True
    assert status["state"] == "configured"
    assert status["reason"] == ""


def test_warning_must_be_before_limit(monkeypatch):
    monkeypatch.setenv("FLOOR_LIMIT_SECONDS", "10")
    monkeypatch.setenv("FLOOR_WARNING_SECONDS", "10")
    with pytest.raises(ValueError):
        load_settings()


def test_only_poc_languages_are_active():
    assert is_active("ko") and is_active("en") and is_active("vi")
    assert not is_active("zh")   # 레지스트리에는 있으나 PoC 비활성


def test_credentials_are_read_from_the_referenced_file(tmp_path, monkeypatch):
    credentials = tmp_path / "translator_api.txt"
    credentials.write_text(
        "# comment\nAZURE_SPEECH_KEY=file-key\nAZURE_SPEECH_REGION=koreacentral\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AZURE_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("AZURE_SPEECH_KEY", "")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "")

    settings = load_settings()

    assert settings.speech_key == "file-key"
    assert settings.speech_region == "koreacentral"
    assert settings.azure_status()["state"] == "configured"


def test_environment_variable_wins_over_the_credentials_file(tmp_path, monkeypatch):
    credentials = tmp_path / "translator_api.txt"
    credentials.write_text("AZURE_SPEECH_KEY=file-key\n", encoding="utf-8")
    monkeypatch.setenv("AZURE_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("AZURE_SPEECH_KEY", "env-key")

    assert load_settings().speech_key == "env-key"


def test_empty_key_in_the_file_names_the_file_in_the_reason(tmp_path, monkeypatch):
    credentials = tmp_path / "translator_api.txt"
    credentials.write_text(
        "AZURE_SPEECH_KEY= \nAZURE_SPEECH_REGION=koreacentral\n", encoding="utf-8"
    )
    monkeypatch.setenv("AZURE_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("AZURE_SPEECH_KEY", "")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "")

    status = load_settings().azure_status()

    assert status["state"] == "unconfigured"
    assert "translator_api.txt" in status["reason"]
    assert "AZURE_SPEECH_KEY" in status["reason"]
    assert "AZURE_SPEECH_REGION" not in status["reason"]  # 채워진 항목은 지적하지 않는다


def test_missing_credentials_file_is_reported_not_swallowed(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist.txt"
    monkeypatch.setenv("AZURE_CREDENTIALS_FILE", str(missing))
    monkeypatch.setenv("AZURE_SPEECH_KEY", "")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "")

    status = load_settings().azure_status()

    assert status["state"] == "unconfigured"
    assert "찾을 수 없습니다" in status["reason"]
    assert "does-not-exist.txt" in status["reason"]


def test_credentials_file_values_never_leak_into_the_reason(tmp_path, monkeypatch):
    credentials = tmp_path / "translator_api.txt"
    credentials.write_text("AZURE_SPEECH_KEY=\nAZURE_SPEECH_REGION=secret-region\n",
                           encoding="utf-8")
    monkeypatch.setenv("AZURE_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("AZURE_SPEECH_KEY", "")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "")

    assert "secret-region" not in load_settings().azure_status()["reason"]


def test_build_provider_without_key_returns_unavailable():
    provider = build_provider(Settings("", "koreacentral", 30, 25))

    assert isinstance(provider, UnconfiguredProvider)
    assert provider.available is False


def test_unconfigured_provider_refuses_instead_of_faking_translation():
    provider = UnconfiguredProvider("키가 없습니다.")

    async def attempt():
        await provider.start_session("ko", ["en"], None, None)

    with pytest.raises(ProviderUnavailable) as error:
        asyncio.run(attempt())
    assert "키가 없습니다." in str(error.value)


def test_summary_is_unconfigured_without_deepseek_key():
    settings = Settings("k", "koreacentral", 30, 25)
    status = settings.summary_status()
    assert status["state"] == "unconfigured"
    assert "DEEPSEEK_API_KEY" in status["reason"]


def test_summary_is_configured_with_deepseek_key():
    settings = Settings("k", "koreacentral", 30, 25, deepseek_api_key="ds-key")
    assert settings.summary_status()["state"] == "configured"
    assert settings.summary_model == "deepseek-flash"


def test_unconfigured_summary_provider_refuses():
    from app.providers.summary import SummaryUnavailable, build_summary_provider
    provider = build_summary_provider(Settings("k", "koreacentral", 30, 25))
    assert provider.available is False
    with pytest.raises(SummaryUnavailable):
        asyncio.run(provider.summarize([{"original": "x"}], "ko"))


def test_transcript_text_uses_original_only():
    from app.providers.summary import transcript_to_text
    text = transcript_to_text([{
        "time_kst": "10:00:00", "speaker": "홍길동", "source_lang": "ko",
        "original": "안녕하세요", "translations": {"en": "Hello"},
    }])
    assert text == "[10:00:00] 홍길동 (한국어): 안녕하세요"


def _deepseek_transport(status=200, content="## 회의 개요\n- 테스트", captured=None):
    import httpx, json

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "nope"}})
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })
    return httpx.MockTransport(handler)


def test_deepseek_request_shape_and_response_parsing():
    import json
    from app.providers.summary import DEEPSEEK_URL, DeepSeekSummaryProvider
    captured = []
    provider = DeepSeekSummaryProvider("ds-key", "deepseek-flash",
                                       transport=_deepseek_transport(captured=captured))
    transcript = [{"time_kst": "10:00:00", "speaker": "A", "source_lang": "ko",
                   "original": "안녕하세요", "translations": {}}]
    text = asyncio.run(provider.summarize(transcript, "en"))

    assert text == "## 회의 개요\n- 테스트"
    request = captured[0]
    assert str(request.url) == DEEPSEEK_URL
    assert request.headers["authorization"] == "Bearer ds-key"
    body = json.loads(request.content)
    assert body["model"] == "deepseek-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert body["messages"][0]["role"] == "system" and "English" in body["messages"][0]["content"]
    assert body["messages"][1]["content"] == "[10:00:00] A (한국어): 안녕하세요"


@pytest.mark.parametrize("status,expected", [(401, "키"), (402, "잔액"), (500, "HTTP 500")])
def test_deepseek_errors_are_reported_without_faking_a_summary(status, expected):
    from app.providers.summary import DeepSeekSummaryProvider, SummaryUnavailable
    provider = DeepSeekSummaryProvider("ds-key", "deepseek-flash",
                                       transport=_deepseek_transport(status=status))
    with pytest.raises(SummaryUnavailable, match=expected):
        asyncio.run(provider.summarize([{"time_kst": "t", "speaker": "A",
                                         "source_lang": "ko", "original": "x"}], "ko"))


def test_deepseek_refuses_empty_transcript():
    from app.providers.summary import DeepSeekSummaryProvider, SummaryUnavailable
    provider = DeepSeekSummaryProvider("ds-key", "deepseek-flash", transport=_deepseek_transport())
    with pytest.raises(SummaryUnavailable):
        asyncio.run(provider.summarize([], "ko"))
