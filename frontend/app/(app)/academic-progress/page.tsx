"use client";
import { useState, useMemo } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useRole, useUser } from "@/stores/auth.store";
import { GraduationCap, Loader2, ExternalLink } from "lucide-react";
import { committeeRoleLabel } from "@/lib/utils";

interface ProgressCourse {
  offering_id: string; course_number: string; course_title: string;
  course_credit: string; semester_name: string | null; academic_year: string | null;
  enrollment_status: string;
}
interface ProgressData {
  student: { id: string; name: string | null; roll_no: string | null; department_name: string | null; program_name: string | null; program_level: string | null };
  courses: ProgressCourse[];
  credit_summary: { total_credit_taken: number };
}
interface GpaSemester { semester_id: string; sgpa: number; credits: number }
interface GpaData { cgpa: number; semesters: GpaSemester[] }
interface CommitteeMemberOut { id: string; faculty_name: string | null; role: string; accepted: boolean | null }
interface CommitteeSummary { status_label: string; research_title: string | null; research_area: string | null; members: CommitteeMemberOut[] }
interface StudentOpt { id: string; full_name: string; role: string; }
interface CalendarOpt { id: string; name: string; academic_year: string }
interface SemesterOpt { id: string; calendar_id: string; name: string }

const ENROLLMENT_STATUS_COLOR: Record<string, string> = {
  pending: "bg-amber-100 text-amber-700", approved: "bg-green-100 text-green-700",
  rejected: "bg-red-100 text-red-700", withdrawn: "bg-gray-100 text-gray-600",
};
// Roles that can call GET /auth/users (must match auth.py's list_users RBAC).
const USER_LOOKUP_ROLES = ["super_admin", "academic_admin", "registrar", "hod", "examiner"];

export default function AcademicProgressPage() {
  const role = useRole();
  const currentUser = useUser();
  const searchParams = useSearchParams();
  const canLookupUsers = USER_LOOKUP_ROLES.includes(role ?? "");
  const isStudent = role === "student";

  const [calendarId, setCalendarId] = useState("");
  const [semesterId, setSemesterId] = useState("");
  const [pickedStudentId, setPickedStudentId] = useState(searchParams.get("student") ?? "");

  const targetStudentId = isStudent ? currentUser?.id ?? "" : pickedStudentId;

  const { data: calendars = [] } = useQuery<CalendarOpt[]>({
    queryKey: ["ams-calendars"],
    queryFn: async () => (await api.get("/academic/calendars")).data,
  });
  const { data: allSemesters = [] } = useQuery<SemesterOpt[]>({
    queryKey: ["ams-semesters-all"],
    queryFn: async () => (await api.get("/academic/semesters")).data,
  });
  const semesters = allSemesters.filter((s) => !calendarId || s.calendar_id === calendarId);

  const { data: students = [] } = useQuery<StudentOpt[]>({
    queryKey: ["ams-users-students"],
    queryFn: async () => (await api.get("/auth/users", { params: { role: "student" } })).data,
    enabled: canLookupUsers,
  });

  const { data: progress, isLoading: progressLoading, isError: progressError } = useQuery<ProgressData>({
    queryKey: ["ams-credit-details", targetStudentId, calendarId, semesterId],
    queryFn: async () => (await api.get(`/credit-details/student/${targetStudentId}`, {
      params: { ...(calendarId ? { calendar_id: calendarId } : {}), ...(semesterId ? { semester_id: semesterId } : {}) },
    })).data,
    enabled: !!targetStudentId,
    retry: false,
  });

  const { data: gpa } = useQuery<GpaData>({
    queryKey: ["ams-student-gpa", targetStudentId],
    queryFn: async () => (await api.get(`/grading/student/${targetStudentId}/gpa`)).data,
    enabled: !!targetStudentId,
    retry: false,
  });

  const { data: committee } = useQuery<CommitteeSummary>({
    queryKey: ["ams-student-committee", targetStudentId],
    queryFn: async () => (await api.get(`/research/committees/student/${targetStudentId}`)).data,
    enabled: !!targetStudentId,
    retry: false,
  });

  const semesterNameLookup = useMemo(() => {
    const map: Record<string, string> = {};
    allSemesters.forEach((s) => { map[s.id] = s.name; });
    return map;
  }, [allSemesters]);

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="mb-6">
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><GraduationCap size={24} className="text-[#0D6E6E]" />Student Academic Progress</h1>
        <p className="text-gray-700 text-base mt-1">Enrollment, credits, and results derived from existing academic records</p>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 mb-5">
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
        {!isStudent && canLookupUsers && (
          <select value={pickedStudentId} onChange={(e) => setPickedStudentId(e.target.value)}
            className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none min-w-[220px]">
            <option value="">Select student…</option>
            {students.map((s) => <option key={s.id} value={s.id}>{s.full_name}</option>)}
          </select>
        )}
      </div>

      {!isStudent && !canLookupUsers && !pickedStudentId && (
        <div className="bg-amber-50 border border-amber-200 text-amber-800 text-sm rounded-xl p-4 mb-5">
          Student lookup requires admin/HOD/registrar/examiner access. Open this page from a student&apos;s row in Teacher Courses or Enrollment to view their progress.
        </div>
      )}

      {!targetStudentId ? null : progressLoading ? (
        <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
      ) : progressError || !progress ? (
        <div className="text-center py-16 text-gray-600 bg-white rounded-2xl border border-gray-200">
          <p>You are not authorized to view this student&apos;s academic progress, or the student was not found.</p>
        </div>
      ) : (
        <div className="space-y-6">
          {/* Student information */}
          <div className="bg-white rounded-2xl border border-gray-200 p-5 grid grid-cols-2 md:grid-cols-4 gap-4">
            <div><p className="text-xs font-semibold text-gray-500 uppercase">Name</p><p className="text-sm text-gray-900 font-medium">{progress.student.name ?? "—"}</p></div>
            <div><p className="text-xs font-semibold text-gray-500 uppercase">Roll No</p><p className="text-sm text-gray-900 font-mono">{progress.student.roll_no ?? "—"}</p></div>
            <div><p className="text-xs font-semibold text-gray-500 uppercase">Department</p><p className="text-sm text-gray-900">{progress.student.department_name ?? "—"}</p></div>
            <div><p className="text-xs font-semibold text-gray-500 uppercase">Program / Degree</p><p className="text-sm text-gray-900">{progress.student.program_name ?? "—"}</p></div>
          </div>

          {/* Courses */}
          <div>
            <h2 className="text-base font-bold text-gray-900 mb-2">Academic Courses</h2>
            <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
              {progress.courses.length === 0 ? (
                <p className="text-sm text-gray-600 text-center py-8">No enrollments found for the selected period.</p>
              ) : (
                <table className="w-full text-sm">
                  <thead className="bg-gray-50 border-b border-gray-200">
                    <tr>{["Sl No", "Course Number", "Course Title", "Credit", "Semester", "Academic Year", "Status", "Action"].map((h) => (
                      <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>
                    ))}</tr>
                  </thead>
                  <tbody>
                    {progress.courses.map((c, i) => (
                      <tr key={c.offering_id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                        <td className="px-4 py-3 text-gray-600">{i + 1}</td>
                        <td className="px-4 py-3 font-mono font-bold text-[#0D6E6E]">{c.course_number}</td>
                        <td className="px-4 py-3 max-w-xs truncate">{c.course_title}</td>
                        <td className="px-4 py-3 font-mono text-sm">{c.course_credit}</td>
                        <td className="px-4 py-3 text-gray-600">{c.semester_name ?? "—"}</td>
                        <td className="px-4 py-3 text-gray-600">{c.academic_year ?? "—"}</td>
                        <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-sm font-semibold ${ENROLLMENT_STATUS_COLOR[c.enrollment_status] ?? "bg-gray-100"}`}>{c.enrollment_status}</span></td>
                        <td className="px-4 py-3">
                          <a href={`/grading?offering=${c.offering_id}`}
                            className="inline-flex items-center gap-1 text-sm font-semibold text-[#0D6E6E] hover:underline">
                            <ExternalLink size={12} /> Grade Sheet
                          </a>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            <p className="text-xs text-gray-500 mt-2">
              Credit Type and a per-year/semester &quot;Course Session&quot; label are not yet represented in AMS and are not shown here.
            </p>
          </div>

          {/* Credit summary */}
          <div className="bg-white rounded-2xl border border-gray-200 p-5">
            <h2 className="text-base font-bold text-gray-900 mb-3">Academic Credit Summary</h2>
            <div className="flex items-baseline gap-3">
              <p className="text-sm text-gray-600">Total Credit Taken (approved enrollments)</p>
              <p className="text-2xl font-bold text-[#0D6E6E]">{progress.credit_summary.total_credit_taken}</p>
            </div>
            <p className="text-xs text-gray-500 mt-2">
              Thesis Evaluation, Total, and Remaining Credit are not shown — AMS does not currently define a thesis-evaluation value or a required-credit total for any program.
            </p>
          </div>

          {/* Results / GPA */}
          {gpa && (
            <div className="bg-white rounded-2xl border border-gray-200 p-5">
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-base font-bold text-gray-900">Academic Results</h2>
                <p className="text-sm text-gray-700">CGPA: <span className="font-bold text-[#0D6E6E]">{gpa.cgpa}</span></p>
              </div>
              {gpa.semesters.length === 0 ? (
                <p className="text-sm text-gray-600">No published results yet.</p>
              ) : (
                <table className="w-full text-sm">
                  <thead className="bg-gray-50 border-b border-gray-200">
                    <tr>{["Semester", "SGPA", "Credits"].map((h) => (
                      <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-700">{h}</th>
                    ))}</tr>
                  </thead>
                  <tbody>
                    {gpa.semesters.map((s, i) => (
                      <tr key={s.semester_id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                        <td className="px-4 py-2.5">{semesterNameLookup[s.semester_id] ?? s.semester_id}</td>
                        <td className="px-4 py-2.5 font-mono">{s.sgpa}</td>
                        <td className="px-4 py-2.5">{s.credits}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}

          {/* Advisory Committee (reused from the Research module, read-only) */}
          {committee && (
            <div className="bg-white rounded-2xl border border-gray-200 p-5">
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-base font-bold text-gray-900">Advisory Committee</h2>
                <span className="px-2 py-0.5 rounded-full text-sm font-semibold bg-gray-100 text-gray-700">{committee.status_label}</span>
              </div>
              {committee.research_title && <p className="text-sm text-gray-700 mb-1">{committee.research_title}</p>}
              {committee.members.length === 0 ? (
                <p className="text-sm text-gray-600">No members yet.</p>
              ) : (
                <div className="space-y-1.5">
                  {committee.members.map((m) => (
                    <div key={m.id} className="flex items-center gap-2 text-sm">
                      <span className="font-medium text-gray-900">{m.faculty_name ?? "—"}</span>
                      <span className="text-gray-500">— {committeeRoleLabel(m.role)}</span>
                    </div>
                  ))}
                </div>
              )}
              <a href="/research" className="inline-flex items-center gap-1 text-sm font-semibold text-[#0D6E6E] hover:underline mt-3">
                <ExternalLink size={12} /> Manage in Advisory Committees
              </a>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
