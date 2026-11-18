"""회의방·참가자·발언권(floor) 상태.

메모리에만 존재한다. 디스크에 쓰지 않으며 방이 닫히면 사라진다.
WebSocket 이나 asyncio 에 의존하지 않아 단위 테스트가 가능하다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

KST = timezone(timedelta(hours=9))


def now_kst_label() -> str:
    """UI·회의록에 표시할 KST 시각."""
    return datetime.now(KST).strftime("%H:%M:%S")


@dataclass
class Participant:
    id: str
    name: str
    speak_lang: str
    caption_lang: str
    is_host: bool = False
    connection: Any = None  # WebSocket. 상태 로직은 이 값을 해석하지 않는다.

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "speak_lang": self.speak_lang,
            "caption_lang": self.caption_lang,
            "is_host": self.is_host,
        }


class FloorError(Exception):
    """발언권 요청이 거부된 이유를 담는다."""


@dataclass
class Room:
    code: str
    participants: Dict[str, Participant] = field(default_factory=dict)
    floor_holder_id: Optional[str] = None
    transcript: List[dict] = field(default_factory=list)
    closed: bool = False

    # --- 참가자 ---------------------------------------------------------
    def add_participant(self, name: str, speak_lang: str, caption_lang: str,
                        connection: Any = None) -> Participant:
        if self.closed:
            raise FloorError("이미 종료된 회의방입니다.")
        participant = Participant(
            id=uuid.uuid4().hex[:12],
            name=name,
            speak_lang=speak_lang,
            caption_lang=caption_lang,
            is_host=not self.participants,  # 첫 입장자가 Host
            connection=connection,
        )
        self.participants[participant.id] = participant
        return participant

    def remove_participant(self, participant_id: str) -> Optional[Participant]:
        """퇴장 처리. 발언 중이었다면 잠금을 반드시 함께 푼다."""
        participant = self.participants.pop(participant_id, None)
        if participant is None:
            return None
        if self.floor_holder_id == participant_id:
            self.floor_holder_id = None
        return participant

    def host(self) -> Optional[Participant]:
        for participant in self.participants.values():
            if participant.is_host:
                return participant
        return None

    def is_empty(self) -> bool:
        return not self.participants

    # --- 발언권 ---------------------------------------------------------
    def acquire_floor(self, participant_id: str) -> Participant:
        if participant_id not in self.participants:
            raise FloorError("참가자를 찾을 수 없습니다.")
        if self.floor_holder_id == participant_id:
            raise FloorError("이미 발언 중입니다.")
        if self.floor_holder_id is not None:
            holder = self.participants[self.floor_holder_id]
            raise FloorError(f"{holder.name} 님이 말하는 중입니다.")
        self.floor_holder_id = participant_id
        return self.participants[participant_id]

    def release_floor(self, participant_id: str) -> bool:
        """해제 경로는 이 함수 하나로 모은다(정상 종료·30초 초과·연결 끊김)."""
        if self.floor_holder_id != participant_id:
            return False
        self.floor_holder_id = None
        return True

    def floor_holder(self) -> Optional[Participant]:
        if self.floor_holder_id is None:
            return None
        return self.participants.get(self.floor_holder_id)

    # --- 자막 -----------------------------------------------------------
    def append_final(self, speaker: Participant, original: str,
                     translations: Dict[str, str]) -> dict:
        entry = {
            "id": uuid.uuid4().hex[:12],
            "speaker_id": speaker.id,
            "speaker": speaker.name,
            "time_kst": now_kst_label(),
            "source_lang": speaker.speak_lang,
            "original": original,
            "translations": dict(translations),
        }
        self.transcript.append(entry)
        return entry

    def caption_languages(self) -> List[str]:
        """현재 방에서 필요한 자막 언어 집합."""
        return sorted({p.caption_lang for p in self.participants.values()})


class RoomManager:
    def __init__(self) -> None:
        self.rooms: Dict[str, Room] = {}

    def get_or_create(self, code: str) -> Room:
        normalized = code.strip().upper()
        if not normalized:
            raise FloorError("회의방 이름이 비어 있습니다.")
        room = self.rooms.get(normalized)
        if room is None:
            room = Room(code=normalized)
            self.rooms[normalized] = room
        return room

    def get(self, code: str) -> Optional[Room]:
        return self.rooms.get(code.strip().upper())

    def close(self, code: str) -> Optional[Room]:
        """방을 닫고 메모리에서 제거한다. 자막·음성은 어디에도 남기지 않는다."""
        room = self.rooms.pop(code.strip().upper(), None)
        if room is not None:
            room.closed = True
            room.transcript.clear()
            room.participants.clear()
            room.floor_holder_id = None
        return room
