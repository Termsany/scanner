import type { Metadata } from "next";

import { MeView } from "@/components/MeView";

export const metadata: Metadata = { title: "My Trading · Sirb", robots: { index: false, follow: false } };

export default function MePage() {
  return <MeView />;
}
