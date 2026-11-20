import type { Metadata } from "next";

import { MeView } from "@/components/MeView";

export const metadata: Metadata = { title: "My Trading · Alpha Scanner", robots: { index: false, follow: false } };

export default function MePage() {
  return <MeView />;
}
