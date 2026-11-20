import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { Dashboard } from "@/components/Dashboard";
import { CHAINS, type Chain } from "@/lib/types";

type Params = Promise<{ chain: string; address: string }>;

export async function generateMetadata({ params }: { params: Params }): Promise<Metadata> {
  const { chain, address } = await params;
  return { title: `${decodeURIComponent(address).slice(0, 6)}… on ${chain} · Sirb` };
}

/** Deep link (used by Telegram alerts): dashboard with the inspector already open. */
export default async function TokenPage({ params }: { params: Params }) {
  const { chain, address } = await params;
  if (!CHAINS.includes(chain as Chain)) notFound();
  return <Dashboard initialToken={{ chain: chain as Chain, address: decodeURIComponent(address) }} />;
}
