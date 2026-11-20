import type { Metadata } from "next";

import { AccountsView } from "@/components/AccountsView";

export const metadata: Metadata = {
  title: "Trading Accounts · Sirb",
  robots: { index: false, follow: false },
};

export default function AccountsPage() {
  return <AccountsView />;
}
