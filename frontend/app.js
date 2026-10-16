"use strict";

// 외부(AWS·API) 문자열은 모두 textContent로만 화면에 넣는다.

const $ = (id) => document.getElementById(id);

class ApiError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

async function fetchJson(path) {
  let response;
  try {
    response = await fetch(path, { headers: { Accept: "application/json" } });
  } catch {
    throw new ApiError("NETWORK_ERROR", "서버에 연결할 수 없습니다. 서버가 실행 중인지 확인하세요.");
  }

  let body = null;
  try {
    body = await response.json();
  } catch {
    // 본문이 JSON이 아닐 수 있다.
  }

  if (!response.ok) {
    const detail = body && typeof body.detail === "object" ? body.detail : null;
    throw new ApiError(
      (detail && detail.code) || `HTTP_${response.status}`,
      (detail && detail.message) || `요청이 실패했습니다. (HTTP ${response.status})`
    );
  }
  if (body === null) {
    throw new ApiError("INVALID_RESPONSE", "서버 응답을 해석할 수 없습니다.");
  }
  return body;
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
  const node = $(id);
  node.className = `card-value ${kind}`;
  node.textContent = value;
}

/* ---------- 탭 ---------- */

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    document.querySelectorAll(".panel").forEach((p) => {
      p.hidden = p.id !== `panel-${tab.dataset.tab}`;
    });
  });
});

/* ---------- 계정 ---------- */

async function loadContext() {
  setStatus("context-status", "loading", "계정 정보를 조회하는 중...");
  $("context-info").hidden = true;
  try {
    const data = await fetchJson("/api/context");
    $("ctx-account").textContent = data.account_id;
    $("ctx-arn").textContent = data.caller_arn;
    $("ctx-region").textContent = data.region;
    $("context-info").hidden = false;
    setStatus("context-status", "hidden");
  } catch (err) {
    setStatus("context-status", "error", err.message, err.code);
  }
}

/* ---------- 목록 공통 ---------- */

async function loadList({ path, statusId, countId, emptyMessage, render, container }) {
  setStatus(statusId, "loading", "조회하는 중...");
  setCount(countId, "loading", "조회 중");
  container.hidden = true;
  try {
    const data = await fetchJson(path);
    const items = Array.isArray(data.items) ? data.items : [];
    setCount(countId, "", String(data.count));
    if (items.length === 0) {
      setStatus(statusId, "empty", emptyMessage);
      return;
    }
    render(items);
    container.hidden = false;
    setStatus(statusId, "hidden");
  } catch (err) {
    // 실패는 0개로 표시하지 않는다.
    setCount(countId, "error", "오류");
    setStatus(statusId, "error", err.message, err.code);
  }
}

/* ---------- EC2 ---------- */

function renderEc2(items) {
  const rows = items.map((item) => {
    const tr = document.createElement("tr");
    tr.append(
      el("td", item.name),
      el("td", item.instance_id, "mono"),
      stateCell(item.state),
      el("td", item.instance_type),
      el("td", item.private_ip ?? "-", "mono"),
      el("td", item.public_ip ?? "-", "mono")
    );
    return tr;
  });
  $("ec2-body").replaceChildren(...rows);
}

function stateCell(state) {
  const td = document.createElement("td");
  td.append(el("span", state ?? "-", `badge state-${state ?? "unknown"}`));
  return td;
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

async function refreshAll() {
  const button = $("refresh-btn");
  button.disabled = true;
  button.textContent = "조회 중...";
  try {
    await Promise.allSettled([
      loadContext(),
      loadList({
        path: "/api/ec2",
        statusId: "ec2-status",
        countId: "count-ec2",
        emptyMessage: "EC2 인스턴스가 없습니다.",
        render: renderEc2,
        container: $("ec2-table-wrap"),
      }),
      loadList({
        path: "/api/s3",
        statusId: "s3-status",
        countId: "count-s3",
        emptyMessage: "S3 버킷이 없습니다.",
        render: renderS3,
        container: $("s3-table-wrap"),
      }),
      loadList({
        path: "/api/security-groups",
        statusId: "sg-status",
        countId: "count-sg",
        emptyMessage: "보안 그룹이 없습니다.",
        render: renderSecurityGroups,
        container: $("sg-list"),
      }),
    ]);
    $("last-fetched").textContent = new Date().toLocaleString("ko-KR");
  } finally {
    button.disabled = false;
    button.textContent = "조회";
  }
}

$("refresh-btn").addEventListener("click", refreshAll);
refreshAll();
