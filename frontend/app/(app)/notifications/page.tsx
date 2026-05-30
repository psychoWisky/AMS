"use client";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { Bell, CheckCheck, Loader2 } from "lucide-react";
import { formatDate } from "@/lib/utils";

interface Notification { id: string; type: string; title: string; message: string | null; is_read: boolean; entity_type: string | null; created_at: string; }

const TYPE_COLOR: Record<string, string> = { enrollment: "bg-blue-100 text-blue-700", result: "bg-green-100 text-green-700", workflow: "bg-purple-100 text-purple-700", system: "bg-gray-100 text-gray-700" };

export default function NotificationsPage() {
  const qc = useQueryClient();

  const { data: notifications = [], isLoading } = useQuery<Notification[]>({
    queryKey: ["ams-notifications-all"],
    queryFn: async () => (await api.get("/notifications")).data,
  });

  const markAll = useMutation({
    mutationFn: () => api.patch("/notifications/read-all"),
    onSuccess: () => { toast.success("All marked as read."); qc.invalidateQueries({ queryKey: ["ams-notifications-all"] }); },
  });

  const markOne = useMutation({
    mutationFn: (id: string) => api.patch(`/notifications/${id}/read`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ams-notifications-all"] }),
  });

  return (
    <div className="p-6 max-w-3xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><Bell size={24} className="text-[#0D6E6E]" />Notifications</h1>
        {notifications.some((n) => !n.is_read) && (
          <button onClick={() => markAll.mutate()} className="flex items-center gap-2 text-sm text-[#0D6E6E] hover:underline">
            <CheckCheck size={15} /> Mark all read
          </button>
        )}
      </div>

      {isLoading ? <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div> : notifications.length === 0 ? (
        <div className="text-center py-16 text-gray-600 bg-white rounded-2xl border border-gray-200">
          <Bell size={40} className="mx-auto mb-3 opacity-30" /><p>No notifications.</p>
        </div>
      ) : (
        <div className="space-y-2">
          {notifications.map((n) => (
            <div key={n.id} onClick={() => !n.is_read && markOne.mutate(n.id)}
              className={`bg-white rounded-2xl border p-4 cursor-pointer transition-all hover:shadow-sm ${n.is_read ? "border-gray-100 opacity-70" : "border-[#0D6E6E]/20 shadow-sm"}`}>
              <div className="flex items-start gap-3">
                {!n.is_read && <div className="w-2 h-2 rounded-full bg-[#0D6E6E] mt-2 shrink-0" />}
                {n.is_read && <div className="w-2 h-2 shrink-0" />}
                <div className="flex-1">
                  <div className="flex items-center gap-2 mb-1">
                    <span className={`px-2 py-0.5 rounded text-sm font-semibold ${TYPE_COLOR[n.type] ?? "bg-gray-100 text-gray-600"}`}>{n.type}</span>
                    <span className="text-sm text-gray-600">{formatDate(n.created_at, "relative")}</span>
                  </div>
                  <p className="text-sm font-semibold text-gray-900">{n.title}</p>
                  {n.message && <p className="text-sm text-gray-600 mt-0.5">{n.message}</p>}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
