import type { Metadata } from "next";
import { Suspense } from "react";

import { ResetView } from "@/components/ResetView";

export const metadata: Metadata = { title: "New Password · Sirb", robots: { index: false, follow: false } };

export default function ResetPage() {
  return (
    <Suspense>
      <ResetView />
    </Suspense>
  );
}
