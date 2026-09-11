/* Talkio calls — 1:1 WebRTC voice/video, signaled over the app WebSocket.
   Exposes window.Calls; app.js wires it to the socket and UI. */
(() => {
  const ICE = { iceServers: [{ urls: "stun:stun.l.google.com:19302" }] };

  const $ = (id) => document.getElementById(id);
  let pc = null, localStream = null, sendSignal = null;
  let peerId = null, video = false, pendingOffer = null;

  function ui(show) {
    $("call-modal").classList.toggle("hidden", !show);
  }

  async function getMedia(withVideo) {
    localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: withVideo });
    $("local-video").srcObject = localStream;
    $("local-video").classList.toggle("hidden", !withVideo);
    $("voice-only").classList.toggle("hidden", withVideo);
  }

  function newPC() {
    pc = new RTCPeerConnection(ICE);
    localStream.getTracks().forEach((t) => pc.addTrack(t, localStream));
    pc.ontrack = (e) => { $("remote-video").srcObject = e.streams[0]; };
    pc.onicecandidate = (e) => {
      if (e.candidate) sendSignal({ type: "call-ice", to: peerId, candidate: e.candidate });
    };
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "connected") $("call-status").textContent = "Connected";
      if (["failed", "disconnected", "closed"].includes(pc.connectionState)) cleanup();
    };
  }

  function cleanup() {
    if (pc) { pc.close(); pc = null; }
    if (localStream) { localStream.getTracks().forEach((t) => t.stop()); localStream = null; }
    $("remote-video").srcObject = null;
    peerId = null; pendingOffer = null;
    ui(false);
    $("incoming").classList.add("hidden");
  }

  const Calls = {
    init(signalFn) { sendSignal = signalFn; },

    async start(targetUserId, peer, withVideo) {
      peerId = targetUserId; video = withVideo;
      $("call-peer-name").textContent = peer.name;
      $("call-peer-avatar").src = peer.avatar || Calls.avatarFor(peer.name);
      $("call-status").textContent = "Calling " + peer.name + "…";
      ui(true);
      try { await getMedia(withVideo); } catch { alert("Microphone/camera access denied."); cleanup(); return; }
      newPC();
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      sendSignal({ type: "call-offer", to: peerId, sdp: offer, video: withVideo });
    },

    onOffer(msg) {
      pendingOffer = msg;
      $("incoming-name").textContent = msg.from_name;
      $("incoming-kind").textContent = (msg.video ? "Video" : "Voice") + " call";
      $("incoming-avatar").src = msg.from_avatar || Calls.avatarFor(msg.from_name);
      $("incoming").classList.remove("hidden");
    },

    async accept() {
      const msg = pendingOffer;
      if (!msg) return;
      $("incoming").classList.add("hidden");
      peerId = msg.from; video = !!msg.video;
      $("call-peer-name").textContent = msg.from_name;
      $("call-peer-avatar").src = msg.from_avatar || Calls.avatarFor(msg.from_name);
      $("call-status").textContent = "Connecting…";
      ui(true);
      try { await getMedia(video); } catch { alert("Microphone/camera access denied."); cleanup(); return; }
      newPC();
      await pc.setRemoteDescription(msg.sdp);
      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      sendSignal({ type: "call-answer", to: peerId, sdp: answer });
    },

    decline() {
      if (pendingOffer) sendSignal({ type: "call-decline", to: pendingOffer.from });
      pendingOffer = null;
      $("incoming").classList.add("hidden");
    },

    async onAnswer(msg) { if (pc) await pc.setRemoteDescription(msg.sdp); },

    async onIce(msg) { if (pc && msg.candidate) { try { await pc.addIceCandidate(msg.candidate); } catch {} } },

    onEnd() { cleanup(); },

    hangup() {
      if (peerId) sendSignal({ type: "call-end", to: peerId });
      cleanup();
    },

    toggleMute() {
      if (!localStream) return;
      const t = localStream.getAudioTracks()[0];
      if (t) { t.enabled = !t.enabled; $("mute-btn").classList.toggle("off", !t.enabled); }
    },

    toggleCam() {
      if (!localStream) return;
      const t = localStream.getVideoTracks()[0];
      if (t) { t.enabled = !t.enabled; $("cam-btn").classList.toggle("off", !t.enabled); }
    },

    avatarFor(name) {
      return window.talkioAvatar ? window.talkioAvatar(name) : "";
    },
  };

  $("hangup-btn").addEventListener("click", () => Calls.hangup());
  $("mute-btn").addEventListener("click", () => Calls.toggleMute());
  $("cam-btn").addEventListener("click", () => Calls.toggleCam());
  $("accept-btn").addEventListener("click", () => Calls.accept());
  $("decline-btn").addEventListener("click", () => Calls.decline());

  window.Calls = Calls;
})();
