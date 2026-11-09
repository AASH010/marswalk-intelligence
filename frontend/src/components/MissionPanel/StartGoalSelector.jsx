function Coordinate({ label, point }) {
  return (
    <div className="coordinate-block">
      <span>{label}</span>
      {point ? (
        <code>{Math.round(point.x).toLocaleString()} / {Math.round(point.y).toLocaleString()}</code>
      ) : (
        <code className="muted">NOT SET</code>
      )}
    </div>
  );
}

function StartGoalSelector({ start, goal, selectionMode, onSelectionMode }) {
  return (
    <div className="mission-fields">
      <Coordinate label="START" point={start} />
      <Coordinate label="GOAL" point={goal} />
      <div className="selection-actions">
        <button className={selectionMode === "start" ? "active" : ""} onClick={() => onSelectionMode("start")}>SET START</button>
        <button className={selectionMode === "goal" ? "active" : ""} onClick={() => onSelectionMode("goal")}>SET GOAL</button>
      </div>
    </div>
  );
}

export default StartGoalSelector;
