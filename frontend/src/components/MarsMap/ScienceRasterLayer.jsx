
import { ImageOverlay } from "react-leaflet";

const CRISM_URL = "/science/crism/crism_d2300_overlay.png";

const CRISM_BOUNDS = [
  [1090000, 4350000], // South-West
  [1100000, 4360000], // North-East
];

function ScienceRasterLayer({
  enabled = false,
  opacity = 0.45,
}) {
  if (!enabled) return null;

  return (
    <ImageOverlay
      url={CRISM_URL}
      bounds={CRISM_BOUNDS}
      opacity={opacity}
      zIndex={200}
      alt="CRISM D2300 spectral signal"
      interactive={false}
    />
  );
}

export default ScienceRasterLayer;
