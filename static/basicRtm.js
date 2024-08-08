$(document).ready(function () {
  const APP_ID = $("#appid").val();
  const USER_ID = $("#uid").val();
  const TOKEN = $("#rtm_token").val();
  const channelName = $("#channel").val(); // Replace with your desired channel name

  // Create an instance of the Agora RTM client
  const rtmClient = AgoraRTM.createInstance(APP_ID);

  // Create an instance of an RTM channel
  const rtmChannel = rtmClient.createChannel(channelName);

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
    window.rtm_data.timestamp = Math.floor(Date.now() / 1000);
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

  // Call joinRTMChannel with the user ID to start the process
  joinRTMChannel(USER_ID);
});

