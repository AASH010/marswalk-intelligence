function pointDistanceMeters(a, b) {
  const dx = Number(a[0]) - Number(b.x);
  const dy = Number(a[1]) - Number(b.y);
  return Math.sqrt(dx * dx + dy * dy);
}

function minDistanceToRoute(target, coordinates = []) {
  if (!coordinates.length) return null;
  return Math.min(...coordinates.map((point) => pointDistanceMeters(point, target)));
}

export function analyzeScienceAlongRoute(route, targets = [], radiusMeters = 500) {
  if (!route?.coordinates?.length) return { nearby: [], nearest: null };
  const nearby = targets
    .map((target) => ({ ...target, routeDistanceM: minDistanceToRoute(target, route.coordinates) }))
    .filter((target) => target.routeDistanceM != null && target.routeDistanceM <= radiusMeters)
    .sort((a, b) => a.routeDistanceM - b.routeDistanceM);
  return { nearby, nearest: nearby[0] || null };
}

function ScienceOpportunitySummary({ route, targets = [] }) {
  const { nearby, nearest } = analyzeScienceAlongRoute(route, targets);
  return (
    <div className="science-opportunity">
      <div className="science-opportunity__metric">
        <span>ROUTE-ADJACENT TARGETS</span>
        <strong>{nearby.length}</strong>
      </div>
      <div className="science-opportunity__metric">
        <span>NEAREST TARGET</span>
        <strong>{nearest ? `${(nearest.routeDistanceM / 1000).toFixed(2)} km` : "—"}</strong>
      </div>
      <p>
        Proximity is a post-route science analysis only. It does not alter A* terrain cost or route selection.
      </p>
      {nearby.length > 0 && (
        <div className="science-opportunity__list">
          {nearby.slice(0, 3).map((target) => (
            <div key={target.id}>
              <span>{target.id}</span>
              <strong>{(target.routeDistanceM / 1000).toFixed(2)} km</strong>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default ScienceOpportunitySummary;
