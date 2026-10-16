"use strict";

// 외부(AWS·API) 문자열은 모두 textContent로만 화면에 넣는다.
// 앱 토큰은 이 변수(JS 메모리)에만 보관한다. URL·localStorage·로그에는 넣지 않는다.
let appToken = null;
// 로그아웃 시 진행 중인 요청을 취소해 지운 화면에 결과가 다시 그려지지 않게 한다.
let sessionController = null;

// 제어용 메모리 상태
let currentContext = null; // 마지막으로 확인한 계정·리전
let lastEc2Items = [];
// 응답을 받지 못한 요청: instance_id -> {action, request_id}. 재전송 시 같은 키를 쓴다.
const pendingRequests = new Map();
// 응답 대기 중인 instance_id (버튼 중복 클릭 방지)
const inFlight = new Set();
// 확인창에 띄운 대상
let confirmTarget = null;

const ACTION_LABEL = { start: "시작", stop: "중지" };
const REQUIRED_STATE = { start: "stopped", stop: "running" };

const $ = (id) => document.getElementById(id);

class ApiError extends Error {
  constructor(code, message, status) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

async function fetchJson(path, { method = "GET", body } = {}) {
  if (!appToken) throw new ApiError("APP_AUTH_REQUIRED", "앱 토큰을 입력하세요.", 401);
  const signal = sessionController.signal;
  const headers = { Accept: "application/json", Authorization: `Bearer ${appToken}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      cache: "no-store",
    });
  } catch {
    if (signal.aborted) throw new ApiError("ABORTED", "요청이 취소되었습니다.");
    throw new ApiError("NETWORK_ERROR", "서버에 연결할 수 없습니다. 서버가 실행 중인지 확인하세요.");
  }

  let data = null;
  try {
    data = await response.json();
  } catch {
    // 본문이 JSON이 아닐 수 있다.
  }
  if (signal.aborted) throw new ApiError("ABORTED", "요청이 취소되었습니다.");

  if (!response.ok) {
    const detail = data && typeof data.detail === "object" ? data.detail : null;
    throw new ApiError(
      (detail && detail.code) || `HTTP_${response.status}`,
      (detail && detail.message) || `요청이 실패했습니다. (HTTP ${response.status})`,
      response.status
    );
  }
  if (data === null) {
    throw new ApiError("INVALID_RESPONSE", "서버 응답을 해석할 수 없습니다.", response.status);
  }
  return { status: response.status, data };
}

async function getJson(path) {
  return (await fetchJson(path)).data;
}

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function setStatus(id, kind, message, code) {
  const box = $(id);
  box.replaceChildren();
  box.className = `status ${kind}`;
  if (kind === "hidden") return;
  if (code) box.append(el("strong", code), " ");
  box.append(message);
}

function setCount(id, kind, value) {
  if (!id) return;
  const node = $(id);
  node.className = `card-value ${kind}`;
  node.textContent = value;
}

/* ---------- 탭 ---------- */

function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => {
    p.hidden = p.id !== `panel-${name}`;
  });
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => showTab(tab.dataset.tab));
});

/* ---------- 계정 ---------- */

function renderContext(data) {
  currentContext = { account_id: data.account_id, region: data.region };
  $("ctx-account").textContent = data.account_id;
  $("ctx-arn").textContent = data.caller_arn;
  $("ctx-region").textContent = data.region;
  $("context-info").hidden = false;
  setStatus("context-status", "hidden");
}

async function loadContext() {
  setStatus("context-status", "loading", "계정 정보를 조회하는 중...");
  $("context-info").hidden = true;
  try {
    renderContext(await getJson("/api/context"));
  } catch (err) {
    if (handleSessionError(err)) return;
    currentContext = null;
    setStatus("context-status", "error", err.message, err.code);
  }
}

/* ---------- 목록 공통 ---------- */

// 같은 데이터를 여러 곳(예: EC2 탭과 제어 탭)에 그릴 수 있도록 상태·컨테이너를 배열로 둔다.
const LISTS = {
  ec2: {
    path: "/api/ec2",
    statusIds: ["ec2-status", "control-status"],
    containerIds: ["ec2-table-wrap", "control-table-wrap"],
    countId: "count-ec2",
    emptyMessage: "EC2 인스턴스가 없습니다.",
    render: renderEc2,
  },
  s3: {
    path: "/api/s3",
    statusIds: ["s3-status"],
    containerIds: ["s3-table-wrap"],
    countId: "count-s3",
    emptyMessage: "S3 버킷이 없습니다.",
    render: renderS3,
  },
  sg: {
    path: "/api/security-groups",
    statusIds: ["sg-status"],
    containerIds: ["sg-list"],
    countId: "count-sg",
    emptyMessage: "보안 그룹이 없습니다.",
    render: renderSecurityGroups,
  },
  actions: {
    path: "/api/actions",
    statusIds: ["actions-status"],
    containerIds: ["actions-table-wrap"],
    countId: null,
    emptyMessage: "제어 이력이 없습니다.",
    render: renderActions,
  },
};

async function loadList({ path, statusIds, containerIds, countId, emptyMessage, render }) {
  const setAll = (kind, message, code) => statusIds.forEach((id) => setStatus(id, kind, message, code));
  const showContainers = (visible) => containerIds.forEach((id) => ($(id).hidden = !visible));

  setAll("loading", "조회하는 중...");
  setCount(countId, "loading", "조회 중");
  showContainers(false);
  try {
    const data = await getJson(path);
    const items = Array.isArray(data.items) ? data.items : [];
    setCount(countId, "", String(data.count));
    render(items);
    if (items.length === 0) {
      setAll("empty", emptyMessage);
      return;
    }
    showContainers(true);
    setAll("hidden");
  } catch (err) {
    if (handleSessionError(err)) return;
    // 실패는 0개로 표시하지 않는다.
    setCount(countId, "error", "오류");
    setAll("error", err.message, err.code);
  }
}

/* ---------- EC2 ---------- */

function renderEc2(items) {
  lastEc2Items = items;
  const rows = items.map((item) => {
    const tr = document.createElement("tr");
    tr.append(
      el("td", item.name),
      el("td", item.instance_id, "mono"),
      stateCell(item.state),
      el("td", item.instance_type),
      el("td", item.private_ip ?? "-", "mono"),
      el("td", item.public_ip ?? "-", "mono"),
      el("td", item.controllable ? "허용" : "-", item.controllable ? "allowed" : "muted")
    );
    return tr;
  });
  $("ec2-body").replaceChildren(...rows);
  renderControlTable();
}

function stateCell(state) {
  const td = document.createElement("td");
  td.append(el("span", state ?? "-", `badge state-${state ?? "unknown"}`));
  return td;
}

/* ---------- 제어 ---------- */

function renderControlTable() {
  const rows = lastEc2Items.map((item) => {
    const tr = document.createElement("tr");
    const actionsCell = el("td");
    const box = el("div", null, "row-actions");
    const pending = pendingRequests.get(item.instance_id);
    const busy = inFlight.has(item.instance_id);

    if (pending) {
      // 응답을 받지 못한 요청은 같은 키로만 재전송한다.
      const resend = el("button", `${ACTION_LABEL[pending.action]} 재전송(같은 키)`, "secondary");
      resend.type = "button";
      resend.disabled = busy;
      resend.addEventListener("click", () => sendAction(item, pending.action, pending.request_id));
      box.append(resend);
    } else if (!item.controllable) {
      box.append(el("span", "허용 대상 아님", "muted"));
    } else {
      for (const action of ["start", "stop"]) {
        const button = el("button", ACTION_LABEL[action], action === "stop" ? "danger" : "");
        button.type = "button";
        // stopped에서만 시작, running에서만 중지. 전환 상태·응답 대기 중에는 모두 비활성화.
        button.disabled = busy || item.state !== REQUIRED_STATE[action];
        button.addEventListener("click", () => openConfirm(item, action));
        box.append(button);
      }
    }
    actionsCell.append(box);
    tr.append(el("td", item.name), el("td", item.instance_id, "mono"), stateCell(item.state), actionsCell);
    return tr;
  });
  $("control-body").replaceChildren(...rows);
}

function openConfirm(item, action) {
  if (!currentContext) {
    setStatus("control-result", "error", "계정 정보를 확인하지 못했습니다. 상단의 조회를 먼저 실행하세요.");
    return;
  }
  confirmTarget = { item, action };
  $("confirm-account").textContent = currentContext.account_id;
  $("confirm-region").textContent = currentContext.region;
  $("confirm-name").textContent = item.name;
  $("confirm-id").textContent = item.instance_id;
  $("confirm-action").textContent = `${ACTION_LABEL[action]} (${action})`;
  $("confirm-dialog").showModal();
}

function closeConfirm() {
  confirmTarget = null;
  $("confirm-dialog").close();
}

// 취소하면 POST를 보내지 않는다.
$("confirm-cancel").addEventListener("click", closeConfirm);
$("confirm-dialog").addEventListener("cancel", () => {
  confirmTarget = null;
});

$("confirm-ok").addEventListener("click", () => {
  const target = confirmTarget;
  closeConfirm();
  if (!target) return;
  // 승인 한 번에 키 하나. 응답이 유실되면 이 키를 그대로 유지한다.
  const requestId = crypto.randomUUID();
  sendAction(target.item, target.action, requestId);
});

async function sendAction(item, action, requestId) {
  const id = item.instance_id;
  if (inFlight.has(id)) return;
  pendingRequests.set(id, { action, request_id: requestId });
  inFlight.add(id);
  renderControlTable();
  setStatus("control-result", "loading", `${item.name} ${ACTION_LABEL[action]} 요청을 보내는 중...`);

  try {
    const { status, data } = await fetchJson(`/api/ec2/${encodeURIComponent(id)}/actions`, {
      method: "POST",
      body: { action, request_id: requestId },
    });
    pendingRequests.delete(id);
    const prefix = status === 202 ? "AWS가 요청을 접수했습니다." : "같은 요청 키의 기존 결과입니다.";
    const kind = data.status === "ACCEPTED" ? "success" : "warning";
    setStatus("control-result", kind, `${prefix} 상태: ${data.status}${data.message ? ` · ${data.message}` : ""}`);
    // 이력만 한 번 다시 읽는다. EC2 상태는 새로고침으로 확인한다(자동 폴링 없음).
    loadList(LISTS.actions);
  } catch (err) {
    if (handleSessionError(err)) return;
    if (err.status && err.status < 500) {
      // 서버가 명확히 판정한 요청(4xx)은 키를 정리한다.
      pendingRequests.delete(id);
      setStatus("control-result", "error", err.message, err.code);
    } else {
      // 응답 유실·서버 오류: 같은 키를 보관하고 자동 재요청하지 않는다.
      setStatus(
        "control-result",
        "warning",
        `${err.message} 결과를 확인하려면 이력을 새로고침하거나 '재전송(같은 키)'을 누르세요.`,
        err.code
      );
    }
    loadList(LISTS.actions);
  } finally {
    inFlight.delete(id);
    renderControlTable();
  }
}

function formatUtc(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("ko-KR");
}

function renderActions(items) {
  const rows = items.map((item) => {
    const tr = document.createElement("tr");
    const statusCell = el("td");
    statusCell.append(el("span", item.status, `badge status-${item.status}`));
    tr.append(
      el("td", formatUtc(item.created_at)),
      el("td", item.instance_id, "mono"),
      el("td", ACTION_LABEL[item.action] ?? item.action),
      statusCell,
      el("td", item.message ?? "-", "wrap")
    );
    tr.title = `request_id: ${item.request_id}\naws_request_id: ${item.aws_request_id ?? "-"}`;
    return tr;
  });
  $("actions-body").replaceChildren(...rows);
}

/* ---------- S3 ---------- */

function renderS3(items) {
  const rows = items.map((item) => {
    const tr = document.createElement("tr");
    tr.append(el("td", item.name, "mono"));
    return tr;
  });
  $("s3-body").replaceChildren(...rows);
}

/* ---------- 보안 그룹 ---------- */

function protocolLabel(protocol) {
  if (protocol === "-1") return "전체";
  return String(protocol).toUpperCase();
}

function portLabel(perm) {
  if (perm.IpProtocol === "-1") return "전체";
  if (perm.FromPort === undefined || perm.FromPort === null) return "-";
  if (perm.FromPort === -1) return "전체";
  return perm.FromPort === perm.ToPort ? String(perm.FromPort) : `${perm.FromPort}-${perm.ToPort}`;
}

function sourcesOf(perm) {
  const sources = [];
  (perm.IpRanges || []).forEach((r) => sources.push(r.CidrIp));
  (perm.Ipv6Ranges || []).forEach((r) => sources.push(r.CidrIpv6));
  (perm.UserIdGroupPairs || []).forEach((p) => sources.push(p.GroupId));
  (perm.PrefixListIds || []).forEach((p) => sources.push(p.PrefixListId));
  return sources;
}

function renderSecurityGroups(items) {
  const blocks = items.map((group) => {
    const block = el("article", null, "sg-block");
    const title = el("h3");
    title.append(el("span", group.GroupName), " ", el("span", group.GroupId, "mono muted"));
    block.append(title);

    const perms = group.IpPermissions || [];
    if (perms.length === 0) {
      block.append(el("p", "인바운드 규칙이 없습니다.", "muted"));
      return block;
    }

    const wrap = el("div", null, "table-wrap");
    const table = document.createElement("table");
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    headRow.append(el("th", "프로토콜"), el("th", "포트"), el("th", "소스"));
    thead.append(headRow);
    const tbody = document.createElement("tbody");
    perms.forEach((perm) => {
      const tr = document.createElement("tr");
      const sources = sourcesOf(perm);
      const srcCell = document.createElement("td");
      if (sources.length === 0) {
        srcCell.textContent = "-";
      } else {
        sources.forEach((s) => srcCell.append(el("div", s, "mono")));
      }
      tr.append(el("td", protocolLabel(perm.IpProtocol)), el("td", portLabel(perm), "mono"), srcCell);
      tbody.append(tr);
    });
    table.append(thead, tbody);
    wrap.append(table);
    block.append(wrap);
    return block;
  });
  $("sg-list").replaceChildren(...blocks);
}

/* ---------- 조회 ---------- */

async function loadLists(keys = Object.keys(LISTS)) {
  await Promise.allSettled(keys.map((key) => loadList(LISTS[key])));
  if (appToken) $("last-fetched").textContent = new Date().toLocaleString("ko-KR");
}

async function withButton(button, task) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "조회 중...";
  try {
    await task();
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

$("refresh-btn").addEventListener("click", (e) =>
  withButton(e.currentTarget, () => Promise.allSettled([loadContext(), loadLists()]))
);
// 제어 탭의 수동 새로고침: 계정 확인 + EC2 현재 상태 + 이력
$("control-refresh-btn").addEventListener("click", (e) =>
  withButton(e.currentTarget, () => Promise.allSettled([loadContext(), loadLists(["ec2", "actions"])]))
);

/* ---------- 연결·로그아웃 ---------- */

function clearData() {
  currentContext = null;
  lastEc2Items = [];
  pendingRequests.clear();
  inFlight.clear();
  confirmTarget = null;
  if ($("confirm-dialog").open) $("confirm-dialog").close();

  $("ctx-account").textContent = "";
  $("ctx-arn").textContent = "";
  $("ctx-region").textContent = "";
  $("context-info").hidden = true;
  setStatus("context-status", "hidden");
  setStatus("control-result", "hidden");
  ["ec2-body", "s3-body", "sg-list", "control-body", "actions-body"].forEach((id) => $(id).replaceChildren());
  Object.values(LISTS).forEach(({ statusIds, containerIds, countId }) => {
    statusIds.forEach((id) => setStatus(id, "hidden"));
    containerIds.forEach((id) => ($(id).hidden = true));
    setCount(countId, "", "-");
  });
  $("last-fetched").textContent = "-";
}

function endSession() {
  appToken = null;
  if (sessionController) sessionController.abort();
  sessionController = null;
  clearData();
  $("app-view").hidden = true;
  $("session-actions").hidden = true;
  $("login-view").hidden = false;
  $("token-input").value = "";
  showTab("dashboard");
}

// 401이면 세션을 끝내고 입력 화면으로 돌아간다. 취소된 요청은 조용히 무시한다.
function handleSessionError(err) {
  if (err.code === "ABORTED") return true;
  if (err.status === 401 && appToken) {
    endSession();
    setStatus("login-status", "error", "토큰이 거부되었습니다. 다시 입력하세요.", err.code);
    return true;
  }
  return false;
}

async function connect(event) {
  event.preventDefault();
  const input = $("token-input");
  const token = input.value.trim();
  input.value = "";
  if (!token) {
    setStatus("login-status", "error", "토큰을 입력하세요.");
    return;
  }

  appToken = token;
  sessionController = new AbortController();
  const button = $("connect-btn");
  button.disabled = true;
  setStatus("login-status", "loading", "연결하는 중...");
  try {
    const context = await getJson("/api/context");
    setStatus("login-status", "hidden");
    $("login-view").hidden = true;
    $("app-view").hidden = false;
    $("session-actions").hidden = false;
    renderContext(context);
    await loadLists();
  } catch (err) {
    if (err.code === "ABORTED") return;
    appToken = null;
    sessionController = null;
    setStatus("login-status", "error", err.message, err.code);
  } finally {
    button.disabled = false;
  }
}

$("login-form").addEventListener("submit", connect);
$("logout-btn").addEventListener("click", () => {
  endSession();
  setStatus("login-status", "hidden");
});
