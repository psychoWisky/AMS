import Link from "next/link";
import { ShieldAlert } from "lucide-react";
import { DASHBOARD_ROUTE } from "@/lib/navigation";

export function AccessDenied() {
  return (
    <div className="p-6 max-w-lg mx-auto text-center py-24" role="alert">
      <div className="w-14 h-14 rounded-2xl bg-red-50 text-red-600 flex items-center justify-center mx-auto mb-4">
        <ShieldAlert size={28} />
      </div>
      <h1 className="text-2xl font-bold text-gray-900 mb-2">Access Denied</h1>
      <p className="text-base text-gray-700 mb-6">You do not have permission to access this page.</p>
      <Link href={DASHBOARD_ROUTE}
        className="inline-block px-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-base hover:bg-[#178F8F]">
        Go to Dashboard
      </Link>
    </div>
  );
}
