import { useEffect, useState } from "react";
import RouteComparison from "./RouteComparison";
import ScienceOpportunitySummary from "./ScienceOpportunitySummary";

function Metric({ label, value, unit }) {
  return <div className="metric"><span>{label}</span><strong>{value ?? "—"}{unit ? <small>{unit}</small> : null}</strong></div>;
}

function InspectTabs({ tab, onChange }) {
  return (
    <div className="inspect-tabs" role="tablist" aria-label="Inspection views">
      {["summary", "evidence", "provenance"].map((item) => (
        <button key={item} className={tab === item ? "active" : ""} onClick={() => onChange(item)} type="button" role="tab" aria-selected={tab === item}>
          {item}
        </button>
      ))}
    </div>
  );
}

function TargetInspect({ target, tab }) {
  if (tab === "evidence") return <div className="evidence-block evidence-block--expanded"><div className="evidence-label">SCIENCE EVIDENCE</div><p>{target.signal_name} is a browse-derived CRISM signal. It is not a quantitative mineral abundance estimate and does not constitute mineral confirmation.</p><div className="evidence-callout">Interpret this target as a science opportunity for investigation, not as a confirmed mineral occurrence.</div></div>;
  if (tab === "provenance") return <div className="provenance-inspect"><div><span>PRODUCT</span><strong>{target.source || "CRISM browse product"}</strong></div><div><span>SIGNAL</span><strong>{target.signal_name}</strong></div><div><span>FRAME</span><strong>MARS PROJECTED XY</strong></div><div><span>INTERPRETATION</span><strong>BROWSE-DERIVED</strong></div></div>;
  return <><div className="inspect-id">{target.id}</div><div className="metrics-grid"><Metric label="Signal" value={Number(target.signal).toFixed(2)} /><Metric label="Area" value={target.area_m2} unit="m²" /><Metric label="Mars X" value={Math.round(target.x).toLocaleString()} /><Metric label="Mars Y" value={Math.round(target.y).toLocaleString()} /></div><div className="inspect-inline-status"><span>SCIENCE TYPE</span><strong>BROWSE SIGNAL</strong></div></>;
}

function ConsumablesPanel({ eva, inputs, onInput }) {
  return (
    <div className="consumables-panel">
      <div className="compact-list">
        <div><span>Duration</span><strong>{eva ? `${Number(eva.estimated_duration_hours).toFixed(2)} h` : "—"}</strong></div>
        <div><span>O₂ estimate</span><strong>{eva ? `${Math.round(eva.estimated_oxygen_liters).toLocaleString()} L` : "—"}</strong></div>
        <div><span>Capacity used</span><strong>{eva ? `${Number(eva.oxygen_percent_of_suit_capacity).toFixed(0)}%` : "—"}</strong></div>
        <div><span>Margin</span><strong>{eva ? `${Number(eva.safety_margin_hours).toFixed(2)} h` : "—"}</strong></div>
      </div>
      <div className="assumption-grid">
        <label><span>PAYLOAD</span><input type="number" min="0" step="1" value={inputs.payloadKg} onChange={(e) => onInput("payloadKg", e.target.value)} /><small>kg</small></label>
        <label><span>AVG. SLOPE</span><input type="number" min="0" step="0.5" value={inputs.averageSlopePercent} onChange={(e) => onInput("averageSlopePercent", e.target.value)} /><small>%</small></label>
      </div>
      <p className="model-note">Planning-model outputs only. Assumptions and provenance are explicit; these values are not operational flight limits.</p>
    </div>
  );
}

function RouteInspect({ route, eva, scienceTargets, tab, routeSet, comparisonLoading, onCompareRoutes, consumableInputs, onConsumableInput }) {
  if (tab === "evidence") return <div className="evidence-block evidence-block--expanded"><div className="evidence-label">NAVIGATION EVIDENCE</div><p>This route is generated from terrain/navigation criteria. Science opportunities are evaluated independently after route generation.</p><div className="evidence-callout">Science does not modify the A* terrain cost or route search.</div></div>;
  if (tab === "provenance") return <div className="provenance-inspect"><div><span>ROUTE MODE</span><strong>{route.mode?.toUpperCase() || "TERRAIN"}</strong></div><div><span>TERRAIN SOURCE</span><strong>CTX DEM · 20 m</strong></div><div><span>ROUTE MODEL</span><strong>A* TERRAIN SEARCH</strong></div><div><span>SCIENCE</span><strong>INDEPENDENT ANALYSIS</strong></div><div><span>EVA MODEL</span><strong>CONSUMABLES MODEL</strong></div></div>;
  return <>
    <div className="inspect-id">{route.mode?.toUpperCase() || "TERRAIN ROUTE"}</div>
    <div className="metrics-grid"><Metric label="Distance" value={Number(route.distance_km).toFixed(2)} unit="km" /><Metric label="Terrain cost" value={Number(route.average_raw_terrain_cost).toFixed(2)} /><Metric label="Optimization" value={Number(route.average_optimization_cost).toFixed(2)} /><Metric label="Science targets" value={scienceTargets.length} /></div>
    <ScienceOpportunitySummary route={route} targets={scienceTargets} />
    <div className="evidence-block"><div className="evidence-label">EVA & MISSION RESOURCES</div><ConsumablesPanel eva={eva} inputs={consumableInputs} onInput={onConsumableInput} /></div>
    <RouteComparison routes={routeSet} activeMode={route.mode} onCompare={onCompareRoutes} loading={comparisonLoading} />
  </>;
}

function MissionInsights({ route, scienceTargets, selectedTarget, selectedObject, eva, routeSet, comparisonLoading, onCompareRoutes, consumableInputs, onConsumableInput }) {
  const [tab, setTab] = useState("summary");
  const hasSelection = selectedTarget || selectedObject === "route";
  useEffect(() => setTab("summary"), [selectedTarget?.id, selectedObject]);
  return <aside className="insights-panel">
    <div className="panel-kicker">MISSION INSPECTOR</div>
    <section className="insight-section insight-section--inspect">
      {!hasSelection && <><div className="section-heading"><span>NO SELECTION</span><em>MAP</em></div><p className="empty-copy">Select a terrain route or science target on the map to inspect metrics, evidence, provenance, and mission-resource assumptions.</p></>}
      {hasSelection && <InspectTabs tab={tab} onChange={setTab} />}
      {selectedTarget && <TargetInspect target={selectedTarget} tab={tab} />}
      {!selectedTarget && selectedObject === "route" && route && <RouteInspect route={route} eva={eva} scienceTargets={scienceTargets} tab={tab} routeSet={routeSet} comparisonLoading={comparisonLoading} onCompareRoutes={onCompareRoutes} consumableInputs={consumableInputs} onConsumableInput={onConsumableInput} />}
    </section>
    <section className="insight-section"><div className="section-heading"><span>SCIENCE OPPORTUNITIES</span><em>{scienceTargets.length}</em></div><p className="empty-copy">Targets are interpreted independently from terrain route generation. Route proximity is a post-route analysis, not an A* objective.</p></section>
  </aside>;
}

export default MissionInsights;
