let map;
let marker;

function initMap() {
  let initialLat = 0;
  let initialLng = 0;
  let zoomLevel = 2;
  let checkpointsList = window.checkpointsList;
  if (checkpointsList && checkpointsList.length > 0) {
    initialLat = parseFloat(checkpointsList[0].latitude);
    initialLng = parseFloat(checkpointsList[0].longitude);
    zoomLevel = window.mapZoomLevel;
  }

  map = L.map("map").setView([initialLat, initialLng], zoomLevel);

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
}

// Define updateMarker globally
window.updateMarker = function (latitude, longitude) {
  const position = [latitude, longitude];
  if (!marker) {
    const mainMarkerEl = document.createElement("div");
    mainMarkerEl.className = "main-marker";
    mainMarkerEl.style.backgroundColor = "red";
    mainMarkerEl.style.border = "2px solid white";
    mainMarkerEl.style.borderRadius = "50%";
    mainMarkerEl.style.width = "20px";
    mainMarkerEl.style.height = "20px";

    marker = L.marker(position, {
      icon: L.divIcon({
        className: "main-marker-icon",
        html: mainMarkerEl.outerHTML,
        iconSize: [20, 20],
        iconAnchor: [10, 10],
      }),
      zIndexOffset: 1000,
    }).addTo(map);
    map.setView(position, window.mapZoomLevel);
    return;
  }

  const previous = marker.getLatLng();
  if (
    Math.abs(previous.lat - latitude) > 1e-7 ||
    Math.abs(previous.lng - longitude) > 1e-7
  ) {
    marker.setLatLng(position);
    map.panTo(position, { animate: false });
  }
};

document.addEventListener("DOMContentLoaded", function () {
  initMap();
  addCheckpointsToMap();
});

function addCheckpointsToMap() {
  const groupedCheckpoints = {};
  checkpointsList.forEach((checkpoint) => {
    const key = `${checkpoint.latitude},${checkpoint.longitude}`;
    if (!groupedCheckpoints[key]) {
      groupedCheckpoints[key] = [];
    }
    groupedCheckpoints[key].push(checkpoint);
  });

  Object.entries(groupedCheckpoints).forEach(([coords, checkpoints]) => {
    const [latitude, longitude] = coords.split(',').map(parseFloat);

    const el = document.createElement("div");
    el.className = "custom-marker";
    el.style.backgroundColor = "green";
    el.style.color = "white";
    el.style.borderRadius = "5%";
    el.style.display = "flex";
    el.style.justifyContent = "center";
    el.style.alignItems = "center";
    el.style.padding = "4px";
    el.style.minWidth = "18px";
    el.style.minHeight = "18px";

    const checkpointNumber = document.createElement("div");
    checkpointNumber.className = "checkpoint-number";
    const sequences = checkpoints.map(cp => cp.sequence).join('/');
    checkpointNumber.innerHTML = sequences;
    checkpointNumber.style.fontSize = checkpoints.length > 1 ? "12px" : "14px";
    checkpointNumber.style.fontWeight = "bold";
    el.appendChild(checkpointNumber);

    const marker = L.marker([latitude, longitude], {
      icon: L.divIcon({
        className: "custom-marker",
        html: el.outerHTML,
        iconSize: null
      })
    }).addTo(map);

    const popupContent = checkpoints.map(cp => `Checkpoint ${cp.sequence}`).join('<br>');
    marker.bindPopup(popupContent);
  });
}
