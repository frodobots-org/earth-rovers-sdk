// Loaded only by the /sdk page. Forwards every RTM telemetry message to the
// SDK server over /ws/ingest so it can be cached and fanned out to dashboard
// clients without extra page.evaluate round-trips.
(function () {
  var rtmTokenInput = document.getElementById("rtm_token");
  var ingestTokenInput = document.getElementById("ingest_token");
  if (!rtmTokenInput || !rtmTokenInput.value) return;
  if (!ingestTokenInput || !ingestTokenInput.value) return;

  var ws = null;
  var retryDelay = 1000;

  function connect() {
    ws = new WebSocket(
      (location.protocol === "https:" ? "wss://" : "ws://") +
        location.host +
        "/ws/ingest?token=" +
        encodeURIComponent(ingestTokenInput.value)
    );
    ws.onopen = function () {
      retryDelay = 1000;
    };
    ws.onclose = function () {
      setTimeout(connect, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 10000);
    };
    ws.onerror = function () {
      ws.close();
    };
  }
  connect();

  document.addEventListener("message-from-peer", function (event) {
    if (ws && ws.readyState === WebSocket.OPEN && event.detail) {
      // Local delivery should stay near-zero, but never build an unbounded
      // browser queue if the server is temporarily overloaded.
      if (ws.bufferedAmount > 64 * 1024) return;
      ws.send(JSON.stringify(event.detail));
    }
  });
})();
