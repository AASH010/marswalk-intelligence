import { CircleMarker, Tooltip } from "react-leaflet";

function ScienceTargetsLayer({ targets = [], enabled = true, selectedId, onSelect }) {
  if (!enabled) return null;
  return targets.map((target) => {
    const selected = target.id === selectedId;
    return (
      <CircleMarker
        key={target.id}
        center={[target.y, target.x]}
        radius={selected ? 9 : 6}
        pathOptions={{
          color: selected ? "#f1d9ff" : "#c7a6ff",
          fillColor: "#9b6cff",
          fillOpacity: selected ? 0.95 : 0.72,
          weight: selected ? 2 : 1.5,
        }}
        eventHandlers={{ click: () => onSelect?.(target) }}
      >
        <Tooltip direction="top" offset={[0, -5]}>
          <strong>{target.id}</strong><br />{target.signal_name} browse signal
        </Tooltip>
      </CircleMarker>
    );
  });
}

export default ScienceTargetsLayer;
