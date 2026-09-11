/* Talkio frontend app v2 */
(() => {
  const $ = (id) => document.getElementById(id);

  // Backend base URL. Priority: previously-saved choice > config.js > same origin.
  // Same origin ("") is correct when the backend itself is serving this frontend;
  // a saved/explicit URL is needed when this frontend runs standalone on another PC.
  function stripSlash(u) { return (u || "").trim().replace(/\/+$/, ""); }
  let API = stripSlash(localStorage.getItem("talkio_api")) || stripSlash(window.TALKIO_API) || "";

  // Turns a backend-relative path (e.g. "/files/xyz.png") into a full URL
  // pointing at the configured backend. Absolute URLs and data: URIs pass through.
  function mediaUrl(path) {
    if (!path || /^(https?:|data:)/i.test(path)) return path;
    return API + path;
  }

  async function ensureBackend() {
    // If we're already same-origin (API === "" and this page IS the backend), skip the check.
    try {
      const r = await fetch(API + "/api/config");
      if (r.ok) return true;
    } catch {}
    // Unreachable — ask once and remember the answer on this PC.
    const guess = API || "http://";
    const entered = window.prompt(
      "Can't reach the Talkio backend.\n\nEnter the backend server address, e.g.\n" +
      "http://192.168.1.42:8000  (same WiFi)  or  https://your-tunnel.trycloudflare.com (remote)",
      guess
    );
    if (!entered) return false;
    API = stripSlash(entered);
    localStorage.setItem("talkio_api", API);
    try {
      const r2 = await fetch(API + "/api/config");
      return r2.ok;
    } catch {
      return false;
    }
  }

  let token = localStorage.getItem("talkio_token") || "";
  let me = null, ws = null, config = null;
  let conversations = [], activeConv = null, onlineIds = [];
  const typers = new Map();   // user_id -> {name, timeout}

  function renderTyping() {
    const names = [...typers.values()].map((t) => t.name);
    if (!names.length) { $("typing").classList.add("hidden"); return; }
    $("typing").innerHTML = `<span class="typing-dots"><i></i><i></i><i></i></span> ` +
      esc(names.join(", ")) + (names.length > 1 ? " are typing…" : " is typing…");
    $("typing").classList.remove("hidden");
  }
  function setTyper(uid, name) {
    clearTimeout(typers.get(uid)?.timeout);
    typers.set(uid, { name, timeout: setTimeout(() => { typers.delete(uid); renderTyping(); }, 6000) });
    renderTyping();
  }
  function clearTyper(uid) {
    clearTimeout(typers.get(uid)?.timeout);
    typers.delete(uid);
    renderTyping();
  }

  window.toast = (text, kind = "") => {
    const t = document.createElement("div");
    t.className = "toast " + kind;
    t.textContent = text;
    $("toasts").appendChild(t);
    setTimeout(() => t.remove(), 5000);
  };

  function notify(title, body) {
    if (localStorage.getItem("talkio_notif") !== "on") return;
    if (Notification?.permission === "granted" && document.hidden)
      new Notification(title, { body: body.slice(0, 120) });
  }
  function updateTitle() {
    const n = conversations.reduce((a, c) => a + (c.unread || 0), 0);
    document.title = n ? `(${n}) Talkio` : "Talkio";
  }

  // ---- local avatar generator (works offline / on LAN with no internet) ----
  const AVATAR_COLORS = ["#e0745e", "#d98e4a", "#5aa77f", "#5f87c7", "#a06cc4", "#c75f8b"];
  function localAvatar(name) {
    const initials = (name || "?").trim().split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
    let h = 0; for (const ch of name || "") h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    const bg = AVATAR_COLORS[h % AVATAR_COLORS.length];
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96">
      <rect width="96" height="96" fill="${bg}"/>
      <text x="48" y="60" font-family="sans-serif" font-size="38" font-weight="700"
            fill="#fff" text-anchor="middle">${initials}</text></svg>`;
    return "data:image/svg+xml," + encodeURIComponent(svg);
  }
  const avatarFor = (name, url) => url ? mediaUrl(url) : localAvatar(name);
  window.talkioAvatar = localAvatar; // used by calls.js

  const fmtTime = (ts) => {
    const d = new Date(ts * 1000), now = new Date();
    if (d.toDateString() === now.toDateString())
      return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const yest = new Date(now - 864e5);
    if (d.toDateString() === yest.toDateString()) return "Yesterday";
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  };
  const fmtSize = (n) => n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

  async function api(path, opts = {}) {
    const res = await fetch(API + path, {
      ...opts,
      headers: {
        ...(opts.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
        Authorization: "Bearer " + token, ...(opts.headers || {}),
      },
    });
    if (res.status === 401) { logout(); throw new Error("unauthorized"); }
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    return res.json();
  }

  // ============================== auth ==============================

  async function boot() {
    const ok = await ensureBackend();
    if (!ok) {
      $("login-error").textContent =
        "Cannot reach the Talkio backend. Reload the page to try entering the address again.";
      return;
    }
    config = await fetch(API + "/api/config").then((r) => r.json());
    if (config.dev_mode) $("dev-login").classList.remove("hidden");
    if (config.google_client_id && window.google?.accounts) {
      google.accounts.id.initialize({
        client_id: config.google_client_id,
        callback: async (resp) => loginWith("/api/auth/google", { id_token: resp.credential }),
      });
      google.accounts.id.renderButton($("google-btn"), { theme: "outline", size: "large", shape: "pill", width: 300 });
    } else if (!config.dev_mode) {
      $("login-error").textContent = "GOOGLE_CLIENT_ID is not configured on the server.";
    }
    if (token) {
      try { me = await api("/api/me"); enterApp(); } catch { /* stay on login */ }
    }
  }

  async function loginWith(path, body) {
    try {
      const res = await fetch(API + path, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error((await res.json()).detail || "Login failed");
      const data = await res.json();
      token = data.token; me = data.user;
      localStorage.setItem("talkio_token", token);
      enterApp();
    } catch (e) { $("login-error").textContent = e.message; }
  }

  function logout() {
    localStorage.removeItem("talkio_token");
    location.reload();
  }

  $("dev-login-btn").addEventListener("click", () =>
    loginWith("/api/auth/dev", { email: $("dev-email").value.trim(), name: $("dev-name").value.trim() }));

  // ============================== app shell ==============================

  async function enterApp() {
    $("login-screen").classList.add("hidden");
    $("app").classList.remove("hidden");
    $("me-name").textContent = me.name;
    $("me-email").textContent = me.email;
    $("me-avatar").src = avatarFor(me.name, me.avatar);
    connectWS();
    await loadConversations();
  }

  function connectWS() {
    // Derive ws(s):// host from API if it's set (cross-origin case), else from this page.
    let wsHost, wsProto;
    if (API) {
      const u = new URL(API);
      wsHost = u.host;
      wsProto = u.protocol === "https:" ? "wss" : "ws";
    } else {
      wsHost = location.host;
      wsProto = location.protocol === "https:" ? "wss" : "ws";
    }
    ws = new WebSocket(`${wsProto}://${wsHost}/ws?token=${token}`);
    window.Calls.init((payload) => ws.send(JSON.stringify(payload)));
    ws.onopen = () => $("ws-status").classList.add("hidden");
    ws.onmessage = (ev) => handleWS(JSON.parse(ev.data));
    ws.onclose = () => { $("ws-status").classList.remove("hidden"); setTimeout(connectWS, 2000); };
  }

  function handleWS(msg) {
    switch (msg.type) {
      case "message":
        if (activeConv && msg.conversation_id === activeConv.id) {
          clearTyper(msg.sender_id);
          appendMessage(msg);
          api(`/api/conversations/${activeConv.id}/read`, { method: "POST" }).catch(() => {});
          if (msg.kind !== "text") loadShared();
        }
        if (msg.sender_id !== me.id && (!activeConv || msg.conversation_id !== activeConv.id || document.hidden))
          notify(msg.sender_name, msg.kind === "text" ? msg.content : "📎 sent a file");
        loadConversations();
        break;
      case "read_update":
        if (activeConv && msg.conversation_id === activeConv.id && msg.user_id !== me.id) {
          const peer = activeConv.members.find((m) => m.id === msg.user_id);
          if (peer) peer.last_read_at = msg.read_at;
          document.querySelectorAll(".msg.out .ticks").forEach((el) => {
            if (+el.dataset.ts <= msg.read_at) { el.textContent = "✓✓"; el.classList.add("read"); }
          });
        }
        break;
      case "agent_request":
        window.toast(`🔑 ${msg.requester_name} requested access to ${msg.agent_name} — open 🤖 Agents to approve`, "info");
        notify("Agent access request", `${msg.requester_name} → ${msg.agent_name}`);
        break;
      case "agent_request_resolved":
        window.toast(msg.approved ? `✅ Access to ${msg.agent_name} approved — say hi!`
                                  : `❌ Access to ${msg.agent_name} was declined`, msg.approved ? "ok" : "err");
        break;
      case "reaction": {
        const el = document.querySelector(`[data-mid="${msg.message_id}"] .reactions`);
        if (el) renderReactions(el, msg.message_id, msg.reactions);
        break;
      }
      case "message_edit": {
        const el = document.querySelector(`[data-mid="${msg.message_id}"] .msg-text`);
        if (el) { el.textContent = msg.content; el.insertAdjacentHTML("beforeend", '<i class="edited"> (edited)</i>'); }
        break;
      }
      case "message_delete": {
        const el = document.querySelector(`[data-mid="${msg.message_id}"]`);
        if (el) { el.className = "msg deleted"; el.innerHTML = "<i>Message deleted</i>"; }
        loadConversations();
        break;
      }
      case "typing":
        if (activeConv && msg.conversation_id === activeConv.id && msg.user_id !== me.id)
          setTyper(msg.user_id, msg.name);
        break;
      case "conversation_update":
        loadConversations().then(() => {
          if (activeConv && activeConv.id === msg.conversation_id) {
            const fresh = conversations.find((c) => c.id === activeConv.id);
            if (fresh) { activeConv = fresh; renderChatHeader(); renderDetails(); }
          }
        });
        break;
      case "conversation_removed":
        if (activeConv && activeConv.id === msg.conversation_id) {
          activeConv = null;
          $("chat-view").classList.add("hidden");
          $("chat-empty").classList.remove("hidden");
          $("details").classList.add("hidden");
        }
        loadConversations();
        break;
      case "presence":
        onlineIds = msg.online;
        renderConvList();
        if (activeConv) renderChatHeader();
        break;
      case "conversation_new":
        loadConversations();
        break;
      case "call-offer": window.Calls.onOffer(msg); break;
      case "call-answer": window.Calls.onAnswer(msg); break;
      case "call-ice": window.Calls.onIce(msg); break;
      case "call-end": case "call-decline": window.Calls.onEnd(); break;
    }
  }

  // ============================== conversations ==============================

  async function loadConversations() {
    conversations = await api("/api/conversations");
    renderConvList();
    updateTitle();
  }

  function convIcon(c) {
    if (c.type === "group" || c.type === "channel") {
      if (c.image) return `<img class="avatar" src="${mediaUrl(c.image)}">`;
      return `<div class="conv-icon">${c.type === "group" ? "👥" : "#️⃣"}</div>`;
    }
    const peer = c.peer || {};
    const online = peer.id && onlineIds.includes(peer.id);
    return `<div class="avatar-wrap"><img class="avatar" src="${avatarFor(c.name, peer.avatar)}">
            ${online || peer.is_bot ? '<span class="dot"></span>' : ""}</div>`;
  }

  function renderConvList() {
    const q = $("search").value.trim().toLowerCase();
    $("conv-list").innerHTML = conversations
      .filter((c) => !q || (c.name || "").toLowerCase().includes(q))
      .map((c) => `
        <div class="conv-item ${activeConv?.id === c.id ? "active" : ""}" data-id="${c.id}">
          ${convIcon(c)}
          <div class="conv-body">
            <b>${esc(displayName(c))}</b>
            <div class="preview">${esc(c.last_message || "No messages yet")}</div>
          </div>
          <div class="meta">
            <span class="time">${c.last_at ? fmtTime(c.last_at) : ""}</span>
            ${c.unread ? `<span class="badge">${c.unread > 99 ? "99+" : c.unread}</span>` : ""}
          </div>
        </div>`).join("") || `<p class="muted">No conversations — hit + to start one.</p>`;
    document.querySelectorAll(".conv-item").forEach((el) =>
      el.addEventListener("click", () => openConversation(el.dataset.id)));
  }

  const displayName = (c) => c.type === "channel" ? "# " + c.name : c.name || "Group";

  async function openConversation(cid) {
    activeConv = conversations.find((c) => c.id === cid);
    if (!activeConv) return;
    $("chat-empty").classList.add("hidden");
    $("chat-view").classList.remove("hidden");
    typers.forEach((t) => clearTimeout(t.timeout)); typers.clear(); renderTyping();
    lastDay = ""; clearReply();
    document.body.classList.add("mobile-chat");
    renderChatHeader();
    renderDetails();
    loadShared();
    const msgs = await api(`/api/conversations/${cid}/messages`);
    $("messages").innerHTML = "";
    msgs.forEach(appendMessage);
    await api(`/api/conversations/${cid}/read`, { method: "POST" }).catch(() => {});
    await loadConversations();
  }

  function renderChatHeader() {
    const c = activeConv;
    $("chat-name").textContent = displayName(c);
    const isDM = c.type === "dm";
    const peer = c.peer || {};
    const online = isDM && (onlineIds.includes(peer.id) || peer.is_bot);
    $("chat-avatar").src = isDM ? avatarFor(c.name, peer.avatar) : (mediaUrl(c.image) || avatarFor(displayName(c)));
    $("chat-dot").classList.toggle("hidden", !online);
    const lastSeen = (ts) => {
      if (!ts) return "Offline";
      const s2 = Math.floor(Date.now() / 1000 - ts);
      if (s2 < 60) return "Last seen just now";
      if (s2 < 3600) return `Last seen ${Math.floor(s2 / 60)}m ago`;
      if (s2 < 86400) return `Last seen ${Math.floor(s2 / 3600)}h ago`;
      return "Last seen " + new Date(ts * 1000).toLocaleDateString();
    };
    $("chat-status").textContent = isDM
      ? (peer.is_bot ? "AI Assistant · always on" : online ? "Online" : lastSeen(peer.last_seen_at))
      : `${c.members.length} members`;
    $("chat-status").className = "status" + (online ? " online" : "");
    const callable = isDM && !peer.is_bot;
    $("voice-call-btn").style.display = callable ? "" : "none";
    $("video-call-btn").style.display = callable ? "" : "none";
  }

  function renderDetails() {
    const c = activeConv;
    $("details").classList.remove("hidden");
    const isDM = c.type === "dm";
    const peer = c.peer || {};
    $("detail-avatar").src = isDM ? avatarFor(c.name, peer.avatar) : (mediaUrl(c.image) || avatarFor(displayName(c)));
    $("detail-name").textContent = displayName(c);
    $("detail-sub").textContent = isDM
      ? (peer.is_bot ? (peer.slug === "nova" ? `${config.bot_name} · your in-app AI assistant`
                        : `AI Agent · owner: ${peer.agent_owner_name || "?"}`)
         : peer.email || "")
      : (c.description || (c.type === "group" ? "Group" : "Channel"));
    $("detail-members").classList.toggle("hidden", isDM);
    if (!isDM) renderGroupSettings(c);
  }

  function renderGroupSettings(c) {
    const admin = c.my_role === "admin";
    const el = $("member-list");
    el.innerHTML = `
      ${admin ? `
        <div class="gs-block">
          <input id="gs-name" type="text" value="${esc(c.name)}" placeholder="Group name">
          <input id="gs-desc" type="text" value="${esc(c.description || "")}" placeholder="Description">
          <div class="gs-row">
            <button id="gs-save" class="btn-primary sm">Save</button>
            <button id="gs-image" class="btn-ghost sm">🖼 Change image</button>
            <input id="gs-image-input" type="file" accept="image/*" class="hidden">
          </div>
        </div>` : ""}
      <div class="gs-members">
        ${c.members.map((m) => {
          const isAgent = m.is_bot && m.slug !== "nova";
          const canRemove = admin || (isAgent && m.agent_owner_id === me.id);
          return `
          <div class="member-row">
            <img class="avatar" src="${avatarFor(m.name, m.avatar)}">
            <div class="member-info">
              <span>${esc(m.name)}${m.is_bot ? " 🤖" : ""}</span>
              <small>${m.role === "admin" ? "⭐ Admin" : isAgent ? "@" + esc(m.slug) + " · " + esc(m.agent_owner_name || "") : m.is_bot ? "@nova" : esc(m.email || "")}</small>
            </div>
            ${onlineIds.includes(m.id) || m.is_bot ? '<span class="mini-dot"></span>' : ""}
            ${admin && !m.is_bot && m.id !== me.id ? `<button class="gs-role mini-btn" data-uid="${m.id}" data-role="${m.role === "admin" ? "member" : "admin"}" title="${m.role === "admin" ? "Remove admin" : "Make admin"}">${m.role === "admin" ? "⭐→" : "⭐"}</button>` : ""}
            ${canRemove && m.id !== me.id ? `<button class="gs-remove mini-btn" data-uid="${m.id}" title="Remove from group">✕</button>` : ""}
          </div>`;
        }).join("")}
      </div>
      <button id="gs-add" class="btn-ghost sm">➕ Add members / agents</button>
      <div id="gs-add-list" class="gs-add-list hidden"></div>
      <div class="gs-danger">
        <button id="gs-leave" class="mini-btn warn">🚪 Leave group</button>
        ${admin ? '<button id="gs-delete" class="mini-btn danger">🗑 Delete group</button>' : ""}
      </div>`;

    $("gs-save")?.addEventListener("click", async () => {
      try {
        await api("/api/conversations/" + c.id, { method: "PUT",
          body: JSON.stringify({ name: $("gs-name").value, description: $("gs-desc").value }) });
      } catch (e) { alert(e.message); }
    });
    $("gs-image")?.addEventListener("click", () => $("gs-image-input").click());
    $("gs-image-input")?.addEventListener("change", async () => {
      const f = $("gs-image-input").files[0];
      if (!f) return;
      const fd = new FormData(); fd.append("file", f);
      try { await api(`/api/conversations/${c.id}/image`, { method: "POST", body: fd }); }
      catch (e) { alert(e.message); }
    });
    el.querySelectorAll(".gs-role").forEach((b) => b.addEventListener("click", async () => {
      try {
        await api(`/api/conversations/${c.id}/members/${b.dataset.uid}/role`,
          { method: "PUT", body: JSON.stringify({ role: b.dataset.role }) });
      } catch (e) { alert(e.message); }
    }));
    el.querySelectorAll(".gs-remove").forEach((b) => b.addEventListener("click", async () => {
      try { await api(`/api/conversations/${c.id}/members/${b.dataset.uid}`, { method: "DELETE" }); }
      catch (e) { alert(e.message); }
    }));
    $("gs-add")?.addEventListener("click", async () => {
      const box = $("gs-add-list");
      if (!box.classList.toggle("hidden")) {
        const opts = await api(`/api/conversations/${c.id}/addable`);
        box.innerHTML = opts.length ? opts.map((u) => `
          <div class="pick-row" data-add="${u.id}">
            <img class="avatar" src="${avatarFor(u.name, u.avatar)}">
            ${esc(u.name)}${u.is_bot ? " 🤖" : ""}<span class="join">Add →</span>
          </div>`).join("") : "<p class='muted'>Nobody left to add (agents need to be shared with you first).</p>";
        box.querySelectorAll("[data-add]").forEach((r) => r.addEventListener("click", async () => {
          try {
            await api(`/api/conversations/${c.id}/members`,
              { method: "POST", body: JSON.stringify({ user_ids: [r.dataset.add] }) });
            box.classList.add("hidden");
          } catch (e) { alert(e.message); }
        }));
      }
    });
    $("gs-leave")?.addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      if (btn.dataset.armed !== "1") {
        btn.dataset.armed = "1"; btn.textContent = "Really leave?";
        setTimeout(() => { btn.dataset.armed = ""; btn.textContent = "🚪 Leave group"; }, 2500);
        return;
      }
      try { await api(`/api/conversations/${c.id}/leave`, { method: "POST" }); }
      catch (e2) { alert(e2.message); }
    });
    $("gs-delete")?.addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      if (btn.dataset.armed !== "1") {
        btn.dataset.armed = "1"; btn.textContent = "Really delete for everyone?";
        setTimeout(() => { btn.dataset.armed = ""; btn.textContent = "🗑 Delete group"; }, 2500);
        return;
      }
      try { await api("/api/conversations/" + c.id, { method: "DELETE" }); }
      catch (e2) { alert(e2.message); }
    });
  }

  async function loadShared() {
    if (!activeConv) return;
    try {
      const s = await api(`/api/conversations/${activeConv.id}/shared`);
      $("shared-media").innerHTML = s.media.length
        ? `<div class="media-grid">${s.media.map((m) =>
            `<a href="${mediaUrl(m.content)}" target="_blank"><img src="${mediaUrl(m.content)}" loading="lazy"></a>`).join("")}</div>`
        : `<p class="muted">No media shared yet</p>`;
      $("shared-files").innerHTML = s.files.length
        ? s.files.map((f) => `
            <a class="file-card" href="${mediaUrl(f.content)}" target="_blank" download="${esc(f.file_name)}">
              <span class="file-icon">📄</span>
              <span class="file-meta"><b>${esc(f.file_name)}</b><small>${fmtSize(f.file_size)}</small></span>
              <span class="file-dl">⬇</span>
            </a>`).join("")
        : `<p class="muted">No files shared yet</p>`;
    } catch { /* ignore */ }
  }

  // ============================== messages ==============================

  function renderReactions(container, mid, reactions) {
    container.innerHTML = (reactions || []).map((r) => `
      <button class="react-chip ${r.user_ids.includes(me.id) ? "mine" : ""}" data-emoji="${r.emoji}">
        ${r.emoji} ${r.count}
      </button>`).join("");
    container.querySelectorAll(".react-chip").forEach((b) =>
      b.addEventListener("click", () =>
        ws.send(JSON.stringify({ type: "react", message_id: mid, emoji: b.dataset.emoji }))));
  }

  const REACT_SET = ["👍", "❤️", "😂", "😮", "😢", "🎉"];

  let lastDay = "", replyTo = null;

  function setReply(m) {
    replyTo = m;
    $("reply-to-name").textContent = m.sender_name;
    $("reply-to-preview").textContent = (m.kind === "text" ? m.content : m.kind === "image" ? "📷 Photo" : "📎 " + m.file_name).slice(0, 80);
    $("reply-bar").classList.remove("hidden");
    $("msg-input").focus();
  }
  function clearReply() { replyTo = null; $("reply-bar").classList.add("hidden"); }
  $("reply-cancel").addEventListener("click", clearReply);
  function maybeDaySeparator(ts) {
    const d = new Date(ts * 1000);
    const label = d.toDateString() === new Date().toDateString() ? "Today"
      : d.toDateString() === new Date(Date.now() - 864e5).toDateString() ? "Yesterday"
      : d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
    if (label !== lastDay) {
      lastDay = label;
      const sep = document.createElement("div");
      sep.className = "day-sep";
      sep.innerHTML = `<span>${label}</span>`;
      $("messages").appendChild(sep);
    }
  }

  function appendMessage(m) {
    maybeDaySeparator(m.created_at);
    const mine = m.sender_id === me.id;
    const div = document.createElement("div");
    div.dataset.mid = m.id;
    if (m.deleted) {
      div.className = "msg deleted";
      div.innerHTML = "<i>Message deleted</i>";
    } else {
      div.className = "msg " + (mine ? "out" : "in") + (m.sender_is_bot ? " bot" : "");
      const showSender = (!mine && activeConv.type !== "dm") || (!mine && m.sender_is_bot);
      let body;
      if (m.kind === "image") {
        body = `<a href="${mediaUrl(m.content)}" target="_blank"><img class="msg-img" src="${mediaUrl(m.content)}" loading="lazy"></a>`;
      } else if (m.kind === "file") {
        body = `<a class="file-card in-msg" href="${mediaUrl(m.content)}" target="_blank" download="${esc(m.file_name)}">
                  <span class="file-icon">📄</span>
                  <span class="file-meta"><b>${esc(m.file_name)}</b><small>${fmtSize(m.file_size)}</small></span>
                  <span class="file-dl">⬇</span></a>`;
      } else {
        body = `<span class="msg-text">${esc(m.content)}</span>${m.edited ? '<i class="edited"> (edited)</i>' : ""}`;
      }
      if (m.reply_preview)
        body = `<div class="reply-quote"><b>${esc(m.reply_sender)}</b> ${esc(m.reply_preview)}</div>` + body;
      const t = new Date(m.created_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      let ticks = "";
      if (mine && activeConv.type === "dm" && !activeConv.peer?.is_bot) {
        const read = (activeConv.peer?.last_read_at || 0) >= m.created_at;
        ticks = `<span class="ticks ${read ? "read" : ""}" data-ts="${m.created_at}">${read ? "✓✓" : "✓"}</span>`;
      }
      body += `<span class="msg-time">${t} ${ticks}</span>`;
      div.innerHTML =
        (showSender ? `<span class="sender ${m.sender_is_bot ? "bot-tag" : ""}">${esc(m.sender_name)}</span>` : "") +
        body +
        `<div class="reactions"></div>` +
        `<div class="msg-actions">
           <button class="ma reply" title="Reply">↩</button>
           <button class="ma react" title="React">🙂</button>
           ${mine && m.kind === "text" ? '<button class="ma edit" title="Edit">✎</button>' : ""}
           ${mine ? '<button class="ma del" title="Delete">🗑</button>' : ""}
         </div>`;
      renderReactions(div.querySelector(".reactions"), m.id, m.reactions);
      div.querySelector(".ma.reply").addEventListener("click", () => setReply(m));
      div.querySelector(".ma.react").addEventListener("click", (e) => openReactPicker(e.currentTarget, m.id));
      div.querySelector(".ma.edit")?.addEventListener("click", () => {
        const cur = div.querySelector(".msg-text")?.textContent || "";
        const next = prompt("Edit message:", cur);
        if (next && next.trim() && next !== cur)
          ws.send(JSON.stringify({ type: "edit", message_id: m.id, content: next.trim() }));
      });
      div.querySelector(".ma.del")?.addEventListener("click", () => {
        if (confirm("Delete this message?"))
          ws.send(JSON.stringify({ type: "delete", message_id: m.id }));
      });
    }
    $("messages").appendChild(div);
    $("messages").scrollTop = $("messages").scrollHeight;
  }

  function openReactPicker(anchor, mid) {
    closeReactPicker();
    const pop = document.createElement("div");
    pop.className = "react-pop";
    pop.innerHTML = REACT_SET.map((e) => `<button>${e}</button>`).join("");
    pop.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", () => {
        ws.send(JSON.stringify({ type: "react", message_id: mid, emoji: b.textContent }));
        closeReactPicker();
      }));
    anchor.closest(".msg").appendChild(pop);
    setTimeout(() => document.addEventListener("click", closeReactPicker, { once: true }), 0);
  }
  function closeReactPicker() { document.querySelectorAll(".react-pop").forEach((p) => p.remove()); }

  // ============================== composer ==============================

  function sendMessage() {
    const text = $("msg-input").value.trim();
    if (!text || !activeConv || !ws || ws.readyState !== 1) return;
    ws.send(JSON.stringify({ type: "message", conversation_id: activeConv.id, content: text,
                             reply_to: replyTo?.id || "" }));
    $("msg-input").value = "";
    clearReply();
  }
  $("send-btn").addEventListener("click", sendMessage);

  // ---- @mention autocomplete ----
  function mentionCandidates(prefix) {
    if (!activeConv) return [];
    const q = prefix.toLowerCase();
    return activeConv.members
      .filter((m) => m.id !== me.id)
      .map((m) => ({ handle: m.slug || m.name.toLowerCase().replace(/\s+/g, ""), m }))
      .filter((x) => !q || x.handle.includes(q) || x.m.name.toLowerCase().includes(q))
      .slice(0, 6);
  }
  function hideMention() { $("mention-pop").classList.add("hidden"); }
  function showMentionPop() {
    const input = $("msg-input");
    const upto = input.value.slice(0, input.selectionStart ?? input.value.length);
    const match = upto.match(/@([\w.\-]*)$/);
    if (!match || !activeConv || activeConv.type === "dm") { hideMention(); return null; }
    const cands = mentionCandidates(match[1]);
    if (!cands.length) { hideMention(); return null; }
    const pop = $("mention-pop");
    pop.innerHTML = cands.map((x, i) => `
      <div class="mention-item ${i === 0 ? "sel" : ""}" data-handle="${esc(x.handle)}">
        <img class="avatar" src="${avatarFor(x.m.name, x.m.avatar)}">
        <b>${esc(x.m.name)}</b><small>@${esc(x.handle)}${x.m.is_bot ? " · 🤖" : ""}</small>
      </div>`).join("");
    pop.classList.remove("hidden");
    pop.querySelectorAll(".mention-item").forEach((el) =>
      el.addEventListener("mousedown", (e) => { e.preventDefault(); insertMention(el.dataset.handle); }));
    return match;
  }
  function insertMention(handle) {
    const input = $("msg-input");
    const pos = input.selectionStart ?? input.value.length;
    const upto = input.value.slice(0, pos).replace(/@([\w.\-]*)$/, "@" + handle + " ");
    input.value = upto + input.value.slice(pos);
    hideMention();
    input.focus();
    input.selectionStart = input.selectionEnd = upto.length;
  }
  $("msg-input").addEventListener("input", showMentionPop);
  $("msg-input").addEventListener("blur", () => setTimeout(hideMention, 150));

  $("msg-input").addEventListener("keydown", (e) => {
    const pop = $("mention-pop");
    if (!pop.classList.contains("hidden")) {
      const items = [...pop.querySelectorAll(".mention-item")];
      const idx = items.findIndex((i) => i.classList.contains("sel"));
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        items[idx]?.classList.remove("sel");
        const next = e.key === "ArrowDown" ? (idx + 1) % items.length : (idx - 1 + items.length) % items.length;
        items[next]?.classList.add("sel");
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        insertMention(items[Math.max(idx, 0)].dataset.handle);
        return;
      }
      if (e.key === "Escape") { hideMention(); return; }
    }
    if (e.key === "Enter") sendMessage();
    else if (activeConv && ws?.readyState === 1)
      ws.send(JSON.stringify({ type: "typing", conversation_id: activeConv.id }));
  });
  $("search").addEventListener("input", renderConvList);
  $("back-btn").addEventListener("click", () => document.body.classList.remove("mobile-chat"));
  $("chat-name").addEventListener("click", () => {
    if (window.innerWidth <= 720) $("details").classList.toggle("mobile-open");
  });
  $("details").addEventListener("click", (e) => {
    if (window.innerWidth <= 720 && e.target === $("details")) $("details").classList.remove("mobile-open");
  });

  // ---- attachments ----
  $("attach-btn").addEventListener("click", () => activeConv && $("file-input").click());
  $("file-input").addEventListener("change", async () => {
    const file = $("file-input").files[0];
    if (!file || !activeConv) return;
    const fd = new FormData();
    fd.append("file", file);
    try {
      await api(`/api/conversations/${activeConv.id}/upload`, { method: "POST", body: fd });
    } catch (e) { alert("Upload failed: " + e.message); }
    $("file-input").value = "";
  });

  // ---- emoji picker ----
  const EMOJIS = ("😀 😄 😆 😂 🤣 😊 😍 😘 😜 🤔 😎 🥳 😅 🙃 😇 🤗 👍 👎 👏 🙏 💪 🤝 ✌️ 🤙 " +
                  "❤️ 🧡 💛 💚 💙 💜 🔥 ⭐ ✨ 🎉 🎊 🎁 ☕ 🍕 🍔 🌮 ⚽ 🏀 🏔️ 🥾 🌄 🚗 ✈️ 📷").split(" ");
  $("emoji-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    const p = $("emoji-pop");
    if (!p.classList.toggle("hidden")) {
      if (!p.innerHTML)
        p.innerHTML = EMOJIS.map((em) => `<button>${em}</button>`).join("");
      p.querySelectorAll("button").forEach((b) => b.onclick = () => {
        $("msg-input").value += b.textContent;
        $("msg-input").focus();
      });
      setTimeout(() => document.addEventListener("click",
        () => p.classList.add("hidden"), { once: true }), 0);
    }
  });

  // ============================== calls ==============================

  $("voice-call-btn").addEventListener("click", () => startCall(false));
  $("video-call-btn").addEventListener("click", () => startCall(true));
  function startCall(video) {
    const peer = activeConv?.peer;
    if (!peer) return;
    if (!onlineIds.includes(peer.id)) { alert(peer.name + " is offline right now."); return; }
    window.Calls.start(peer.id, peer, video);
  }

  // ============================== new conversation modal ==============================

  let modalType = "dm", pickedUsers = new Set(), allUsers = [];

  $("new-chat-btn").addEventListener("click", async () => {
    allUsers = (await api("/api/users")).filter((u) => u.id !== me.id);
    pickedUsers = new Set();
    setModalType("dm");
    $("modal").classList.remove("hidden");
  });
  $("modal-cancel").addEventListener("click", () => $("modal").classList.add("hidden"));
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => setModalType(t.dataset.type)));

  async function setModalType(type) {
    modalType = type;
    pickedUsers.clear();
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.type === type));
    $("conv-name").classList.toggle("hidden", type === "dm");
    $("conv-name").value = "";
    const browsing = type === "channel";
    $("channel-browse").classList.toggle("hidden", !browsing);
    renderUserPick();
    if (browsing) {
      const chans = await api("/api/channels");
      $("channel-browse").innerHTML = chans.length
        ? "<p class='muted'>Or join an existing channel:</p>" + chans.map((c) => `
            <div class="pick-row" data-join="${c.id}">
              <div class="conv-icon">#️⃣</div>${esc(c.name)}<span class="join">Join →</span>
            </div>`).join("")
        : "";
      document.querySelectorAll("[data-join]").forEach((el) =>
        el.addEventListener("click", async () => {
          await api(`/api/channels/${el.dataset.join}/join`, { method: "POST" });
          $("modal").classList.add("hidden");
          await loadConversations();
          openConversation(el.dataset.join);
        }));
    }
  }

  function renderUserPick() {
    $("user-pick").innerHTML = allUsers.map((u) => `
      <div class="pick-row ${pickedUsers.has(u.id) ? "selected" : ""}" data-uid="${u.id}">
        <img class="avatar" src="${avatarFor(u.name, u.avatar)}">
        ${esc(u.name)}${u.is_bot ? " ✨ <small>AI Assistant</small>" : ""}
      </div>`).join("");
    document.querySelectorAll("[data-uid]").forEach((el) =>
      el.addEventListener("click", () => {
        const uid = el.dataset.uid;
        if (modalType === "dm") { pickedUsers = new Set([uid]); }
        else { pickedUsers.has(uid) ? pickedUsers.delete(uid) : pickedUsers.add(uid); }
        renderUserPick();
      }));
  }

  $("modal-create").addEventListener("click", async () => {
    try {
      const conv = await api("/api/conversations", {
        method: "POST",
        body: JSON.stringify({ type: modalType, name: $("conv-name").value, member_ids: [...pickedUsers] }),
      });
      $("modal").classList.add("hidden");
      await loadConversations();
      openConversation(conv.id);
    } catch (e) { alert(e.message); }
  });

  window.openConv = openConversation;
  window.reloadConvs = loadConversations;
  boot();
})();

/* ================= v3: profile + agents ================= */
(() => {
  const $ = (id) => document.getElementById(id);
  const token = () => localStorage.getItem("talkio_token") || "";
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      ...opts,
      headers: {
        ...(opts.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
        Authorization: "Bearer " + token(), ...(opts.headers || {}),
      },
    });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    return res.json();
  }

  // ---------------- profile ----------------
  $("me-open").addEventListener("click", async () => {
    const me = await api("/api/me");
    $("profile-name").value = me.name;
    $("profile-title").value = me.title || "";
    $("profile-email").textContent = me.email;
    $("profile-avatar").src = me.avatar || window.talkioAvatar(me.name);
    $("profile-modal").classList.remove("hidden");
  });
  $("profile-cancel").addEventListener("click", () => $("profile-modal").classList.add("hidden"));
  $("avatar-upload-btn").addEventListener("click", () => $("avatar-input").click());
  $("avatar-input").addEventListener("change", async () => {
    const f = $("avatar-input").files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    try {
      const r = await api("/api/me/avatar", { method: "POST", body: fd });
      $("profile-avatar").src = mediaUrl(r.avatar);
      $("me-avatar").src = mediaUrl(r.avatar);
    } catch (e) { alert(e.message); }
    $("avatar-input").value = "";
  });
  $("profile-save").addEventListener("click", async () => {
    try {
      const me = await api("/api/me", {
        method: "PUT",
        body: JSON.stringify({ name: $("profile-name").value, title: $("profile-title").value }),
      });
      $("me-name").textContent = me.name;
      $("profile-modal").classList.add("hidden");
    } catch (e) { alert(e.message); }
  });

  // ---------------- settings ----------------
  function themeLabel() {
    return (document.documentElement.dataset.theme === "dark") ? "Switch to light" : "Switch to dark";
  }
  $("settings-btn").addEventListener("click", async () => {
    const me = await api("/api/me");
    $("settings-email").textContent = me.email;
    $("theme-toggle").textContent = themeLabel();
    $("notif-toggle").textContent = localStorage.getItem("talkio_notif") === "on" ? "Disable" : "Enable";
    $("settings-modal").classList.remove("hidden");
  });
  $("settings-close").addEventListener("click", () => $("settings-modal").classList.add("hidden"));
  $("theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("talkio_theme", next);
    $("theme-toggle").textContent = themeLabel();
  });
  $("notif-toggle").addEventListener("click", async () => {
    if (localStorage.getItem("talkio_notif") === "on") {
      localStorage.setItem("talkio_notif", "off");
    } else {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { window.toast("Notifications blocked by the browser", "err"); return; }
      localStorage.setItem("talkio_notif", "on");
      window.toast("Desktop notifications enabled", "ok");
    }
    $("notif-toggle").textContent = localStorage.getItem("talkio_notif") === "on" ? "Disable" : "Enable";
  });
  $("settings-logout").addEventListener("click", () => {
    localStorage.removeItem("talkio_token"); location.reload();
  });
  $("settings-logout-all").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    if (btn.dataset.armed !== "1") {
      btn.dataset.armed = "1"; btn.textContent = "Really sign out everywhere?";
      setTimeout(() => { btn.dataset.armed = ""; btn.textContent = "All devices"; }, 2500);
      return;
    }
    try {
      const r = await api("/api/auth/logout_all", { method: "POST" });
      localStorage.setItem("talkio_token", r.token);   // this device gets a fresh token
      location.reload();
    } catch (err) { alert(err.message); }
  });

  // ---------------- global search ----------------
  $("global-search-btn").addEventListener("click", () => {
    $("search-modal").classList.remove("hidden");
    $("global-search-input").value = "";
    $("search-results").innerHTML = "<p class='muted'>Type at least 2 characters</p>";
    $("global-search-input").focus();
  });
  $("search-close").addEventListener("click", () => $("search-modal").classList.add("hidden"));
  let searchTimer = null;
  $("global-search-input").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runGlobalSearch, 300);
  });
  async function runGlobalSearch() {
    const q = $("global-search-input").value.trim();
    if (q.length < 2) { $("search-results").innerHTML = "<p class='muted'>Type at least 2 characters</p>"; return; }
    const r = await api("/api/search?q=" + encodeURIComponent(q));
    const fmt = (ts) => new Date(ts * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
    $("search-results").innerHTML = `
      ${r.messages.length ? "<h3>Messages</h3>" + r.messages.map((m) => `
        <div class="sr-row" data-conv="${m.conversation_id}">
          <div class="sr-body"><b>${esc(m.sender_name)}</b> in ${esc(m.conv_type === "dm" ? "DM" : m.conv_name)}
          <small>${fmt(m.created_at)}</small><div class="sr-text">${esc(m.content.slice(0, 100))}</div></div>
        </div>`).join("") : ""}
      ${r.users.length ? "<h3>People & bots</h3>" + r.users.map((u) => `
        <div class="sr-row" data-user="${u.id}">
          <img class="avatar" src="${avatarFor(u.name, u.avatar)}">
          <div class="sr-body"><b>${esc(u.name)}</b><small>${esc(u.email)}${u.is_bot ? " · 🤖" : ""}</small></div>
        </div>`).join("") : ""}
      ${r.agents.length ? "<h3>My agents</h3>" + r.agents.map((a) => `
        <div class="sr-row" data-user="${a.user_id}">
          <span class="conv-icon">🤖</span>
          <div class="sr-body"><b>${esc(a.name)}</b><small>@${esc(a.slug)} · ${esc(a.description || "")}</small></div>
        </div>`).join("") : ""}
      ${!r.messages.length && !r.users.length && !r.agents.length ? "<p class='muted'>No results</p>" : ""}`;
    document.querySelectorAll(".sr-row[data-conv]").forEach((el) =>
      el.addEventListener("click", () => { $("search-modal").classList.add("hidden"); window.openConv(el.dataset.conv); }));
    document.querySelectorAll(".sr-row[data-user]").forEach((el) =>
      el.addEventListener("click", async () => {
        const conv = await api("/api/conversations", { method: "POST",
          body: JSON.stringify({ type: "dm", member_ids: [el.dataset.user] }) });
        $("search-modal").classList.add("hidden");
        await window.reloadConvs();
        window.openConv(conv.id);
      }));
  }

  // ---------------- agents ----------------
  let editingId = null, provider = "claude";

  function setProvider(prov) {
    provider = prov;
    document.querySelectorAll(".prov").forEach((b) =>
      b.classList.toggle("active", b.dataset.prov === prov));
    $("claude-fields").classList.toggle("hidden", prov !== "claude");
    $("ollama-fields").classList.toggle("hidden", prov !== "ollama");
  }
  document.querySelectorAll(".prov").forEach((b) =>
    b.addEventListener("click", () => setProvider(b.dataset.prov)));

  function ollamaStatus(text, kind) {
    const el = $("ollama-status");
    el.textContent = text;
    el.className = "ollama-status " + (kind || "");
    el.classList.remove("hidden");
  }
  function fillModels(models, keep) {
    const sel = $("agent-ollama-model");
    sel.innerHTML = models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
    if (keep && models.includes(keep)) sel.value = keep;
    sel.classList.toggle("hidden", !models.length);
    $("agent-ollama-model-manual").classList.toggle("hidden", !!models.length);
  }
  async function probeOllama(detect) {
    ollamaStatus("Connecting…", "");
    try {
      let r;
      if (detect) {
        r = await api("/api/ollama/detect", { method: "POST", body: "{}" });
        $("agent-ollama-url").value = r.url;
      } else {
        const url = $("agent-ollama-url").value.trim();
        if (!url) { ollamaStatus("Enter a URL first (or use Detect)", "err"); return; }
        r = await api("/api/ollama/models", { method: "POST", body: JSON.stringify({ url }) });
      }
      fillModels(r.models, $("agent-ollama-model-manual").value || $("agent-ollama-model").value);
      ollamaStatus(r.models.length
        ? `🟢 Online — ${r.models.length} model${r.models.length > 1 ? "s" : ""} found. Pick one below.`
        : "🟡 " + (r.note || "Connected, but no models installed"), r.models.length ? "ok" : "warn");
    } catch (e) { ollamaStatus("🔴 " + e.message, "err"); fillModels([]); }
  }
  $("ollama-detect").addEventListener("click", () => probeOllama(true));
  $("ollama-check").addEventListener("click", () => probeOllama(false));

  $("agents-btn").addEventListener("click", () => { openAgents(); });
  $("agents-close").addEventListener("click", () => $("agents-modal").classList.add("hidden"));
  $("agent-form-cancel").addEventListener("click", resetForm);

  function resetForm() {
    editingId = null;
    $("agent-form-title").textContent = "Create a new agent";
    $("agent-save").textContent = "Create agent";
    $("agent-form-cancel").classList.add("hidden");
    $("agent-name").value = ""; $("agent-desc").value = ""; $("agent-key").value = "";
    $("agent-model").value = "claude-sonnet-4-6";
    $("agent-ollama-url").value = "";
    $("agent-ollama-model").innerHTML = ""; $("agent-ollama-model").classList.add("hidden");
    $("agent-ollama-model-manual").value = ""; $("agent-ollama-model-manual").classList.remove("hidden");
    $("ollama-status").classList.add("hidden");
    $("agent-key").placeholder = "Claude API key (sk-ant-...) — or 'test' for echo mode";
    setProvider("claude");
  }

  async function openAgents() {
    $("agents-modal").classList.remove("hidden");
    resetForm();
    await renderRequests();
    await renderAgents();
  }

  async function renderRequests() {
    const reqs = await api("/api/agents/requests");
    const el = $("agent-requests");
    el.classList.toggle("hidden", !reqs.length);
    el.innerHTML = reqs.length ? "<h3>🔑 Pending access requests</h3>" + reqs.map((r) => `
      <div class="req-row">
        <span><b>${esc(r.requester_name)}</b> <small>(${esc(r.requester_email)})</small> → 🤖 ${esc(r.agent_name)}</span>
        <button class="btn-primary sm" data-approve="${r.id}">Approve</button>
        <button class="btn-ghost sm" data-reject="${r.id}">Reject</button>
      </div>`).join("") : "";
    el.querySelectorAll("[data-approve]").forEach((b) => b.addEventListener("click", async () => {
      await api(`/api/agents/requests/${b.dataset.approve}/approve`, { method: "POST" });
      renderRequests(); renderAgents();
    }));
    el.querySelectorAll("[data-reject]").forEach((b) => b.addEventListener("click", async () => {
      await api(`/api/agents/requests/${b.dataset.reject}/reject`, { method: "POST" });
      renderRequests();
    }));
  }

  async function renderAgents() {
    const list = await api("/api/agents");
    $("agent-list").innerHTML = list.length ? list.map((a) => `
      <div class="agent-card" data-aid="${a.id}">
        <div class="agent-top">
          <span>🤖</span><b>${esc(a.name)}</b>
          <span class="agent-tag">@${esc(a.slug)}</span>
          ${a.provider === "ollama" ? '<span class="agent-tag ollama">🖥️ ollama</span>' : ""}
          ${a.echo_mode ? '<span class="agent-tag echo">echo mode</span>' : ""}
          ${a.is_owner ? "" : `<span class="agent-tag shared">shared by ${esc(a.owner_name)}</span>`}
        </div>
        <div class="agent-desc">${esc(a.description || "General assistant")} · ${esc(a.model)}</div>
        ${a.is_owner ? `
          <div class="agent-actions">
            <button class="ag-edit">✎ Edit</button>
            <button class="ag-share">🔗 Share</button>
            <button class="ag-del">🗑 Delete</button>
            ${(a.shared_with || []).map((u) =>
              `<span class="share-chip" title="${esc(u.email)}">${esc(u.name)}<button class="ag-unshare" data-uid="${u.id}">✕</button></span>`).join("")}
          </div>
          <div class="share-box hidden">
            <input class="share-email" type="email" placeholder="Their sign-in email (e.g. friend@gmail.com)">
            <button class="share-go btn-primary">Grant access</button>
            <span class="share-msg"></span>
          </div>` : ""}
      </div>`).join("")
      : `<p class="muted">No agents yet — create your first one below.</p>`;

    document.querySelectorAll(".agent-card").forEach((card) => {
      const aid = card.dataset.aid;
      const agent = list.find((x) => x.id === aid);
      card.querySelector(".ag-edit")?.addEventListener("click", () => {
        editingId = aid;
        $("agent-form-title").textContent = "Edit " + agent.name;
        $("agent-save").textContent = "Save changes";
        $("agent-form-cancel").classList.remove("hidden");
        $("agent-name").value = agent.name;
        $("agent-desc").value = agent.description || "";
        $("agent-key").value = "";
        $("agent-key").placeholder = agent.api_key_set ? "Leave blank to keep current key" : "Claude API key";
        setProvider(agent.provider || "claude");
        if (agent.provider === "ollama") {
          $("agent-ollama-url").value = agent.ollama_url || "";
          $("agent-ollama-model-manual").value = agent.model;
          $("agent-ollama-model").classList.add("hidden");
          $("agent-ollama-model-manual").classList.remove("hidden");
        } else {
          $("agent-model").value = agent.model;
        }
      });
      card.querySelector(".ag-del")?.addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        if (btn.dataset.armed !== "1") {
          btn.dataset.armed = "1";
          btn.textContent = "Really delete?";
          setTimeout(() => { btn.dataset.armed = ""; btn.textContent = "🗑 Delete"; }, 2500);
          return;
        }
        await api("/api/agents/" + aid, { method: "DELETE" });
        renderAgents();
      });
      const shareBox = card.querySelector(".share-box");
      card.querySelector(".ag-share")?.addEventListener("click", () => {
        shareBox.classList.toggle("hidden");
        if (!shareBox.classList.contains("hidden")) shareBox.querySelector(".share-email").focus();
      });
      const doShare = async () => {
        const input = shareBox.querySelector(".share-email");
        const msg = shareBox.querySelector(".share-msg");
        const email = input.value.trim();
        if (!email) { msg.textContent = "Enter an email"; msg.className = "share-msg err"; return; }
        msg.textContent = "Sharing…"; msg.className = "share-msg";
        try {
          await api(`/api/agents/${aid}/share`, { method: "POST", body: JSON.stringify({ email }) });
          msg.textContent = "✓ Access granted to " + email;
          msg.className = "share-msg ok";
          setTimeout(renderAgents, 900);
        } catch (e) { msg.textContent = e.message; msg.className = "share-msg err"; }
      };
      shareBox?.querySelector(".share-go").addEventListener("click", doShare);
      shareBox?.querySelector(".share-email").addEventListener("keydown", (e) => {
        if (e.key === "Enter") doShare();
      });
      card.querySelectorAll(".ag-unshare").forEach((b) =>
        b.addEventListener("click", async () => {
          await api(`/api/agents/${aid}/share/${b.dataset.uid}`, { method: "DELETE" });
          renderAgents();
        }));
    });
  }

  $("agent-save").addEventListener("click", async () => {
    const body = {
      name: $("agent-name").value,
      description: $("agent-desc").value,
      provider,
      api_key: provider === "claude" ? ($("agent-key").value || null) : null,
      ollama_url: provider === "ollama" ? $("agent-ollama-url").value : "",
      model: provider === "ollama"
        ? (!$("agent-ollama-model").classList.contains("hidden") && $("agent-ollama-model").value
            ? $("agent-ollama-model").value : $("agent-ollama-model-manual").value)
        : $("agent-model").value,
    };
    try {
      if (editingId) await api("/api/agents/" + editingId, { method: "PUT", body: JSON.stringify(body) });
      else await api("/api/agents", { method: "POST", body: JSON.stringify(body) });
      resetForm();
      renderAgents();
    } catch (e) { alert(e.message); }
  });
})();
