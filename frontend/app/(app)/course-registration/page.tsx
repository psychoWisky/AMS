"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useUser } from "@/stores/auth.store";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ClipboardCheck, Loader2, CheckSquare, Square } from "lucide-react";
import { CREDIT_TYPE_LABELS } from "@/lib/utils";

interface Calendar { id: string; name: string; academic_year: string; }
interface Semester { id: string; calendar_id: string; name: string; }
interface DepartmentOpt { id: string; name: string; code: string; }
interface Offering {
  id: string; course_number: string; course_title: string; credit_structure: string;
  category: string | null; credit_type: string | null; is_research: boolean;
  semester_name: string | null; department_id: string | null; department_name: string | null; faculty_names: string[];
  status: string; enrolled_count: number; max_enrollment: number;
}
interface EnrollmentItem {
  id: string; offering_id: string; course_number: string; course_title: string;
  status: string; status_label: string; remarks: string | null;
}
interface Registration {
  id: string; semester_id: string; calendar_id: string; stage: string; status_label: string;
  revert_remark: string | null; submitted_at: string; items: EnrollmentItem[];
}

const STAGE_STYLE: Record<string, string> = {
  teacher_pending: "bg-amber-100 text-amber-700",
  major_advisor_pending: "bg-blue-100 text-blue-700",
  hod_pending: "bg-purple-100 text-purple-700",
  hod_approved: "bg-green-100 text-green-700",
  reverted: "bg-red-100 text-red-700",
};
const ITEM_STATUS_STYLE: Record<string, string> = {
  pending: "bg-amber-100 text-amber-700",
  approved: "bg-green-100 text-green-700",
  reverted: "bg-red-100 text-red-700",
  withdrawn: "bg-gray-100 text-gray-600",
};

export default function CourseRegistrationPage() {
  const user = useUser();
  const qc = useQueryClient();
  const [calendarId, setCalendarId] = useState("");
  const [semesterId, setSemesterId] = useState("");
  // Course-visibility change — a student's own department no longer limits
  // this catalogue; this is purely an optional narrowing filter across ALL
  // departments' courses. "" = All Departments (the default).
  const [departmentFilter, setDepartmentFilter] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmSubmit, setConfirmSubmit] = useState(false);

  const { data: calendars = [] } = useQuery<Calendar[]>({
    queryKey: ["ams-calendars"],
    queryFn: async () => (await api.get("/academic/calendars")).data,
  });
  const { data: semesters = [] } = useQuery<Semester[]>({
    queryKey: ["ams-semesters-for-registration", calendarId],
    queryFn: async () => (await api.get(`/academic/calendars/${calendarId}/semesters`)).data,
    enabled: !!calendarId,
  });

  // Department filter master data — the existing Department master-data
  // endpoint, not a new concept.
  const { data: departments = [] } = useQuery<DepartmentOpt[]>({
    queryKey: ["ams-departments"],
    queryFn: async () => (await api.get("/departments")).data,
  });

  const { data: offerings = [], isLoading: offeringsLoading } = useQuery<Offering[]>({
    queryKey: ["ams-eligible-offerings", calendarId, semesterId, departmentFilter],
    queryFn: async () => (await api.get("/courses/offerings/all", {
      params: {
        calendar_id: calendarId, semester_id: semesterId,
        ...(departmentFilter ? { department_id: departmentFilter } : {}),
      },
    })).data,
    enabled: !!calendarId && !!semesterId,
  });

  const { data: myRegistrations = [], isLoading: regsLoading } = useQuery<Registration[]>({
    queryKey: ["ams-my-registrations"],
    queryFn: async () => (await api.get("/enrollment/registrations")).data,
    enabled: !!user,
  });

  const currentRegistration = myRegistrations.find((r) => r.semester_id === semesterId);

  const submitRegistration = useMutation({
    mutationFn: () => api.post("/enrollment/register", {
      calendar_id: calendarId, semester_id: semesterId, offering_ids: Array.from(selected),
    }),
    onSuccess: () => {
      toast.success("Registration submitted.");
      setSelected(new Set());
      qc.invalidateQueries({ queryKey: ["ams-my-registrations"] });
      qc.invalidateQueries({ queryKey: ["ams-eligible-offerings"] });
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to submit registration."),
  });

  function toggle(id: string) {
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2 mb-2"><ClipboardCheck size={24} className="text-[#0D6E6E]" />Course Registration</h1>
      <p className="text-gray-700 text-sm mb-6">Select and submit your courses for approval by your Course Teacher, Major Advisor, and HOD.</p>

      <div className="flex flex-wrap gap-3 mb-5">
        <select value={calendarId} onChange={(e) => { setCalendarId(e.target.value); setSemesterId(""); setSelected(new Set()); }}
          className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
          <option value="">Select Academic Year…</option>
          {calendars.map((c) => <option key={c.id} value={c.id}>{c.academic_year}</option>)}
        </select>
        <select value={semesterId} onChange={(e) => { setSemesterId(e.target.value); setSelected(new Set()); }} disabled={!calendarId}
          className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none disabled:opacity-50">
          <option value="">Select Semester…</option>
          {semesters.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        {/* Department filter (this task's confirmed requirement) — a
            student's OWN department no longer restricts this catalogue; this
            is purely an optional narrowing filter across ALL departments. */}
        <select value={departmentFilter} onChange={(e) => { setDepartmentFilter(e.target.value); setSelected(new Set()); }}
          className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
          <option value="">All Departments</option>
          {departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </select>
      </div>

      {currentRegistration && (
        <div className="bg-white rounded-2xl border border-gray-200 p-5 mb-6">
          <div className="flex items-center justify-between mb-3">
            <h2 className="font-bold text-gray-800">Your Registration for This Semester</h2>
            <span className={`inline-flex px-2.5 py-1 rounded-full text-xs font-semibold ${STAGE_STYLE[currentRegistration.stage] ?? "bg-gray-100"}`}>{currentRegistration.status_label}</span>
          </div>
          {currentRegistration.revert_remark && (
            <div className="bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-800 mb-3">
              <span className="font-bold">Reverted — reason: </span>{currentRegistration.revert_remark}
              {currentRegistration.stage !== "reverted" && <span className="block text-xs text-red-700 mt-1">(Corrected courses have been sent back for re-review — see below.)</span>}
            </div>
          )}
          <div className="space-y-2">
            {currentRegistration.items.map((it) => (
              <div key={it.id} className="flex items-center justify-between p-2.5 bg-gray-50 rounded-lg text-sm">
                <div>
                  <span className="font-mono font-bold text-[#0D6E6E]">{it.course_number}</span> <span>{it.course_title}</span>
                  {it.remarks && <p className="text-xs text-red-600 mt-0.5">{it.remarks}</p>}
                </div>
                <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${ITEM_STATUS_STYLE[it.status] ?? "bg-gray-100"}`}>{it.status_label}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {!currentRegistration && semesterId && (
        <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden overflow-x-auto">
          {offeringsLoading || regsLoading ? (
            <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
          ) : offerings.length === 0 ? (
            <div className="text-center py-16 text-gray-600">
              <ClipboardCheck size={40} className="mx-auto mb-3 opacity-30" />
              <p>
                {departmentFilter
                  ? "No eligible courses found for this department in this semester."
                  : "No eligible courses found for this semester. If this seems wrong, your academic program may not be configured — contact administration."}
              </p>
            </div>
          ) : (
            <table className="w-full text-sm min-w-[900px]">
              <thead className="bg-gray-50 border-b border-gray-200">
                <tr>{["", "Course Number", "Course Title", "Department", "Credit", "Credit Type", "Course Teachers"].map((h) => (
                  <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>
                ))}</tr>
              </thead>
              <tbody>
                {offerings.map((o, i) => (
                  <tr key={o.id} onClick={() => toggle(o.id)} className={`cursor-pointer ${i % 2 === 0 ? "bg-white" : "bg-gray-50/50"} hover:bg-[#E6F4F4]`}>
                    <td className="px-4 py-3">{selected.has(o.id) ? <CheckSquare size={18} className="text-[#0D6E6E]" /> : <Square size={18} className="text-gray-400" />}</td>
                    <td className="px-4 py-3 font-mono font-bold text-[#0D6E6E] whitespace-nowrap">{o.course_number}</td>
                    <td className="px-4 py-3">{o.course_title}</td>
                    <td className="px-4 py-3 text-gray-600 whitespace-nowrap">{o.department_name ?? "—"}</td>
                    <td className="px-4 py-3 font-mono">{o.credit_structure}</td>
                    <td className="px-4 py-3 text-gray-600">{o.credit_type ? CREDIT_TYPE_LABELS[o.credit_type] : "—"}</td>
                    <td className="px-4 py-3 text-gray-600">{o.faculty_names.join(", ") || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {offerings.length > 0 && (
            <div className="flex justify-end p-4 border-t border-gray-100">
              <button onClick={() => setConfirmSubmit(true)} disabled={selected.size === 0 || submitRegistration.isPending}
                className="px-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-50">
                Submit Registration ({selected.size} course{selected.size === 1 ? "" : "s"})
              </button>
            </div>
          )}
        </div>
      )}

      {confirmSubmit && (
        <ConfirmDialog
          title="Submit Course Registration"
          message={`Submit registration for ${selected.size} course(s)? Each course will need approval from its Course Teacher, then your Major Advisor, then HOD.`}
          confirmLabel="Yes, Submit"
          confirmClassName="bg-[#0D6E6E] hover:bg-[#178F8F] text-white"
          onCancel={() => setConfirmSubmit(false)}
          onConfirm={() => { submitRegistration.mutate(); setConfirmSubmit(false); }}
        />
      )}
    </div>
  );
}
