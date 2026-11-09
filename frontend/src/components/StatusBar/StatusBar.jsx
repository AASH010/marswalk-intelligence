function StatusBar({ route, scienceCount }) {
  return (
    <footer className="statusbar">
      <span><b>AOI</b> 10 × 10 km</span>
      <span><b>GRID</b> 500 × 500</span>
      <span><b>RESOLUTION</b> 20 m</span>
      <span><b>ROUTE</b> {route ? `${route.distance_km.toFixed(2)} km` : "READY"}</span>
      <span><b>SCIENCE</b> {scienceCount} targets</span>
      <span className="statusbar__source"><b>DATA</b> CTX · CRISM</span>
    </footer>
  );
}

export default StatusBar;
