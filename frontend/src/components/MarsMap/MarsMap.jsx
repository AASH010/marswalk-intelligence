import { useEffect } from "react";
import { MapContainer, TileLayer, useMap } from "react-leaflet";
import "leaflet/dist/leaflet.css";

import { MarsCRS } from "../../utils/marsCRS";
import { MARS_MAP } from "../../utils/mapConfig";
import AOILayer from "./AOILayer";
import StartGoalLayer from "./StartGoalLayer";
import RouteLayer from "./RouteLayer";
import ScienceTargetsLayer from "./ScienceTargetsLayer";
import ScienceRasterLayer from "./ScienceRasterLayer";
import MapClickHandler from "./MapControls";

const CTX_BOUNDS = [
  [1042131.6352, 4329192.1081],
  [1143347.4242, 4418058.7893],
];

function FitCTX() {
  const map = useMap();
  useEffect(() => {
    map.fitBounds(CTX_BOUNDS, { padding: [20, 20], animate: false });
    map.setMinZoom(map.getZoom());
  }, [map]);
  return null;
}

function MarsMap({ layers, start, goal, route, scienceTargets, onMapClick, onScienceSelect, onRouteSelect, selectedTargetId }) {
  return (
    <MapContainer
      crs={MarsCRS}
      center={[MARS_MAP.center.y, MARS_MAP.center.x]}
      zoom={4}
      zoomSnap={0.25}
      minZoom={1}
      maxZoom={7}
      maxBounds={CTX_BOUNDS}
      maxBoundsViscosity={1}
      zoomControl
      style={{ width: "100%", height: "100%", background: "#111316" }}
    >
      <FitCTX />
      <MapClickHandler onMapClick={onMapClick} />

      {layers.ctx?.enabled && (
        <TileLayer
          url="/tiles/ctx/{z}/{x}/{y}.png"
          tileSize={256}
          minZoom={1}
          maxNativeZoom={7}
          maxZoom={7}
          updateWhenZooming={false}
          attribution="NASA / MarsWalk Intelligence"
          noWrap
          opacity={layers.ctx?.opacity ?? 1}
        />
      )}

      {layers.aoi?.enabled && <AOILayer />}
      <StartGoalLayer start={start} goal={goal} enabled={layers.startGoal?.enabled} />
      <RouteLayer route={route} enabled={layers.route?.enabled} onSelect={onRouteSelect} />
      <ScienceRasterLayer
  enabled={layers.crism?.enabled}
  opacity={layers.crism?.opacity ?? 0.45}
      />
      <ScienceTargetsLayer
        targets={scienceTargets}
        enabled={layers.scienceTargets?.enabled}
        selectedId={selectedTargetId}
        onSelect={onScienceSelect}
      />
    </MapContainer>
  );
}

export default MarsMap;
