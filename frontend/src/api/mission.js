export async function estimateConsumables({ route, payloadKg = 15, averageSlopePercent = 5 }) {
  const response = await fetch("/api/v1/mission/consumables", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      distance_km: route.distance_km,
      average_raw_terrain_cost: route.average_raw_terrain_cost,
      average_slope_percent: averageSlopePercent,
      payload_kg: payloadKg,
    }),
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
