"use client";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/services/api";
import { LayoutGrid, Search, Loader2, ChevronLeft, ChevronRight } from "lucide-react";

// Vice Chancellor's read-only institutional dashboard. Reuses the exact same GET /students
// endpoint and filters as Super Admin's Students page, but this page has NO edit action, NO
// mutation of any kind — it is a view only. Backend authorization is independent: `students.py`
// grants VC a separate `_READ_ROLES` tuple on the two GET routes only; PATCH remains restricted
// to `_MANAGE_ROLES` (Super Admin), so even a tampered request from this page cannot mutate anything.

interface Student {
  id: string; email: string; full_name: string;
  student_roll: string | null;
  program_name: string | null; department_name: string | null; college_name: string | null;
  academic_year: string | null; latest_semester: string | null; is_active: boolean;
}
interface StudentPage { items: Student[]; total: number; page: number; page_size: number; }
interface Opt { id: string; name: string; }
interface Calendar { id: string; academic_year: string; }
interface Semester { id: string; name: string; }
interface Programme { id: string; name: string; code: string; }

const PAGE_SIZE = 25;

export default function VcDashboardPage() {
  const [search, setSearch] = useState("");
  const [q, setQ] = useState("");
  const [academicYearId, setAcademicYearId] = useState("");
  const [semesterId, setSemesterId] = useState("");
  const [departmentId, setDepartmentId] = useState("");
  const [programId, setProgramId] = useState("");
  const [collegeId, setCollegeId] = useState("");
  const [page, setPage] = useState(1);

  useEffect(() => {
    const t = setTimeout(() => { setQ(search.trim()); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [search]);
  const changeFilter = (setter: (v: string) => void) => (v: string) => { setter(v); setPage(1); };

  const { data: calendars = [] } = useQuery<Calendar[]>({
    queryKey: ["ams-calendars"],
    queryFn: async () => (await api.get("/academic/calendars")).data,
  });
  const { data: semesters = [] } = useQuery<Semester[]>({
    queryKey: ["ams-vc-semesters", academicYearId],
    queryFn: async () => (await api.get(academicYearId ? `/academic/calendars/${academicYearId}/semesters` : "/academic/semesters")).data,
  });
  const { data: departments = [] } = useQuery<Opt[]>({
    queryKey: ["ams-departments"],
    queryFn: async () => (await api.get("/departments")).data,
  });
  const { data: programmes = [] } = useQuery<Programme[]>({
    queryKey: ["ams-programs"],
    queryFn: async () => (await api.get("/departments/programs")).data,
  });
  const { data: colleges = [] } = useQuery<Opt[]>({
    queryKey: ["ams-colleges-active"],
    queryFn: async () => (await api.get("/admin/colleges")).data,
  });

  const params = {
    q: q || undefined, academic_year_id: academicYearId || undefined, semester_id: semesterId || undefined,
    department_id: departmentId || undefined, program_id: programId || undefined, college_id: collegeId || undefined,
    page, page_size: PAGE_SIZE,
  };
  const { data, isLoading, isError, isFetching } = useQuery<StudentPage>({
    queryKey: ["ams-vc-students", params],
    queryFn: async () => (await api.get("/students", { params })).data,
    placeholderData: (previous) => previous,
  });
  const students = data?.items ?? [];
  const total = data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const filterSelect = (label: string, value: string, onChange: (v: string) => void, options: { value: string; label: string }[]) => (
    <select value={value} onChange={(e) => onChange(e.target.value)} aria-label={label}
      className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] bg-white max-w-[220px]">
      <option value="">{label}: All</option>
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );

  return (
    <div className="p-6 w-full">
      <div className="mb-6">
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><LayoutGrid size={24} className="text-[#0D6E6E]" />Institutional Dashboard</h1>
        <p className="text-gray-700 text-base mt-1">Read-only institutional view of students across departments, programmes and colleges.</p>
      </div>

      <div className="flex flex-wrap gap-3 mb-4">
        <div className="relative">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search name, roll no., email…"
            className="pl-9 pr-3 py-2.5 border border-gray-200 rounded-xl text-base w-72 focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
        </div>
        {filterSelect("Academic Year", academicYearId, (v) => { changeFilter(setAcademicYearId)(v); setSemesterId(""); }, calendars.map((c) => ({ value: c.id, label: c.academic_year })))}
        {filterSelect("Semester", semesterId, changeFilter(setSemesterId), semesters.map((s) => ({ value: s.id, label: s.name })))}
        {filterSelect("Department", departmentId, changeFilter(setDepartmentId), departments.map((d) => ({ value: d.id, label: d.name })))}
        {filterSelect("Programme", programId, changeFilter(setProgramId), programmes.map((p) => ({ value: p.id, label: p.name })))}
        {filterSelect("College", collegeId, changeFilter(setCollegeId), colleges.map((c) => ({ value: c.id, label: c.name })))}
      </div>

      <div className="bg-white rounded-2xl border border-gray-200 overflow-auto max-h-[65vh]">
        {isLoading ? (
          <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div>
        ) : isError ? (
          <div className="text-center py-16 text-red-600">Could not load students. Please try again.</div>
        ) : students.length === 0 ? (
          <div className="text-center py-16 text-gray-600"><LayoutGrid size={40} className="mx-auto mb-3 opacity-30" /><p>No students match the current filters.</p></div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
              <tr>{["Name", "Roll No", "Programme", "Department", "College", "Academic Year", "Semester", "Status"].map((h) => (
                <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700 whitespace-nowrap">{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {students.map((s, i) => (
                <tr key={s.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                  <td className="px-4 py-3 font-medium">{s.full_name}<p className="text-xs text-gray-500 font-normal">{s.email}</p></td>
                  <td className="px-4 py-3 font-mono text-[#0D6E6E] whitespace-nowrap">{s.student_roll ?? "—"}</td>
                  <td className="px-4 py-3 text-gray-700">{s.program_name ?? "—"}</td>
                  <td className="px-4 py-3 text-gray-700">{s.department_name ?? "—"}</td>
                  <td className="px-4 py-3 text-gray-700">{s.college_name ?? "—"}</td>
                  <td className="px-4 py-3 text-gray-700 whitespace-nowrap">{s.academic_year ?? <span className="text-gray-400">Not assigned</span>}</td>
                  <td className="px-4 py-3 text-gray-700">{s.latest_semester ?? "—"}</td>
                  <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${s.is_active ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-600"}`}>{s.is_active ? "Active" : "Inactive"}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="flex items-center justify-between mt-3 text-sm text-gray-700">
        <p>{total === 0 ? "0 students" : `Showing ${(page - 1) * PAGE_SIZE + 1}–${Math.min(page * PAGE_SIZE, total)} of ${total} student${total === 1 ? "" : "s"}`}{isFetching && !isLoading ? " · updating…" : ""}</p>
        <div className="flex items-center gap-2">
          <button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page <= 1} aria-label="Previous page"
            className="p-2 border border-gray-200 rounded-lg disabled:opacity-40 hover:bg-gray-50"><ChevronLeft size={16} /></button>
          <span>Page {page} of {pages}</span>
          <button onClick={() => setPage((p) => Math.min(pages, p + 1))} disabled={page >= pages} aria-label="Next page"
            className="p-2 border border-gray-200 rounded-lg disabled:opacity-40 hover:bg-gray-50"><ChevronRight size={16} /></button>
        </div>
      </div>
    </div>
  );
}
