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

  function addCheckpointsToMap(checkpoints) {
    // nano
    checkpoints = [
      {
        id: 3780,
        sequence: 1,
        latitude: "22.7537",
        longitude: "114.0908",
      },
      {
        id: 3781,
        sequence: 2,
        latitude: "22.7540",
        longitude: "114.0911",
      },
      {
        id: 3782,
        sequence: 3,
        latitude: "22.7543",
        longitude: "114.0914",
      },
      {
        id: 3783,
        sequence: 4,
        latitude: "22.7546",
        longitude: "114.0917",
      },
      {
        id: 3784,
        sequence: 5,
        latitude: "22.7549",
        longitude: "114.0920",
      },
      {
        id: 3785,
        sequence: 6,
        latitude: "22.7552",
        longitude: "114.0923",
      },
      {
        id: 3786,
        sequence: 7,
        latitude: "22.7555",
        longitude: "114.0926",
      },
      {
        id: 3787,
        sequence: 8,
        latitude: "22.7558",
        longitude: "114.0929",
      },
    ];
    checkpoints.forEach((checkpoint) => {
      const el = document.createElement("div");
      el.className = "marker";
      el.innerHTML = checkpoint.sequence;

      new mapboxgl.Marker(el)
        .setLngLat([
          parseFloat(checkpoint.longitude),
          parseFloat(checkpoint.latitude),
        ])
        .addTo(map);
    });
  }

  // Initialize Mapbox
  mapboxgl.accessToken =
    "pk.eyJ1Ijoic2FuYXRlbSIsImEiOiJjbHl4YzM5eXEwODd0MnJweXZ5dXh1MTg1In0.WjPAxzNMkHLwp5thyarUwQ";
  let map;
  let marker;

  function initializeMap(latitude, longitude) {
    map = new mapboxgl.Map({
      container: "map",
      style: "mapbox://styles/mapbox/streets-v11",
      center: [longitude, latitude],
      zoom: 22,
    });

    // Create the main marker with a robot style
    const mainMarkerEl = document.createElement("div");
    mainMarkerEl.className = "main-marker";
    const antenna = document.createElement("div");
    antenna.className = "antenna";
    mainMarkerEl.appendChild(antenna);
    new mapboxgl.Marker(mainMarkerEl)
      .setLngLat([longitude, latitude])
      .addTo(map);

    // Add checkpoints to the map
    addCheckpointsToMap(checkpointsList);
  }

  function updateMarker(latitude, longitude) {
    if (marker) {
      marker.setLngLat([longitude, latitude]);
      map.setCenter([longitude, latitude]);
    }
  }

  rtmClient.on("MessageFromPeer", function (message, peerId) {
    const controls = JSON.parse(new TextDecoder().decode(message.rawMessage));
    const event = new CustomEvent("message-from-peer", { detail: controls });
    window.rtm_data = controls;
    const formattedMessage = formatMessage(controls);
    $("#messages").html(formattedMessage);
    document.dispatchEvent(event);

    if (controls.latitude && controls.longitude) {
      const latitude = parseFloat(controls.latitude);
      const longitude = parseFloat(controls.longitude);
      if (!map) {
        initializeMap(latitude, longitude);
      } else {
        updateMarker(latitude, longitude);
      }
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

