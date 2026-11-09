
import mockTargets from "../mock/science-targets.json";

export async function getScienceTargets() {
  try {
    const response = await fetch("/api/v1/science/targets");

    if (!response.ok) {
      throw new Error("Science endpoint unavailable");
    }

    const data = await response.json();

    if (!Array.isArray(data.features)) {
      throw new Error("Invalid science response");
    }

    // Never silently replace valid empty API data with mock targets.
    return data.features.map((feature) => ({
      ...feature.properties,
      id: feature.properties.id,
      x: feature.geometry.coordinates[0],
      y: feature.geometry.coordinates[1],
      evidence: feature.properties.evidence || [],
    }));
  } catch (error) {
    console.error("Science API error:", error);

    // Explicit fallback only when API is unavailable.
    return mockTargets.map((target) => ({
      ...target,
      isMock: true,
    }));
  }
}
