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
  let resolveRtmReady;
  let rejectRtmReady;
  const rtmReadyPromise = new Promise((resolve, reject) => {
    resolveRtmReady = resolve;
    rejectRtmReady = reject;
  });

  // Event listener for connection state changes
  rtmClient.on("ConnectionStateChange", (newState, reason) => {
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
  function joinRTMChannel(uid) {
    rtmClient
      .login({ token: TOKEN, uid: String(uid) })
      .then(() => {
        console.log("AgoraRTM client login success");
        // Join a channel
        rtmChannel
          .join()
          .then(() => {
            console.log("RTM Channel join success");
            resolveRtmReady();
            // You can now send messages or set up more event listeners
          })
          .catch((error) => {
            console.log("Failed to join channel for error: " + error);
            rejectRtmReady(error);
          });
      })
      .catch((err) => {
        console.log("AgoraRTM client login failure", err);
        rejectRtmReady(err);
      });
  }

  async function waitForRtmReady(timeoutMs = 5000) {
    return Promise.race([
      rtmReadyPromise,
      new Promise((_, reject) =>
        setTimeout(
          () => reject(new Error("RTM not ready: login/join timeout")),
          timeoutMs
        )
      ),
    ]);
  }

  // Function to send message — returns a promise so callers can await it
  function sendMessage(json) {
    const message = JSON.stringify(json);
    console.warn("sending message to bot", botUid);
    console.warn("message", message);
    return waitForRtmReady()
      .then(() =>
        rtmClient.sendMessageToPeer(
          {
            text: message,
          },
          botUid
        )
      )
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
  joinRTMChannel(USER_ID);
});

