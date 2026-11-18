"""FastAPI 진입점 - 회의방 WebSocket 라우팅."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import LANGUAGES, active_languages, is_active, load_settings
from .export_docx import build_minutes_docx
from .providers import ProviderUnavailable, build_provider
from .providers.summary import SummaryUnavailable, build_summary_provider
from .rooms import KST, FloorError, Participant, Room, RoomManager, now_kst_label

class KstFormatter(logging.Formatter):
    """서버 시간대와 무관하게 로그 시각을 KST 로 남긴다."""

    def formatTime(self, record, datefmt=None):  # noqa: N802 (logging API)
        stamp = datetime.fromtimestamp(record.created, KST)
        return stamp.strftime(datefmt or "%Y-%m-%d %H:%M:%S KST")


logging.basicConfig(level=logging.INFO)
for _handler in logging.getLogger().handlers:
    _handler.setFormatter(KstFormatter("%(asctime)s %(levelname)s %(message)s"))
logger = logging.getLogger("translator")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

settings = load_settings()
provider = build_provider(settings)
summary_provider = build_summary_provider(settings)
manager = RoomManager()

# 방 코드 -> 현재 발화 턴 상태
active_turns: Dict[str, dict] = {}
# 방 코드 -> 진행 중인 회의 정리 태스크 (중복 요청 방지)
summary_tasks: Dict[str, asyncio.Task] = {}

app = FastAPI(title="실시간 회의 번역기 PoC")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
async def status() -> JSONResponse:
    return JSONResponse(
        {
            "azure": settings.azure_status(),
            "summary": settings.summary_status(),
            "passcode_required": bool(settings.room_passcode),
            "languages": [
                {"code": lang.code, "label": lang.label} for lang in active_languages()
            ],
            "limits": {
                "floor_limit_seconds": settings.floor_limit_seconds,
                "floor_warning_seconds": settings.floor_warning_seconds,
            },
        }
    )


@app.post("/api/export/docx")
async def export_docx(request: Request) -> Response:
    """브라우저가 보낸 자막·정리 결과를 DOCX 로 변환해 돌려준다. 서버에 남기지 않는다."""
    payload = await request.json()
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return JSONResponse({"message": "entries 목록이 필요합니다."}, status_code=400)
    room_code = str(payload.get("room") or "meeting")[:12]
    caption_lang = str(payload.get("caption_lang") or "ko")
    raw_summaries = payload.get("summaries")
    summaries = {
        str(code): text for code, text in (raw_summaries or {}).items()
        if isinstance(text, str) and text.strip()
    } if isinstance(raw_summaries, dict) else {}
    data = build_minutes_docx(room_code, entries, caption_lang, summaries, now_kst_label())
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="meeting-{room_code}.docx"'},
    )


# --- 전송 helper -------------------------------------------------------

async def send_json(participant: Participant, payload: dict) -> None:
    if participant.connection is None:
        return
    try:
        await participant.connection.send_json(payload)
    except Exception:
        # 이미 끊긴 연결. 정리는 각 연결의 finally 가 담당한다.
        logger.debug("[ws] 전송 실패 - %s", participant.name)


async def broadcast(room: Room, payload: dict) -> None:
    for participant in list(room.participants.values()):
        await send_json(participant, payload)


async def broadcast_participants(room: Room) -> None:
    holder = room.floor_holder()
    await broadcast(
        room,
        {
            "type": "participants",
            "list": [p.public() for p in room.participants.values()],
            "floor": {"id": holder.id, "name": holder.name} if holder else None,
        },
    )


# --- 발언권 -------------------------------------------------------------

async def stop_turn(room: Room, reason: str) -> None:
    """발언권 해제 경로는 이 함수 하나로 모은다."""
    turn = active_turns.pop(room.code, None)
    if turn is None:
        if room.floor_holder_id is not None:
            room.floor_holder_id = None
            await broadcast_participants(room)
        return

    # 제한 시간이 다 됐을 때는 타이머 태스크 자신이 여기로 들어온다.
    # 그 상태에서 cancel() 하면 자기 자신이 취소되어 아래 close/broadcast 가 중단된다.
    timer: Optional[asyncio.Task] = turn.get("timer")
    if timer is not None and timer is not asyncio.current_task() and not timer.done():
        timer.cancel()

    session = turn.get("session")
    if session is not None:
        try:
            await session.close()
        except Exception:
            logger.exception("[turn] 세션 종료 실패")

    room.release_floor(turn["participant_id"])
    logger.info(
        "[turn] 발언 종료 - 방=%s 화자=%s 사유=%s",
        room.code, turn.get("name"), reason,
    )
    # 화자가 콘솔 로그 없이도 «오디오는 갔는데 인식 0건» 을 알 수 있게 통계를 같이 보낸다.
    stats = {
        "audio_bytes": getattr(session, "audio_bytes", 0),
        "interim": getattr(session, "interim_count", 0),
        "final": getattr(session, "final_count", 0),
    }
    await broadcast(room, {
        "type": "floor_released", "reason": reason,
        "speaker_id": turn["participant_id"], "stats": stats,
    })
    await broadcast_participants(room)


async def floor_timer(room: Room, participant_id: str) -> None:
    """서버가 타이머의 진실 공급원이다. 클라이언트 타이머는 표시용."""
    warning_at = settings.floor_warning_seconds
    limit = settings.floor_limit_seconds
    await asyncio.sleep(warning_at)
    if room.floor_holder_id != participant_id:
        return
    await broadcast(room, {"type": "floor_warning", "remaining": limit - warning_at})
    await asyncio.sleep(limit - warning_at)
    if room.floor_holder_id != participant_id:
        return
    await stop_turn(room, "time_limit")


def translations_by_app_code(
    raw: Dict[str, str], source_lang: str, original: str
) -> Dict[str, str]:
    """Azure 번역 코드 키를 앱 언어 코드 키로 되돌린다."""
    result: Dict[str, str] = {source_lang: original}
    for code, language in LANGUAGES.items():
        if code == source_lang:
            continue
        text = raw.get(language.translate_code)
        if text:
            result[code] = text
    return result


async def start_turn(room: Room, participant: Participant) -> None:
    async def on_interim(text: str) -> None:
        await broadcast(
            room,
            {
                "type": "interim",
                "speaker": participant.name,
                "speaker_id": participant.id,
                "source_lang": participant.speak_lang,
                "text": text,
            },
        )

    async def on_final(original: str, raw: Dict[str, str]) -> None:
        translations = translations_by_app_code(raw, participant.speak_lang, original)
        entry = room.append_final(participant, original, translations)
        await broadcast(room, {"type": "final", "entry": entry})

    session = await provider.start_session(
        source_lang=participant.speak_lang,
        target_langs=room.caption_languages(),
        on_interim=on_interim,
        on_final=on_final,
    )
    active_turns[room.code] = {
        "participant_id": participant.id,
        "name": participant.name,
        "session": session,
        "timer": asyncio.create_task(floor_timer(room, participant.id)),
    }


# --- WebSocket ---------------------------------------------------------

@app.websocket("/ws")
async def meeting_socket(websocket: WebSocket) -> None:
    params = websocket.query_params
    code = (params.get("room") or "").strip().upper()
    name = (params.get("name") or "").strip()
    speak_lang = (params.get("speak") or "").strip()
    caption_lang = (params.get("caption") or "").strip()
    passcode = (params.get("passcode") or "").strip()

    await websocket.accept()

    problem = None
    if not code:
        problem = "회의방 이름을 입력하세요."
    elif not name:
        problem = "참석자 이름을 입력하세요."
    elif not is_active(speak_lang) or not is_active(caption_lang):
        problem = "지원하지 않는 언어입니다."
    elif settings.room_passcode and not hmac.compare_digest(passcode, settings.room_passcode):
        problem = "접속 코드가 올바르지 않습니다."
    if problem:
        await websocket.send_json({"type": "error", "message": problem, "fatal": True})
        await websocket.close()
        return

    room = manager.get_or_create(code)
    participant = room.add_participant(name, speak_lang, caption_lang, websocket)
    logger.info("[ws] 입장 - 방=%s 이름=%s host=%s", room.code, name, participant.is_host)

    await websocket.send_json(
        {
            "type": "welcome",
            "you": participant.public(),
            "room": {"code": room.code},
            "azure": settings.azure_status(),
            "summary": settings.summary_status(),
            "limits": {
                "floor_limit_seconds": settings.floor_limit_seconds,
                "floor_warning_seconds": settings.floor_warning_seconds,
            },
            "transcript": room.transcript,
        }
    )
    await broadcast_participants(room)

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                await handle_audio(room, participant, message["bytes"])
                continue

            text = message.get("text")
            if text is None:
                continue
            await handle_command(room, participant, text)
    except WebSocketDisconnect:
        pass
    finally:
        if room.floor_holder_id == participant.id:
            await stop_turn(room, "disconnected")
        room.remove_participant(participant.id)
        logger.info(
            "[ws] 퇴장 - 방=%s 이름=%s 남은=%d명",
            room.code, participant.name, len(room.participants),
        )
        if room.is_empty():
            task = summary_tasks.pop(room.code, None)
            if task is not None and not task.done():
                task.cancel()
            manager.close(room.code)
            logger.info("[room] 빈 방 제거 - %s (자막 메모리에서 삭제)", room.code)
        else:
            await broadcast_participants(room)


async def handle_audio(room: Room, participant: Participant, chunk: bytes) -> None:
    turn = active_turns.get(room.code)
    if turn is None or turn["participant_id"] != participant.id:
        return  # 발언권 없는 오디오는 조용히 버린다.
    await turn["session"].push_audio(chunk)


async def run_summary(room: Room, participant: Participant) -> None:
    """자막 스냅샷을 활성 언어 전부로 동시에 요약해 요청자에게만 돌려준다. 결과는 서버에 남기지 않는다."""
    snapshot = list(room.transcript)
    languages = [lang.code for lang in active_languages()]
    results = await asyncio.gather(
        *(summary_provider.summarize(snapshot, code) for code in languages),
        return_exceptions=True,
    )
    texts: Dict[str, str] = {}
    errors: Dict[str, str] = {}
    for code, result in zip(languages, results):
        if isinstance(result, SummaryUnavailable):
            errors[code] = str(result)
        elif isinstance(result, BaseException):
            logger.error("[summary] 실패 - 방=%s 언어=%s", room.code, code, exc_info=result)
            errors[code] = "회의 정리에 실패했습니다."
        else:
            texts[code] = result
    logger.info("[summary] 회의 정리 - 방=%s 요청자=%s 자막=%d건 성공=%s 실패=%s",
                room.code, participant.name, len(snapshot), list(texts), list(errors))
    if not texts:
        await send_json(participant, {"type": "summary_failed", "message": next(iter(errors.values()))})
        return
    await send_json(participant, {"type": "summary", "texts": texts, "errors": errors})


async def handle_command(room: Room, participant: Participant, raw: str) -> None:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        await send_json(participant, {"type": "error", "message": "잘못된 메시지 형식"})
        return

    kind = message.get("type")

    if kind == "floor_request":
        if not provider.available:
            await send_json(
                participant,
                {
                    "type": "floor_denied",
                    "message": settings.azure_status()["reason"] or "Azure 설정이 필요합니다.",
                },
            )
            return
        try:
            room.acquire_floor(participant.id)
        except FloorError as error:
            await send_json(participant, {"type": "floor_denied", "message": str(error)})
            return
        try:
            await start_turn(room, participant)
        except ProviderUnavailable as error:
            room.release_floor(participant.id)
            await send_json(participant, {"type": "floor_denied", "message": str(error)})
            return
        except Exception:
            room.release_floor(participant.id)
            logger.exception("[turn] 세션 시작 실패")
            await send_json(
                participant,
                {"type": "floor_denied", "message": "음성 세션을 시작하지 못했습니다."},
            )
            return
        logger.info("[turn] 발언 시작 - 방=%s 화자=%s", room.code, participant.name)
        await broadcast_participants(room)

    elif kind == "floor_release":
        if room.floor_holder_id == participant.id:
            await stop_turn(room, "released")

    elif kind == "summarize":
        if not participant.is_host:
            await send_json(participant, {"type": "error", "message": "회의 정리는 Host 만 할 수 있습니다."})
            return
        running = summary_tasks.get(room.code)
        if running is not None and not running.done():
            await send_json(participant, {"type": "summary_failed", "message": "이미 회의 정리가 진행 중입니다."})
            return
        # API 응답을 기다리는 동안 이 소켓의 수신 루프가 막히지 않도록 태스크로 돌린다.
        summary_tasks[room.code] = asyncio.create_task(run_summary(room, participant))

    elif kind == "end_room":
        if not participant.is_host:
            await send_json(
                participant,
                {"type": "error", "message": "회의 종료는 Host 만 할 수 있습니다."},
            )
            return
        await stop_turn(room, "room_closed")
        await broadcast(room, {"type": "room_closed"})
        manager.close(room.code)
        logger.info("[room] Host 종료 - %s (자막 메모리에서 삭제)", room.code)

    else:
        await send_json(participant, {"type": "error", "message": f"알 수 없는 요청: {kind}"})
