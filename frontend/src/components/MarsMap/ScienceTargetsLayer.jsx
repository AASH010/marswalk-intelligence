
import { CircleMarker, Tooltip } from "react-leaflet";

function ScienceTargetsLayer({
  targets = [],
  enabled = true,
  selectedId,
  onSelect,
}) {
  if (!enabled) return null;

  return targets.map((target) => {
    const selected = target.id === selectedId;
    const score = Number(target.science_value ?? 0);

    return (
      <CircleMarker
        key={target.id}
        center={[target.y, target.x]}
        radius={selected ? 9 : 5 + score * 2}
        pathOptions={{
          color: selected ? "#ffffff" : "#c7a6ff",
          fillColor: "#9b6cff",
          fillOpacity: selected ? 1 : 0.65 + score * 0.3,
          weight: selected ? 2.5 : 1.5,
        }}
        eventHandlers={{
          click: () => onSelect?.(target),
        }}
      >
        <Tooltip direction="top" offset={[0, -5]}>
          <strong>{target.name}</strong>
          <br />
          Science ranking: {(score * 100).toFixed(1)}
          <br />
          CRISM Spectral Candidate
        </Tooltip>
      </CircleMarker>
    );
  });
}

export default ScienceTargetsLayer;
