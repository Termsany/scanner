import type { Metadata } from "next";

import { LiveView } from "@/components/LiveView";

export const metadata: Metadata = {
  title: "Live Trading · Alpha Scanner",
  robots: { index: false, follow: false },
};

export default function LivePage() {
  return <LiveView />;
}
