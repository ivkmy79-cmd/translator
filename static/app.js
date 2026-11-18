"use strict";

const SAMPLE_RATE = 16000;

const state = {
  socket: null,
  me: null,
  roomCode: "",
  captionLang: "ko",
  limits: { floor_limit_seconds: 30, floor_warning_seconds: 25 },
  azure: null,
  summary: null,        // 서버의 회의 정리 설정 상태
  summaries: {},        // 언어 코드 -> 마지막 정리 결과 (DOCX 에 같이 넣는다)
  summaryLang: "",      // 패널에 표시 중인 언어
  summaryErrors: {},    // 언어 코드 -> 실패 사유
  languages: [],        // 서버가 준 활성 언어 [{code, label}]
  passcodeRequired: false,
  entries: [],
  recording: false,
  audio: null,          // { context, stream, node, source }
  countdownTimer: null,
  secondsLeft: 0,
  micPeak: 0,           // 최근 1초 동안의 최대 입력 진폭 (0~32767)
};

const MIC_SILENCE_PEAK = 300; // 이 값 이하가 계속되면 마이크 입력이 없는 것으로 본다

const el = (id) => document.getElementById(id);

// --- 상태 배지 --------------------------------------------------------

function setBadge(node, ok, label) {
  node.className = "badge " + (ok ? "badge-ok" : "badge-bad");
  node.querySelector(".badge-text").textContent = label;
}

function applyAzureStatus(status) {
  state.azure = status;
  const ok = status.state === "configured";
  setBadge(el("join-azure"), ok, status.label);
  const reason = el("join-azure-reason");
  reason.textContent = status.reason || "";
  reason.hidden = !status.reason;
  if (el("azure-badge")) setBadge(el("azure-badge"), ok, status.label);
}

// --- 입장 화면 --------------------------------------------------------

async function loadStatus() {
  const response = await fetch("/api/status");
  const data = await response.json();
  applyAzureStatus(data.azure);
  state.limits = data.limits;
  state.passcodeRequired = Boolean(data.passcode_required);
  el("passcode-field").hidden = !state.passcodeRequired;
  refreshJoinButton();

  for (const select of [el("speak-lang"), el("caption-lang")]) {
    select.innerHTML = "";
    for (const lang of data.languages) {
      const option = document.createElement("option");
      option.value = lang.code;
      option.textContent = lang.label;
      select.appendChild(option);
    }
  }
  state.languages = data.languages;
  el("speak-lang").value = "ko";
  el("caption-lang").value = "ko";
}

function refreshJoinButton() {
  const ready =
    el("consent").checked &&
    el("name").value.trim().length > 0 &&
    el("room").value.trim().length > 0 &&
    (!state.passcodeRequired || el("passcode").value.trim().length > 0);
  el("join-btn").disabled = !ready;
}

function showJoinError(message) {
  const node = el("join-error");
  node.textContent = message;
  node.hidden = !message;
}

// --- WebSocket --------------------------------------------------------

function connect() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const query = new URLSearchParams({
    room: el("room").value.trim(),
    name: el("name").value.trim(),
    speak: el("speak-lang").value,
    caption: el("caption-lang").value,
    passcode: el("passcode").value.trim(),
  });
  state.captionLang = el("caption-lang").value;

  const socket = new WebSocket(`${protocol}//${location.host}/ws?${query}`);
  socket.binaryType = "arraybuffer";
  state.socket = socket;

  socket.onmessage = (event) => handleMessage(JSON.parse(event.data));
  socket.onclose = () => {
    stopRecording("연결 종료");
    if (el("conn-badge")) setBadge(el("conn-badge"), false, "연결 끊김");
    setTalkState("disabled", "연결이 끊겼습니다");
  };
  socket.onerror = () => showJoinError("서버에 연결하지 못했습니다.");
}

function send(payload) {
  if (state.socket && state.socket.readyState === WebSocket.OPEN) {
    state.socket.send(JSON.stringify(payload));
  }
}

function handleMessage(message) {
  switch (message.type) {
    case "welcome":
      state.me = message.you;
      state.summary = message.summary || null;
      state.roomCode = message.room.code;
      state.limits = message.limits;
      state.entries = message.transcript.slice();
      enterMeeting(message.azure);
      break;

    case "participants":
      renderParticipants(message.list, message.floor);
      break;

    case "interim":
      renderInterim(message);
      break;

    case "final":
      state.entries.push(message.entry);
      clearInterim();
      renderEntry(message.entry);
      break;

    case "floor_warning":
      el("talk-btn").classList.add("warning");
      el("floor-status").textContent = `${message.remaining}초 후 발언이 자동 종료됩니다`;
      break;

    case "floor_released":
      if (state.recording) {
        stopRecording(message.reason === "time_limit" ? "30초 제한으로 종료됨" : "발언 종료");
      }
      if (message.stats && state.me && message.speaker_id === state.me.id) {
        el("floor-status").textContent = turnSummary(message.stats);
      }
      break;

    case "floor_denied":
      stopRecording(message.message);
      break;

    case "summary":
      state.summaries = message.texts;
      state.summaryErrors = message.errors || {};
      if (!state.summaries[state.summaryLang]) state.summaryLang = state.captionLang;
      if (!state.summaries[state.summaryLang]) state.summaryLang = Object.keys(state.summaries)[0];
      renderSummaryTabs();
      showSummary(state.summaryLang);
      el("summary-meta").textContent = `${state.entries.length}건 기준`;
      el("summarize").disabled = false;
      break;

    case "summary_failed":
      showSummaryNotice("회의 정리 실패: " + message.message);
      el("summarize").disabled = false;
      break;

    case "room_closed":
      stopRecording("회의가 종료되었습니다");
      setTalkState("disabled", "회의가 종료되었습니다");
      alert("Host 가 회의를 종료했습니다. 자막은 서버에서 삭제되었습니다.");
      if (state.socket) state.socket.close();
      break;

    case "error":
      if (message.fatal) showJoinError(message.message);
      else el("floor-status").textContent = message.message;
      break;
  }
}

// --- 회의 화면 --------------------------------------------------------

function enterMeeting(azure) {
  el("join-view").hidden = true;
  el("meeting-view").hidden = false;
  el("room-code").textContent = state.roomCode;
  setBadge(el("conn-badge"), true, "연결됨");
  applyAzureStatus(azure);
  el("host-controls").hidden = !state.me.is_host;
  applySummaryStatus(state.summary);

  el("transcript").innerHTML = "";
  state.entries.forEach(renderEntry);

  if (azure.state === "configured") {
    setTalkState("idle", "발언 준비됨");
  } else {
    setTalkState("disabled", azure.reason);
  }
}

function renderParticipants(list, floor) {
  el("participant-count").textContent = `참가자 ${list.length}명`;
  const container = el("participants");
  container.innerHTML = "";
  for (const person of list) {
    const item = document.createElement("li");
    item.textContent = person.name + (person.is_host ? " (Host)" : "");
    if (floor && floor.id === person.id) item.classList.add("speaking");
    container.appendChild(item);
  }

  const holdingByMe = floor && state.me && floor.id === state.me.id;
  if (el("talk-btn").disabled && state.azure && state.azure.state !== "configured") return;

  if (!floor) {
    if (!state.recording) setTalkState("idle", "발언 준비됨");
  } else if (holdingByMe) {
    // 내 발언 중 - 카운트다운이 라벨을 관리한다.
  } else {
    setTalkState("locked", `${floor.name} 님이 말하는 중`);
  }
}

// --- 회의 정리 (Host) --------------------------------------------------

function applySummaryStatus(status) {
  const button = el("summarize");
  const ok = status && status.state === "configured";
  button.disabled = !ok;
  button.title = ok ? "지금까지의 자막을 정리합니다" : (status ? status.reason : "");
}

function showSummaryNotice(text) {
  const body = el("summary-body");
  body.innerHTML = "";
  const node = document.createElement("p");
  node.className = "pending";
  node.textContent = text;
  body.appendChild(node);
  el("summary-panel").hidden = false;
}

// 언어 탭: 성공한 언어는 전환 버튼, 실패한 언어는 사유를 제목으로 단 비활성 버튼.
function renderSummaryTabs() {
  const tabs = el("summary-tabs");
  tabs.innerHTML = "";
  for (const lang of state.languages) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = lang.label;
    if (state.summaries[lang.code]) {
      button.classList.toggle("active", lang.code === state.summaryLang);
      button.addEventListener("click", () => showSummary(lang.code));
    } else {
      button.disabled = true;
      button.title = (state.summaryErrors && state.summaryErrors[lang.code]) || "";
    }
    tabs.appendChild(button);
  }
}

function showSummary(code) {
  state.summaryLang = code;
  for (const button of el("summary-tabs").children) {
    button.classList.toggle("active", button.textContent === labelOf(code));
  }
  renderSummary(state.summaries[code] || "");
}

function labelOf(code) {
  const found = state.languages.find((lang) => lang.code === code);
  return found ? found.label : code;
}

// Provider 가 돌려준 «## 제목 / - 항목» 텍스트를 제목·목록으로 그린다. HTML 은 만들지 않는다.
function renderSummary(text) {
  const body = el("summary-body");
  body.innerHTML = "";
  let list = null;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith("## ")) {
      list = null;
      const heading = document.createElement("h3");
      heading.textContent = line.slice(3).trim();
      body.appendChild(heading);
    } else if (line.startsWith("- ")) {
      if (!list) { list = document.createElement("ul"); body.appendChild(list); }
      const item = document.createElement("li");
      item.textContent = line.slice(2).trim();
      list.appendChild(item);
    } else {
      list = null;
      const para = document.createElement("p");
      para.textContent = line;
      body.appendChild(para);
    }
  }
  el("summary-panel").hidden = false;
}

function requestSummary() {
  if (state.entries.length === 0) {
    el("floor-status").textContent = "정리할 자막이 아직 없습니다.";
    return;
  }
  el("summarize").disabled = true;
  el("summary-tabs").innerHTML = "";
  el("summary-meta").textContent = "";
  showSummaryNotice(`자막 ${state.entries.length}건을 한국어·영어·베트남어로 정리하는 중… (수십 초 걸릴 수 있습니다)`);
  send({ type: "summarize" });
}

async function exportDocx() {
  const response = await fetch("/api/export/docx", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      room: state.roomCode,
      caption_lang: state.captionLang,
      entries: state.entries,
      summaries: state.summaries,
    }),
  });
  if (!response.ok) {
    el("floor-status").textContent = "DOCX 생성에 실패했습니다.";
    return;
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `meeting-${state.roomCode}.docx`;
  link.click();
  URL.revokeObjectURL(url);
}

function captionText(entry) {
  return entry.translations[state.captionLang] || entry.original;
}

function renderEntry(entry) {
  const node = document.createElement("article");
  node.className = "entry";

  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = `${entry.speaker} · ${entry.time_kst} KST`;

  const translated = document.createElement("div");
  translated.className = "translated";
  translated.textContent = captionText(entry);

  node.appendChild(meta);
  node.appendChild(translated);

  if (entry.original !== translated.textContent) {
    const original = document.createElement("div");
    original.className = "original";
    original.textContent = entry.original;
    node.appendChild(original);
  }

  const list = el("transcript");
  list.appendChild(node);
  list.scrollTop = list.scrollHeight;
}

function renderInterim(message) {
  const node = el("interim");
  node.hidden = false;
  node.textContent = `${message.speaker} · ${message.text}`;
}

function clearInterim() {
  const node = el("interim");
  node.hidden = true;
  node.textContent = "";
}

// --- 발언 버튼 --------------------------------------------------------

function setTalkState(mode, statusText) {
  const button = el("talk-btn");
  button.classList.remove("recording", "warning");
  button.disabled = mode === "disabled" || mode === "locked";
  button.setAttribute("aria-disabled", String(button.disabled));

  if (mode === "idle") button.textContent = "말하려면 누르기";
  else if (mode === "recording") button.classList.add("recording");
  else if (mode === "locked") button.textContent = statusText;
  else if (mode === "disabled") button.textContent = "발언 불가";

  el("floor-status").textContent = statusText || "";
}

function turnSummary(stats) {
  const kb = Math.round(stats.audio_bytes / 1024);
  if (stats.audio_bytes === 0) return "발언 종료 · 서버에 도착한 오디오 없음";
  if (stats.final === 0) return `발언 종료 · 오디오 ${kb}KB 전송됐지만 인식 0건 — 마이크 입력·말하는 언어를 확인하세요`;
  return `발언 종료 · 오디오 ${kb}KB · 인식 ${stats.final}건`;
}

function micStatusText() {
  const peak = state.micPeak;
  state.micPeak = 0;
  return peak > MIC_SILENCE_PEAK ? "마이크 입력 감지됨" : "마이크 입력 없음 — 장치·음소거를 확인하세요";
}

function startCountdown() {
  state.secondsLeft = state.limits.floor_limit_seconds;
  const button = el("talk-btn");
  const tick = () => {
    button.textContent = `말하는 중 · 남은 ${state.secondsLeft}초 (누르면 종료)`;
    if (state.secondsLeft < state.limits.floor_limit_seconds) {
      el("floor-status").textContent = micStatusText();
    }
    state.secondsLeft -= 1;
  };
  tick();
  state.countdownTimer = setInterval(tick, 1000);
}

function stopCountdown() {
  if (state.countdownTimer) clearInterval(state.countdownTimer);
  state.countdownTimer = null;
}

// --- 오디오 -----------------------------------------------------------

async function startRecording() {
  const context = new AudioContext({ sampleRate: SAMPLE_RATE });
  await context.audioWorklet.addModule("/static/pcm-worklet.js");
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  const source = context.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(context, "pcm-worklet");
  node.port.onmessage = (event) => {
    const samples = new Int16Array(event.data);
    for (let i = 0; i < samples.length; i += 1) {
      const level = Math.abs(samples[i]);
      if (level > state.micPeak) state.micPeak = level;
    }
    if (state.socket && state.socket.readyState === WebSocket.OPEN && state.recording) {
      state.socket.send(event.data);
    }
  };
  source.connect(node);
  state.audio = { context, stream, node, source };
}

function stopRecording(statusText) {
  stopCountdown();
  if (state.audio) {
    state.audio.node.port.onmessage = null;
    state.audio.source.disconnect();
    state.audio.node.disconnect();
    state.audio.stream.getTracks().forEach((track) => track.stop());
    state.audio.context.close();
    state.audio = null;
  }
  if (!state.recording) return;
  state.recording = false;
  setTalkState("idle", statusText || "발언 준비됨");
}

async function onTalkClick() {
  if (state.recording) {
    send({ type: "floor_release" });
    stopRecording("발언 종료");
    return;
  }
  try {
    await startRecording();
  } catch (error) {
    setTalkState("idle", "마이크를 사용할 수 없습니다: " + error.message);
    return;
  }
  state.recording = true;
  setTalkState("recording", "");
  startCountdown();
  send({ type: "floor_request" });
}

// --- Host 내보내기 ----------------------------------------------------

function download(filename, content, mime) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function exportTxt() {
  const lines = state.entries.map(
    (entry) => `[${entry.time_kst} KST] ${entry.speaker}\n  원문: ${entry.original}\n  번역: ${captionText(entry)}`
  );
  download(`meeting-${state.roomCode}.txt`, lines.join("\n\n"), "text/plain;charset=utf-8");
}

function exportJson() {
  download(
    `meeting-${state.roomCode}.json`,
    JSON.stringify({ room: state.roomCode, entries: state.entries }, null, 2),
    "application/json;charset=utf-8"
  );
}

// --- 초기화 -----------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  loadStatus().catch(() => showJoinError("서버 상태를 불러오지 못했습니다."));

  ["consent", "name", "room", "passcode"].forEach((id) =>
    el(id).addEventListener("input", refreshJoinButton)
  );
  el("consent").addEventListener("change", refreshJoinButton);

  el("join-btn").addEventListener("click", () => {
    showJoinError("");
    connect();
  });

  el("talk-btn").addEventListener("click", onTalkClick);
  el("export-txt").addEventListener("click", exportTxt);
  el("export-json").addEventListener("click", exportJson);
  el("export-docx").addEventListener("click", exportDocx);
  el("summarize").addEventListener("click", requestSummary);
  el("summary-close").addEventListener("click", () => { el("summary-panel").hidden = true; });
  el("end-room").addEventListener("click", () => {
    if (confirm("회의를 종료하고 모든 자막을 삭제할까요?")) send({ type: "end_room" });
  });
});
