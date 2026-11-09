export async function planRoute({ start, goal, mode }) {
  const response = await fetch("/api/v1/navigate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ start: [start.x, start.y], goal: [goal.x, goal.y], mode }),
  });
  if (!response.ok) throw new Error(await response.text());
  const data = await response.json();
  const feature = data.features?.[0];
  if (!feature) throw new Error("Route API returned no route feature.");
  return { ...feature.properties, coordinates: feature.geometry.coordinates };
}
