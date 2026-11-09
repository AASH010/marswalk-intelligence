import { useEffect, useState } from "react";

import HUD from "./components/HUD/HUD";
import ControlPanel from "./components/ControlPanel/ControlPanel";
import MissionPanel from "./components/MissionPanel/MissionPanel";
import MissionInsights from "./components/MissionInsights/MissionInsights";
import MarsMap from "./components/MarsMap/MarsMap";
import StatusBar from "./components/StatusBar/StatusBar";

import { INITIAL_LAYERS } from "./utils/layerConfig";
import { getScienceTargets } from "./api/science";
import { planRoute } from "./api/routes";
import { estimateConsumables } from "./api/mission";

import "./App.css";

function App() {
  const [layers, setLayers] = useState(INITIAL_LAYERS);
  const [start, setStart] = useState({ x: 4351200, y: 1091400 });
  const [goal, setGoal] = useState({ x: 4357600, y: 1098200 });
  const [selectionMode, setSelectionMode] = useState("start");
  const [mode, setMode] = useState("balanced");
  const [route, setRoute] = useState(null);
  const [scienceTargets, setScienceTargets] = useState([]);
  const [selectedTarget, setSelectedTarget] = useState(null);
  const [selectedObject, setSelectedObject] = useState(null);
  const [eva, setEva] = useState(null);
  const [loading, setLoading] = useState(false);
  const [notice, setNotice] = useState("");
  const [activeLayer, setActiveLayer] = useState("ctx");
  const [routeSet, setRouteSet] = useState({});
  const [comparisonLoading, setComparisonLoading] = useState(false);
  const [consumableInputs, setConsumableInputs] = useState({ payloadKg: 15, averageSlopePercent: 5 });

  useEffect(() => {
    getScienceTargets().then(setScienceTargets);
  }, []);

  const handleToggleLayer = (key, enabled) => {
    setLayers((previous) => ({ ...previous, [key]: { ...previous[key], enabled } }));
  };

  const handleOpacityChange = (key, opacity) => {
    setLayers((previous) => ({ ...previous, [key]: { ...previous[key], opacity } }));
  };

  const handleMapClick = (latlng) => {
    setSelectedTarget(null);
    setSelectedObject(null);
    const point = { x: latlng.lng, y: latlng.lat };
    if (selectionMode === "start") setStart(point);
    if (selectionMode === "goal") setGoal(point);
    setNotice(`${selectionMode.toUpperCase()} updated`);
  };

  const handlePlan = async () => {
    setLoading(true);
    setNotice("");
    try {
      const nextRoute = await planRoute({ start, goal, mode });
      setRoute(nextRoute);
      setRouteSet((previous) => ({ ...previous, [mode]: nextRoute }));
      setSelectedTarget(null);
      setSelectedObject("route");
      setActiveLayer("route");
      try {
        const nextEva = await estimateConsumables({ route: nextRoute, ...consumableInputs });
        setEva(nextEva);
      } catch {
        setEva(null);
        setNotice("Route ready · EVA model unavailable");
      }
    } catch (error) {
      setNotice(error.message || "Route service unavailable");
    } finally {
      setLoading(false);
    }
  };


  const handleCompareRoutes = async () => {
    setComparisonLoading(true);
    setNotice("");
    try {
      const results = await Promise.all(
        ["fastest", "balanced", "conservative"].map(async (routeMode) => {
          const result = await planRoute({ start, goal, mode: routeMode });
          return [routeMode, result];
        })
      );
      const nextSet = Object.fromEntries(results);
      setRouteSet(nextSet);
      if (nextSet[mode]) {
        setRoute(nextSet[mode]);
        setSelectedObject("route");
        setActiveLayer("route");
      }
      setNotice("ROUTE SET READY");
    } catch (error) {
      setNotice(error.message || "Route comparison unavailable");
    } finally {
      setComparisonLoading(false);
    }
  };

  const handleConsumableInput = async (key, value) => {
    const nextInputs = { ...consumableInputs, [key]: Number(value) };
    setConsumableInputs(nextInputs);
    if (!route) return;
    try {
      const nextEva = await estimateConsumables({ route, ...nextInputs });
      setEva(nextEva);
    } catch {
      setNotice("EVA model unavailable");
    }
  };

  return (
    <div className="app-shell">
      <HUD status={notice || (route ? "ROUTE READY" : "PLANNING")} />

      <div className="workspace">
        <aside className="left-rail">
          <MissionPanel
            start={start}
            goal={goal}
            selectionMode={selectionMode}
            onSelectionMode={setSelectionMode}
            mode={mode}
            onModeChange={setMode}
            onPlan={handlePlan}
            loading={loading}
          />
          <ControlPanel
            layers={layers}
            onToggleLayer={handleToggleLayer}
            onOpacityChange={handleOpacityChange}
            scienceTargets={scienceTargets}
            activeLayer={activeLayer}
            onActiveLayerChange={setActiveLayer}
          />
        </aside>

        <main className="map-stage">
          <div className="map-overlay map-overlay--top-left">
            <span className="map-chip">CTX</span>
            <span className="map-chip map-chip--muted">MARS CRS</span>
          </div>
          <div className="map-overlay map-overlay--top-right">
            <span className="map-instruction">
              {selectionMode === "start" ? "Click map to set START" : "Click map to set GOAL"}
            </span>
          </div>
          <MarsMap
            layers={layers}
            start={start}
            goal={goal}
            route={route}
            scienceTargets={scienceTargets}
            activeLayer={activeLayer}
            onActiveLayerChange={setActiveLayer}
            onMapClick={handleMapClick}
            onScienceSelect={(target) => { setSelectedTarget(target); setSelectedObject("science"); setActiveLayer("scienceTargets"); }}
            onRouteSelect={() => { setSelectedTarget(null); setSelectedObject("route"); setActiveLayer("route"); }}
            selectedTargetId={selectedTarget?.id}
          />
        </main>

        <MissionInsights
          route={route}
          scienceTargets={scienceTargets}
          selectedTarget={selectedTarget}
          selectedObject={selectedObject}
          eva={eva}
          routeSet={routeSet}
          comparisonLoading={comparisonLoading}
          onCompareRoutes={handleCompareRoutes}
          consumableInputs={consumableInputs}
          onConsumableInput={handleConsumableInput}
        />
      </div>

      <StatusBar route={route} scienceCount={scienceTargets.length} />
    </div>
  );
}

export default App;
