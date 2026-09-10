"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useRole } from "@/stores/auth.store";
import { toast } from "sonner";
import { formatDate, ADMIN_ROLES } from "@/lib/utils";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { CalendarDays, Plus, ChevronDown, ChevronRight, Clock, BookOpen, Loader2, Trash2 } from "lucide-react";

interface Calendar { id: string; name: string; academic_year: string; start_date: string; end_date: string; status: string; }
interface Semester { id: string; calendar_id: string; name: string; sem_type: string; start_date: string; end_date: string; status: string; registration_start: string | null; exam_start: string | null; result_declaration: string | null; }

const STATUS_COLOR: Record<string, string> = {
  draft: "bg-gray-100 text-gray-700",
  active: "bg-green-100 text-green-700",
  closed: "bg-red-100 text-red-700",
  upcoming: "bg-blue-100 text-blue-700",
  completed: "bg-purple-100 text-purple-700",
};

export default function CalendarPage() {
  const role = useRole();
  const qc = useQueryClient();
  const isAdmin = role ? ADMIN_ROLES.includes(role) : false;
  const [expanded, setExpanded] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showSemCreate, setShowSemCreate] = useState<string | null>(null);
  const [form, setForm] = useState({ name: "", academic_year: "", start_date: "", end_date: "" });
  const [semForm, setSemForm] = useState({ name: "", sem_type: "odd", start_date: "", end_date: "", registration_start: "", exam_start: "", exam_end: "", result_declaration: "" });
  const [deleteTarget, setDeleteTarget] = useState<Calendar | null>(null);

  const { data: calendars = [], isLoading } = useQuery<Calendar[]>({
    queryKey: ["ams-calendars"],
    queryFn: async () => (await api.get("/academic/calendars")).data,
  });

  const { data: semesters = [] } = useQuery<Semester[]>({
    queryKey: ["ams-semesters", expanded],
    queryFn: async () => expanded ? (await api.get(`/academic/calendars/${expanded}/semesters`)).data : [],
    enabled: !!expanded,
  });

  const createCal = useMutation({
    mutationFn: (d: typeof form) => api.post("/academic/calendars", d),
    onSuccess: () => { toast.success("Calendar created."); qc.invalidateQueries({ queryKey: ["ams-calendars"] }); setShowCreate(false); setForm({ name: "", academic_year: "", start_date: "", end_date: "" }); },
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      const msg = Array.isArray(detail) ? detail.map((e: { msg: string }) => e.msg).join(", ") : (typeof detail === "string" ? detail : "Failed to create calendar.");
      toast.error(msg);
    },
  });

  const createSem = useMutation({
    mutationFn: (d: typeof semForm & { calendar_id: string }) => api.post("/academic/semesters", d),
    onSuccess: () => { toast.success("Semester created."); qc.invalidateQueries({ queryKey: ["ams-semesters", expanded] }); setShowSemCreate(null); },
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      const msg = Array.isArray(detail) ? detail.map((e: { msg: string }) => e.msg).join(", ") : (typeof detail === "string" ? detail : "Failed to create semester.");
      toast.error(msg);
    },
  });

  const updateStatus = useMutation({
    mutationFn: ({ id, type, status }: { id: string; type: "cal" | "sem"; status: string }) =>
      type === "cal" ? api.patch(`/academic/calendars/${id}/status?status=${status}`) : api.patch(`/academic/semesters/${id}/status?status=${status}`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["ams-calendars"] }); qc.invalidateQueries({ queryKey: ["ams-semesters", expanded] }); },
  });

  // Delete Academic Year (this task's confirmed requirement) — only ever
  // offered in the UI for a DRAFT year; the backend independently enforces
  // this regardless of what the frontend shows.
  const deleteCal = useMutation({
    mutationFn: (id: string) => api.delete(`/academic/calendars/${id}`),
    onSuccess: () => {
      toast.success("Academic year deleted.");
      qc.invalidateQueries({ queryKey: ["ams-calendars"] });
      setDeleteTarget(null);
    },
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      toast.error(typeof detail === "string" ? detail : "Failed to delete academic year.");
      setDeleteTarget(null);
    },
  });

  if (isLoading) return <div className="flex items-center justify-center py-24 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>;

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><CalendarDays size={24} className="text-[#0D6E6E]" />Academic Calendar</h1>
          <p className="text-gray-700 text-base mt-1">Manage academic sessions, semesters, and key dates</p>
        </div>
        {isAdmin && (
          <button onClick={() => setShowCreate(true)}
            className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-base hover:bg-[#178F8F]">
            <Plus size={16} /> New Calendar
          </button>
        )}
      </div>

      {/* Create Calendar Modal */}
      {showCreate && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md p-6">
            <h3 className="text-xl font-bold mb-4">Create Academic Calendar</h3>
            <div className="space-y-3">
              {[["Name", "name", "text", "e.g. 2024-25"], ["Academic Year", "academic_year", "text", "e.g. 2024-25"], ["Start Date", "start_date", "date", ""], ["End Date", "end_date", "date", ""]].map(([label, key, type, ph]) => (
                <div key={key}>
                  <label className="block text-base font-semibold text-gray-700 mb-1">{label}</label>
                  <input type={type} placeholder={ph} value={(form as Record<string, string>)[key]}
                    onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
              ))}
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={() => setShowCreate(false)} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium hover:bg-gray-50">Cancel</button>
              <button
                onClick={() => {
                  if (!form.name.trim() || !form.academic_year.trim() || !form.start_date || !form.end_date) {
                    toast.error("Please fill in all fields before creating."); return;
                  }
                  createCal.mutate(form);
                }}
                disabled={createCal.isPending}
                className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-60">
                {createCal.isPending ? "Creating…" : "Create"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Calendars List */}
      <div className="space-y-3">
        {calendars.length === 0 && <div className="text-center py-16 text-gray-600"><CalendarDays size={40} className="mx-auto mb-3 opacity-30" /><p>No academic calendars yet.</p></div>}
        {calendars.map((cal) => (
          <div key={cal.id} className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
            <div className="flex items-center gap-3 p-4 cursor-pointer" onClick={() => setExpanded((e) => e === cal.id ? null : cal.id)}>
              {expanded === cal.id ? <ChevronDown size={16} className="text-gray-600" /> : <ChevronRight size={16} className="text-gray-600" />}
              <div className="flex-1">
                <h3 className="font-bold text-gray-900">{cal.name}</h3>
                <p className="text-sm text-gray-700">{formatDate(cal.start_date, "short")} – {formatDate(cal.end_date, "short")}</p>
              </div>
              <span className={`px-3 py-1 rounded-full text-sm font-semibold ${STATUS_COLOR[cal.status] ?? "bg-gray-100"}`}>{cal.status}</span>
              {isAdmin && (
                <select value={cal.status} onChange={(e) => updateStatus.mutate({ id: cal.id, type: "cal", status: e.target.value })}
                  onClick={(e) => e.stopPropagation()}
                  className="text-sm border border-gray-200 rounded-lg px-2 py-1 focus:outline-none">
                  {["draft","active","closed"].map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              )}
              {/* Delete (this task's confirmed requirement) — only ever shown
                  for a DRAFT year; the backend independently rejects any
                  attempt on a non-DRAFT one regardless of this check. */}
              {isAdmin && cal.status === "draft" && (
                <button onClick={(e) => { e.stopPropagation(); setDeleteTarget(cal); }}
                  title="Delete draft academic year"
                  className="p-1.5 text-red-600 hover:bg-red-50 rounded-lg">
                  <Trash2 size={16} />
                </button>
              )}
            </div>

            {expanded === cal.id && (
              <div className="border-t border-gray-100 px-4 pb-4">
                <div className="flex items-center justify-between mb-3 pt-3">
                  <p className="text-base font-semibold text-gray-700">Semesters</p>
                  {isAdmin && <button onClick={() => setShowSemCreate(cal.id)}
                    className="text-sm flex items-center gap-1 text-[#0D6E6E] hover:underline"><Plus size={12} /> Add Semester</button>}
                </div>

                {showSemCreate === cal.id && (
                  <div className="bg-gray-50 rounded-xl p-4 mb-3">
                    <div className="grid grid-cols-2 gap-3 mb-3">
                      {[["Semester Name", "name", "text", "Semester I (Odd)"], ["Type", "sem_type", "text", "odd/even"],
                        ["Start Date", "start_date", "date", ""], ["End Date", "end_date", "date", ""],
                        ["Reg Start", "registration_start", "date", ""], ["Exam Start", "exam_start", "date", ""],
                        ["Exam End", "exam_end", "date", ""], ["Result Declaration", "result_declaration", "date", ""]].map(([label, key, type, ph]) => (
                        <div key={key}>
                          <label className="block text-base font-semibold text-gray-600 mb-0.5">{label}</label>
                          <input type={type} placeholder={ph} value={(semForm as Record<string, string>)[key]}
                            onChange={(e) => setSemForm((f) => ({ ...f, [key]: e.target.value }))}
                            className="w-full border border-gray-200 rounded-lg px-2 py-1.5 text-base focus:outline-none focus:ring-1 focus:ring-[#0D6E6E]" />
                        </div>
                      ))}
                    </div>
                    <div className="flex gap-2">
                      <button onClick={() => setShowSemCreate(null)} className="flex-1 py-2 border border-gray-200 rounded-lg text-base font-medium">Cancel</button>
                      <button
                        onClick={() => {
                          if (!semForm.name.trim() || !semForm.start_date || !semForm.end_date) {
                            toast.error("Semester name, start date and end date are required."); return;
                          }
                          createSem.mutate({ ...semForm, calendar_id: cal.id });
                        }}
                        disabled={createSem.isPending}
                        className="flex-1 py-2 bg-[#0D6E6E] text-white rounded-lg text-base font-bold">
                        {createSem.isPending ? "Saving…" : "Add Semester"}
                      </button>
                    </div>
                  </div>
                )}

                <div className="space-y-2">
                  {semesters.filter((s) => s.calendar_id === cal.id).map((sem) => (
                    <div key={sem.id} className="flex items-center gap-3 p-3 bg-gray-50 rounded-xl">
                      <BookOpen size={15} className="text-[#0D6E6E] shrink-0" />
                      <div className="flex-1">
                        <p className="text-sm font-semibold text-gray-800">{sem.name}</p>
                        <p className="text-sm text-gray-700">
                          {formatDate(sem.start_date, "short")} – {formatDate(sem.end_date, "short")}
                          {sem.exam_start && ` · Exam: ${formatDate(sem.exam_start, "short")}`}
                          {sem.result_declaration && ` · Results: ${formatDate(sem.result_declaration, "short")}`}
                        </p>
                      </div>
                      <span className={`px-2 py-0.5 rounded-full text-sm font-semibold ${STATUS_COLOR[sem.status] ?? "bg-gray-100"}`}>{sem.status}</span>
                      {isAdmin && (
                        <select value={sem.status} onChange={(e) => updateStatus.mutate({ id: sem.id, type: "sem", status: e.target.value })}
                          className="text-sm border border-gray-200 rounded-lg px-2 py-1 focus:outline-none">
                          {["upcoming","active","completed"].map((s) => <option key={s} value={s}>{s}</option>)}
                        </select>
                      )}
                    </div>
                  ))}
                  {semesters.filter((s) => s.calendar_id === cal.id).length === 0 && (
                    <p className="text-sm text-gray-600 text-center py-4">No semesters added yet.</p>
                  )}
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Delete confirmation (this task's confirmed requirement) */}
      {deleteTarget && (
        <ConfirmDialog
          title="Delete Academic Year"
          message={`Delete the draft academic year "${deleteTarget.name}"? This cannot be undone.`}
          confirmLabel={deleteCal.isPending ? "Deleting…" : "Yes, Delete"}
          onCancel={() => setDeleteTarget(null)}
          onConfirm={() => deleteCal.mutate(deleteTarget.id)}
        />
      )}
    </div>
  );
}
