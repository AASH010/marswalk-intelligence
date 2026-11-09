import { CircleMarker, Tooltip } from "react-leaflet";

function Point({ point, type }) {
  if (!point) return null;
  const isStart = type === "start";
  return (
    <CircleMarker
      center={[point.y, point.x]}
      radius={7}
      pathOptions={{
        color: isStart ? "#7ee787" : "#ff6b6b",
        fillColor: isStart ? "#7ee787" : "#ff6b6b",
        fillOpacity: 0.95,
        weight: 2,
      }}
    >
      <Tooltip direction="top" offset={[0, -7]}>
        <strong>{isStart ? "START" : "GOAL"}</strong>
        <br />
        X {Math.round(point.x).toLocaleString()}
        <br />
        Y {Math.round(point.y).toLocaleString()}
      </Tooltip>
    </CircleMarker>
  );
}

function StartGoalLayer({ start, goal, enabled = true }) {
  if (!enabled) return null;
  return <><Point point={start} type="start" /><Point point={goal} type="goal" /></>;
}

export default StartGoalLayer;
