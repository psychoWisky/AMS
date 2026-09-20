"use client";
import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { GraduationCap, Search, Loader2, Pencil, ChevronLeft, ChevronRight } from "lucide-react";

// Super Admin's global student management (route access: see lib/navigation.ts;
// every endpoint below is independently Super Admin-only on the backend).
// There is no separate Student table — a student is a user account. A student's
// Academic Year / Semester are derived from their registrations and enrollments,
// so they are filters and read-only columns here, not editable fields.

interface Student {
  id: string; email: string; full_name: string;
  first_name: string | null; middle_name: string | null; last_name: string | null;
  student_roll: string | null; mobile: string | null; date_of_birth: string | null;
  gender: string | null; blood_group: string | null; father_name: string | null;
  abc_id: string | null; address: string | null; admission_year: number | null;
  program_id: string | null; program_name: string | null; program_code: string | null;
  department_id: string | null; department_name: string | null;
  college_id: string | null; college_name: string | null;
  is_active: boolean; latest_academic_year: string | null; latest_semester: string | null;
}
interface StudentPage { items: Student[]; total: number; page: number; page_size: number; }
interface Opt { id: string; name: string; }
interface Calendar { id: string; academic_year: string; }
interface Semester { id: string; name: string; calendar_id?: string; }
interface Programme { id: string; name: string; code: string; }

const PAGE_SIZE = 25;
const GENDERS = ["Male", "Female", "Other"];
const BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"];

const EMPTY_FORM = {
  first_name: "", middle_name: "", last_name: "", student_roll: "", email: "", mobile: "", date_of_birth: "",
  gender: "", blood_group: "", father_name: "", abc_id: "", address: "", admission_year: "",
  program_id: "", department_id: "", college_id: "", is_active: true,
};
type Form = typeof EMPTY_FORM;
// Fields a student cannot exist without: replaced, never cleared.
const REQUIRED: (keyof Form)[] = ["first_name", "last_name", "student_roll", "email", "program_id", "department_id"];

function errorMessage(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail[0]?.msg) {
    const where = Array.isArray(detail[0].loc) ? String(detail[0].loc[detail[0].loc.length - 1]).replace(/_/g, " ") : "";
    return `${where ? where + ": " : ""}${detail[0].msg}`;
  }
  return fallback;
}

function toForm(s: Student): Form {
  return {
    first_name: s.first_name ?? "", middle_name: s.middle_name ?? "", last_name: s.last_name ?? "",
    student_roll: s.student_roll ?? "", email: s.email, mobile: s.mobile ?? "", date_of_birth: s.date_of_birth ?? "",
    gender: s.gender ?? "", blood_group: s.blood_group ?? "", father_name: s.father_name ?? "", abc_id: s.abc_id ?? "",
    address: s.address ?? "", admission_year: s.admission_year ? String(s.admission_year) : "",
    program_id: s.program_id ?? "", department_id: s.department_id ?? "", college_id: s.college_id ?? "", is_active: s.is_active,
  };
}

export default function StudentsPage() {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [q, setQ] = useState("");
  const [academicYearId, setAcademicYearId] = useState("");
  const [semesterId, setSemesterId] = useState("");
  const [departmentId, setDepartmentId] = useState("");
  const [programId, setProgramId] = useState("");
  const [collegeId, setCollegeId] = useState("");
  const [page, setPage] = useState(1);
  const [editing, setEditing] = useState<Student | null>(null);
  const [form, setForm] = useState<Form>(EMPTY_FORM);
  const [initial, setInitial] = useState<Form>(EMPTY_FORM);

  // Search is sent to the server (debounced); the page resets whenever a filter changes.
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
    queryKey: ["ams-students-semesters", academicYearId],
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
  // The Edit dialog's Department options follow the chosen Programme (Department<->Programme association).
  const { data: formDepartments = [] } = useQuery<Opt[]>({
    queryKey: ["ams-departments-for-program", form.program_id],
    queryFn: async () => (await api.get("/departments", { params: { program_id: form.program_id } })).data,
    enabled: !!editing && !!form.program_id,
  });

  // College <-> Programme dependent options (UX only — the backend rejects any pair that is
  // not mapped): a chosen college narrows the programmes to the ones it offers, and a chosen
  // programme narrows the colleges to the ones offering it. Both lists come from the same mapping,
  // so the two dropdowns can never strand each other.
  const { data: collegeProgrammes, isSuccess: collegeProgrammesLoaded } = useQuery<{ program_id: string }[]>({
    queryKey: ["ams-college-programs", form.college_id],
    queryFn: async () => (await api.get(`/admin/colleges/${form.college_id}/programs`)).data,
    enabled: !!editing && !!form.college_id,
  });
  const { data: programmeColleges, isSuccess: programmeCollegesLoaded } = useQuery<{ college_id: string }[]>({
    queryKey: ["ams-program-colleges", form.program_id],
    queryFn: async () => (await api.get(`/departments/programs/${form.program_id}/colleges`)).data,
    enabled: !!editing && !!form.program_id,
  });
  const offeredProgrammeIds = form.college_id && collegeProgrammesLoaded ? new Set((collegeProgrammes ?? []).map((r) => r.program_id)) : null;
  const offeringCollegeIds = form.program_id && programmeCollegesLoaded ? new Set((programmeColleges ?? []).map((r) => r.college_id)) : null;
  const relationChanged = form.college_id !== initial.college_id || form.program_id !== initial.program_id;
  const pairNotMapped = !!(offeredProgrammeIds && form.program_id && !offeredProgrammeIds.has(form.program_id));

  const params = {
    q: q || undefined, academic_year_id: academicYearId || undefined, semester_id: semesterId || undefined,
    department_id: departmentId || undefined, program_id: programId || undefined, college_id: collegeId || undefined,
    page, page_size: PAGE_SIZE,
  };
  const { data, isLoading, isError, isFetching } = useQuery<StudentPage>({
    queryKey: ["ams-students", params],
    queryFn: async () => (await api.get("/students", { params })).data,
    placeholderData: (previous) => previous,
  });
  const students = data?.items ?? [];
  const total = data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const openEdit = (s: Student) => { const f = toForm(s); setForm(f); setInitial(f); setEditing(s); };
  const closeEdit = () => { setEditing(null); setForm(EMPTY_FORM); setInitial(EMPTY_FORM); };

  const save = useMutation({
    mutationFn: () => {
      // Only fields the admin changed are sent; a blank optional field clears it (null).
      const body: Record<string, string | number | boolean | null> = {};
      (Object.keys(form) as (keyof Form)[]).forEach((k) => {
        if (form[k] === initial[k]) return;
        const v = form[k];
        body[k] = typeof v === "boolean" ? v : k === "admission_year" ? (v ? Number(v) : null) : (v === "" ? null : v);
      });
      return api.patch(`/students/${editing?.id}`, body);
    },
    onSuccess: () => {
      toast.success("Student updated.");
      qc.invalidateQueries({ queryKey: ["ams-students"] });
      closeEdit();
    },
    onError: (e: unknown) => toast.error(errorMessage(e, "Failed to update student.")),
  });

  function submit() {
    const missing = REQUIRED.filter((k) => !String(form[k]).trim());
    if (missing.length) { toast.error(`Required: ${missing.map((k) => k.replace(/_/g, " ")).join(", ")}.`); return; }
    if (JSON.stringify(form) === JSON.stringify(initial)) { closeEdit(); return; }
    if (relationChanged && pairNotMapped) { toast.error("This programme is not offered by the selected college. Choose a mapped combination."); return; }
    save.mutate();
  }

  const input = (label: string, key: keyof Form, type = "text", required = false) => (
    <div>
      <label className="block text-base font-semibold text-gray-700 mb-1">{label}{required ? " *" : ""}</label>
      <input type={type} value={String(form[key])} onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.value }))}
        className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
    </div>
  );
  const select = (label: string, key: keyof Form, options: { value: string; label: string }[], required = false) => (
    <div>
      <label className="block text-base font-semibold text-gray-700 mb-1">{label}{required ? " *" : ""}</label>
      <select value={String(form[key])} onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.value }))}
        className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
        <option value="">{required ? "Select…" : "None"}</option>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </div>
  );
  const filterSelect = (label: string, value: string, onChange: (v: string) => void, options: { value: string; label: string }[], disabled = false) => (
    <select value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled} aria-label={label}
      className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] bg-white max-w-[220px] disabled:opacity-50">
      <option value="">{label}: All</option>
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );

  return (
    <div className="p-6 w-full">
      <div className="mb-6">
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><GraduationCap size={24} className="text-[#0D6E6E]" />Students</h1>
        <p className="text-gray-700 text-base mt-1">All students — search, filter by academic placement, and edit their full profile</p>
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
          <div className="text-center py-16 text-gray-600"><GraduationCap size={40} className="mx-auto mb-3 opacity-30" /><p>No students match the current filters.</p></div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
              <tr>{["Name", "Roll No", "Programme", "Department", "College", "Academic Year", "Semester", "Status", "Action"].map((h) => (
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
                  <td className="px-4 py-3 text-gray-700 whitespace-nowrap">{s.latest_academic_year ?? "—"}</td>
                  <td className="px-4 py-3 text-gray-700">{s.latest_semester ?? "—"}</td>
                  <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${s.is_active ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-600"}`}>{s.is_active ? "Active" : "Inactive"}</span></td>
                  <td className="px-4 py-3">
                    <button onClick={() => openEdit(s)} title="Edit student" className="p-1.5 text-gray-600 hover:bg-gray-100 rounded-lg"><Pencil size={16} /></button>
                  </td>
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

      {editing && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-3xl p-6 max-h-[90vh] overflow-y-auto">
            <h3 className="text-xl font-bold mb-1">Edit Student</h3>
            <p className="text-sm text-gray-600 mb-4">
              {editing.full_name}. Academic Year and Semester are derived from the student&apos;s registrations and enrollments and cannot be edited here.
            </p>

            <p className="text-sm font-bold text-gray-800 mb-2">Identity</p>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-4">
              {input("First Name", "first_name", "text", true)}
              {input("Middle Name", "middle_name")}
              {input("Last Name", "last_name", "text", true)}
              {input("Roll Number", "student_roll", "text", true)}
              {input("Email", "email", "email", true)}
              {input("ABC ID", "abc_id")}
            </div>

            <p className="text-sm font-bold text-gray-800 mb-2">Personal</p>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-4">
              {input("Date of Birth", "date_of_birth", "date")}
              {select("Gender", "gender", GENDERS.map((g) => ({ value: g, label: g })))}
              {select("Blood Group", "blood_group", BLOOD_GROUPS.map((g) => ({ value: g, label: g })))}
              {input("Father's Name", "father_name")}
              {input("Mobile No.", "mobile")}
              <div className="md:col-span-2">
                <label className="block text-base font-semibold text-gray-700 mb-1">Address</label>
                <textarea rows={2} value={form.address} onChange={(e) => setForm((f) => ({ ...f, address: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] resize-none" />
              </div>
            </div>

            <p className="text-sm font-bold text-gray-800 mb-2">Academic</p>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-4">
              {select("Programme", "program_id", programmes
                .filter((p) => !offeredProgrammeIds || offeredProgrammeIds.has(p.id) || p.id === form.program_id)
                .map((p) => ({ value: p.id, label: offeredProgrammeIds && !offeredProgrammeIds.has(p.id) ? `${p.name} (not offered by this college)` : p.name })), true)}
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Department *</label>
                <select value={form.department_id} onChange={(e) => setForm((f) => ({ ...f, department_id: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">Select…</option>
                  {/* keep the current value selectable while the programme's department list loads */}
                  {(form.program_id ? formDepartments : departments).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                  {form.department_id && !(form.program_id ? formDepartments : departments).some((d) => d.id === form.department_id) && (
                    <option value={form.department_id}>{editing.department_name ?? "Current department"}</option>
                  )}
                </select>
                {form.program_id && formDepartments.length === 0 && (
                  <p className="text-xs text-amber-600 mt-1">No departments are associated with this programme yet.</p>
                )}
              </div>
              {select("College", "college_id", colleges
                .filter((c) => !offeringCollegeIds || offeringCollegeIds.has(c.id) || c.id === form.college_id)
                .map((c) => ({ value: c.id, label: offeringCollegeIds && !offeringCollegeIds.has(c.id) ? `${c.name} (does not offer this programme)` : c.name })))}
              {input("Admission Year", "admission_year", "number")}
              {offeredProgrammeIds && offeredProgrammeIds.size === 0 && (
                <p className="md:col-span-2 text-xs text-amber-600">No programmes are mapped to this college yet — map them under Administration → Colleges.</p>
              )}
              {relationChanged && pairNotMapped && offeredProgrammeIds && offeredProgrammeIds.size > 0 && (
                <p className="md:col-span-2 text-xs text-amber-600">The selected programme is not offered by the selected college; choose a mapped combination to save.</p>
              )}
            </div>

            <label className="flex items-center gap-2 text-base font-semibold text-gray-700 mb-1">
              <input type="checkbox" checked={form.is_active} onChange={(e) => setForm((f) => ({ ...f, is_active: e.target.checked }))} className="w-4 h-4 accent-[#0D6E6E]" />
              Account active (an inactive student cannot log in)
            </label>

            <div className="flex gap-3 mt-5">
              <button onClick={closeEdit} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium hover:bg-gray-50">Cancel</button>
              <button onClick={submit} disabled={save.isPending}
                className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-60">
                {save.isPending ? "Saving…" : "Save Changes"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
