"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useRole } from "@/stores/auth.store";
import { Users, Search, Loader2, ChevronLeft } from "lucide-react";

// Course Students task (BUSINESS_LOGIC.md Section L.5 / STUDENT_SIDE_
// IMPLEMENTATION_PLAN.md CR-7) — browse course offerings and view their
// enrolled-student roster. Reuses existing endpoints only:
// `GET /courses/offerings/all` for the offering browser (already
// unrestricted for DPGS/Incharge Academic Cell, department-scoped for HOD
// server-side — this page never trusts a client-side role check for
// authorization, only for which controls to show) and
// `GET /enrollment/offering/{id}?status=approved` for the roster.

interface Offering {
  id: string; course_number: string; course_title: string; program_level: string | null;
  credit_structure: string; category: string | null; is_research: boolean;
  semester_id: string; semester_name: string | null; academic_year: string | null;
  section: string | null; max_enrollment: number; status: string;
  department_id: string | null; department_name: string | null;
  enrolled_count: number;
}
interface Calendar { id: string; name: string; academic_year: string; }
interface Semester { id: string; calendar_id: string; name: string; }
interface DepartmentOpt { id: string; name: string; code: string; }
interface RosterStudent {
  id: string; student_id: string; student_name: string | null; student_roll: string | null;
  student_email: string | null; department_name: string | null; classification: string | null;
  status: string; status_label: string;
}

const LEVELS = ["UG", "PG", "PhD"];

export default function CourseStudentsPage() {
  const role = useRole();
  // HOD's offering list is already department-scoped SERVER-SIDE regardless
  // of this flag (list_all_offerings ignores a client department_id for HOD
  // entirely) — the department dropdown is hidden for HOD purely so the UI
  // never implies a choice the backend would silently ignore.
  const isHod = role === "hod";

  const [calendarId, setCalendarId] = useState("");
  const [semesterId, setSemesterId] = useState("");
  const [departmentId, setDepartmentId] = useState("");
  const [level, setLevel] = useState("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Offering | null>(null);

  const { data: calendars = [] } = useQuery<Calendar[]>({
    queryKey: ["ams-calendars"],
    queryFn: async () => (await api.get("/academic/calendars")).data,
  });
  const { data: allSemesters = [] } = useQuery<Semester[]>({
    queryKey: ["ams-semesters-all"],
    queryFn: async () => (await api.get("/academic/semesters")).data,
  });
  const semesters = allSemesters.filter((s) => !calendarId || s.calendar_id === calendarId);
  const { data: departments = [] } = useQuery<DepartmentOpt[]>({
    queryKey: ["ams-departments"],
    queryFn: async () => (await api.get("/departments")).data,
    enabled: !isHod,
  });

  const { data: offerings = [], isLoading, isError } = useQuery<Offering[]>({
    queryKey: ["ams-course-students-offerings", calendarId, semesterId, departmentId, level],
    queryFn: async () => (await api.get("/courses/offerings/all", {
      params: {
        calendar_id: calendarId || undefined, semester_id: semesterId || undefined,
        department_id: !isHod && departmentId ? departmentId : undefined,
        level: level || undefined,
      },
    })).data,
  });

  const filtered = offerings.filter((o) => {
    const q = search.trim().toLowerCase();
    if (!q) return true;
    return o.course_number.toLowerCase().includes(q) || o.course_title.toLowerCase().includes(q);
  });

  if (selected) return <RosterView offering={selected} onBack={() => setSelected(null)} />;

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2 mb-2"><Users size={24} className="text-[#0D6E6E]" />Course Students</h1>
      <p className="text-gray-700 text-sm mb-6">Browse course offerings and view their enrolled-student roster.</p>

      <div className="flex flex-wrap gap-3 mb-4">
        <select value={calendarId} onChange={(e) => { setCalendarId(e.target.value); setSemesterId(""); }}
          className="border border-gray-200 rounded-xl px-3 py-2.5 text-sm focus:outline-none">
          <option value="">All Academic Years</option>
          {calendars.map((c) => <option key={c.id} value={c.id}>{c.academic_year}</option>)}
        </select>
        <select value={semesterId} onChange={(e) => setSemesterId(e.target.value)}
          className="border border-gray-200 rounded-xl px-3 py-2.5 text-sm focus:outline-none">
          <option value="">All Semesters</option>
          {semesters.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        {!isHod && (
          <select value={departmentId} onChange={(e) => setDepartmentId(e.target.value)}
            className="border border-gray-200 rounded-xl px-3 py-2.5 text-sm focus:outline-none">
            <option value="">All Departments</option>
            {departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
        )}
        <select value={level} onChange={(e) => setLevel(e.target.value)}
          className="border border-gray-200 rounded-xl px-3 py-2.5 text-sm focus:outline-none">
          <option value="">All Levels</option>
          {LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
        </select>
        <div className="relative flex-1 min-w-[220px]">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search by course code or title…"
            className="w-full border border-gray-200 rounded-xl pl-9 pr-3 py-2.5 text-sm focus:outline-none" />
        </div>
      </div>

      <div className="bg-white rounded-2xl border border-gray-200 overflow-auto max-h-[65vh]">
        {isLoading ? (
          <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
        ) : isError ? (
          <div className="text-center py-14 text-red-600">Could not load course offerings.</div>
        ) : filtered.length === 0 ? (
          <div className="text-center py-14 text-gray-600"><Users size={40} className="mx-auto mb-3 opacity-30" /><p>No course offerings match the selected filters.</p></div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10"><tr>
              {["Course Code", "Course Title", "Credits", "Semester", "Section", "Department", "Enrolled", "Action"].map((h) => (
                <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-700">{h}</th>
              ))}
            </tr></thead>
            <tbody>
              {filtered.map((o, i) => (
                <tr key={o.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                  <td className="px-4 py-2.5 font-mono font-bold text-[#0D6E6E]">{o.course_number}</td>
                  <td className="px-4 py-2.5">{o.course_title}</td>
                  <td className="px-4 py-2.5 font-mono">{o.credit_structure}</td>
                  <td className="px-4 py-2.5">{o.semester_name ?? "—"}{o.academic_year ? ` · ${o.academic_year}` : ""}</td>
                  <td className="px-4 py-2.5">{o.section ?? "—"}</td>
                  <td className="px-4 py-2.5">{o.department_name ?? "—"}</td>
                  <td className="px-4 py-2.5 font-semibold">{o.enrolled_count}</td>
                  <td className="px-4 py-2.5">
                    <button onClick={() => setSelected(o)}
                      className="px-3 py-1.5 text-sm font-semibold text-[#0D6E6E] border border-[#0D6E6E] rounded-lg hover:bg-[#E6F4F4]">
                      View Students
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function RosterView({ offering, onBack }: { offering: Offering; onBack: () => void }) {
  // `status: "approved"` is sent explicitly here (matching the convention
  // already used by frontend/app/(app)/teacher-courses/page.tsx) — the
  // backend independently force-pins DPGS/Incharge Academic Cell to
  // "approved" regardless of this param (enrollment.py's `offering_
  // enrollments`), but HOD/Super Admin honor whatever `status` is sent, so
  // this page must still ask for "approved" explicitly to show only
  // actually-enrolled students, never pending/withdrawn ones.
  const { data: students = [], isLoading, isError } = useQuery<RosterStudent[]>({
    queryKey: ["ams-course-students-roster", offering.id],
    queryFn: async () => (await api.get(`/enrollment/offering/${offering.id}`, { params: { status: "approved" } })).data,
  });

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <button onClick={onBack} className="flex items-center gap-1 text-sm font-semibold text-[#0D6E6E] mb-4 hover:underline">
        <ChevronLeft size={16} /> Back to Course Offerings
      </button>

      <div className="bg-white rounded-2xl border border-gray-200 p-5 mb-6">
        <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2 mb-1"><Users size={22} className="text-[#0D6E6E]" />{offering.course_title}</h1>
        <p className="text-gray-600 text-sm mb-4 font-mono">{offering.course_number}{offering.section ? ` — Section ${offering.section}` : ""}</p>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
          <div><p className="text-gray-500">Semester</p><p className="font-semibold">{offering.semester_name ?? "—"}{offering.academic_year ? ` (${offering.academic_year})` : ""}</p></div>
          <div><p className="text-gray-500">Department</p><p className="font-semibold">{offering.department_name ?? "—"}</p></div>
          <div><p className="text-gray-500">Credits</p><p className="font-semibold font-mono">{offering.credit_structure}</p></div>
          <div><p className="text-gray-500">Total Enrolled</p><p className="font-semibold">{isLoading ? "—" : students.length}</p></div>
        </div>
      </div>

      <div className="bg-white rounded-2xl border border-gray-200 overflow-auto max-h-[60vh]">
        {isLoading ? (
          <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
        ) : isError ? (
          <div className="text-center py-14 text-red-600">Could not load the enrolled-student roster.</div>
        ) : students.length === 0 ? (
          <div className="text-center py-14 text-gray-600"><Users size={40} className="mx-auto mb-3 opacity-30" /><p>No students are currently enrolled in this offering.</p></div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10"><tr>
              {["Sl No", "Roll No.", "Name", "Department", "Classification"].map((h) => (
                <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-700">{h}</th>
              ))}
            </tr></thead>
            <tbody>
              {students.map((s, i) => (
                <tr key={s.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                  <td className="px-4 py-2.5 text-gray-600">{i + 1}</td>
                  <td className="px-4 py-2.5 font-mono font-semibold text-[#0D6E6E]">{s.student_roll ?? "—"}</td>
                  <td className="px-4 py-2.5 font-medium text-gray-900">{s.student_name ?? "—"}</td>
                  <td className="px-4 py-2.5">{s.department_name ?? "—"}</td>
                  <td className="px-4 py-2.5 capitalize">{s.classification ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
