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
  let rtmJoinPromise = null;
  let rtmReconnectTimer = null;
  window.rtmReady = false;
  window.rtmConnectionState = "DISCONNECTED";
  window.rtmLastReason = "";
  window.rtmLastError = "";
  window.rtmLoggedIn = false;
  window.rtmChannelJoined = false;

  // Event listener for connection state changes
  rtmClient.on("ConnectionStateChange", (newState, reason) => {
    console.log(
      "on connection state changed to " + newState + " reason: " + reason
    );
    window.rtmConnectionState = newState;
    window.rtmLastReason = reason || "";
    if (newState === "CONNECTED") {
      window.rtmReady = true;
      window.rtmLoggedIn = true;
      if (rtmReconnectTimer) {
        clearTimeout(rtmReconnectTimer);
        rtmReconnectTimer = null;
      }
      return;
    }

    if (newState === "ABORTED" || newState === "DISCONNECTED") {
      window.rtmReady = false;
      window.rtmLoggedIn = false;
      window.rtmChannelJoined = false;
      scheduleRtmReconnect(reason || newState);
    }
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
    if (jsonData && jsonData.type === "scene_caption") {
      return formatSceneCaption(jsonData);
    }

    let formattedMessage =
      '<div class="card"><div class="card-body"><h5 class="card-title">Message from Peer</h5><ul class="list-group">';
    for (const [key, value] of Object.entries(jsonData)) {
      formattedMessage += `<li class="list-group-item"><strong>${key}:</strong> ${value}</li>`;
    }
    formattedMessage += "</ul></div></div>";
    return formattedMessage;
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function formatSceneCaption(data) {
    const caption = escapeHtml(data.caption || "No caption available");
    const frame = data.front_frame || "";
    const imageSrc = frame ? `data:image/png;base64,${frame}` : "";
    const imageBlock = imageSrc
      ? `<img src="${imageSrc}" class="img-fluid rounded mb-3" alt="Rover camera frame" />`
      : '<div class="alert alert-warning mb-3">No image frame available</div>';
    return `
      <div class="card">
        <div class="card-body">
          <h5 class="card-title">What I See</h5>
          ${imageBlock}
          <p class="card-text">${caption}</p>
        </div>
      </div>
    `;
  }

  // Function to join the RTM channel
  async function joinRTMChannel(uid, forceReconnect = false) {
    if (!TOKEN) {
      window.rtmReady = true;
      window.rtmConnectionState = "DISABLED";
      return true;
    }

    if (
      !forceReconnect &&
      (window.rtmReady ||
        window.rtmConnectionState === "CONNECTED" ||
        window.rtmChannelJoined)
    ) {
      return true;
    }

    if (rtmJoinPromise) {
      return rtmJoinPromise;
    }

    rtmJoinPromise = (async () => {
      if (forceReconnect) {
        try {
          if (window.rtmChannelJoined) {
            await rtmChannel.leave();
          }
        } catch (error) {
          // Ignore if channel was already detached.
        }
        window.rtmChannelJoined = false;
        try {
          if (window.rtmLoggedIn) {
            await rtmClient.logout();
          }
        } catch (error) {
          // Ignore if client was already logged out.
        }
        window.rtmLoggedIn = false;
      }

      try {
        if (!window.rtmLoggedIn) {
          await rtmClient.login({ token: TOKEN, uid: String(uid) });
          console.log("AgoraRTM client login success");
          window.rtmLoggedIn = true;
        }
        if (!window.rtmChannelJoined) {
          await rtmChannel.join();
          console.log("RTM Channel join success");
          window.rtmChannelJoined = true;
        }
        window.rtmReady = true;
        window.rtmConnectionState = "CONNECTED";
        window.rtmLastError = "";
        return true;
      } catch (err) {
        window.rtmReady = false;
        window.rtmLoggedIn = false;
        window.rtmChannelJoined = false;
        window.rtmLastError = String(err);
        console.log("AgoraRTM join failure", err);
        throw err;
      } finally {
        rtmJoinPromise = null;
      }
    })();

    return rtmJoinPromise;
  }

  function scheduleRtmReconnect(reason) {
    if (rtmReconnectTimer || rtmJoinPromise) {
      return;
    }

    console.warn("[rtm] scheduling reconnect, reason:", reason);
    rtmReconnectTimer = setTimeout(() => {
      rtmReconnectTimer = null;
      joinRTMChannel(USER_ID, true).catch((error) => {
        console.warn("[rtm] reconnect failed:", error);
        scheduleRtmReconnect("retry_after_failure");
      });
    }, 1000);
  }

  function isRtmUsable() {
    if (!TOKEN) {
      return true;
    }
    return Boolean(
      window.rtmLoggedIn &&
        window.rtmChannelJoined &&
        (window.rtmReady || window.rtmConnectionState === "CONNECTED")
    );
  }

  async function ensureRtmReady(timeoutMs = 5000) {
    if (!TOKEN) {
      return true;
    }

    if (isRtmUsable()) {
      return true;
    }

    return Promise.race([
      joinRTMChannel(USER_ID, false),
      new Promise((_, reject) =>
        setTimeout(
          () => reject(new Error("RTM not ready: login/join timeout")),
          timeoutMs
        )
      ),
    ]);
  }
  window.ensureRtmReady = ensureRtmReady;

  function shouldRetrySendAfterReconnect(err) {
    const message = String(err || "");
    return (
      message.includes("RtmInvalidStatusError") ||
      message.includes("Error Code 102") ||
      message.toLowerCase().includes("not logged in")
    );
  }

  // Function to send message — returns a promise so callers can await it
  function sendMessage(json) {
    const message = JSON.stringify(json);
    console.warn("sending message to bot", botUid);
    console.warn("message", message);
    return ensureRtmReady()
      .then(() =>
        rtmClient.sendMessageToPeer(
          {
            text: message,
          },
          botUid
        )
      )
      .catch((err) => {
        if (!shouldRetrySendAfterReconnect(err)) {
          throw err;
        }

        console.warn("[rtm] send retry after reconnect:", err);
        window.rtmReady = false;
        window.rtmLoggedIn = false;
        window.rtmChannelJoined = false;
        window.rtmConnectionState = "DISCONNECTED";
        return joinRTMChannel(USER_ID, true).then(() =>
          rtmClient.sendMessageToPeer(
            {
              text: message,
            },
            botUid
          )
        );
      })
      .then(() => {
        console.warn("Message sent successfully:", message);
        return { success: true };
      })
      .catch((err) => {
        console.warn("Error sending message:", err);
        throw err;
      });
  }

  // Make the function globally accessible
  window.sendMessage = sendMessage;

  // Call joinRTMChannel with the user ID to start the process
  if (TOKEN) {
    joinRTMChannel(USER_ID);
  } else {
    window.rtmReady = true;
    window.rtmConnectionState = "DISABLED";
    console.log("RTM disabled for this page: no token provided");
  }
});
