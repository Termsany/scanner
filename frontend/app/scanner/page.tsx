import type { Metadata } from "next";

import { Dashboard } from "@/components/Dashboard";

export const metadata: Metadata = { title: "Scanner · Sirb" };

export default function ScannerPage() {
  return <Dashboard />;
}
