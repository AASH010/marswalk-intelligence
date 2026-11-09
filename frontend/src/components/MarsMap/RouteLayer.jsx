import { Polyline, Tooltip } from "react-leaflet";

function RouteLayer({ route, enabled = true, onSelect }) {
  if (!enabled || !route?.coordinates?.length) return null;
  const positions = route.coordinates.map(([x, y]) => [y, x]);
  return (
    <Polyline
      positions={positions}
      pathOptions={{ color: "#65d9ff", weight: 4, opacity: 0.95 }}
      eventHandlers={{ click: () => onSelect?.(route) }}
    >
      <Tooltip sticky>Terrain-based navigation route · select to inspect</Tooltip>
    </Polyline>
  );
}

export default RouteLayer;
