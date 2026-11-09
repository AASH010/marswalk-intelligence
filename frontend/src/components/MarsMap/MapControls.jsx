import { useMapEvents } from "react-leaflet";

function MapClickHandler({ onMapClick }) {
  useMapEvents({ click: (event) => onMapClick?.(event.latlng) });
  return null;
}

export default MapClickHandler;
