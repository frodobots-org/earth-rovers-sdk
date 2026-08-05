$(document).ready(function () {
  window.rtmReady = false;
  const APP_ID = $("#appid").val();
  const USER_ID = $("#uid").val();
  const TOKEN = $("#rtm_token").val();
  const channelName = $("#channel").val(); // Replace with your desired channel name
  const botUid = $("#bot_uid").val();

  // Create an instance of the Agora RTM client
  const rtmClient = AgoraRTM.createInstance(APP_ID);

  // Create an instance of an RTM channel
  const rtmChannel = rtmClient.createChannel(channelName);

  // Event listener for connection state changes
  rtmClient.on("ConnectionStateChange", (newState, reason) => {
    window.rtmReady = newState === "CONNECTED";
    console.log(
      "on connection state changed to " + newState + " reason: " + reason
    );
  });

  // Event listener for receiving a channel message
  rtmChannel.on("ChannelMessage", () => {
    // Channel messages are not used by the SDK hot path.
  });

  rtmClient.on("MessageFromPeer", function (message, peerId) {
    // An SDK RTM user can receive direct messages from more than one peer.
    // Only the rover selected by the auth response is a telemetry source.
    if (String(peerId) !== String(botUid)) return;

    let controls;
    try {
      controls = JSON.parse(new TextDecoder().decode(message.rawMessage));
    } catch (_) {
      return;
    }
    if (!controls || typeof controls !== "object") return;
    const event = new CustomEvent("message-from-peer", { detail: controls });
    window.rtm_data = controls;
    window.rtm_data.timestamp = Date.now() / 1000;
    document.dispatchEvent(event);

    if (
      controls.latitude !== null &&
      controls.latitude !== undefined &&
      controls.longitude !== null &&
      controls.longitude !== undefined
    ) {
      const latitude = parseFloat(controls.latitude);
      const longitude = parseFloat(controls.longitude);
      window.updateMarker(latitude, longitude);
    }
  });

  // Function to join the RTM channel
  function joinRTMChannel(uid) {
    return rtmClient
      .login({ token: TOKEN, uid: String(uid) })
      .then(() => {
        console.log("AgoraRTM client login success");
        return rtmChannel
          .join()
          .then(() => {
            console.log("RTM Channel join success");
            window.rtmReady = true;
          })
          .catch((error) => {
            console.log("Failed to join channel for error: " + error);
          });
      })
      .catch((err) => {
        console.log("AgoraRTM client login failure", err);
      });
  }

  // Function to send message
  const SEND_TIMEOUT_MS = 4000;
  const UNREACHABLE_MSG =
    "Rover unreachable - it may be offline or the mission has ended";

  function sendMessage(json) {
    if (!window.rtmReady) {
      return Promise.reject(new Error("RTM client is not connected"));
    }
    const message = JSON.stringify(json);
    const send = rtmClient.sendMessageToPeer({ text: message }, botUid);
    // Agora waits 10s for a peer ACK; when the bot left the channel that is
    // far too slow for a control loop. Race a shorter deadline and swallow
    // the late Agora rejection so it doesn't spam the console.
    send.catch(() => undefined);
    let timer;
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(UNREACHABLE_MSG)), SEND_TIMEOUT_MS);
    });
    return Promise.race([send, deadline])
      .then(() => undefined)
      .catch((err) => {
        const text = String((err && err.message) || err);
        console.warn("Error sending message:", text);
        throw /timed out|timeout/i.test(text) ? new Error(UNREACHABLE_MSG) : err;
      })
      .finally(() => clearTimeout(timer));
  }

  // Make the function globally accessible
  window.sendMessage = sendMessage;

  // Call joinRTMChannel with the user ID to start the process
  joinRTMChannel(USER_ID);
});
