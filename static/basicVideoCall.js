var client; // Agora client
var localTracks = {
  videoTrack: null,
  audioTrack: null
};
var remoteUsers = {};
// Agora client options
var options = {
  appid: null,
  channel: null,
  uid: null,
  token: null,
  role: null
};

var rtm = {
  clientRtm: null,
  channelRtm: null,
  localInvitation: null
};

//uid for audience
var random = Math.floor( Math.random() * 99999 ) + 1;
//uid for host
var host = 55555;


// the demo can auto join channel with params in url
$(() => {
  var urlParams = new URL(location.href).searchParams;
  options.appid = urlParams.get("appid");
  options.channel = urlParams.get("channel");
  options.token = urlParams.get("token");
  if (options.appid && options.channel) {
    $("#appid").val(options.appid);
    $("#token").val(options.token);
    $("#channel").val(options.channel);
  }
})

$("#join-form").submit(async function (e) {
  e.preventDefault();
  $("#join").attr("disabled", true);
  try {
    options.appid = $("#appid").val();
    options.token = $("#token").val();
    options.channel = $("#channel").val();
    options.role = $('input[name="role"]:checked').val();
console.log("options.role" + options.role);
    (options.role == "host") ? options.uid = host : options.uid = random;

    if (options.role == "host"){
      //RTC Join
      await join();
      //RTM Join
      await joinRtm();
    }else{
      //RTM Join
      await joinRtm();
    }
    if(options.token) {
      $("#success-alert-with-token").css("display", "block");
    } else {
      $("#success-alert a").attr("href", `index.html?appid=${options.appid}&channel=${options.channel}&token=${options.token}`);
      $("#success-alert").css("display", "block");
    }
  } catch (error) {
    console.error(error);
  } finally {
    $("#leave").attr("disabled", false);
  }
})


$("#leave").click(function (e) {
  leave();
  leaveRtm();
})

$("#invitation").click(function (e) {
  console.log("invitation")
  invitation();
})

function leaveRtm(){

  rtm.channelRtm.leave(function(){
    console.log("AgoraRTM channel leave success");
  });

  rtm.clientRtm.logout(function(){
    console.log("AgoraRTM client logout success");
  });
}


async function joinRtm () {
  rtm.clientRtm = await AgoraRTM.createInstance(options.appid);
  rtm.channelRtm = await rtm.clientRtm.createChannel(options.channel);
  await receiveFromPeerMessage();
  await receiveChannelMessage();
  rtm.clientRtm.on("ConnectionStateChange", handleConnectionStateChange);

  Promise.resolve()
  .then(loginRtm)
  .then(joinnRtm)
  .then(function(value){
    return new Promise(function (resolve, reject) {
      if (options.uid != host){
        request();
      }
      resolve();
    });
  });

}

function loginRtm (){
  return new Promise(function (resolve, reject) {
      rtm.clientRtm.login({uid: "" + options.uid}).then(function(){
      console.log("AgoraRTM client login success");
      resolve('loginRtm Success');
    }).catch(function(err){
      console.log("AgoraRTM client login failure, ", err);
    });
  });
}

function joinnRtm () {
  return new Promise(function (resolve, reject) {
      rtm.channelRtm.join().then(function(){
      console.log("AgoraRTM client join success");
      resolve('joinnRtm Success');
    }).catch(function (err){
      console.log("AgoraRTM client join failure, ", err);
    });
  });
}

function onRejected(error) {
  console.log("error = " + error);
}

function handleConnectionStateChange(newState, reason) {
  console.log("on connection state changed to " + newState + " reason:" + reason);
}

function setDispMessage(localMessage){
  currentMessage = $("#messageDisp").val();
  $("#messageDisp").val(currentMessage + localMessage + "\n")
}

function receiveFromPeerMessage(){
  rtm.clientRtm.on('MessageFromPeer', function (sentMessage, senderId) {
    console.log("AgoraRTM client got message: " + JSON.stringify(sentMessage) + " from " + senderId);

    setDispMessage(sentMessage.text);
    console.log((sentMessage.text == senderId + ":requested") && (options.uid == host));
    if ((sentMessage.text == senderId + ":requested") && (options.uid == host)){
       var res = confirm("Are you sure " + senderId + " to be joined?");
      (res == true) ? permit(senderId) : deny(senderId);
      return;
    }

    console.log(sentMessage.text == options.uid + ":permitted");
    if (sentMessage.text == options.uid + ":permitted"){
      join();
      return;
    }

    console.log(sentMessage.text == options.uid + ":denied");
    if (sentMessage.text == options.uid + ":denied"){
      alert("Please wait for a while as we will invite you from the host.");
      return;
    }

  });

  rtm.clientRtm.on('RemoteInvitationReceived', function (remoteInvitation) {
  console.log(remoteInvitation.callerId);
    var res = confirm("You got an invitation from the host. Do you want to enter the room?");
    if (res == true){
      remoteInvitation.accept();
      join();
    }else{
      remoteInvitation.refuse();

    }
  });
}

function receiveChannelMessage(){

  rtm.channelRtm.on('MemberJoined', memberId => {
    console.log("AgoraRTM client is joined member: " + memberId);
    add(memberId);
  });

  rtm.channelRtm.on('MemberLeft', memberId => {
    console.log("AgoraRTM client is left member: " + memberId);
    remove(memberId);
  });
 }


function add (id) {
  console.log("add: " + id);
  $('<option/>', {
   value: id,
   text: id,
  }).appendTo("#audienceId");
}

function remove (id) {
  console.log("remove: " + id);
  $('select#audienceId option[value=' + id + ']').remove();
}

function request () {
  sendMessageToPeer(prepMessage("requested",options.uid),host);
}

function permit (id) {
  sendMessageToPeer(prepMessage("permitted",id),id);
}

function deny (id) {
  sendMessageToPeer(prepMessage("denied",id),id);
}

async function invitation () {
  rtm.localInvitation = await rtm.clientRtm.createLocalInvitation($("#audienceId").val());
  rtm.localInvitation.on("LocalInvitationAccepted", handleLocalInvitationAccepted);
  rtm.localInvitation.on("LocalInvitationRefused", handleLocalInvitationRefused);
  await rtm.localInvitation.send();
}


function prepMessage(msg,id){
  return id + ":" + msg;
}

function sendMessageToPeer(localMessage,id){
  setDispMessage(localMessage);
  rtm.clientRtm.sendMessageToPeer({text:localMessage}, id+"").then(function(){
    console.log("AgoraRTM client succeed in sending peer message: " + localMessage);
  }).catch(function(err){
    console.log("AgoraRTM client failed to sending role" + err);
  });
}

async function join() {
  // create Agora client
  client = AgoraRTC.createClient({ mode: "rtc", codec: "h264" });

  // add event listener to play remote tracks when remote user publishs.
  client.on("user-published", handleUserPublished);
  client.on("user-unpublished", handleUserUnpublished);

  // join a channel and create local tracks, we can use Promise.all to run them concurrently
  [ options.uid, localTracks.audioTrack, localTracks.videoTrack ] = await Promise.all([
    // join the channel
    client.join(options.appid, options.channel, options.token || null,options.uid),
    // create local tracks, using microphone and camera
    AgoraRTC.createMicrophoneAudioTrack(),
    AgoraRTC.createCameraVideoTrack()
  ]);
  
  // play local video track
  localTracks.videoTrack.play("local-player");
  $("#local-player-name").text(`localVideo(${options.uid})`);

  // publish local tracks to channel
  await client.publish(Object.values(localTracks));
  console.log("publish success");
}

async function leave() {
  for (trackName in localTracks) {
    var track = localTracks[trackName];
    if(track) {
      track.stop();
      track.close();
      localTracks[trackName] = undefined;
    }
  }

  // remove remote users and player views
  remoteUsers = {};
  $("#remote-playerlist").html("");

  // leave the channel
  await client.leave();

  $("#local-player-name").text("");
  $("#join").attr("disabled", false);
  $("#leave").attr("disabled", true);
  console.log("client leaves channel success");
}

async function subscribe(user, mediaType) {
  const uid = user.uid;
  // subscribe to a remote user
  await client.subscribe(user, mediaType);
  console.log("subscribe success");
  if (mediaType === 'video') {
    const player = $(`
      <div id="player-wrapper-${uid}">
        <p class="player-name">remoteUser(${uid})</p>
        <div id="player-${uid}" class="player"></div>
      </div>
    `);
    $("#remote-playerlist").append(player);
    user.videoTrack.play(`player-${uid}`);
  }
  if (mediaType === 'audio') {
    user.audioTrack.play();
  }
}

function handleUserPublished(user, mediaType) {
  console.log("handleUserPublished:" + user.uid);
  const id = user.uid;
  remoteUsers[id] = user;
  subscribe(user, mediaType);
}

function handleUserUnpublished(user) {
  console.log("handleUserUnpublished:" + user.uid);
  const id = user.uid;
  delete remoteUsers[id];
  $(`#player-wrapper-${id}`).remove();
}


function handleLocalInvitationAccepted(response) {
  console.log("handleLocalInvitationAccepted");
  console.log(response);
}

function handleLocalInvitationRefused(response) {
  console.log("handleLocalInvitationRefused");
  console.log(response);
}

