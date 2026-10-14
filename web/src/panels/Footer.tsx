/* Attribution. Exactly `strandline.outputs.ATTRIBUTION` — taken from the run's
   own report, which the pipeline writes from that constant — plus the credits
   this screen owes for what it draws, and the words "educational prototype".
   Rule 1: this line is never optional and never shortened. */

import type { RunReport } from "../lib/api";

/** The fallback is the same text `strandline.outputs.ATTRIBUTION` carries, for
 *  the moment before a report has loaded. */
const ATTRIBUTION = [
  "Contains modified Copernicus Sentinel data 2026.",
  "Produced using Copernicus WorldDEM-30 © DLR e.V. 2010–2014 and © Airbus Defence and " +
    "Space GmbH 2014–2018 provided under COPERNICUS by the European Union and ESA; all rights reserved.",
  "© OpenStreetMap contributors.",
];

export function Footer({ report }: { report: RunReport | null }) {
  const lines: string[] = Array.isArray(report?.attribution) ? report!.attribution : ATTRIBUTION;

  return (
    <footer className="footer">
      <p className="caption">
        {lines.join(" ")} Terrain and hillshade: Copernicus DEM. Basemap tiles: © OpenStreetMap
        contributors.
      </p>
      <p className="caption footer__flag">Educational prototype — not for operational use.</p>
    </footer>
  );
}
