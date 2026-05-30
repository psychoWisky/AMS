"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useRole } from "@/stores/auth.store";
import { toast } from "sonner";
import { ADMIN_ROLES } from "@/lib/utils";
import { BookOpen, Plus, Search, Loader2, Globe, EyeOff } from "lucide-react";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";

interface Course { id: string; course_number: string; title: string; credit_structure: string; course_type: string; program_level: string; status: string; department_id: string | null; }
interface Offering { id: string; course_number: string; course_title: string; credit_structure: string; section: string | null; max_enrollment: number; enrolled_count: number; status: string; faculty_names: string[]; }
interface Calendar { id: string; name: string; academic_year: string; }
interface Semester { id: string; calendar_id: string; name: string; }
interface FacultyUser { id: string; full_name: string; designation: string | null; }

const CREDIT_FORMATS = ["2+0","0+2","1+1","0+1","3+0","2+1","1+2","3+1","4+0","0+4","2+2"];
const LEVELS = ["UG","PG","PhD"];
const STATUS_COLOR: Record<string, string> = { active: "bg-green-100 text-green-700", inactive: "bg-gray-100 text-gray-600", archived: "bg-red-100 text-red-700", draft: "bg-gray-100 text-gray-700", published: "bg-green-100 text-green-700", closed: "bg-red-100 text-red-700" };

export default function CoursesPage() {
  const role = useRole();
  const qc = useQueryClient();
  const isAdmin = role ? ADMIN_ROLES.includes(role) : false;
  const [tab, setTab] = useState<"courses" | "offerings">("courses");
  const [search, setSearch] = useState("");
  const [levelFilter, setLevelFilter] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [showOfferingCreate, setShowOfferingCreate] = useState(false);
  const [form, setForm] = useState({ course_number: "", title: "", credit_theory: "3", credit_practical: "0", program_level: "UG" });
  const [offeringForm, setOfferingForm] = useState({ calendar_id: "", semester_id: "", course_id: "", max_enrollment: "60", section: "", faculty_ids: [] as string[] });
  const [confirm, setConfirm] = useState<{ action: () => void; title: string; message: string; confirmLabel: string; confirmClassName?: string } | null>(null);

  const { data: courses = [], isLoading } = useQuery<Course[]>({
    queryKey: ["ams-courses"],
    queryFn: async () => (await api.get("/courses")).data,
  });

  const { data: offerings = [] } = useQuery<Offering[]>({
    queryKey: ["ams-offerings"],
    queryFn: async () => (await api.get("/courses/offerings/all")).data,
    enabled: tab === "offerings",
  });

  const { data: calendars = [] } = useQuery<Calendar[]>({
    queryKey: ["ams-calendars"],
    queryFn: async () => (await api.get("/academic/calendars")).data,
    enabled: showOfferingCreate,
  });

  const { data: semesters = [] } = useQuery<Semester[]>({
    queryKey: ["ams-semesters-for-offering", offeringForm.calendar_id],
    queryFn: async () => (await api.get(`/academic/calendars/${offeringForm.calendar_id}/semesters`)).data,
    enabled: !!offeringForm.calendar_id,
  });

  const { data: facultyUsers = [] } = useQuery<FacultyUser[]>({
    queryKey: ["ams-faculty"],
    queryFn: async () => (await api.get("/auth/users")).data.filter((u: { role: string }) => ["faculty","hod","research_supervisor"].includes(u.role)),
    enabled: showOfferingCreate,
  });

  const createCourse = useMutation({
    mutationFn: (d: typeof form) => api.post("/courses", { ...d, credit_theory: parseInt(d.credit_theory), credit_practical: parseInt(d.credit_practical) }),
    onSuccess: () => { toast.success("Course created."); qc.invalidateQueries({ queryKey: ["ams-courses"] }); setShowCreate(false); },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "Failed."),
  });

  const createOffering = useMutation({
    mutationFn: () => api.post("/courses/offerings", {
      calendar_id: offeringForm.calendar_id,
      semester_id: offeringForm.semester_id,
      course_id: offeringForm.course_id,
      max_enrollment: parseInt(offeringForm.max_enrollment),
      section: offeringForm.section || null,
      faculty_ids: offeringForm.faculty_ids,
    }),
    onSuccess: () => {
      toast.success("Offering created.");
      qc.invalidateQueries({ queryKey: ["ams-offerings"] });
      setShowOfferingCreate(false);
      setOfferingForm({ calendar_id: "", semester_id: "", course_id: "", max_enrollment: "60", section: "", faculty_ids: [] });
    },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "Failed to create offering."),
  });

  const publishOffering = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      api.patch(`/courses/offerings/${id}/status?status=${status}`),
    onSuccess: (_, { status }) => {
      toast.success(status === "published" ? "Offering published — students can now enroll." : "Offering unpublished.");
      qc.invalidateQueries({ queryKey: ["ams-offerings"] });
    },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "Failed."),
  });

  const filteredCourses = courses.filter((c) =>
    (!levelFilter || c.program_level === levelFilter) &&
    (!search || c.course_number.toLowerCase().includes(search.toLowerCase()) || c.title.toLowerCase().includes(search.toLowerCase()))
  );

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><BookOpen size={24} className="text-[#0D6E6E]" />Courses</h1>
          <p className="text-gray-700 text-base mt-1">Course catalog, credit structures, and semester offerings</p>
        </div>
        {isAdmin && tab === "courses" && (
          <button onClick={() => setShowCreate(true)}
            className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-base hover:bg-[#178F8F]">
            <Plus size={16} /> Add Course
          </button>
        )}
        {isAdmin && tab === "offerings" && (
          <button onClick={() => setShowOfferingCreate(true)}
            className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-base hover:bg-[#178F8F]">
            <Plus size={16} /> Add Offering
          </button>
        )}
      </div>

      {/* Tabs */}
      <div className="flex gap-2 mb-5">
        {(["courses","offerings"] as const).map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-4 py-2 rounded-xl text-sm font-semibold capitalize transition-colors ${tab === t ? "bg-[#0D6E6E] text-white" : "text-gray-600 hover:bg-gray-100"}`}>
            {t === "courses" ? "Course Catalog" : "Semester Offerings"}
          </button>
        ))}
      </div>

      {/* Filters */}
      <div className="flex gap-3 mb-4">
        <div className="relative flex-1 max-w-xs">
          <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-600" />
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search courses…"
            className="w-full pl-9 pr-4 py-2.5 border border-gray-200 rounded-xl text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
        </div>
        <select value={levelFilter} onChange={(e) => setLevelFilter(e.target.value)}
          className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
          <option value="">All Levels</option>
          {LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
        </select>
      </div>

      {/* Create Course Modal */}
      {showCreate && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md p-6">
            <h3 className="text-xl font-bold mb-4">Add New Course</h3>
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
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Theory Credits</label>
                  <input type="number" min={0} max={6} value={form.credit_theory}
                    onChange={(e) => setForm((f) => ({ ...f, credit_theory: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Practical Credits</label>
                  <input type="number" min={0} max={6} value={form.credit_practical}
                    onChange={(e) => setForm((f) => ({ ...f, credit_practical: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Credit Format</label>
                <p className="text-sm text-gray-700 mb-2">Supported: {CREDIT_FORMATS.join(", ")}</p>
                <p className="text-base font-bold text-[#0D6E6E]">Current: {form.credit_theory}+{form.credit_practical}</p>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Program Level</label>
                <div className="flex gap-2">
                  {LEVELS.map((l) => (
                    <button key={l} type="button" onClick={() => setForm((f) => ({ ...f, program_level: l }))}
                      className={`flex-1 py-2 rounded-xl text-sm font-semibold border-2 transition-all ${form.program_level === l ? "border-[#0D6E6E] bg-[#0D6E6E] text-white" : "border-gray-200 text-gray-600"}`}>
                      {l}
                    </button>
                  ))}
                </div>
              </div>
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={() => setShowCreate(false)} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium hover:bg-gray-50">Cancel</button>
              <button onClick={() => createCourse.mutate(form)} disabled={createCourse.isPending}
                className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-60">
                {createCourse.isPending ? "Creating…" : "Create Course"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Course Catalog */}
      {tab === "courses" && (
        <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
          {isLoading ? (
            <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
          ) : filteredCourses.length === 0 ? (
            <div className="text-center py-16 text-gray-600"><BookOpen size={40} className="mx-auto mb-3 opacity-30" /><p>No courses found.</p></div>
          ) : (
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b border-gray-200">
                <tr>{["Course No.", "Title", "Credits", "Type", "Level", "Status"].map((h) => (
                  <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>
                ))}</tr>
              </thead>
              <tbody>
                {filteredCourses.map((c, i) => (
                  <tr key={c.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                    <td className="px-4 py-3 font-mono font-bold text-[#0D6E6E]">{c.course_number}</td>
                    <td className="px-4 py-3 font-medium text-gray-900 max-w-xs truncate">{c.title}</td>
                    <td className="px-4 py-3"><span className="font-mono text-sm bg-[#E6F4F4] text-[#0D6E6E] px-2 py-0.5 rounded">{c.credit_structure}</span></td>
                    <td className="px-4 py-3 capitalize text-gray-600">{c.course_type}</td>
                    <td className="px-4 py-3"><span className="px-2 py-0.5 bg-blue-50 text-blue-700 rounded text-sm font-semibold">{c.program_level}</span></td>
                    <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-sm font-semibold ${STATUS_COLOR[c.status] ?? "bg-gray-100"}`}>{c.status}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Create Offering Modal */}
      {showOfferingCreate && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md p-6">
            <h3 className="text-xl font-bold mb-1">Add Semester Offering</h3>
            <p className="text-sm text-gray-600 mb-4">Schedule a course for a specific semester with assigned faculty and seat limit.</p>
            <div className="space-y-3">
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Academic Year *</label>
                <select value={offeringForm.calendar_id} onChange={(e) => setOfferingForm((f) => ({ ...f, calendar_id: e.target.value, semester_id: "" }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                  <option value="">Select academic year…</option>
                  {calendars.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Semester *</label>
                <select value={offeringForm.semester_id} onChange={(e) => setOfferingForm((f) => ({ ...f, semester_id: e.target.value }))}
                  disabled={!offeringForm.calendar_id}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] disabled:opacity-50">
                  <option value="">Select semester…</option>
                  {semesters.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
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
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Assign Faculty (optional)</label>
                <select multiple value={offeringForm.faculty_ids}
                  onChange={(e) => setOfferingForm((f) => ({ ...f, faculty_ids: Array.from(e.target.selectedOptions, (o) => o.value) }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] h-24">
                  {facultyUsers.map((f) => <option key={f.id} value={f.id}>{f.full_name}{f.designation ? ` — ${f.designation}` : ""}</option>)}
                </select>
                <p className="text-sm text-gray-600 mt-1">Hold Ctrl / Cmd to select multiple faculty</p>
              </div>
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={() => { setShowOfferingCreate(false); setOfferingForm({ calendar_id: "", semester_id: "", course_id: "", max_enrollment: "60", section: "", faculty_ids: [] }); }}
                className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium hover:bg-gray-50">Cancel</button>
              <button
                onClick={() => {
                  if (!offeringForm.calendar_id || !offeringForm.semester_id || !offeringForm.course_id) {
                    toast.error("Academic year, semester and course are required."); return;
                  }
                  createOffering.mutate();
                }}
                disabled={createOffering.isPending}
                className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-60">
                {createOffering.isPending ? "Creating…" : "Create Offering"}
              </button>
            </div>
          </div>
        </div>
      )}

      {confirm && <ConfirmDialog title={confirm.title} message={confirm.message} confirmLabel={confirm.confirmLabel} confirmClassName={confirm.confirmClassName} onConfirm={() => { confirm.action(); setConfirm(null); }} onCancel={() => setConfirm(null)} />}

      {/* Offerings */}
      {tab === "offerings" && (
        <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
          {offerings.length === 0 ? (
            <div className="text-center py-16 text-gray-600"><BookOpen size={40} className="mx-auto mb-3 opacity-30" /><p>No offerings yet.</p></div>
          ) : (
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b border-gray-200">
                <tr>{["Course", "Title", "Credits", "Section", "Faculty", "Enrollment", "Status", ...(isAdmin ? ["Action"] : [])].map((h) => (
                  <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>
                ))}</tr>
              </thead>
              <tbody>
                {offerings.map((o, i) => (
                  <tr key={o.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                    <td className="px-4 py-3 font-mono font-bold text-[#0D6E6E]">{o.course_number}</td>
                    <td className="px-4 py-3 max-w-xs truncate">{o.course_title}</td>
                    <td className="px-4 py-3 font-mono text-sm">{o.credit_structure}</td>
                    <td className="px-4 py-3">{o.section ?? "—"}</td>
                    <td className="px-4 py-3 text-gray-600">{o.faculty_names.join(", ") || "—"}</td>
                    <td className="px-4 py-3">{o.enrolled_count}/{o.max_enrollment}</td>
                    <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-sm font-semibold ${STATUS_COLOR[o.status] ?? "bg-gray-100"}`}>{o.status}</span></td>
                    {isAdmin && (
                      <td className="px-4 py-3">
                        {o.status === "draft" || o.status === "closed" ? (
                          <button
                            onClick={() => setConfirm({
                              action: () => publishOffering.mutate({ id: o.id, status: "published" }),
                              title: "Publish Offering",
                              message: `Publish ${o.course_number} — ${o.course_title}? Students will be able to see and enroll in this offering.`,
                              confirmLabel: "Yes, Publish",
                              confirmClassName: "bg-green-600 hover:bg-green-700 text-white",
                            })}
                            disabled={publishOffering.isPending}
                            className="flex items-center gap-1.5 px-3 py-1.5 bg-green-600 text-white text-sm font-semibold rounded-lg hover:bg-green-700 disabled:opacity-50">
                            <Globe size={13} /> Publish
                          </button>
                        ) : o.status === "published" ? (
                          <button
                            onClick={() => setConfirm({
                              action: () => publishOffering.mutate({ id: o.id, status: "draft" }),
                              title: "Unpublish Offering",
                              message: `Unpublish ${o.course_number} — ${o.course_title}? Students will no longer see this offering.`,
                              confirmLabel: "Yes, Unpublish",
                              confirmClassName: "bg-gray-600 hover:bg-gray-700 text-white",
                            })}
                            disabled={publishOffering.isPending}
                            className="flex items-center gap-1.5 px-3 py-1.5 border border-gray-300 text-gray-600 text-sm font-semibold rounded-lg hover:bg-gray-50 disabled:opacity-50">
                            <EyeOff size={13} /> Unpublish
                          </button>
                        ) : null}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
