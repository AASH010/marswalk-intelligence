
import { ImageOverlay } from "react-leaflet";

const CRISM_URL =
  "/science/crism/crism_dual_band_overlay.png";

const CRISM_BOUNDS = [
  [1090000, 4350000],
  [1100000, 4360000],
];

function ScienceRasterLayer({
  enabled = false,
  opacity = 0.75,
}) {
  if (!enabled) return null;

  return (
    <ImageOverlay
      url={CRISM_URL}
      bounds={CRISM_BOUNDS}
      opacity={opacity}
      zIndex={200}
      alt="CRISM dual-band spectral signal overlay"
      interactive={false}
    />
  );
}

export default ScienceRasterLayer;
