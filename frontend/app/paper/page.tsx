import type { Metadata } from "next";

import { PaperView } from "@/components/PaperView";

export const metadata: Metadata = {
  title: "Paper Trading · Sirb",
  description: "Simulated trades that follow tracked wallets and scanner signals, compared side by side.",
};

export default function PaperPage() {
  return <PaperView />;
}
