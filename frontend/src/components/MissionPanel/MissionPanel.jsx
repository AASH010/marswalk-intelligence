import StartGoalSelector from "./StartGoalSelector";
import RouteModeSelector from "./RouteModeSelector";

function MissionPanel({ start, goal, selectionMode, onSelectionMode, mode, onModeChange, onPlan, loading }) {
  return (
    <section className="mission-panel">
      <div className="panel-kicker">MISSION</div>
      <div className="panel-title">PLAN MARSWALK</div>
      <p className="panel-description">Define a traverse, inspect the terrain, then review independent science opportunities.</p>

      <StartGoalSelector
        start={start}
        goal={goal}
        selectionMode={selectionMode}
        onSelectionMode={onSelectionMode}
      />

      <div className="field-label">ROUTE MODE</div>
      <RouteModeSelector mode={mode} onChange={onModeChange} />

      <button className="primary-action" disabled={!start || !goal || loading} onClick={onPlan}>
        {loading ? "PLANNING…" : "PLAN TERRAIN ROUTE"}
      </button>
    </section>
  );
}

export default MissionPanel;
