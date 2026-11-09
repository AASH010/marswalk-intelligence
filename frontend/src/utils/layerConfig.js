export const LAYER_GROUPS = [
  {
    id: "base",
    label: "Base",
    layers: [
      { id: "ctx", label: "CTX Orthomosaic", enabled: true, opacity: 1 },
    ],
  },
  {
    id: "terrain",
    label: "Terrain",
    layers: [
      { id: "aoi", label: "Operational AOI", enabled: true, opacity: 1 },
      { id: "slope", label: "Slope", enabled: false, opacity: 0.55, disabled: true },
      { id: "elevation", label: "Elevation", enabled: false, opacity: 0.55, disabled: true },
    ],
  },
  {
    id: "navigation",
    label: "Navigation",
    layers: [
      { id: "startGoal", label: "Start / Goal", enabled: true, opacity: 1 },
      { id: "route", label: "Route", enabled: true, opacity: 1 },
    ],
  },
  {
    id: "science",
    label: "Science",
    layers: [
      { id: "crism", label: "CRISM D2300", enabled: false, opacity: 0.45 },
      { id: "scienceTargets", label: "Science Targets", enabled: true, opacity: 1 },
    ],
  },
];

export const INITIAL_LAYERS = Object.fromEntries(
  LAYER_GROUPS.flatMap((group) =>
    group.layers.map((layer) => [layer.id, { enabled: layer.enabled, opacity: layer.opacity }])
  )
);
