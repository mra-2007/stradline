import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "@fontsource/barlow/400.css";
import "@fontsource/barlow/500.css";
import "@fontsource/barlow-semi-condensed/500.css";
import "@fontsource/barlow-semi-condensed/600.css";
import "@fontsource/noto-sans-devanagari/400.css";
import "@fontsource/noto-sans-devanagari/600.css";

// MapLibre first: its stylesheet claims .maplibregl-map, and our own rules
// have to come after it to win.
import "maplibre-gl/dist/maplibre-gl.css";

import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/app.css";
import { App } from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
