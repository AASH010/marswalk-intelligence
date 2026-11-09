import { LAYER_GROUPS } from "../../utils/layerConfig";

const LAYER_DETAILS = {
  ctx: {
    kicker: "BASE IMAGERY",
    description: "CTX orthomosaic used as the visual reference for the operational sector.",
    source: "CTX ORTHOMOSAIC",
    legend: "MONOCHROME IMAGERY",
  },
  aoi: {
    kicker: "OPERATIONAL EXTENT",
    description: "Mission working area used for the current Jezero planning scenario.",
    source: "JEZERO · 10 × 10 KM AOI",
    legend: "AOI BOUNDARY",
  },
  startGoal: {
    kicker: "NAVIGATION ANCHORS",
    description: "Current traverse start and goal coordinates in the Mars projected frame.",
    source: "MISSION INPUT",
    legend: "START / GOAL",
  },
  route: {
    kicker: "TERRAIN NAVIGATION",
    description: "Terrain-derived A* route. Science opportunities are evaluated independently.",
    source: "CTX DEM · 20 M",
    legend: "ROUTE / WALKABLE PATH",
  },
  scienceTargets: {
    kicker: "SCIENCE TARGETS",
    description: "Browse-derived science opportunities extracted from the current science workflow.",
    source: "CRISM · D2300",
    legend: "TARGETS / BROWSE SIGNAL",
  },
  crism: {
    kicker: "SCIENCE RASTER",
    description: "Aligned CRISM D2300 browse signal. Raster rendering is pending verification.",
    source: "CRISM · D2300",
    legend: "LOW ───────── HIGH",
  },
};

function LayerToggle({ label, checked, opacity, disabled, status, active, onActivate, onChange, onOpacityChange }) {
  return (
    <div className={`layer-row${disabled ? " layer-row--disabled" : ""}${active ? " layer-row--active" : ""}`}>
      <div className="layer-row__main">
        <label className="layer-row__check">
          <input
            type="checkbox"
            checked={checked}
            disabled={disabled}
            onChange={(event) => onChange?.(event.target.checked)}
          />
          <span className="layer-row__label">{label}</span>
        </label>
        {status ? <span className="layer-row__status">{status}</span> : null}
        {!disabled && (
          <button className="layer-row__inspect" type="button" onClick={onActivate} aria-label={`Inspect ${label}`}>
            {active ? "ACTIVE" : "VIEW"}
          </button>
        )}
      </div>
      {!disabled && active && (
        <div className="layer-row__opacity">
          <span>OPACITY</span>
          <input
            type="range"
            min="0"
            max="1"
            step="0.05"
            value={opacity}
            aria-label={`${label} opacity`}
            onChange={(event) => onOpacityChange?.(Number(event.target.value))}
          />
          <strong>{Math.round(opacity * 100)}%</strong>
        </div>
      )}
    </div>
  );
}

function ActiveLayer({ layerId, layers }) {
  const detail = LAYER_DETAILS[layerId] || LAYER_DETAILS.ctx;
  const opacity = layers[layerId]?.opacity ?? 1;
  return (
    <div className="active-layer">
      <div className="active-layer__header">
        <div>
          <div className="panel-kicker">ACTIVE LAYER</div>
          <strong>{detail.kicker}</strong>
        </div>
        <span>{Math.round(opacity * 100)}%</span>
      </div>
      <p>{detail.description}</p>
      <div className="active-layer__legend">
        <span>LEGEND</span>
        <strong>{detail.legend}</strong>
      </div>
      <div className="active-layer__source">
        <span>SOURCE</span>
        <strong>{detail.source}</strong>
      </div>
    </div>
  );
}

function ControlPanel({ layers, onToggleLayer, onOpacityChange, scienceTargets = [], activeLayer = "ctx", onActiveLayerChange }) {
  return (
    <aside className="control-panel">
      <div className="panel-kicker">LAYER MANAGER</div>
      <div className="layer-manager-intro">Select a layer to inspect its role, source, and display state.</div>

      {LAYER_GROUPS.map((group) => (
        <section className="control-panel__section" key={group.id}>
          <h3>{group.label}</h3>
          {group.layers.map((layer) => (
            <LayerToggle
              key={layer.id}
              label={layer.id === "scienceTargets" ? `${layer.label} · ${scienceTargets.length}` : layer.label}
              checked={layers[layer.id]?.enabled ?? false}
              opacity={layers[layer.id]?.opacity ?? layer.opacity ?? 1}
              disabled={layer.disabled}
              status={layer.status}
              active={activeLayer === layer.id}
              onActivate={() => onActiveLayerChange?.(layer.id)}
              onChange={(value) => onToggleLayer(layer.id, value)}
              onOpacityChange={(value) => onOpacityChange(layer.id, value)}
            />
          ))}
        </section>
      ))}

      <ActiveLayer layerId={activeLayer} layers={layers} />

      <div className="science-note">
        <span className="science-note__label">SCIENCE EVIDENCE</span>
        <p>CRISM D2300 is a browse-derived signal, not a quantitative mineral abundance map or mineral confirmation.</p>
      </div>

      <div className="provenance-block">
        <div className="provenance-block__heading">DATA PROVENANCE</div>
        <div className="provenance-row"><span>BASE</span><strong>CTX ORTHOMOSAIC</strong></div>
        <div className="provenance-row"><span>TERRAIN</span><strong>CTX DEM · 20 m</strong></div>
        <div className="provenance-row"><span>SCIENCE</span><strong>CRISM · D2300</strong></div>
      </div>
    </aside>
  );
}

export default ControlPanel;
