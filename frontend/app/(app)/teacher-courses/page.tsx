"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { BookOpen, Search, Loader2, X, Eye, Crown } from "lucide-react";

interface Offering {
  id: string; calendar_id: string; semester_id: string; course_id: string;
  course_number: string; course_title: string; credit_structure: string;
  section: string | null; status: string;
  department_id: string | null; department_name: string | null;
  faculty_names: string[]; enrolled_count: number;
}
interface Calendar { id: string; name: string; academic_year: string; }
interface Semester { id: string; calendar_id: string; name: string; }
interface OfferingInstructor { id: string; name: string; designation: string | null; department_name: string | null; role: string; is_leader: boolean; }
interface OfferingDetail extends Offering { faculty: OfferingInstructor[]; }
interface RosterStudent { id: string; student_id: string; student_name: string; student_roll: string | null; student_email: string | null; status: string; }

export default function TeacherCoursesPage() {
  const [calendarId, setCalendarId] = useState("");
  const [semesterId, setSemesterId] = useState("");
  const [selected, setSelected] = useState<OfferingDetail | null>(null);
  const [studentSearch, setStudentSearch] = useState("");
  const [viewStudent, setViewStudent] = useState<RosterStudent | null>(null);

  const { data: calendars = [] } = useQuery<Calendar[]>({
    queryKey: ["ams-calendars"],
    queryFn: async () => (await api.get("/academic/calendars")).data,
  });

  const { data: allSemesters = [] } = useQuery<Semester[]>({
    queryKey: ["ams-semesters-all"],
    queryFn: async () => (await api.get("/academic/semesters")).data,
  });

  const semesters = allSemesters.filter((s) => !calendarId || s.calendar_id === calendarId);

  const { data: offerings = [], isLoading } = useQuery<Offering[]>({
    queryKey: ["ams-teacher-courses", calendarId, semesterId],
    queryFn: async () => (await api.get("/courses/offerings/all", {
      params: { mine: true, ...(calendarId ? { calendar_id: calendarId } : {}), ...(semesterId ? { semester_id: semesterId } : {}) },
    })).data,
  });

  const { data: roster = [], isLoading: rosterLoading } = useQuery<RosterStudent[]>({
    queryKey: ["ams-teacher-course-roster", selected?.id],
    queryFn: async () => (await api.get(`/enrollment/offering/${selected?.id}`, { params: { status: "approved" } })).data,
    enabled: !!selected,
  });

  async function openDetail(offeringId: string) {
    try {
      const res = await api.get(`/courses/offerings/${offeringId}`);
      setSelected(res.data);
      setStudentSearch("");
    } catch {
      toast.error("Failed to load course offering.");
    }
  }

  function calendarLabel(id: string) {
    const c = calendars.find((c) => c.id === id);
    return c?.academic_year ?? "—";
  }
  function semesterLabel(id: string) {
    return allSemesters.find((s) => s.id === id)?.name ?? "—";
  }

  const filteredRoster = roster.filter((s) => {
    const q = studentSearch.trim().toLowerCase();
    if (!q) return true;
    return (s.student_name ?? "").toLowerCase().includes(q) || (s.student_roll ?? "").toLowerCase().includes(q);
  });

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="mb-6">
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><BookOpen size={24} className="text-[#0D6E6E]" />Teacher Courses</h1>
        <p className="text-gray-700 text-base mt-1">Courses assigned to you as an instructor</p>
      </div>

      {/* Filters */}
      <div className="flex gap-3 mb-4">
        <select value={calendarId} onChange={(e) => { setCalendarId(e.target.value); setSemesterId(""); }}
          className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
          <option value="">All Academic Years</option>
          {calendars.map((c) => <option key={c.id} value={c.id}>{c.academic_year}</option>)}
        </select>
        <select value={semesterId} onChange={(e) => setSemesterId(e.target.value)}
          className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
          <option value="">All Semesters</option>
          {semesters.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
      </div>

      {/* Course table */}
      <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
        {isLoading ? (
          <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
        ) : offerings.length === 0 ? (
          <div className="text-center py-16 text-gray-600"><BookOpen size={40} className="mx-auto mb-3 opacity-30" /><p>No courses assigned to you for the selected period.</p></div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>{["Sl No", "Course Number", "Course Title", "Course Credit", "Course Instructors", "Department", "Action"].map((h) => (
                <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {offerings.map((o, i) => (
                <tr key={o.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                  <td className="px-4 py-3 text-gray-600">{i + 1}</td>
                  <td className="px-4 py-3 font-mono font-bold text-[#0D6E6E]">{o.course_number}</td>
                  <td className="px-4 py-3 max-w-xs truncate">{o.course_title}</td>
                  <td className="px-4 py-3 font-mono text-sm">{o.credit_structure}</td>
                  <td className="px-4 py-3 text-gray-600">{o.faculty_names.join(", ") || "—"}</td>
                  <td className="px-4 py-3 text-gray-600">{o.department_name ?? "—"}</td>
                  <td className="px-4 py-3">
                    <button onClick={() => openDetail(o.id)}
                      className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-semibold text-[#0D6E6E] border border-[#0D6E6E] rounded-lg hover:bg-[#E6F4F4]">
                      <Eye size={13} /> View Details
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* View Details modal */}
      {selected && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-4xl max-h-[90vh] overflow-y-auto">
            <div className="flex items-start justify-between px-6 py-5 border-b border-gray-100">
              <div>
                <p className="font-mono text-sm text-[#0D6E6E] font-bold">{selected.course_number}</p>
                <h3 className="text-xl font-bold text-gray-900">{selected.course_title}</h3>
              </div>
              <button onClick={() => setSelected(null)} className="text-gray-400 hover:text-gray-700"><X size={20} /></button>
            </div>

            <div className="p-6 space-y-6">
              {/* Section 1: Course information */}
              <div className="grid grid-cols-2 md:grid-cols-3 gap-4 bg-gray-50 rounded-xl p-4">
                <div><p className="text-xs font-semibold text-gray-500 uppercase">Department</p><p className="text-sm text-gray-800">{selected.department_name ?? "—"}</p></div>
                <div><p className="text-xs font-semibold text-gray-500 uppercase">Course Credit</p><p className="text-sm text-gray-800 font-mono">{selected.credit_structure}</p></div>
                <div><p className="text-xs font-semibold text-gray-500 uppercase">Academic Year</p><p className="text-sm text-gray-800">{calendarLabel(selected.calendar_id)}</p></div>
                <div><p className="text-xs font-semibold text-gray-500 uppercase">Semester</p><p className="text-sm text-gray-800">{semesterLabel(selected.semester_id)}</p></div>
                <div><p className="text-xs font-semibold text-gray-500 uppercase">Section</p><p className="text-sm text-gray-800">{selected.section ?? "—"}</p></div>
                <div><p className="text-xs font-semibold text-gray-500 uppercase">Status</p><p className="text-sm text-gray-800 capitalize">{selected.status}</p></div>
              </div>

              {/* Section 2: Course Instructors */}
              <div>
                <h4 className="text-base font-bold text-gray-900 mb-2">Course Instructors</h4>
                <div className="bg-white border border-gray-200 rounded-xl overflow-hidden">
                  {selected.faculty.length === 0 ? (
                    <p className="text-sm text-gray-600 text-center py-6">No instructors assigned yet.</p>
                  ) : (
                    <table className="w-full text-sm">
                      <thead className="bg-gray-50 border-b border-gray-200">
                        <tr>{["Sl No", "Name", "Designation", "Department", "Leader"].map((h) => (
                          <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-700">{h}</th>
                        ))}</tr>
                      </thead>
                      <tbody>
                        {selected.faculty.map((f, i) => (
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

              {/* Section 3: Student List */}
              <div>
                <div className="flex items-center justify-between mb-2">
                  <h4 className="text-base font-bold text-gray-900">Student List</h4>
                  <div className="relative w-64">
                    <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
                    <input value={studentSearch} onChange={(e) => setStudentSearch(e.target.value)} placeholder="Search name or roll no…"
                      className="w-full pl-8 pr-3 py-1.5 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                  </div>
                </div>
                <div className="bg-white border border-gray-200 rounded-xl overflow-hidden">
                  {rosterLoading ? (
                    <div className="flex justify-center py-8"><Loader2 className="animate-spin text-gray-600" /></div>
                  ) : filteredRoster.length === 0 ? (
                    <p className="text-sm text-gray-600 text-center py-6">{roster.length === 0 ? "No students enrolled yet." : "No students match your search."}</p>
                  ) : (
                    <table className="w-full text-sm">
                      <thead className="bg-gray-50 border-b border-gray-200">
                        <tr>{["Sl No", "Roll No", "Name", "Email", "Gender", "Department", "Class", "Action"].map((h) => (
                          <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-700">{h}</th>
                        ))}</tr>
                      </thead>
                      <tbody>
                        {filteredRoster.map((s, i) => (
                          <tr key={s.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                            <td className="px-4 py-2.5 text-gray-600">{i + 1}</td>
                            <td className="px-4 py-2.5 font-mono text-sm">{s.student_roll ?? "—"}</td>
                            <td className="px-4 py-2.5 font-medium text-gray-900">{s.student_name ?? "—"}</td>
                            <td className="px-4 py-2.5 text-gray-600">{s.student_email ?? "—"}</td>
                            <td className="px-4 py-2.5 text-gray-400">—</td>
                            <td className="px-4 py-2.5 text-gray-600">{selected.department_name ?? "—"}</td>
                            <td className="px-4 py-2.5 text-gray-400">—</td>
                            <td className="px-4 py-2.5">
                              <button onClick={() => setViewStudent(s)}
                                className="flex items-center gap-1 px-2.5 py-1 text-xs font-semibold text-[#0D6E6E] border border-[#0D6E6E] rounded-lg hover:bg-[#E6F4F4]">
                                <Eye size={12} /> View
                              </button>
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

      {/* Student view-details modal */}
      {viewStudent && (
        <div className="fixed inset-0 bg-black/40 z-[60] flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm p-6">
            <div className="flex items-start justify-between mb-4">
              <h3 className="text-lg font-bold text-gray-900">Student Details</h3>
              <button onClick={() => setViewStudent(null)} className="text-gray-400 hover:text-gray-700"><X size={18} /></button>
            </div>
            <div className="space-y-2 text-sm">
              <div className="flex justify-between"><span className="text-gray-500">Name</span><span className="font-medium text-gray-900">{viewStudent.student_name ?? "—"}</span></div>
              <div className="flex justify-between"><span className="text-gray-500">Roll No</span><span className="font-mono text-gray-900">{viewStudent.student_roll ?? "—"}</span></div>
              <div className="flex justify-between"><span className="text-gray-500">Email</span><span className="text-gray-900">{viewStudent.student_email ?? "—"}</span></div>
              <div className="flex justify-between"><span className="text-gray-500">Department</span><span className="text-gray-900">{selected?.department_name ?? "—"}</span></div>
              <div className="flex justify-between"><span className="text-gray-500">Gender</span><span className="text-gray-400">—</span></div>
              <div className="flex justify-between"><span className="text-gray-500">Class</span><span className="text-gray-400">—</span></div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
