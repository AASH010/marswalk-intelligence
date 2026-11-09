function HUD({ status = "PLANNING", dataStatus = "DATA READY" }) {
  return (
    <header className="topbar">
      <div className="brand">
        <div className="brand__mark">MW</div>
        <div>
          <div className="brand__name">MarsWalk Intelligence</div>
          <div className="brand__sub">MISSION-AWARE MARS SURFACE PLANNING</div>
        </div>
      </div>

      <div className="mission-context">
        <span>JEZERO CRATER</span>
        <span className="mission-context__divider">/</span>
        <span>10 × 10 KM AOI</span>
      </div>

      <div className="topbar__status">
        <span className="status-dot" />
        <span>{dataStatus}</span>
        <span className="topbar__status-divider" />
        <span>{status}</span>
      </div>
    </header>
  );
}

export default HUD;
