/* Placeholder: the three.js intro lands here in milestone 6. */

import type { Viewpoint } from "../map/flyIn";

interface Props {
  runId: string;
  bbox: [number, number, number, number] | null;
  onOpen(from: Viewpoint | null): void;
}

export function Intro(_props: Props) {
  return null;
}
