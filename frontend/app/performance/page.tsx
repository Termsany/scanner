import type { Metadata } from "next";

import { PerformanceView } from "@/components/PerformanceView";

export const metadata: Metadata = {
  title: "Signal Performance · Alpha Scanner",
  description: "How Alpha Scanner signals performed 1h, 6h and 24h after they fired.",
};

export default function PerformancePage() {
  return <PerformanceView />;
}
