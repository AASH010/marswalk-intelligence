# MarsWalk Intelligence — V7 Frontend Growth

V7 continues from V6 and changes frontend only. Backend, data processing, A*, CRISM alignment, and consumables implementation are not modified.

## Added
- Mission Inspector remains evidence/provenance aware.
- Route-adjacent science analysis is computed after route generation using projected XY geometry.
- Three-mode route comparison: fastest / balanced / conservative.
- Consumables model inputs are exposed as explicit planning assumptions: payload (kg) and average slope (%).
- EVA outputs remain model outputs and are explicitly separated from operational limits.
- Active Layer + opacity workflow remains configuration-driven.
- Science language continues to distinguish CRISM D2300 browse-derived signal from mineral confirmation.

## Scientific architecture rule preserved
A* remains terrain/navigation only. Science opportunity analysis never changes the A* objective or route search.
