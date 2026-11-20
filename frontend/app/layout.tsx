import type { Metadata, Viewport } from "next";
import { Inter, JetBrains_Mono, Readex_Pro } from "next/font/google";

import "./globals.css";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });
const jetbrains = JetBrains_Mono({ subsets: ["latin"], variable: "--font-jetbrains", display: "swap" });
// Readex Pro covers Arabic and Latin: the display face and the Arabic text face of the Sirb identity
const readex = Readex_Pro({
  subsets: ["arabic", "latin"], weight: ["400", "500", "600", "700"], variable: "--font-readex", display: "swap",
});

export const metadata: Metadata = {
  title: "سرب Sirb — Follow the smart flock",
  description: "Sirb finds the Solana wallets that keep winning and copies their trades in seconds, in your own wallet, with your budget and loss limit.",
  openGraph: {
    title: "سرب Sirb — Follow the smart flock",
    description: "Copy the smart wallets on Solana in seconds, with your own budget and loss limit.",
    siteName: "Sirb",
  },
};

export const viewport: Viewport = {
  themeColor: "#070a12",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${jetbrains.variable} ${readex.variable}`}>
      <body className="min-h-dvh">{children}</body>
    </html>
  );
}
