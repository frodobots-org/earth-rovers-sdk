$(document).ready(function () {
  const APP_ID = $("#appid").val();
  const USER_ID = $("#uid").val();
  const TOKEN = $("#rtm_token").val();
  const channelName = $("#channel").val(); // Replace with your desired channel name
  const botUid = $("#bot_uid").val();

  // Create an instance of the Agora RTM client
  const rtmClient = AgoraRTM.createInstance(APP_ID);

  // Create an instance of an RTM channel
  const rtmChannel = rtmClient.createChannel(channelName);

  // Event listener for connection state changes.
  // Agora RTM Web SDK emits "ConnectionStateChanged" (not "ConnectionStateChange").
  window.rtmConnectionState = "DISCONNECTED";
  window.rtmConnectionReason = null;
  rtmClient.on("ConnectionStateChanged", (newState, reason) => {
    window.rtmConnectionState = newState;
    window.rtmConnectionReason = reason;
    console.log(
      "on connection state changed to " + newState + " reason: " + reason
    );
  });

  // Event listener for receiving a channel message
  rtmChannel.on("ChannelMessage", ({ text }, senderId) => {
    console.log("AgoraRTM msg from user " + senderId + " received: \n" + text);
  });

  rtmClient.on("MessageFromPeer", function (message, peerId) {
    const controls = JSON.parse(new TextDecoder().decode(message.rawMessage));
    const event = new CustomEvent("message-from-peer", { detail: controls });
    window.rtm_data = controls;
    window.rtm_data.timestamp = (Date.now() / 1000).toFixed(6);
    const formattedMessage = formatMessage(controls);
    $("#messages").html(formattedMessage);
    document.dispatchEvent(event);

    if (controls.latitude && controls.longitude) {
      const latitude = parseFloat(controls.latitude);
      const longitude = parseFloat(controls.longitude);
      console.log("updating marker");
      window.updateMarker(latitude, longitude);
    }

    console.log(
      "AgoraRTM peer msg from user " + peerId + " received: \n",
      controls
    );
  });

  function formatMessage(jsonData) {
    let formattedMessage =
      '<div class="card"><div class="card-body"><h5 class="card-title">Message from Peer</h5><ul class="list-group">';
    for (const [key, value] of Object.entries(jsonData)) {
      formattedMessage += `<li class="list-group-item"><strong>${key}:</strong> ${value}</li>`;
    }
    formattedMessage += "</ul></div></div>";
    return formattedMessage;
  }

  function formatRtmError(err) {
    if (err == null) {
      return "unknown RTM error";
    }
    if (typeof err === "string" || typeof err === "number") {
      return String(err);
    }
    const parts = [err.code, err.reason, err.message].filter(
      (v) => v !== undefined && v !== null && v !== ""
    );
    if (parts.length) {
      return parts.map(String).join(":");
    }
    try {
      return JSON.stringify(err);
    } catch (_) {
      return String(err);
    }
  }

  // Function to join the RTM channel
  function joinRTMChannel(uid) {
    rtmClient
      .login({ token: TOKEN, uid: String(uid) })
      .then(() => {
        console.log("AgoraRTM client login success");
        // Belt-and-suspenders if the state event was missed.
        if (window.rtmConnectionState === "DISCONNECTED") {
          window.rtmConnectionState = "CONNECTED";
          window.rtmConnectionReason = "LOGIN_SUCCESS";
        }
        // Join a channel
        rtmChannel
          .join()
          .then(() => {
            console.log("RTM Channel join success");
            // You can now send messages or set up more event listeners
          })
          .catch((error) => {
            console.log("Failed to join channel for error: " + error);
          });
      })
      .catch((err) => {
        console.log("AgoraRTM client login failure", err);
      });
  }

  // Function to send message (returns a Promise so Python can await success/failure)
  function sendMessage(json) {
    const message = JSON.stringify(json);
    console.warn("sending message to bot", botUid);
    console.warn("message", message);

    if (window.rtmConnectionState && window.rtmConnectionState !== "CONNECTED") {
      const detail =
        "RTM not CONNECTED (state=" +
        window.rtmConnectionState +
        ", reason=" +
        (window.rtmConnectionReason || "n/a") +
        ")";
      console.warn(detail);
      return Promise.reject(new Error(detail));
    }

    return rtmClient
      .sendMessageToPeer(
        {
          text: message,
        },
        String(botUid)
      )
      .then((result) => {
        // Agora resolves even when the peer did not receive the message.
        const hasPeerReceived =
          !result || result.hasPeerReceived === undefined
            ? true
            : Boolean(result.hasPeerReceived);
        if (!hasPeerReceived) {
          const detail = "RTM peer did not receive message (hasPeerReceived=false)";
          console.warn(detail, result);
          return Promise.reject(new Error(detail));
        }
        console.warn("Message sent successfully:", message);
        return { ok: true, hasPeerReceived: true };
      })
      .catch((err) => {
        console.warn("Error sending message:", err);
        const detail = formatRtmError(err);
        window.lastRtmSendError = {
          detail: detail,
          state: window.rtmConnectionState || null,
          reason: window.rtmConnectionReason || null,
          at: Date.now(),
        };
        return Promise.reject(new Error(detail || "RTM send failed"));
      });
  }

  // Session health snapshot used by BrowserService auto-reinit
  window.getRtmSessionHealth = function () {
    const ts = window.rtm_data && window.rtm_data.timestamp;
    return {
      state: window.rtmConnectionState || null,
      reason: window.rtmConnectionReason || null,
      timestamp: ts == null ? null : String(ts),
      hasTelemetry: Boolean(window.rtm_data),
      lastSendError: window.lastRtmSendError || null,
    };
  };

  // Make the function globally accessible
  window.sendMessage = sendMessage;

  // Call joinRTMChannel with the user ID to start the process
  joinRTMChannel(USER_ID);
});
