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
    zoomLevel = 22;
  }

  map = L.map("map").setView([initialLat, initialLng], zoomLevel);

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
}

// Define updateMarker globally
window.updateMarker = function (latitude, longitude) {
  if (marker) {
    marker.setLatLng([latitude, longitude]);
  } else {
    const mainMarkerEl = document.createElement("div");
    mainMarkerEl.className = "main-marker";
    const antenna = document.createElement("div");
    antenna.className = "antenna";
    mainMarkerEl.appendChild(antenna);

    marker = L.marker([latitude, longitude], {
      icon: L.divIcon({ className: "main-marker" }),
    }).addTo(map);
  }
  map.setView([latitude, longitude], 22); // Set view with zoom level 22
};

document.addEventListener("DOMContentLoaded", function () {
  initMap();
  console.log(checkpointsList);
  addCheckpointsToMap();
});

function addCheckpointsToMap() {
  checkpointsList.forEach((checkpoint) => {
    const el = document.createElement("div");
    el.className = "custom-marker";
    el.style.backgroundColor = "green";
    el.style.color = "white";
    el.style.borderRadius = "50%";
    el.style.width = "18px";
    el.style.height = "18px";
    el.style.display = "flex";
    el.style.justifyContent = "center";
    el.style.alignItems = "center";

    const checkpointNumber = document.createElement("div");
    checkpointNumber.className = "checkpoint-number";
    checkpointNumber.innerHTML = checkpoint.sequence;
    checkpointNumber.style.fontSize = "14px";
    checkpointNumber.style.fontWeight = "bold";
    el.appendChild(checkpointNumber);

    const marker = L.marker(
      [parseFloat(checkpoint.latitude), parseFloat(checkpoint.longitude)],
      {
        icon: L.divIcon({ className: "custom-marker" }), // Usar la clase personalizada
      }
    ).addTo(map);

    const markerElement = marker.getElement();
    if (markerElement) {
      markerElement.appendChild(el);
    }
    marker.bindPopup(`Checkpoint ${checkpoint.sequence}`);
  });
}

