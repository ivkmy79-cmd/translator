import pytest

from app.rooms import FloorError, RoomManager


def make_room():
    manager = RoomManager()
    room = manager.get_or_create("team1")
    host = room.add_participant("호스트", "ko", "ko")
    guest = room.add_participant("Guest", "en", "en")
    return manager, room, host, guest


def test_first_participant_becomes_host():
    _, _, host, guest = make_room()
    assert host.is_host is True
    assert guest.is_host is False


def test_room_code_is_normalized():
    manager = RoomManager()
    room = manager.get_or_create("team1")
    assert room.code == "TEAM1"
    assert manager.get("TeAm1") is room


def test_floor_is_exclusive():
    _, room, host, guest = make_room()
    room.acquire_floor(host.id)

    with pytest.raises(FloorError) as error:
        room.acquire_floor(guest.id)
    assert "호스트" in str(error.value)

    assert room.floor_holder().id == host.id


def test_double_acquire_by_same_participant_is_rejected():
    _, room, host, _ = make_room()
    room.acquire_floor(host.id)
    with pytest.raises(FloorError):
        room.acquire_floor(host.id)


def test_release_only_by_holder():
    _, room, host, guest = make_room()
    room.acquire_floor(host.id)

    assert room.release_floor(guest.id) is False
    assert room.floor_holder_id == host.id

    assert room.release_floor(host.id) is True
    assert room.floor_holder_id is None


def test_disconnect_of_speaker_clears_the_lock():
    _, room, host, guest = make_room()
    room.acquire_floor(host.id)

    room.remove_participant(host.id)

    assert room.floor_holder_id is None
    room.acquire_floor(guest.id)  # 잠금이 남아 있으면 여기서 실패한다


def test_caption_languages_collects_distinct_targets():
    _, room, _, _ = make_room()
    assert room.caption_languages() == ["en", "ko"]


def test_transcript_keeps_order_and_kst_time():
    _, room, host, _ = make_room()
    first = room.append_final(host, "안녕하세요", {"en": "Hello"})
    second = room.append_final(host, "반갑습니다", {"en": "Nice to meet you"})

    assert [entry["id"] for entry in room.transcript] == [first["id"], second["id"]]
    assert len(first["time_kst"].split(":")) == 3
    assert first["translations"]["en"] == "Hello"


def test_close_removes_room_and_wipes_transcript():
    manager, room, host, _ = make_room()
    room.append_final(host, "안녕하세요", {"en": "Hello"})

    manager.close("TEAM1")

    assert manager.get("TEAM1") is None
    assert room.transcript == []
    assert room.participants == {}
