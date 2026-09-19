"use client";
import Link from "next/link";
import { AlertTriangle } from "lucide-react";
import { DASHBOARD_ROUTE } from "@/lib/navigation";

// Unexpected runtime/render failure inside a page — deliberately a different
// screen from Access Denied, which is only ever shown for a permission
// decision. Renders inside the (app) layout, so the sidebar stays usable.
export default function AppError({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div className="p-6 max-w-lg mx-auto text-center py-24" role="alert">
      <div className="w-14 h-14 rounded-2xl bg-amber-50 text-amber-600 flex items-center justify-center mx-auto mb-4">
        <AlertTriangle size={28} />
      </div>
      <h1 className="text-2xl font-bold text-gray-900 mb-2">Something went wrong</h1>
      <p className="text-base text-gray-700 mb-6">This page could not be displayed. Please try again.</p>
      <div className="flex gap-3 justify-center">
        <button onClick={reset} className="px-5 py-2.5 border border-gray-200 rounded-xl font-semibold text-base hover:bg-gray-50">Try again</button>
        <Link href={DASHBOARD_ROUTE} className="px-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-base hover:bg-[#178F8F]">Go to Dashboard</Link>
      </div>
    </div>
  );
}
