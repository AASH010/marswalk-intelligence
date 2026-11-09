import { useState } from "react";

const MODES = ["fastest", "balanced", "conservative"];

function value(v, digits = 2) {
  return Number.isFinite(Number(v)) ? Number(v).toFixed(digits) : "—";
}

function RouteComparison({ routes = {}, activeMode, onCompare, loading }) {
  const [expanded, setExpanded] = useState(false);
  const available = MODES.filter((mode) => routes[mode]);

  return (
    <section className="comparison-block">
      <div className="section-heading">
        <span>ROUTE COMPARISON</span>
        <em>{available.length}/3</em>
      </div>
      <p className="comparison-copy">
        Compare terrain-only route modes after generation. Science opportunities and EVA estimates remain independent outputs.
      </p>
      <button className="secondary-action" type="button" onClick={() => onCompare?.()} disabled={loading}>
        {loading ? "CALCULATING…" : available.length === 3 ? "REFRESH ROUTE SET" : "COMPARE THREE MODES"}
      </button>

      {available.length > 0 && (
        <div className="comparison-table-wrap">
          <table className="comparison-table">
            <thead>
              <tr><th>MODE</th><th>DIST.</th><th>TERRAIN</th></tr>
            </thead>
            <tbody>
              {available.map((mode) => (
                <tr className={mode === activeMode ? "active" : ""} key={mode}>
                  <td>{mode}</td>
                  <td>{value(routes[mode].distance_km)} km</td>
                  <td>{value(routes[mode].average_raw_terrain_cost)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <button className="text-action" type="button" onClick={() => setExpanded((v) => !v)}>
            {expanded ? "HIDE ROUTE NOTES" : "VIEW ROUTE NOTES"}
          </button>
          {expanded && (
            <div className="comparison-notes">
              {available.map((mode) => (
                <div key={mode}>
                  <strong>{mode.toUpperCase()}</strong>
                  <span>{routes[mode].description || "Terrain-derived navigation result."}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

export default RouteComparison;
