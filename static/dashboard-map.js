// Leaflet map for the dashboard: rover marker with heading arrow, breadcrumb
// trail, and checkpoint markers. Exposes window.DashMap.
(function () {
  var MAX_TRAIL_POINTS = 500;

  var map = null;
  var roverMarker = null;
  var roverArrow = null;
  var trail = null;
  var follow = true;
  var suppressMoveEvent = false;

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

  function drawCheckpoints(checkpoints) {
    // Group checkpoints that share identical coordinates into one marker.
    var grouped = {};
    checkpoints.forEach(function (cp) {
      var key = cp.latitude + "," + cp.longitude;
      if (!grouped[key]) grouped[key] = [];
      grouped[key].push(cp.sequence);
    });

    Object.keys(grouped).forEach(function (key) {
      var parts = key.split(",");
      var icon = L.divIcon({
        className: "",
        html:
          '<div class="checkpoint-icon">' + grouped[key].join(",") + "</div>",
        iconSize: [24, 24],
        iconAnchor: [12, 12],
      });
      L.marker([parseFloat(parts[0]), parseFloat(parts[1])], {
        icon: icon,
      }).addTo(map);
    });
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
    points.push(L.latLng(lat, lng));
    if (points.length > MAX_TRAIL_POINTS) points.shift();
    trail.setLatLngs(points);

    if (follow) {
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
  };
})();
