/*
 *  These procedures use Agora Video Call SDK for Web to enable local and remote
 *  users to join and leave a Video Call channel managed by Agora Platform.
 */

/*
 *  Create an {@link https://docs.agora.io/en/Video/API%20Reference/web_ng/interfaces/iagorartcclient.html|AgoraRTCClient} instance.
 *
 * @param {string} mode - The {@link https://docs.agora.io/en/Voice/API%20Reference/web_ng/interfaces/clientconfig.html#mode| streaming algorithm} used by Agora SDK.
 * @param  {string} codec - The {@link https://docs.agora.io/en/Voice/API%20Reference/web_ng/interfaces/clientconfig.html#codec| client codec} used by the browser.
 */
var client;

/*
 * Clear the video and audio tracks used by `client` on initiation.
 */
var localTracks = {
  videoTrack: null,
  audioTrack: null,
};

/*
 * On initiation no users are connected.
 */
var remoteUsers = {};

/*
 * Subscription queue to serialize subscribe calls (prevents WebRTC negotiation conflicts)
 */
var subscriptionQueue = [];
var isProcessingSubscription = false;

/*
 * On initiation. `client` is not attached to any project or channel for any specific user.
 */
var options = {
  appid: null,
  channel: null,
  uid: null,
  token: null,
};

// you can find all the agora preset video profiles here https://docs.agora.io/en/Voice/API%20Reference/web_ng/globals.html#videoencoderconfigurationpreset
var videoProfiles = [
  {
    label: "360p_7",
    detail: "480×360, 15fps, 320Kbps",
    value: "360p_7",
  },
  {
    label: "360p_8",
    detail: "480×360, 30fps, 490Kbps",
    value: "360p_8",
  },
  {
    label: "480p_1",
    detail: "640×480, 15fps, 500Kbps",
    value: "480p_1",
  },
  {
    label: "480p_2",
    detail: "640×480, 30fps, 1000Kbps",
    value: "480p_2",
  },
  {
    label: "720p_1",
    detail: "1280×720, 15fps, 1130Kbps",
    value: "720p_1",
  },
  {
    label: "720p_2",
    detail: "1280×720, 30fps, 2000Kbps",
    value: "720p_2",
  },
  {
    label: "1080p_1",
    detail: "1920×1080, 15fps, 2080Kbps",
    value: "1080p_1",
  },
  {
    label: "1080p_2",
    detail: "1920×1080, 30fps, 3000Kbps",
    value: "1080p_2",
  },
];
var curVideoProfile;
AgoraRTC.onAutoplayFailed = () => {
  alert("click to start autoplay!");
};
AgoraRTC.onMicrophoneChanged = async (changedDevice) => {
  // When plugging in a device, switch to a device that is newly plugged in.
  if (changedDevice.state === "ACTIVE") {
    localTracks.audioTrack.setDevice(changedDevice.device.deviceId);
    // Switch to an existing device when the current device is unplugged.
  } else if (
    changedDevice.device.label === localTracks.audioTrack.getTrackLabel()
  ) {
    const oldMicrophones = await AgoraRTC.getMicrophones();
    oldMicrophones[0] &&
      localTracks.audioTrack.setDevice(oldMicrophones[0].deviceId);
  }
};
AgoraRTC.onCameraChanged = async (changedDevice) => {
  // When plugging in a device, switch to a device that is newly plugged in.
  if (changedDevice.state === "ACTIVE") {
    localTracks.videoTrack.setDevice(changedDevice.device.deviceId);
    // Switch to an existing device when the current device is unplugged.
  } else if (
    changedDevice.device.label === localTracks.videoTrack.getTrackLabel()
  ) {
    const oldCameras = await AgoraRTC.getCameras();
    oldCameras[0] && localTracks.videoTrack.setDevice(oldCameras[0].deviceId);
  }
};
async function initDevices() {
  mics = await AgoraRTC.getMicrophones();
  const audioTrackLabel = localTracks.audioTrack.getTrackLabel();
  currentMic = mics.find((item) => item.label === audioTrackLabel);
  $(".mic-input").val(currentMic.label);
  $(".mic-list").empty();
  mics.forEach((mic) => {
    $(".mic-list").append(`<a class="dropdown-item" href="#">${mic.label}</a>`);
  });

  cams = await AgoraRTC.getCameras();
  const videoTrackLabel = localTracks.videoTrack.getTrackLabel();
  currentCam = cams.find((item) => item.label === videoTrackLabel);
  $(".cam-input").val(currentCam.label);
  $(".cam-list").empty();
  cams.forEach((cam) => {
    $(".cam-list").append(`<a class="dropdown-item" href="#">${cam.label}</a>`);
  });
}
async function switchCamera(label) {
  currentCam = cams.find((cam) => cam.label === label);
  $(".cam-input").val(currentCam.label);
  // switch device of local video track.
  await localTracks.videoTrack.setDevice(currentCam.deviceId);
}
async function switchMicrophone(label) {
  currentMic = mics.find((mic) => mic.label === label);
  $(".mic-input").val(currentMic.label);
  // switch device of local audio track.
  await localTracks.audioTrack.setDevice(currentMic.deviceId);
}
function initVideoProfiles() {
  videoProfiles.forEach((profile) => {
    $(".profile-list").append(
      `<a class="dropdown-item" label="${profile.label}" href="#">${profile.label}: ${profile.detail}</a>`
    );
  });
  curVideoProfile = videoProfiles.find((item) => item.label == "480p_1");
  $(".profile-input").val(`${curVideoProfile.detail}`);
}
async function changeVideoProfile(label) {
  curVideoProfile = videoProfiles.find((profile) => profile.label === label);
  $(".profile-input").val(`${curVideoProfile.detail}`);
  // change the local video track`s encoder configuration
  localTracks.videoTrack &&
    (await localTracks.videoTrack.setEncoderConfiguration(
      curVideoProfile.value
    ));
}

/*
 * When this page is called with parameters in the URL, this procedure
 * attempts to join a Video Call channel using those parameters.
 */
$(() => {
  initVideoProfiles();
  $(".profile-list").delegate("a", "click", function (e) {
    changeVideoProfile(this.getAttribute("label"));
  });
  var urlParams = new URL(location.href).searchParams;
  options.appid = urlParams.get("appid");
  options.channel = urlParams.get("channel");
  options.token = urlParams.get("token");
  options.uid = urlParams.get("uid");
  if (options.appid && options.channel) {
    $("#uid").val(options.uid);
    $("#appid").val(options.appid);
    $("#token").val(options.token);
    $("#channel").val(options.channel);
    $("#join-form").submit();
  }
});

/*
 * When a user clicks Join or Leave in the HTML form, this procedure gathers the information
 * entered in the form and calls join asynchronously. The UI is updated to match the options entered
 * by the user.
 */
$("#join-form").submit(async function (e) {
  e.preventDefault();
  $("#join").attr("disabled", true);
  try {
    client = AgoraRTC.createClient({
      mode: "rtc",
      codec: getCodec(),
    });
    options.channel = $("#channel").val();
    options.uid = Number($("#uid").val());
    options.appid = $("#appid").val();
    options.token = $("#token").val();
    await join();
    if (options.token) {
      $("#success-alert-with-token").css("display", "block");
    } else {
      $("#success-alert a").attr(
        "href",
        `index.html?appid=${options.appid}&channel=${options.channel}&token=${options.token}`
      );
      $("#success-alert").css("display", "block");
    }
  } catch (error) {
    console.error(error);
  } finally {
    $("#leave").attr("disabled", false);
  }
});

/*
 * Called when a user clicks Leave in order to exit a channel.
 */
$("#leave").click(function (e) {
  leave();
});
$("#agora-collapse").on("show.bs.collapse	", function () {
  initDevices();
});
$(".cam-list").delegate("a", "click", function (e) {
  switchCamera(this.text);
});
$(".mic-list").delegate("a", "click", function (e) {
  switchMicrophone(this.text);
});

/*
 * Join a channel, then create local video and audio tracks and publish them to the channel.
 */
async function join() {
  // Add an event listener to play remote tracks when remote user publishes.
  client.on("user-published", handleUserPublished);
  client.on("user-unpublished", handleUserUnpublished);
  options.uid = await client.join(
    options.appid,
    options.channel,
    options.token || null,
    options.uid || null
  );
  $("#captured-frames").css("display", DEBUG_MODE ? "block" : "none");
}

/*
 * Stop all local and remote tracks then leave the channel.
 */
async function leave() {
  for (trackName in localTracks) {
    var track = localTracks[trackName];
    if (track) {
      track.stop();
      track.close();
      localTracks[trackName] = undefined;
    }
  }

  // Remove remote users and player views.
  remoteUsers = {};
  // Clear subscription queue
  subscriptionQueue = [];
  isProcessingSubscription = false;
  $("#remote-playerlist").html("");

  // leave the channel
  await client.leave();
  $("#local-player-name").text("");
  $("#join").attr("disabled", false);
  $("#leave").attr("disabled", true);
  $("#joined-setup").css("display", "none");
  console.log("client leaves channel success");
}

/*
 * Process subscription queue one at a time to prevent WebRTC negotiation conflicts
 */
async function processSubscriptionQueue() {
  if (isProcessingSubscription || subscriptionQueue.length === 0) {
    return;
  }

  isProcessingSubscription = true;

  while (subscriptionQueue.length > 0) {
    const { user, mediaType, resolve, reject } = subscriptionQueue.shift();
    const uid = user.uid;

    try {
      console.log(`Processing subscription for ${uid} ${mediaType}`);
      await client.subscribe(user, mediaType);
      console.log(`subscribe success for ${uid} ${mediaType}`);

      if (mediaType === "video") {
        const playerWidth =
          uid === 1001 ? "540px" : uid === 1000 ? "1024px" : "auto";
        const playerHeight =
          uid === 1001 ? "360px" : uid === 1000 ? "576px" : "auto";

        // Check if player already exists
        if ($(`#player-wrapper-${uid}`).length === 0) {
          const player = $(`
            <div id="player-wrapper-${uid}">
              <p class="player-name">(${uid})</p>
              <div id="player-${uid}" class="player" style="width: ${playerWidth}; height: ${playerHeight};"></div>
            </div>
          `);
          $("#remote-playerlist").append(player);
        }
        user.videoTrack.play(`player-${uid}`);

        // Check if captured frame div already exists
        if ($(`#captured-frame-${uid}`).length === 0) {
          const capturedFrameDiv = $(`
            <div id="captured-frame-${uid}" style="width: ${playerWidth}; height: ${playerHeight}; display: ${
            DEBUG_MODE ? "block" : "none"
          };">
              <p>Captured Frames (${uid})</p>
              <img id="captured-image-${uid}" style="width: 100%; height: 100%; object-fit: contain;">
              <button id="download-frame-${uid}" class="btn btn-primary mt-2">Download Frame</button>
              <button id="download-base64-${uid}" class="btn btn-secondary mt-2 ml-2">Download Base64</button>
            </div>
          `);
          $("#captured-frames").append(capturedFrameDiv);
        }

        user.videoTrack.captureEnabled = true;
      }
      if (mediaType === "audio") {
        user.audioTrack.play();
      }

      resolve();
    } catch (error) {
      console.error(`subscribe error for ${uid} ${mediaType}:`, error);
      reject(error);
    }

    // Small delay between subscriptions to let WebRTC settle
    await new Promise(r => setTimeout(r, 100));
  }

  isProcessingSubscription = false;
}

/*
 * Add the local use to a remote channel.
 * Queues subscriptions to process them sequentially.
 *
 * @param  {IAgoraRTCRemoteUser} user - The {@link  https://docs.agora.io/en/Voice/API%20Reference/web_ng/interfaces/iagorartcremoteuser.html| remote user} to add.
 * @param {trackMediaType - The {@link https://docs.agora.io/en/Voice/API%20Reference/web_ng/interfaces/itrack.html#trackmediatype | media type} to add.
 */
async function subscribe(user, mediaType) {
  return new Promise((resolve, reject) => {
    subscriptionQueue.push({ user, mediaType, resolve, reject });
    processSubscriptionQueue();
  });
}

/*
 * Add a user who has subscribed to the live channel to the local interface.
 *
 * @param  {IAgoraRTCRemoteUser} user - The {@link  https://docs.agora.io/en/Voice/API%20Reference/web_ng/interfaces/iagorartcremoteuser.html| remote user} to add.
 * @param {trackMediaType - The {@link https://docs.agora.io/en/Voice/API%20Reference/web_ng/interfaces/itrack.html#trackmediatype | media type} to add.
 */
async function handleUserPublished(user, mediaType) {
  const id = user.uid;
  remoteUsers[id] = user;
  try {
    await subscribe(user, mediaType);
  } catch (error) {
    console.error(`Failed to subscribe to user ${id} ${mediaType}:`, error);
  }
}

/*
 * Remove the user specified from the channel in the local interface.
 *
 * @param  {string} user - The {@link  https://docs.agora.io/en/Voice/API%20Reference/web_ng/interfaces/iagorartcremoteuser.html| remote user} to remove.
 */
function handleUserUnpublished(user, mediaType) {
  if (mediaType === "video") {
    const id = user.uid;
    delete remoteUsers[id];
    $(`#player-wrapper-${id}`).remove();
  }
}
function getCodec() {
  var radios = document.getElementsByName("radios");
  var value;
  for (var i = 0; i < radios.length; i++) {
    if (radios[i].checked) {
      value = radios[i].value;
    }
  }
  return value;
}

function isVideoTrackCapturable(videoTrack) {
  if (!videoTrack || !videoTrack.getMediaStreamTrack) {
    return true;
  }
  const media = videoTrack.getMediaStreamTrack();
  if (!media) {
    return false;
  }
  if (media.muted || media.readyState !== "live") {
    return false;
  }
  return true;
}

async function captureFrameAsBase64(videoTrack) {
  try {
    const frame = await videoTrack.getCurrentFrameData();
    if (
      !frame ||
      typeof frame.width !== "number" ||
      typeof frame.height !== "number" ||
      frame.width < 1 ||
      frame.height < 1
    ) {
      return null;
    }
    const canvas = document.createElement("canvas");
    canvas.width = frame.width;
    canvas.height = frame.height;
    const ctx = canvas.getContext("2d");
    ctx.putImageData(frame, 0, 0);
    return canvas.toDataURL(
      `image/${window.imageParams["imageFormat"]}`,
      window.imageParams["imageQuality"]
    );
  } catch (e) {
    console.warn("captureFrameAsBase64:", e && e.message ? e.message : e);
    return null;
  }
}

// Add at the beginning of the file
const DEBUG_MODE = false;
const lastBase64Frames = {};

// Function to get the latest base64 frame for a specific UID
async function getLastBase64Frame(uid) {
  const user = remoteUsers[uid];
  if (!user || !user.videoTrack || !user.videoTrack.captureEnabled) {
    return null;
  }
  if (!isVideoTrackCapturable(user.videoTrack)) {
    return null;
  }

  const base64Frame = await captureFrameAsBase64(user.videoTrack);
  lastBase64Frames[uid] = base64Frame;
  return base64Frame;
}

function initializeImageParams({ imageFormat, imageQuality }) {
  window.imageParams = { imageFormat, imageQuality };
}
window.initializeImageParams = initializeImageParams;
window.getLastBase64Frame = getLastBase64Frame;

let rtcJoinPromise = null;

async function triggerRtcJoin() {
  if (client && client.connectionState === "CONNECTED") {
    return true;
  }
  if (rtcJoinPromise) {
    return rtcJoinPromise;
  }

  rtcJoinPromise = (async () => {
    if (!client) {
      const joinForm = document.getElementById("join-form");
      if (joinForm) {
        const event = new Event("submit", { cancelable: true, bubbles: true });
        joinForm.dispatchEvent(event);
      } else {
        return false;
      }
    }

    const startedAt = Date.now();
    while (Date.now() - startedAt < 10000) {
      if (client && client.connectionState === "CONNECTED") {
        return true;
      }
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    return false;
  })();

  try {
    return await rtcJoinPromise;
  } finally {
    rtcJoinPromise = null;
  }
}
window.triggerRtcJoin = triggerRtcJoin;

async function ensureRtcReady(timeoutMs = 12000) {
  const joined = await triggerRtcJoin();
  if (!joined) {
    return false;
  }

  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    const frontUser = remoteUsers[1000];
    const connected = Boolean(client && client.connectionState === "CONNECTED");
    const hasFrontVideo = Boolean(frontUser && frontUser.videoTrack);
    if (connected && hasFrontVideo) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }

  return false;
}
window.ensureRtcReady = ensureRtcReady;

function pickRoverAudioTrack() {
  const preferred = remoteUsers[1000];
  if (preferred && preferred.audioTrack) {
    return preferred.audioTrack;
  }
  for (const user of Object.values(remoteUsers)) {
    if (user && user.audioTrack) {
      return user.audioTrack;
    }
  }
  return null;
}

function pickRecorderMimeType() {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/ogg",
  ];
  for (const type of candidates) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(type)) {
      return type;
    }
  }
  return "";
}

function blobToDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error || new Error("Failed to read blob"));
    reader.readAsDataURL(blob);
  });
}

async function recordRoverAudio(durationMs = 4000) {
  const ready = await ensureRtcReady();
  if (!ready) {
    return null;
  }

  const roverAudioTrack = pickRoverAudioTrack();
  if (!roverAudioTrack || !roverAudioTrack.getMediaStreamTrack) {
    return null;
  }

  const mediaTrack = roverAudioTrack.getMediaStreamTrack();
  if (!mediaTrack) {
    return null;
  }

  const clonedTrack = mediaTrack.clone ? mediaTrack.clone() : mediaTrack;
  const stream = new MediaStream([clonedTrack]);
  const mimeType = pickRecorderMimeType();
  const chunks = [];

  let recorder;
  try {
    recorder = mimeType
      ? new MediaRecorder(stream, { mimeType })
      : new MediaRecorder(stream);
  } catch (error) {
    console.warn("recordRoverAudio MediaRecorder init failed:", error);
    stream.getTracks().forEach((t) => t.stop());
    return null;
  }

  recorder.ondataavailable = (event) => {
    if (event.data && event.data.size > 0) {
      chunks.push(event.data);
    }
  };

  const stopped = new Promise((resolve) => {
    recorder.onstop = resolve;
  });

  recorder.start();
  await new Promise((resolve) => setTimeout(resolve, Math.max(300, Number(durationMs) || 4000)));
  recorder.stop();
  await stopped;

  stream.getTracks().forEach((t) => t.stop());
  if (!chunks.length) {
    return null;
  }

  const blob = new Blob(chunks, { type: mimeType || "audio/webm" });
  try {
    return await blobToDataUrl(blob);
  } catch (error) {
    console.warn("recordRoverAudio blob conversion failed:", error);
    return null;
  }
}
window.recordRoverAudio = recordRoverAudio;

/*
 * Toggle mute/unmute for remote audio tracks (rover stream).
 * Does not affect browser-generated audio (e.g. TTS playback).
 */
var remoteAudioMuted = false;
function toggleRemoteAudio() {
  remoteAudioMuted = !remoteAudioMuted;
  Object.values(remoteUsers).forEach((user) => {
    if (user.audioTrack) {
      user.audioTrack.setVolume(remoteAudioMuted ? 0 : 100);
    }
  });
  var btn = document.getElementById("mute-remote");
  if (btn) {
    btn.textContent = remoteAudioMuted
      ? "Unmute Remote Audio"
      : "Mute Remote Audio";
    btn.classList.toggle("btn-secondary", !remoteAudioMuted);
    btn.classList.toggle("btn-danger", remoteAudioMuted);
  }
}
window.toggleRemoteAudio = toggleRemoteAudio;

/*
 * Unique URL for Agora: SDK caches decoded buffers by string URL — same path + overwrite = stale/wrong audio.
 */
function ttsCacheBustUrl(audioUrl) {
  try {
    const u = new URL(audioUrl, window.location.origin);
    u.searchParams.set("_ts", String(Date.now()));
    return u.toString();
  } catch (e) {
    const sep = audioUrl.indexOf("?") >= 0 ? "&" : "?";
    return `${audioUrl}${sep}_ts=${Date.now()}`;
  }
}

var playAudioToRoverChain = Promise.resolve();

async function playAudioToRoverImpl(audioUrl) {
  const sourceUrl = ttsCacheBustUrl(audioUrl);
  console.log("[playAudioToRover] fetching audio:", sourceUrl);
  const audioTrack = await AgoraRTC.createBufferSourceAudioTrack({
    source: sourceUrl,
  });
  console.log("[playAudioToRover] track created, duration:", audioTrack.duration, "clientState:", client && client.connectionState);

  let published = false;
  let failSafe;
  let onState;
  try {
    await client.publish(audioTrack);
    published = true;
    console.log("[playAudioToRover] published OK");

    let sawPlaying = false;
    const playbackDone = new Promise((resolve) => {
      const ms = Math.ceil((audioTrack.duration || 3) * 1000) + 1500;
      console.log("[playAudioToRover] failsafe in", ms, "ms, duration:", audioTrack.duration);
      failSafe = setTimeout(() => {
        console.log("[playAudioToRover] failsafe fired, sawPlaying:", sawPlaying);
        if (onState) {
          audioTrack.off("source-state-change", onState);
        }
        resolve();
      }, Math.max(ms, 1500));

      onState = (state) => {
        console.log("[playAudioToRover] state-change:", state, "sawPlaying:", sawPlaying);
        if (state === "playing") {
          sawPlaying = true;
        } else if (state === "stopped" && sawPlaying) {
          clearTimeout(failSafe);
          audioTrack.off("source-state-change", onState);
          resolve();
        }
      };
      audioTrack.on("source-state-change", onState);
    });

    audioTrack.startProcessAudioBuffer();
    await playbackDone;
  } finally {
    if (failSafe) {
      clearTimeout(failSafe);
    }
    if (onState) {
      try {
        audioTrack.off("source-state-change", onState);
      } catch (e) {
        /* ignore */
      }
    }
    if (published) {
      try {
        await client.unpublish(audioTrack);
      } catch (e) {
        console.warn("playAudioToRover unpublish:", e);
      }
    }
    try {
      audioTrack.close();
    } catch (e) {
      console.warn("playAudioToRover close:", e);
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  return "done";
}

/*
 * Play an audio file through the Agora RTC channel so it reaches the rover's speaker.
 * Uses Agora's BufferSourceAudioTrack (not raw MediaStreamDestination + createCustomAudioTrack):
 * the latter often encodes silence in Chromium/headless because Web Audio → MediaStream → WebRTC
 * is unreliable; the buffer-source track uses the SDK's internal path.
 *
 * Serialized: overlapping calls would fight over one client.publish / unpublish cycle.
 *
 * @param {string} audioUrl - URL to the audio file (served from /static/)
 */
async function playAudioToRover(audioUrl) {
  const prev = playAudioToRoverChain;
  let resolveNext;
  playAudioToRoverChain = new Promise(function (resolve) {
    resolveNext = resolve;
  });
  await prev;
  try {
    return await playAudioToRoverImpl(audioUrl);
  } finally {
    resolveNext();
  }
}
window.playAudioToRover = playAudioToRover;
