"use client";
import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useRole, useUser } from "@/stores/auth.store";
import { toast } from "sonner";
import { ADMIN_ROLES, COURSE_CATEGORY_LABELS, CREDIT_TYPE_LABELS } from "@/lib/utils";
import { BookOpen, Plus, Search, Loader2, Globe, EyeOff, Pencil, Trash2, X, Crown, UserPlus } from "lucide-react";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";

interface Course {
  id: string; course_number: string; title: string; credit_structure: string;
  credit_theory: number; credit_practical: number;
  course_type: string; category: string | null; credit_type: string | null;
  program_level: string; status: string; department_id: string | null;
  department_name: string | null; college_name: string | null;
  is_research: boolean; is_compulsory: boolean;
}
interface Offering {
  id: string; course_number: string; course_title: string; credit_structure: string;
  category: string | null; credit_type: string | null; is_research: boolean;
  semester_name: string | null; section: string | null; max_enrollment: number;
  enrolled_count: number; status: string; faculty_names: string[];
  department_id: string | null; department_name: string | null; stream: string | null;
}
interface OfferingInstructor { id: string; name: string; designation: string | null; department_name: string | null; role: string; is_leader: boolean; }
interface OfferingDetail extends Offering { faculty: OfferingInstructor[]; }
interface Calendar { id: string; name: string; academic_year: string; }
interface Semester { id: string; calendar_id: string; name: string; }
interface FacultyUser { id: string; full_name: string; designation: string | null; }
interface DepartmentOpt { id: string; name: string; code: string; stream: string | null; }

const CATEGORY_OPTIONS = Object.keys(COURSE_CATEGORY_LABELS);
const CREDIT_TYPE_OPTIONS = Object.keys(CREDIT_TYPE_LABELS);
const CREDIT_FORMATS = ["2+0","0+2","1+1","0+1","3+0","2+1","1+2","3+1","4+0","0+4","2+2"];
const LEVELS = ["UG","PG","PhD"];
const STATUS_COLOR: Record<string, string> = { active: "bg-green-100 text-green-700", inactive: "bg-gray-100 text-gray-600", archived: "bg-red-100 text-red-700", draft: "bg-gray-100 text-gray-700", published: "bg-green-100 text-green-700", closed: "bg-red-100 text-red-700" };
const MAX_OFFERING_FACULTY = 3;

const EMPTY_COURSE_FORM = { course_number: "", title: "", credit_theory: "3", credit_practical: "0", program_level: "UG", category: "", credit_type: "", status: "active", department_id: "" };

export default function CoursesPage() {
  const role = useRole();
  const user = useUser();
  const qc = useQueryClient();
  const isAdmin = role ? ADMIN_ROLES.includes(role) : false;
  const isHod = role === "hod";
  const [tab, setTab] = useState<"courses" | "offerings">("courses");
  const [search, setSearch] = useState("");
  const [levelFilter, setLevelFilter] = useState("");

  // Course Management state
  const [showCreate, setShowCreate] = useState(false);
  const [editCourse, setEditCourse] = useState<Course | null>(null);
  const [form, setForm] = useState(EMPTY_COURSE_FORM);
  const [confirm, setConfirm] = useState<{ action: () => void; title: string; message: string; confirmLabel: string; confirmClassName?: string } | null>(null);

  // Offer Course state
  const [offerCalendarId, setOfferCalendarId] = useState("");
  const [offerSemesterId, setOfferSemesterId] = useState("");
  const [offerLevel, setOfferLevel] = useState("");
  const [showOfferingCreate, setShowOfferingCreate] = useState(false);
  const [offeringForm, setOfferingForm] = useState({ calendar_id: "", semester_id: "", course_id: "", section: "", max_enrollment: "60", department_id: "" });
  const [facultySearch, setFacultySearch] = useState("");
  const [facultyOpen, setFacultyOpen] = useState(false);
  const [selectedFaculty, setSelectedFaculty] = useState<{ id: string; name: string }[]>([]);
  const [leaderId, setLeaderId] = useState("");
  const [selectedOffering, setSelectedOffering] = useState<OfferingDetail | null>(null);

  const { data: departments = [] } = useQuery<DepartmentOpt[]>({
    queryKey: ["ams-departments"],
    queryFn: async () => (await api.get("/departments")).data,
  });
  const hodDept = departments.find((d) => d.id === user?.department_id);

  const { data: courses = [], isLoading } = useQuery<Course[]>({
    queryKey: ["ams-courses"],
    queryFn: async () => (await api.get("/courses")).data,
  });

  const { data: offerings = [], isLoading: offeringsLoading } = useQuery<Offering[]>({
    queryKey: ["ams-offerings", offerCalendarId, offerSemesterId, offerLevel],
    queryFn: async () => (await api.get("/courses/offerings/all", {
      params: { calendar_id: offerCalendarId || undefined, semester_id: offerSemesterId || undefined, level: offerLevel || undefined },
    })).data,
    enabled: tab === "offerings",
  });

  const { data: calendars = [] } = useQuery<Calendar[]>({
    queryKey: ["ams-calendars"],
    queryFn: async () => (await api.get("/academic/calendars")).data,
  });

  const { data: offerSemesters = [] } = useQuery<Semester[]>({
    queryKey: ["ams-semesters-for-offer-filter", offerCalendarId],
    queryFn: async () => (await api.get(`/academic/calendars/${offerCalendarId}/semesters`)).data,
    enabled: !!offerCalendarId,
  });

  const { data: createSemesters = [] } = useQuery<Semester[]>({
    queryKey: ["ams-semesters-for-offering", offeringForm.calendar_id],
    queryFn: async () => (await api.get(`/academic/calendars/${offeringForm.calendar_id}/semesters`)).data,
    enabled: !!offeringForm.calendar_id,
  });

  const { data: facultyUsers = [] } = useQuery<FacultyUser[]>({
    // BUSINESS_LOGIC.md Section N.4 — for HOD this is scoped to their own
    // department via the department_id param; the backend also enforces this
    // scope server-side for HOD regardless of what's requested here.
    queryKey: ["ams-faculty", isHod ? user?.department_id : "all"],
    queryFn: async () => (await api.get("/auth/users", { params: isHod ? { department_id: user?.department_id } : {} }))
      .data.filter((u: { role: string }) => ["faculty","hod","research_supervisor"].includes(u.role)),
    enabled: showOfferingCreate,
  });

  const filteredFaculty = useMemo(() => {
    const q = facultySearch.trim().toLowerCase();
    return facultyUsers
      .filter((f) => !selectedFaculty.some((s) => s.id === f.id))
      .filter((f) => !q || f.full_name.toLowerCase().includes(q));
  }, [facultyUsers, facultySearch, selectedFaculty]);

  // ── Course Management mutations ──────────────────────────────────────────

  const createCourse = useMutation({
    mutationFn: (d: typeof form) => api.post("/courses", {
      ...d, credit_theory: parseInt(d.credit_theory), credit_practical: parseInt(d.credit_practical),
      category: d.category || null, credit_type: d.credit_type || null,
      department_id: d.department_id || null,
    }),
    onSuccess: () => { toast.success("Course created."); qc.invalidateQueries({ queryKey: ["ams-courses"] }); closeCourseModal(); },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "Failed."),
  });

  const updateCourse = useMutation({
    mutationFn: (d: typeof form) => api.put(`/courses/${editCourse?.id}`, {
      ...d, credit_theory: parseInt(d.credit_theory), credit_practical: parseInt(d.credit_practical),
      category: d.category || null, credit_type: d.credit_type || null,
      department_id: d.department_id || null,
    }),
    onSuccess: () => { toast.success("Course updated."); qc.invalidateQueries({ queryKey: ["ams-courses"] }); closeCourseModal(); },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "Failed to update course."),
  });

  const deleteCourse = useMutation({
    mutationFn: (id: string) => api.delete(`/courses/${id}`),
    onSuccess: () => { toast.success("Course deleted."); qc.invalidateQueries({ queryKey: ["ams-courses"] }); },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "Failed to delete course."),
  });

  function openCreate() {
    setEditCourse(null);
    setForm({ ...EMPTY_COURSE_FORM, department_id: isHod ? (user?.department_id ?? "") : "" });
    setShowCreate(true);
  }
  function openEdit(c: Course) {
    setEditCourse(c);
    setForm({
      course_number: c.course_number, title: c.title,
      credit_theory: String(c.credit_theory ?? 0), credit_practical: String(c.credit_practical ?? 0),
      program_level: c.program_level, category: c.category ?? "", credit_type: c.credit_type ?? "",
      status: c.status, department_id: c.department_id ?? "",
    });
    setShowCreate(true);
  }
  function closeCourseModal() { setShowCreate(false); setEditCourse(null); setForm(EMPTY_COURSE_FORM); }

  // ── Offer Course mutations ───────────────────────────────────────────────

  const createOffering = useMutation({
    mutationFn: () => api.post("/courses/offerings", {
      calendar_id: offeringForm.calendar_id,
      semester_id: offeringForm.semester_id,
      course_id: offeringForm.course_id,
      department_id: isHod ? user?.department_id : offeringForm.department_id,
      max_enrollment: parseInt(offeringForm.max_enrollment),
      section: offeringForm.section || null,
      faculty_ids: selectedFaculty.map((f) => f.id),
      leader_id: leaderId,
    }),
    onSuccess: () => {
      toast.success("Offering created.");
      qc.invalidateQueries({ queryKey: ["ams-offerings"] });
      closeOfferingModal();
    },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "Failed to create offering."),
  });

  const setOfferingStatus = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      api.patch(`/courses/offerings/${id}/status?status=${status}`),
    onSuccess: (_, { status }) => {
      toast.success(status === "published" ? "Offering published — students can now enroll." : status === "closed" ? "Offering removed." : "Offering updated.");
      qc.invalidateQueries({ queryKey: ["ams-offerings"] });
    },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "Failed."),
  });

  function closeOfferingModal() {
    setShowOfferingCreate(false);
    setOfferingForm({ calendar_id: "", semester_id: "", course_id: "", section: "", max_enrollment: "60", department_id: "" });
    setSelectedFaculty([]); setLeaderId(""); setFacultySearch("");
  }

  async function openOfferingDetail(offeringId: string) {
    try {
      const res = await api.get(`/courses/offerings/${offeringId}`);
      setSelectedOffering(res.data);
    } catch {
      toast.error("Failed to load offering.");
    }
  }

  function addFaculty(f: FacultyUser) {
    if (selectedFaculty.length >= MAX_OFFERING_FACULTY) { toast.error(`At most ${MAX_OFFERING_FACULTY} faculty may be assigned.`); return; }
    setSelectedFaculty((s) => [...s, { id: f.id, name: f.full_name }]);
    if (!leaderId) setLeaderId(f.id);
    setFacultySearch("");
  }
  function removeFaculty(id: string) {
    setSelectedFaculty((s) => s.filter((f) => f.id !== id));
    if (leaderId === id) setLeaderId("");
  }

  const filteredCourses = courses.filter((c) =>
    (!levelFilter || c.program_level === levelFilter) &&
    (!search || c.course_number.toLowerCase().includes(search.toLowerCase()) || c.title.toLowerCase().includes(search.toLowerCase()))
  );

  const canManage = isAdmin; // ADMIN_ROLES already includes "hod" — see lib/utils.ts

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><BookOpen size={24} className="text-[#0D6E6E]" />{tab === "courses" ? "Course Management" : "Offer Course"}</h1>
          <p className="text-gray-700 text-base mt-1">
            {tab === "courses" ? "Course catalog and credit structures" : "Offer courses for masters and PhD degree programme"}
          </p>
        </div>
        {canManage && tab === "courses" && (
          <button onClick={openCreate}
            className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-base hover:bg-[#178F8F]">
            <Plus size={16} /> Add Course
          </button>
        )}
        {canManage && tab === "offerings" && (
          <button onClick={() => setShowOfferingCreate(true)}
            className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-base hover:bg-[#178F8F]">
            <Plus size={16} /> Click to Offer Course
          </button>
        )}
      </div>

      {/* Tabs */}
      <div className="flex gap-2 mb-5">
        {(["courses","offerings"] as const).map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-4 py-2 rounded-xl text-sm font-semibold transition-colors ${tab === t ? "bg-[#0D6E6E] text-white" : "text-gray-600 hover:bg-gray-100"}`}>
            {t === "courses" ? "Course Management" : "Offer Course"}
          </button>
        ))}
      </div>

      {/* ── Course Management ─────────────────────────────────────────────── */}
      {tab === "courses" && (
        <>
          <div className="flex gap-3 mb-4">
            <div className="relative flex-1 max-w-xs">
              <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-600" />
              <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search courses…"
                className="w-full pl-9 pr-4 py-2.5 border border-gray-200 rounded-xl text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
            </div>
            <select value={levelFilter} onChange={(e) => setLevelFilter(e.target.value)}
              className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
              <option value="">All Programmes</option>
              {LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
            </select>
          </div>

          <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden overflow-x-auto">
            {isLoading ? (
              <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
            ) : filteredCourses.length === 0 ? (
              <div className="text-center py-16 text-gray-600"><BookOpen size={40} className="mx-auto mb-3 opacity-30" /><p>No courses found.</p></div>
            ) : (
              <table className="w-full text-sm min-w-[1000px]">
                <thead className="bg-gray-50 border-b border-gray-200">
                  <tr>{["Sl No", "Course College", "Course Number", "Course Title", "Programme", "Credit", "Credit Type", "Research", "Compulsory", ...(canManage ? ["Action"] : [])].map((h) => (
                    <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700 whitespace-nowrap">{h}</th>
                  ))}</tr>
                </thead>
                <tbody>
                  {filteredCourses.map((c, i) => (
                    <tr key={c.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                      <td className="px-4 py-3 text-gray-600">{i + 1}</td>
                      <td className="px-4 py-3 text-gray-600">{c.college_name ?? "—"}</td>
                      <td className="px-4 py-3 font-mono font-bold text-[#0D6E6E] whitespace-nowrap">{c.course_number}</td>
                      <td className="px-4 py-3 font-medium text-gray-900 max-w-xs truncate">{c.title}</td>
                      <td className="px-4 py-3"><span className="px-2 py-0.5 bg-blue-50 text-blue-700 rounded text-sm font-semibold">{c.program_level}</span></td>
                      <td className="px-4 py-3"><span className="font-mono text-sm bg-[#E6F4F4] text-[#0D6E6E] px-2 py-0.5 rounded">{c.credit_structure}</span></td>
                      <td className="px-4 py-3 text-gray-600">{c.credit_type ? CREDIT_TYPE_LABELS[c.credit_type] : "—"}</td>
                      <td className="px-4 py-3">{c.is_research ? <span className="text-green-600 font-semibold">Yes</span> : <span className="text-gray-400">No</span>}</td>
                      <td className="px-4 py-3">{c.is_compulsory ? <span className="text-green-600 font-semibold">Yes</span> : <span className="text-gray-400">No</span>}</td>
                      {canManage && (
                        <td className="px-4 py-3">
                          <div className="flex gap-1.5">
                            <button onClick={() => openEdit(c)} className="p-1.5 text-gray-600 hover:bg-gray-100 rounded-lg" title="Update"><Pencil size={15} /></button>
                            <button onClick={() => setConfirm({
                              action: () => deleteCourse.mutate(c.id),
                              title: "Delete Course", message: `Delete ${c.course_number} — ${c.title}? This cannot be undone.`,
                              confirmLabel: "Yes, Delete", confirmClassName: "bg-red-600 hover:bg-red-700 text-white",
                            })} className="p-1.5 text-red-500 hover:bg-red-50 rounded-lg" title="Delete"><Trash2 size={15} /></button>
                          </div>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}

      {/* Add/Edit Course Modal */}
      {showCreate && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md p-6 max-h-[90vh] overflow-y-auto">
            <h3 className="text-xl font-bold mb-4">{editCourse ? "Update Course" : "Add New Course"}</h3>
            <div className="space-y-3">
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Course Number *</label>
                <input value={form.course_number} onChange={(e) => setForm((f) => ({ ...f, course_number: e.target.value }))} placeholder="AGR101"
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Course Title *</label>
                <input value={form.title} onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))} placeholder="Principles of Agronomy"
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Programme</label>
                <div className="flex gap-2">
                  {LEVELS.map((l) => (
                    <button key={l} type="button" onClick={() => setForm((f) => ({ ...f, program_level: l }))}
                      className={`flex-1 py-2 rounded-xl text-sm font-semibold border-2 transition-all ${form.program_level === l ? "border-[#0D6E6E] bg-[#0D6E6E] text-white" : "border-gray-200 text-gray-600"}`}>
                      {l}
                    </button>
                  ))}
                </div>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Course Type</label>
                <select value={form.category} onChange={(e) => setForm((f) => ({ ...f, category: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">Select…</option>
                  {CATEGORY_OPTIONS.map((c) => <option key={c} value={c}>{COURSE_CATEGORY_LABELS[c]}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Credit Type</label>
                <select value={form.credit_type} onChange={(e) => setForm((f) => ({ ...f, credit_type: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">Select…</option>
                  {CREDIT_TYPE_OPTIONS.map((c) => <option key={c} value={c}>{CREDIT_TYPE_LABELS[c]}</option>)}
                </select>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Theory Credit</label>
                  <input type="number" min={0} max={6} value={form.credit_theory}
                    onChange={(e) => setForm((f) => ({ ...f, credit_theory: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Practical Credit</label>
                  <input type="number" min={0} max={6} value={form.credit_practical}
                    onChange={(e) => setForm((f) => ({ ...f, credit_practical: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
              </div>
              <p className="text-sm text-gray-600">Supported formats: {CREDIT_FORMATS.join(", ")} — current: <span className="font-bold text-[#0D6E6E]">{form.credit_theory}+{form.credit_practical}</span></p>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Status</label>
                <div className="flex gap-2">
                  {["active","inactive"].map((s) => (
                    <button key={s} type="button" onClick={() => setForm((f) => ({ ...f, status: s }))}
                      className={`flex-1 py-2 rounded-xl text-sm font-semibold border-2 capitalize transition-all ${form.status === s ? "border-[#0D6E6E] bg-[#0D6E6E] text-white" : "border-gray-200 text-gray-600"}`}>
                      {s}
                    </button>
                  ))}
                </div>
              </div>
              {isHod ? (
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Department</label>
                  <p className="text-base text-gray-700 bg-gray-50 border border-gray-200 rounded-xl px-3 py-2">{hodDept?.name ?? "Your department"}</p>
                </div>
              ) : (
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Department</label>
                  <select value={form.department_id} onChange={(e) => setForm((f) => ({ ...f, department_id: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                    <option value="">None</option>
                    {departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                  </select>
                </div>
              )}
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={closeCourseModal} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium hover:bg-gray-50">Cancel</button>
              <button
                onClick={() => {
                  if (!form.course_number || !form.title) { toast.error("Course Number and Course Title are required."); return; }
                  if (editCourse) { updateCourse.mutate(form); } else { createCourse.mutate(form); }
                }}
                disabled={createCourse.isPending || updateCourse.isPending}
                className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-60">
                {createCourse.isPending || updateCourse.isPending ? "Saving…" : editCourse ? "Save Changes" : "Create Course"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Offer Course ──────────────────────────────────────────────────── */}
      {tab === "offerings" && (
        <>
          <div className="flex gap-3 mb-4">
            <select value={offerCalendarId} onChange={(e) => { setOfferCalendarId(e.target.value); setOfferSemesterId(""); }}
              className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
              <option value="">Select Academic Year…</option>
              {calendars.map((c) => <option key={c.id} value={c.id}>{c.academic_year}</option>)}
            </select>
            <select value={offerSemesterId} onChange={(e) => setOfferSemesterId(e.target.value)} disabled={!offerCalendarId}
              className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none disabled:opacity-50">
              <option value="">Select Semester…</option>
              {offerSemesters.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
            <select value={offerLevel} onChange={(e) => setOfferLevel(e.target.value)}
              className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
              <option value="">Select Programme…</option>
              {LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
            </select>
          </div>

          <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden overflow-x-auto">
            {offeringsLoading ? (
              <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
            ) : offerings.length === 0 ? (
              <div className="text-center py-16 text-gray-600"><BookOpen size={40} className="mx-auto mb-3 opacity-30" /><p>No offerings found for this filter.</p></div>
            ) : (
              <table className="w-full text-sm min-w-[900px]">
                <thead className="bg-gray-50 border-b border-gray-200">
                  <tr>{["Sl No", "Course Number", "Course Title", "Semester", "Credit", "Credit Type", "Research", "Status", "Action"].map((h) => (
                    <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700 whitespace-nowrap">{h}</th>
                  ))}</tr>
                </thead>
                <tbody>
                  {offerings.map((o, i) => (
                    <tr key={o.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                      <td className="px-4 py-3 text-gray-600">{i + 1}</td>
                      <td className="px-4 py-3 font-mono font-bold text-[#0D6E6E] whitespace-nowrap">{o.course_number}</td>
                      <td className="px-4 py-3 max-w-xs truncate">{o.course_title}</td>
                      <td className="px-4 py-3 text-gray-600 whitespace-nowrap">{o.semester_name ?? "—"}</td>
                      <td className="px-4 py-3 font-mono text-sm">{o.credit_structure}</td>
                      <td className="px-4 py-3 text-gray-600">{o.credit_type ? CREDIT_TYPE_LABELS[o.credit_type] : "—"}</td>
                      <td className="px-4 py-3">{o.is_research ? <span className="text-green-600 font-semibold">Yes</span> : <span className="text-gray-400">No</span>}</td>
                      <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-sm font-semibold ${STATUS_COLOR[o.status] ?? "bg-gray-100"}`}>{o.status}</span></td>
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2">
                          <button onClick={() => openOfferingDetail(o.id)}
                            className="text-sm font-semibold text-[#0D6E6E] hover:underline">View Details</button>
                          {canManage && o.status !== "closed" && (
                            <>
                              {o.status === "draft" && (
                                <button onClick={() => setOfferingStatus.mutate({ id: o.id, status: "published" })}
                                  className="flex items-center gap-1 text-sm font-semibold text-green-700 hover:underline"><Globe size={12} /> Publish</button>
                              )}
                              <button onClick={() => setConfirm({
                                action: () => setOfferingStatus.mutate({ id: o.id, status: "closed" }),
                                title: "Remove Offering", message: `Remove ${o.course_number} — ${o.course_title} from this semester? Students will no longer see or be able to register for it.`,
                                confirmLabel: "Yes, Remove", confirmClassName: "bg-red-600 hover:bg-red-700 text-white",
                              })} className="flex items-center gap-1 text-sm font-semibold text-red-600 hover:underline"><EyeOff size={12} /> Remove</button>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}

      {/* Create Offering Modal */}
      {showOfferingCreate && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-lg p-6 max-h-[90vh] overflow-y-auto">
            <h3 className="text-xl font-bold mb-1">Offer Course</h3>
            <p className="text-sm text-gray-600 mb-4">Offer courses for masters and PhD degree programme.</p>
            <div className="space-y-3">
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Academic Year *</label>
                <select value={offeringForm.calendar_id} onChange={(e) => setOfferingForm((f) => ({ ...f, calendar_id: e.target.value, semester_id: "" }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">Select academic year…</option>
                  {calendars.map((c) => <option key={c.id} value={c.id}>{c.academic_year}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Semester *</label>
                <select value={offeringForm.semester_id} onChange={(e) => setOfferingForm((f) => ({ ...f, semester_id: e.target.value }))}
                  disabled={!offeringForm.calendar_id}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] disabled:opacity-50">
                  <option value="">Select semester…</option>
                  {createSemesters.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Course *</label>
                <select value={offeringForm.course_id} onChange={(e) => setOfferingForm((f) => ({ ...f, course_id: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">Select course…</option>
                  {courses.map((c) => <option key={c.id} value={c.id}>{c.course_number} — {c.title}</option>)}
                </select>
              </div>
              {isHod ? (
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Department</label>
                  <p className="text-base text-gray-700 bg-gray-50 border border-gray-200 rounded-xl px-3 py-2">{hodDept?.name ?? "Your department"}</p>
                </div>
              ) : (
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Department *</label>
                  <select value={offeringForm.department_id} onChange={(e) => setOfferingForm((f) => ({ ...f, department_id: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                    <option value="">Select department…</option>
                    {departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                  </select>
                </div>
              )}
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Max Seats</label>
                  <input type="number" min={1} value={offeringForm.max_enrollment}
                    onChange={(e) => setOfferingForm((f) => ({ ...f, max_enrollment: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Section (optional)</label>
                  <input placeholder="e.g. A, B" value={offeringForm.section}
                    onChange={(e) => setOfferingForm((f) => ({ ...f, section: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
              </div>

              {/* Faculty picker: 1-3, exactly one Leader, search/filter */}
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">
                  Assign Faculty ({selectedFaculty.length}/{MAX_OFFERING_FACULTY}) — select 1 to {MAX_OFFERING_FACULTY} and mark exactly one Leader *
                </label>
                {selectedFaculty.length > 0 && (
                  <div className="space-y-1.5 mb-2">
                    {selectedFaculty.map((f) => (
                      <div key={f.id} className="flex items-center justify-between p-2 bg-gray-50 rounded-lg text-sm">
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input type="radio" name="leader" checked={leaderId === f.id} onChange={() => setLeaderId(f.id)} />
                          <span className="font-medium">{f.name}</span>
                          {leaderId === f.id && <span className="flex items-center gap-1 text-xs font-semibold text-amber-700"><Crown size={11} /> Leader</span>}
                        </label>
                        <button type="button" onClick={() => removeFaculty(f.id)} className="text-gray-400 hover:text-red-600"><X size={14} /></button>
                      </div>
                    ))}
                  </div>
                )}
                {selectedFaculty.length < MAX_OFFERING_FACULTY && (
                  <div className="relative">
                    <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
                    {/* Opens the full department faculty list on click/focus (not
                        only once the user starts typing) — BUSINESS_LOGIC.md
                        Section N: search narrows the list, it doesn't gate it. */}
                    <input value={facultySearch} onChange={(e) => setFacultySearch(e.target.value)}
                      onFocus={() => setFacultyOpen(true)}
                      onBlur={() => setTimeout(() => setFacultyOpen(false), 150)}
                      placeholder="Click to browse or search faculty…"
                      className="w-full pl-8 pr-3 py-2 border border-gray-300 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                    {facultyOpen && (
                      <div className="mt-1 border border-gray-200 rounded-xl max-h-48 overflow-y-auto absolute z-10 bg-white w-full shadow-lg">
                        {filteredFaculty.length === 0 ? (
                          <p className="text-sm text-gray-500 p-2">{facultyUsers.length === 0 ? "No faculty found in this department." : "No matching faculty."}</p>
                        ) : filteredFaculty.slice(0, 20).map((f) => (
                          <button key={f.id} type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => { addFaculty(f); setFacultyOpen(false); }}
                            className="w-full flex items-center justify-between text-left px-3 py-2 text-sm hover:bg-[#E6F4F4]">
                            <span>{f.full_name}{f.designation ? ` — ${f.designation}` : ""}</span>
                            <UserPlus size={13} className="text-[#0D6E6E]" />
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={closeOfferingModal}
                className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium hover:bg-gray-50">Cancel</button>
              <button
                onClick={() => {
                  if (!offeringForm.calendar_id || !offeringForm.semester_id || !offeringForm.course_id) {
                    toast.error("Academic year, semester and course are required."); return;
                  }
                  if (!isHod && !offeringForm.department_id) { toast.error("Department is required."); return; }
                  if (selectedFaculty.length === 0) { toast.error("Select at least 1 faculty member."); return; }
                  if (!leaderId) { toast.error("Mark exactly one faculty member as Leader."); return; }
                  createOffering.mutate();
                }}
                disabled={createOffering.isPending}
                className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-60">
                {createOffering.isPending ? "Creating…" : "Offer Course"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Offering View Details modal */}
      {selectedOffering && (
        <div className="fixed inset-0 bg-black/40 z-[60] flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl max-h-[90vh] overflow-y-auto">
            <div className="flex items-start justify-between px-6 py-5 border-b border-gray-100">
              <div>
                <p className="font-mono text-sm text-[#0D6E6E] font-bold">{selectedOffering.course_number}</p>
                <h3 className="text-xl font-bold text-gray-900">{selectedOffering.course_title}</h3>
              </div>
              <button onClick={() => setSelectedOffering(null)} className="text-gray-400 hover:text-gray-700"><X size={20} /></button>
            </div>
            <div className="p-6 space-y-5">
              <div className="grid grid-cols-2 md:grid-cols-3 gap-4 bg-gray-50 rounded-xl p-4">
                <div><p className="text-xs font-semibold text-gray-500 uppercase">Department</p><p className="text-sm text-gray-800">{selectedOffering.department_name ?? "—"}</p></div>
                <div><p className="text-xs font-semibold text-gray-500 uppercase">Credit</p><p className="text-sm text-gray-800 font-mono">{selectedOffering.credit_structure}</p></div>
                <div><p className="text-xs font-semibold text-gray-500 uppercase">Semester</p><p className="text-sm text-gray-800">{selectedOffering.semester_name ?? "—"}</p></div>
                <div><p className="text-xs font-semibold text-gray-500 uppercase">Credit Type</p><p className="text-sm text-gray-800">{selectedOffering.credit_type ? CREDIT_TYPE_LABELS[selectedOffering.credit_type] : "—"}</p></div>
                <div><p className="text-xs font-semibold text-gray-500 uppercase">Section</p><p className="text-sm text-gray-800">{selectedOffering.section ?? "—"}</p></div>
                <div><p className="text-xs font-semibold text-gray-500 uppercase">Status</p><p className="text-sm text-gray-800 capitalize">{selectedOffering.status}</p></div>
              </div>
              <div>
                <h4 className="text-base font-bold text-gray-900 mb-2">Course Instructors</h4>
                <div className="bg-white border border-gray-200 rounded-xl overflow-hidden">
                  {selectedOffering.faculty.length === 0 ? (
                    <p className="text-sm text-gray-600 text-center py-6">No instructors assigned yet.</p>
                  ) : (
                    <table className="w-full text-sm">
                      <thead className="bg-gray-50 border-b border-gray-200">
                        <tr>{["Sl No", "Name", "Designation", "Department", "Leader"].map((h) => (
                          <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-700">{h}</th>
                        ))}</tr>
                      </thead>
                      <tbody>
                        {selectedOffering.faculty.map((f, i) => (
                          <tr key={f.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                            <td className="px-4 py-2.5 text-gray-600">{i + 1}</td>
                            <td className="px-4 py-2.5 font-medium text-gray-900">{f.name}</td>
                            <td className="px-4 py-2.5 text-gray-600">{f.designation ?? "—"}</td>
                            <td className="px-4 py-2.5 text-gray-600">{f.department_name ?? "—"}</td>
                            <td className="px-4 py-2.5">
                              {f.is_leader ? (
                                <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-amber-100 text-amber-700 rounded-full text-xs font-semibold">
                                  <Crown size={11} /> Leader
                                </span>
                              ) : <span className="text-gray-400 text-xs">—</span>}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {confirm && <ConfirmDialog title={confirm.title} message={confirm.message} confirmLabel={confirm.confirmLabel} confirmClassName={confirm.confirmClassName} onConfirm={() => { confirm.action(); setConfirm(null); }} onCancel={() => setConfirm(null)} />}
    </div>
  );
}
