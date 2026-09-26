"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Loader2, TrendingUp } from "lucide-react";
import { api } from "@/services/api";

// Student Result Tracking: the student's OWN registered courses and whether each course's
// result has been compiled (published by the Controller of Examination) or is still pending.
// No marks/grades are exposed here and nothing about unpublished results.

interface TrackingRow { offering_id: string; course_code: string; course_title: string; course_department: string | null; semester: string | null; academic_year: string | null; status: "compiled" | "pending"; }
interface Tracking {
  filters: { academic_years: { calendar_id: string; academic_year: string | null }[]; semesters: { semester_id: string; name: string | null; calendar_id: string }[] };
  rows: TrackingRow[];
}

export default function ResultTrackingPage() {
  const [calendarId, setCalendarId] = useState("");
  const [semesterId, setSemesterId] = useState("");
  const { data: options } = useQuery<Tracking>({
    queryKey: ["ams-result-tracking-options"],
    queryFn: async () => (await api.get("/results/tracking")).data,
  });
  const { data, isLoading, isError } = useQuery<Tracking>({
    queryKey: ["ams-result-tracking", calendarId, semesterId],
    queryFn: async () => (await api.get("/results/tracking", { params: { ...(calendarId ? { calendar_id: calendarId } : {}), ...(semesterId ? { semester_id: semesterId } : {}) } })).data,
  });
  const semesters = (options?.filters.semesters ?? []).filter((s) => !calendarId || s.calendar_id === calendarId);
  const th = "text-left px-4 py-3 font-semibold text-gray-700";

  return (
    <div className="p-6 w-full">
      <div className="mb-6">
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><TrendingUp size={24} className="text-[#0D6E6E]" />Result Tracking</h1>
        <p className="text-gray-700 text-base mt-1">Track whether the results of your registered courses have been compiled</p>
      </div>
      <div className="flex flex-wrap gap-3 mb-4">
        <select value={calendarId} onChange={(e) => { setCalendarId(e.target.value); setSemesterId(""); }} className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
          <option value="">All Academic Years</option>
          {(options?.filters.academic_years ?? []).map((y) => <option key={y.calendar_id} value={y.calendar_id}>{y.academic_year}</option>)}
        </select>
        <select value={semesterId} onChange={(e) => setSemesterId(e.target.value)} className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
          <option value="">All Semesters</option>
          {semesters.map((s) => <option key={s.semester_id} value={s.semester_id}>{s.name}</option>)}
        </select>
      </div>
      <div className="bg-white rounded-2xl border border-gray-200 overflow-auto max-h-[70vh]">
        {isLoading ? <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
          : isError ? <div className="text-center py-14 text-red-600">Could not load your results.</div>
          : !data || data.rows.length === 0 ? <div className="text-center py-14 text-gray-600"><TrendingUp size={40} className="mx-auto mb-3 opacity-30" /><p>No registered courses found for the selected period.</p></div>
          : (
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10"><tr>
                {["SL NO", "Course Code", "Course Title", "Course Department", "Status"].map((h) => <th key={h} className={th}>{h}</th>)}
              </tr></thead>
              <tbody>
                {data.rows.map((r, i) => (
                  <tr key={r.offering_id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                    <td className="px-4 py-3 text-gray-600">{i + 1}</td>
                    <td className="px-4 py-3 font-mono font-bold text-[#0D6E6E]">{r.course_code}</td>
                    <td className="px-4 py-3">{r.course_title}<span className="block text-xs text-gray-500">{r.semester} · {r.academic_year}</span></td>
                    <td className="px-4 py-3">{r.course_department ?? "—"}</td>
                    <td className="px-4 py-3"><span className={`px-2.5 py-1 rounded-full text-sm font-semibold ${r.status === "compiled" ? "bg-green-100 text-green-700" : "bg-amber-100 text-amber-700"}`}>{r.status === "compiled" ? "Compiled" : "Pending"}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </div>
    </div>
  );
}
