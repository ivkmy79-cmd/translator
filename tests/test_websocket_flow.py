"""WebSocket 시퀀스 검증.

conftest 가 FLOOR_LIMIT_SECONDS=2 / FLOOR_WARNING_SECONDS=1 로 고정하므로
30초 제한 로직을 2초 안에 검증할 수 있다.
"""
import asyncio
import dataclasses
import time
from typing import Sequence

import pytest
from fastapi.testclient import TestClient

from app import main
from app.providers.base import SpeechTranslationProvider, TranslationSession


class FakeSession(TranslationSession):
    """오디오 바이트만 세고 아무 자막도 만들지 않는다 (가짜 번역 금지)."""

    def __init__(self) -> None:
        self.audio_bytes = 0
        self.closed = False

    async def push_audio(self, chunk: bytes) -> None:
        self.audio_bytes += len(chunk)

    async def close(self) -> None:
        # 실제 Azure 세션의 close() 는 스레드 대기를 하므로 취소 지점이 생긴다.
        # 여기서도 같은 지점을 만들어야 «타이머가 자기 자신을 취소하는» 결함이 드러난다.
        await asyncio.sleep(0.01)
        self.closed = True


class FakeProvider(SpeechTranslationProvider):
    def __init__(self) -> None:
        self.sessions = []

    @property
    def available(self) -> bool:
        return True

    async def start_session(self, source_lang: str, target_langs: Sequence[str],
                            on_interim, on_final) -> TranslationSession:
        session = FakeSession()
        self.sessions.append(session)
        return session


def connect(client: TestClient, room: str, name: str, speak="ko", caption="ko", passcode=""):
    return client.websocket_connect(
        f"/ws?room={room}&name={name}&speak={speak}&caption={caption}&passcode={passcode}"
    )


@pytest.fixture
def passcode(monkeypatch):
    """서버 설정에 접속 코드를 켠다. settings 는 frozen 이라 복제본으로 바꾼다."""
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, room_passcode="7942"))
    return "7942"


def read_until(socket, kind, where=None, limit=10):
    """참가자 입퇴장 브로드캐스트가 섞이므로 조건까지 맞는 메시지를 기다린다."""
    seen = []
    for _ in range(limit):
        message = socket.receive_json()
        seen.append(message["type"])
        if message["type"] == kind and (where is None or where(message)):
            return message
    raise AssertionError(f"{kind} 메시지를 받지 못했습니다. 받은 순서: {seen}")


def wait_until(predicate, timeout=2.0):
    """연결 종료 후 서버 정리는 비동기로 끝나므로 짧게 폴링한다."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def client():
    main.manager.rooms.clear()
    main.active_turns.clear()
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def fake_provider(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(main, "provider", provider)
    return provider


def test_status_endpoint_reports_unconfigured_azure(client):
    body = client.get("/api/status").json()

    assert body["azure"]["state"] == "unconfigured"
    assert [lang["code"] for lang in body["languages"]] == ["ko", "en", "vi"]
    assert body["passcode_required"] is False


def test_status_reports_passcode_requirement(client, passcode):
    assert client.get("/api/status").json()["passcode_required"] is True


def test_wrong_or_missing_passcode_is_rejected(client, passcode):
    for wrong in ("", "0000"):
        with connect(client, "TEAM0", "호스트", passcode=wrong) as socket:
            error = socket.receive_json()
            assert error["type"] == "error" and error["fatal"] is True
            assert "접속 코드" in error["message"]
    assert main.manager.get("TEAM0") is None  # 거부된 접속은 방을 만들지 않는다


def test_correct_passcode_is_admitted(client, passcode):
    with connect(client, "TEAM0", "호스트", passcode=passcode) as socket:
        assert socket.receive_json()["type"] == "welcome"


def test_two_clients_share_the_room_and_see_each_other(client):
    with connect(client, "TEAM1", "호스트") as first:
        welcome = first.receive_json()
        assert welcome["type"] == "welcome"
        assert welcome["you"]["is_host"] is True
        assert welcome["room"]["code"] == "TEAM1"

        with connect(client, "team1", "Guest", speak="en", caption="en") as second:
            second_welcome = second.receive_json()
            assert second_welcome["you"]["is_host"] is False

            update = read_until(first, "participants", lambda m: len(m["list"]) == 2)
            names = sorted(person["name"] for person in update["list"])
            assert names == ["Guest", "호스트"]


def test_floor_is_denied_while_azure_is_unconfigured(client):
    with connect(client, "TEAM2", "호스트") as socket:
        socket.receive_json()  # welcome
        socket.send_json({"type": "floor_request"})

        denied = read_until(socket, "floor_denied")
        assert "AZURE_SPEECH_KEY" in denied["message"]


def test_second_speaker_is_locked_out(client, fake_provider):
    with connect(client, "TEAM3", "호스트") as first:
        first.receive_json()
        with connect(client, "TEAM3", "Guest") as second:
            second.receive_json()

            first.send_json({"type": "floor_request"})
            granted = read_until(first, "participants", lambda m: m["floor"] is not None)
            assert granted["floor"]["name"] == "호스트"

            second.send_json({"type": "floor_request"})
            denied = read_until(second, "floor_denied")
            assert "호스트" in denied["message"]

            first.send_json({"type": "floor_release"})
            read_until(first, "floor_released")
            assert fake_provider.sessions[0].closed is True


def test_floor_is_released_automatically_at_the_time_limit(client, fake_provider):
    with connect(client, "TEAM4", "호스트") as socket:
        welcome = socket.receive_json()
        assert welcome["limits"]["floor_limit_seconds"] == 2
        assert welcome["limits"]["floor_warning_seconds"] == 1

        socket.send_json({"type": "floor_request"})
        read_until(socket, "participants", lambda m: m["floor"] is not None)

        warning = read_until(socket, "floor_warning")
        assert warning["remaining"] == 1

        # 해제가 안 되면 read_until 은 영원히 막힌다. 서버 상태를 먼저 폴링해 빠르게 실패시킨다.
        room = main.manager.get("TEAM4")
        assert wait_until(lambda: room.floor_holder_id is None, timeout=5), (
            "제한 시간이 지나도 발언권이 해제되지 않았습니다."
        )

        released = read_until(socket, "floor_released")
        assert released["reason"] == "time_limit"
        assert fake_provider.sessions[0].closed is True


def test_audio_from_a_non_holder_is_ignored(client, fake_provider):
    with connect(client, "TEAM5", "호스트") as first:
        first.receive_json()
        with connect(client, "TEAM5", "Guest") as second:
            second.receive_json()

            first.send_json({"type": "floor_request"})
            read_until(first, "participants", lambda m: m["floor"] is not None)

            second.send_bytes(b"\x00\x01" * 100)
            first.send_bytes(b"\x00\x01" * 100)

            first.send_json({"type": "floor_release"})
            read_until(first, "floor_released")

            assert fake_provider.sessions[0].audio_bytes == 200


def test_only_host_can_end_the_room(client, fake_provider):
    with connect(client, "TEAM6", "호스트") as first:
        first.receive_json()
        with connect(client, "TEAM6", "Guest") as second:
            second.receive_json()

            second.send_json({"type": "end_room"})
            error = read_until(second, "error")
            assert "Host" in error["message"]
            assert main.manager.get("TEAM6") is not None

            first.send_json({"type": "end_room"})
            read_until(first, "room_closed")
            assert main.manager.get("TEAM6") is None


def test_room_is_dropped_when_last_participant_leaves(client):
    with connect(client, "TEAM7", "호스트") as socket:
        socket.receive_json()
        assert main.manager.get("TEAM7") is not None

    assert wait_until(lambda: main.manager.get("TEAM7") is None), "빈 방이 제거되지 않았습니다."


# --- 회의 정리 ------------------------------------------------------------

class FakeSummaryProvider:
    def __init__(self, text="## 회의 개요\n- 테스트 회의"):
        self.text = text
        self.calls = []

    @property
    def available(self):
        return True

    async def summarize(self, transcript, language):
        self.calls.append((len(transcript), language))
        return self.text


@pytest.fixture
def fake_summary(monkeypatch):
    provider = FakeSummaryProvider()
    monkeypatch.setattr(main, "summary_provider", provider)
    return provider


def test_status_reports_summary_configuration(client):
    body = client.get("/api/status").json()
    assert body["summary"]["state"] == "unconfigured"


def test_summary_is_refused_while_unconfigured(client):
    with connect(client, "SUM0", "호스트") as socket:
        socket.receive_json()
        socket.send_json({"type": "summarize"})
        failed = read_until(socket, "summary_failed")
        assert "DEEPSEEK_API_KEY" in failed["message"]


def test_only_host_can_summarize(client, fake_summary):
    with connect(client, "SUM1", "호스트") as host:
        host.receive_json()
        with connect(client, "SUM1", "Guest", caption="en") as guest:
            guest.receive_json()
            guest.send_json({"type": "summarize"})
            error = read_until(guest, "error")
            assert "Host" in error["message"]
    assert fake_summary.calls == []


def test_host_receives_summary_in_every_active_language(client, fake_summary):
    with connect(client, "SUM2", "호스트", caption="en") as host:
        host.receive_json()
        room = main.manager.get("SUM2")
        speaker = next(iter(room.participants.values()))
        room.append_final(speaker, "안녕하세요", {"en": "Hello"})

        host.send_json({"type": "summarize"})
        summary = read_until(host, "summary")
        assert sorted(summary["texts"]) == ["en", "ko", "vi"]
        assert all(text.startswith("## 회의 개요") for text in summary["texts"].values())
        assert summary["errors"] == {}
    assert sorted(fake_summary.calls) == [(1, "en"), (1, "ko"), (1, "vi")]


def test_partial_summary_failure_is_reported_per_language(client, monkeypatch):
    from app.providers.summary import SummaryUnavailable

    class FlakyProvider(FakeSummaryProvider):
        async def summarize(self, transcript, language):
            if language == "vi":
                raise SummaryUnavailable("베트남어 실패")
            return await super().summarize(transcript, language)

    monkeypatch.setattr(main, "summary_provider", FlakyProvider())
    with connect(client, "SUM4", "호스트") as host:
        host.receive_json()
        room = main.manager.get("SUM4")
        room.append_final(next(iter(room.participants.values())), "안녕하세요", {})
        host.send_json({"type": "summarize"})
        summary = read_until(host, "summary")
        assert sorted(summary["texts"]) == ["en", "ko"]
        assert summary["errors"] == {"vi": "베트남어 실패"}


def test_docx_export_contains_summary_and_transcript(client):
    import io
    from docx import Document

    response = client.post("/api/export/docx", json={
        "room": "SUM3", "caption_lang": "en",
        "summaries": {"ko": "## 결정 사항\n- 다음 주 배포", "en": "## Decisions\n- Deploy next week", "vi": ""},
        "entries": [{
            "time_kst": "10:00:00", "speaker": "홍길동", "source_lang": "ko",
            "original": "안녕하세요", "translations": {"en": "Hello"},
        }],
    })
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats")
    doc = Document(io.BytesIO(response.content))
    texts = [p.text for p in doc.paragraphs]
    assert "회의 정리 (한국어)" in texts and "다음 주 배포" in texts
    assert "회의 정리 (English)" in texts and "Deploy next week" in texts
    assert "회의 정리 (Tiếng Việt)" not in texts  # 빈 결과는 절을 만들지 않는다
    assert "Hello" in texts and "원문: 안녕하세요" in texts


def test_docx_export_rejects_bad_payload(client):
    assert client.post("/api/export/docx", json={"entries": "nope"}).status_code == 400
