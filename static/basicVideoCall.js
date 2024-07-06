async function initDevices() {
  mics = await AgoraRTC.getMicrophones();
  const audioTrackLabel = localTracks.audioTrack.getTrackLabel();
  currentMic = mics.find(item => item.label === audioTrackLabel);
  document.querySelector(".mic-input").value = currentMic.label;
  const micList = document.querySelector(".mic-list");
  micList.innerHTML = '';
  mics.forEach(mic => {
    const micItem = document.createElement('a');
    micItem.classList.add('dropdown-item');
    micItem.href = '#';
    micItem.textContent = mic.label;
    micList.appendChild(micItem);
  });

  cams = await AgoraRTC.getCameras();
  const videoTrackLabel = localTracks.videoTrack.getTrackLabel();
  currentCam = cams.find(item => item.label === videoTrackLabel);
  document.querySelector(".cam-input").value = currentCam.label;
  const camList = document.querySelector(".cam-list");
  camList.innerHTML = '';
  cams.forEach(cam => {
    const camItem = document.createElement('a');
    camItem.classList.add('dropdown-item');
    camItem.href = '#';
    camItem.textContent = cam.label;
    camList.appendChild(camItem);
  });
}

async function switchCamera(label) {
  currentCam = cams.find(cam => cam.label === label);
  document.querySelector(".cam-input").value = currentCam.label;
  await localTracks.videoTrack.setDevice(currentCam.deviceId);
}

async function switchMicrophone(label) {
  currentMic = mics.find(mic => mic.label === label);
  document.querySelector(".mic-input").value = currentMic.label;
  await localTracks.audioTrack.setDevice(currentMic.deviceId);
}

function initVideoProfiles() {
  videoProfiles.forEach(profile => {
    const profileItem = document.createElement('a');
    profileItem.classList.add('dropdown-item');
    profileItem.setAttribute('label', profile.label);
    profileItem.href = '#';
    profileItem.textContent = `${profile.label}: ${profile.detail}`;
    document.querySelector(".profile-list").appendChild(profileItem);
  });
  curVideoProfile = videoProfiles.find(item => item.label == '480p_1');
  document.querySelector(".profile-input").value = `${curVideoProfile.detail}`;
}

async function changeVideoProfile(label) {
  curVideoProfile = videoProfiles.find(profile => profile.label === label);
  document.querySelector(".profile-input").value = `${curVideoProfile.detail}`;
  if (localTracks.videoTrack) {
    await localTracks.videoTrack.setEncoderConfiguration(curVideoProfile.value);
  }
}

async function join() {
  client.on("user-published", handleUserPublished);
  client.on("user-unpublished", handleUserUnpublished);
  options.uid = await client.join(options.appid, options.channel, options.token || null, options.uid || null);
}

async function leave() {
  for (const trackName in localTracks) {
    const track = localTracks[trackName];
    if (track) {
      track.stop();
      track.close();
      localTracks[trackName] = null;
    }
  }

  remoteUsers = {};
  document.getElementById("remote-playerlist").innerHTML = "";

  await client.leave();
  document.getElementById("local-player-name").textContent = "";
  document.getElementById("join").disabled = false;
  document.getElementById("leave").disabled = true;
  document.getElementById("joined-setup").style.display = "none";
  console.log("client leaves channel success");
}

async function subscribe(user, mediaType) {
  const uid = user.uid;
  await client.subscribe(user, mediaType);
  console.log("subscribe success");
  if (mediaType === "video") {
    const playerWrapper = document.createElement('div');
    playerWrapper.id = `player-wrapper-${uid}`;
    playerWrapper.innerHTML = `
      <p class="player-name">(${uid})</p>
      <div id="player-${uid}" class="player"></div>
    `;
    document.getElementById("remote-playerlist").appendChild(playerWrapper);
    user.videoTrack.play(`player-${uid}`);
  }
  if (mediaType === "audio") {
    user.audioTrack.play();
  }
}

function handleUserPublished(user, mediaType) {
  const id = user.uid;
  remoteUsers[id] = user;
  subscribe(user, mediaType);
}

function handleUserUnpublished(user, mediaType) {
  if (mediaType === "video") {
    const id = user.uid;
    delete remoteUsers[id];
    const playerWrapper = document.getElementById(`player-wrapper-${id}`);
    if (playerWrapper) playerWrapper.remove();
  }
}

function getCodec() {
  const radios = document.getElementsByName("radios");
  let value;
  radios.forEach(radio => {
    if (radio.checked) {
      value = radio.value;
    }
  });
  return value;
}
