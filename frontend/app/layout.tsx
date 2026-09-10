import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { Providers } from "./providers";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "AVFU AMS – Academic Management System",
  description: "Academic Management System for Assam Veterinary & Fishery University",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className={`${inter.className} min-h-full bg-[#F5F7FA]`}>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
