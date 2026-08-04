// Leaflet map for the dashboard: rover marker with heading arrow, breadcrumb
// trail, and checkpoint markers. Exposes window.DashMap.
(function () {
  var MAX_TRAIL_POINTS = 500;
  var CHECKPOINT_RADIUS_M = 15;

  var map = null;
  var roverMarker = null;
  var roverArrow = null;
  var trail = null;
  var follow = true;
  var suppressMoveEvent = false;
  var checkpointGroups = []; // [{latlng, items, marker}]
  var radiusGroup = null;
  var nextCheckpointSeq = null;

  var ROVER_SVG =
    '<svg width="34" height="34" viewBox="0 0 34 34" xmlns="http://www.w3.org/2000/svg">' +
    '<circle cx="17" cy="17" r="9" fill="#171e26" stroke="#ffb224" stroke-width="2"/>' +
    '<path d="M17 3 L22 13 L17 10.5 L12 13 Z" fill="#ffb224"/>' +
    "</svg>";

  function init(options) {
    var checkpoints = options.checkpoints || [];
    var zoom = options.zoom || 18;

    var center = [0, 0];
    var initialZoom = 2;
    if (checkpoints.length > 0) {
      center = [
        parseFloat(checkpoints[0].latitude),
        parseFloat(checkpoints[0].longitude),
      ];
      initialZoom = zoom;
    }

    map = L.map("map", { zoomControl: true }).setView(center, initialZoom);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors",
    }).addTo(map);

    trail = L.polyline([], {
      color: "#ffb224",
      weight: 3,
      opacity: 0.7,
    }).addTo(map);

    drawCheckpoints(checkpoints);

    // A drag by the user turns follow mode off.
    map.on("dragstart", function () {
      if (!suppressMoveEvent) {
        setFollow(false);
        if (typeof window.onFollowChanged === "function") {
          window.onFollowChanged(false);
        }
      }
    });

    return map;
  }

  function checkpointState(sequence) {
    if (nextCheckpointSeq === null || nextCheckpointSeq === undefined) {
      return "pending";
    }
    if (sequence < nextCheckpointSeq) return "done";
    if (sequence === nextCheckpointSeq) return "next";
    return "pending";
  }

  // Coinciding checkpoints share one marker as a segmented pill: every
  // checkpoint keeps its own number and its own state color.
  function checkpointIcon(items) {
    var STATE_LABEL = { done: "completed", next: "next up", pending: "pending" };
    var segments = items
      .map(function (cp) {
        var state = checkpointState(cp.sequence);
        return (
          '<span class="cp-seg ' + state + '">' +
          (state === "done" ? "✓" : "") + cp.sequence +
          "</span>"
        );
      })
      .join("");
    var hasNext = items.some(function (cp) {
      return checkpointState(cp.sequence) === "next";
    });
    var title = items
      .map(function (cp) {
        return (
          "Checkpoint " + cp.sequence + " — " +
          STATE_LABEL[checkpointState(cp.sequence)]
        );
      })
      .join("\n");
    var width = Math.max(26, items.length * 26);
    return L.divIcon({
      className: "",
      html:
        '<div class="cp-marker' + (hasNext ? " has-next" : "") +
        '" title="' + title + '">' + segments + "</div>",
      iconSize: [width, 24],
      iconAnchor: [width / 2, 12],
    });
  }

  function drawCheckpoints(checkpoints) {
    // Group checkpoints that share identical coordinates into one marker.
    var grouped = {};
    checkpoints.forEach(function (cp) {
      var key = cp.latitude + "," + cp.longitude;
      if (!grouped[key]) grouped[key] = [];
      grouped[key].push(cp);
    });

    radiusGroup = L.layerGroup();
    checkpointGroups = Object.keys(grouped).map(function (key) {
      var items = grouped[key].sort(function (a, b) {
        return a.sequence - b.sequence;
      });
      var latlng = L.latLng(
        parseFloat(items[0].latitude),
        parseFloat(items[0].longitude)
      );
      var marker = L.marker(latlng, { icon: checkpointIcon(items) }).addTo(map);
      radiusGroup.addLayer(
        L.circle(latlng, {
          radius: CHECKPOINT_RADIUS_M,
          color: "#6cb2e6",
          weight: 1.5,
          opacity: 0.7,
          fillColor: "#6cb2e6",
          fillOpacity: 0.12,
          interactive: false,
        })
      );
      return { latlng: latlng, items: items, marker: marker };
    });
  }

  function setCheckpointStates(nextSequence) {
    nextCheckpointSeq =
      nextSequence === undefined || nextSequence === null
        ? null
        : parseInt(nextSequence, 10);
    checkpointGroups.forEach(function (group) {
      group.marker.setIcon(checkpointIcon(group.items));
    });
  }

  function setRadiusVisible(visible) {
    if (!map || !radiusGroup) return;
    if (visible) {
      radiusGroup.addTo(map);
    } else {
      map.removeLayer(radiusGroup);
    }
  }

  function updateRover(lat, lng, headingDeg, zoom) {
    if (!map) return;
    var latlng = [lat, lng];

    if (!roverMarker) {
      var icon = L.divIcon({
        className: "rover-icon",
        html: ROVER_SVG,
        iconSize: [34, 34],
        iconAnchor: [17, 17],
      });
      roverMarker = L.marker(latlng, { icon: icon, zIndexOffset: 1000 }).addTo(
        map
      );
      roverArrow = roverMarker.getElement()
        ? roverMarker.getElement().querySelector("svg")
        : null;
      suppressMoveEvent = true;
      map.setView(latlng, zoom || map.getZoom());
      suppressMoveEvent = false;
    } else {
      roverMarker.setLatLng(latlng);
    }

    if (!roverArrow && roverMarker.getElement()) {
      roverArrow = roverMarker.getElement().querySelector("svg");
    }
    if (roverArrow && typeof headingDeg === "number" && !isNaN(headingDeg)) {
      roverArrow.style.transform = "rotate(" + headingDeg + "deg)";
    }

    var points = trail.getLatLngs();
    var last = points.length ? points[points.length - 1] : null;
    var positionChanged =
      !last || Math.abs(last.lat - lat) > 1e-7 || Math.abs(last.lng - lng) > 1e-7;
    if (positionChanged) {
      points.push(L.latLng(lat, lng));
      if (points.length > MAX_TRAIL_POINTS) points.shift();
      trail.setLatLngs(points);
    }

    if (follow && positionChanged) {
      suppressMoveEvent = true;
      map.panTo(latlng);
      suppressMoveEvent = false;
    }
  }

  function setFollow(value) {
    follow = !!value;
    if (follow && roverMarker) {
      suppressMoveEvent = true;
      map.panTo(roverMarker.getLatLng());
      suppressMoveEvent = false;
    }
  }

  window.DashMap = {
    init: init,
    updateRover: updateRover,
    setFollow: setFollow,
    setCheckpointStates: setCheckpointStates,
    setRadiusVisible: setRadiusVisible,
  };
})();
