const MODES = ["fastest", "balanced", "conservative"];

function RouteModeSelector({ mode, onChange }) {
  return (
    <div className="mode-selector">
      {MODES.map((item) => (
        <button key={item} className={mode === item ? "active" : ""} onClick={() => onChange(item)}>
          {item}
        </button>
      ))}
    </div>
  );
}

export default RouteModeSelector;
