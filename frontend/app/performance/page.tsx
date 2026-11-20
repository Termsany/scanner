import type { Metadata } from "next";

import { PerformanceView } from "@/components/PerformanceView";

export const metadata: Metadata = {
  title: "Signal Performance · Sirb",
  description: "How Sirb signals performed 1h, 6h and 24h after they fired.",
};

export default function PerformancePage() {
  return <PerformanceView />;
}
