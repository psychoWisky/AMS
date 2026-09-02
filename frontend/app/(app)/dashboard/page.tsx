"use client";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useUser, useRole } from "@/stores/auth.store";
import { ROLES } from "@/lib/utils";
import {
  GraduationCap, BookOpen, CalendarDays, ClipboardList, BarChart3, Bell,
  IdCard, Coins, FlaskConical, ClipboardCheck, FileText, FileSpreadsheet,
  ArrowRightLeft, BookMarked, CheckSquare, Presentation, TrendingUp, ScrollText,
  BookText, MessageSquare, Pencil, type LucideIcon,
} from "lucide-react";
import Link from "next/link";

interface StudentCreditDetails {
  student: {
    roll_no: string | null;
    department_name: string | null;
    program_name: string | null;
    program_level: string | null;
  };
  credit_summary: { total_credit_taken: number };
}

interface DashboardTile {
  key: string;
  title: string;
  description: string;
  icon: LucideIcon;
  color: string;
  implemented: boolean;
  href?: string;
}

// Every module confirmed on the AVFU Student Dashboard (BUSINESS_LOGIC.md Section K.4).
// `implemented` reflects the ACTUAL current repository state, not the business
// requirement — verified against STUDENT_SIDE_IMPLEMENTATION_PLAN.md Sections 3/19
// before assigning each tile a route. A tile is only ever marked implemented when a
// real, non-fake, student-authorized route already serves that module's data today.
const STUDENT_TILES: DashboardTile[] = [
  { key: "registration", title: "Student Registration", description: "View your registration record", icon: IdCard, color: "bg-blue-50 text-blue-700", implemented: false },
  { key: "credit-details", title: "Student Credit Details", description: "Per-course credit ledger and summary", icon: Coins, color: "bg-teal-50 text-teal-700", implemented: true, href: "/academic-progress" },
  { key: "my-courses", title: "My Courses", description: "Courses selected/approved for your semester", icon: BookOpen, color: "bg-purple-50 text-purple-700", implemented: true, href: "/enrollment" },
  { key: "course-registration", title: "Course Registration", description: "Select courses from semester offerings", icon: ClipboardList, color: "bg-orange-50 text-orange-700", implemented: true, href: "/enrollment" },
  { key: "advisory-committee", title: "Advisory Committee", description: "Your committee members and roles", icon: FlaskConical, color: "bg-green-50 text-green-700", implemented: true, href: "/research" },
  { key: "admission-result", title: "Admission & Result", description: "Admission status and results", icon: ClipboardCheck, color: "bg-blue-50 text-blue-700", implemented: false },
  { key: "progress-report", title: "Progress Report", description: "Submit and track your progress report", icon: FileText, color: "bg-amber-50 text-amber-700", implemented: false },
  { key: "ppw", title: "PPW", description: "Proposed Programme of Work", icon: FileSpreadsheet, color: "bg-amber-50 text-amber-700", implemented: false },
  { key: "migration", title: "Migration", description: "Student migration request", icon: ArrowRightLeft, color: "bg-red-50 text-red-700", implemented: false },
  { key: "publication", title: "Publication", description: "Your research publications", icon: BookMarked, color: "bg-indigo-50 text-indigo-700", implemented: false },
  { key: "comprehensive-exam", title: "Comprehensive Exam", description: "Comprehensive examination status", icon: CheckSquare, color: "bg-purple-50 text-purple-700", implemented: false },
  { key: "conference", title: "Conference", description: "Conference participation records", icon: Presentation, color: "bg-teal-50 text-teal-700", implemented: false },
  { key: "result-tracking", title: "Result Tracking", description: "Track your semester results", icon: TrendingUp, color: "bg-green-50 text-green-700", implemented: false },
  { key: "synopsis", title: "Synopsis", description: "Research synopsis submission", icon: ScrollText, color: "bg-orange-50 text-orange-700", implemented: false },
  { key: "thesis", title: "Thesis", description: "Thesis submission and status", icon: BookText, color: "bg-indigo-50 text-indigo-700", implemented: false },
  { key: "feedback", title: "Feedback", description: "Submit course/faculty feedback", icon: MessageSquare, color: "bg-red-50 text-red-700", implemented: false },
];

function StudentDashboard({ user }: { user: NonNullable<ReturnType<typeof useUser>> }) {
  // Reuses the existing Student Credit Details endpoint (already student-self-authorized)
  // for identity fields (Department/Programme/Roll No.) and the credit summary —
  // no new/duplicate backend endpoint introduced for the dashboard.
  const { data: progress, isError } = useQuery<StudentCreditDetails>({
    queryKey: ["ams-credit-details", user.id],
    queryFn: async () => (await api.get(`/credit-details/student/${user.id}`)).data,
    retry: false,
  });

  const fallback = "—";
  const rollNo = progress?.student.roll_no ?? fallback;
  const department = progress?.student.department_name ?? fallback;
  const programme = progress?.student.program_name ?? fallback;
  const totalCreditTaken = !isError && progress ? progress.credit_summary.total_credit_taken : fallback;

  return (
    <div className="p-6 max-w-6xl mx-auto">
      {/* Identity header */}
      <div className="bg-white rounded-2xl border border-gray-200 p-5 mb-6">
        <div className="flex flex-col sm:flex-row sm:items-center gap-4">
          <div className="w-16 h-16 rounded-full bg-[#0D6E6E] flex items-center justify-center text-white text-xl font-bold flex-shrink-0" aria-hidden="true">
            {user.full_name?.trim()?.[0]?.toUpperCase() ?? "S"}
          </div>
          <div className="flex-1 grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
            <div><p className="text-gray-500">Name</p><p className="font-semibold text-gray-900">{user.full_name}</p></div>
            <div><p className="text-gray-500">Degree / Department</p><p className="font-semibold text-gray-900">{department}</p></div>
            <div><p className="text-gray-500">Programme</p><p className="font-semibold text-gray-900">{programme}</p></div>
            <div><p className="text-gray-500">Roll No.</p><p className="font-semibold text-gray-900 font-mono">{rollNo}</p></div>
          </div>
          <Link
            href="/student-management"
            className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-sm font-semibold text-[#0D6E6E] bg-[#E6F4F4] hover:bg-[#d3ecec] transition-colors flex-shrink-0"
          >
            <Pencil size={14} /> Edit
          </Link>
        </div>
      </div>

      {/* Mandatory profile completion (Section 28.14/28.15) — backend is authoritative
          via require_complete_profile; this banner is UX only. */}
      {!user.profile_complete && (
        <div className="bg-amber-50 border border-amber-300 text-amber-900 rounded-2xl p-4 mb-6 text-sm">
          <span className="font-bold">Complete your student profile before continuing.</span>{" "}
          Some actions (such as Course Registration) require your Date of Birth, Gender, Blood Group, Father&apos;s Name, ABC ID, and Address to be filled in.{" "}
          <Link href="/student-management" className="underline font-semibold">Complete Profile</Link>
        </div>
      )}

      {/* Course Registration eligibility warning — exact confirmed wording (BUSINESS_LOGIC.md K.2).
          Displayed unconditionally as a placeholder: the exact trigger condition is an
          open business question (see STUDENT_SIDE_IMPLEMENTATION_PLAN.md Section 24). */}
      <div className="bg-amber-50 border border-amber-300 text-amber-900 rounded-2xl p-4 mb-6 text-sm">
        <span className="font-bold">Course Registration:</span> All students are required to submit their course registration cards. Failure to submit the course registration card will result in ineligibility to appear for examinations.
      </div>

      {/* Student Research Credit summary */}
      <div className="bg-white rounded-2xl border border-gray-200 p-5 mb-6">
        <h2 className="font-bold text-gray-800 mb-3">Student Research Credit</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
          <div><p className="text-gray-500">Total Credit Taken</p><p className="text-xl font-bold text-[#0D6E6E]">{totalCreditTaken}</p></div>
          <div><p className="text-gray-500">Thesis Evaluation</p><p className="text-xl font-bold text-gray-400">{fallback}</p></div>
          <div><p className="text-gray-500">Total</p><p className="text-xl font-bold text-gray-400">{fallback}</p></div>
          <div><p className="text-gray-500">Remaining Credit</p><p className="text-xl font-bold text-gray-400">{fallback}</p></div>
        </div>
      </div>

      {/* Module tiles — every confirmed module is visible; unimplemented ones are inert. */}
      <h2 className="font-bold text-gray-800 mb-3">Modules</h2>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {STUDENT_TILES.map((tile) => {
          // Section 28.15: mandatory profile completion gates access to the
          // student's own action tiles, but never affects non-student roles
          // (this branch only renders for role === "student").
          const blockedByProfile = tile.implemented && !user.profile_complete;
          if (tile.implemented && tile.href && !blockedByProfile) {
            return (
              <Link key={tile.key} href={tile.href}
                className="bg-white rounded-2xl border border-gray-200 p-5 hover:shadow-md transition-all group hover:border-[#0D6E6E]"
                aria-label={`${tile.title} — available`}>
                <div className={`w-12 h-12 rounded-xl flex items-center justify-center mb-4 ${tile.color}`}>
                  <tile.icon size={22} aria-hidden="true" />
                </div>
                <h3 className="font-bold text-gray-900 group-hover:text-[#0D6E6E] transition-colors">{tile.title}</h3>
                <p className="text-sm text-gray-700 mt-1">{tile.description}</p>
                <span className="inline-block mt-3 px-2 py-0.5 rounded-full text-xs font-semibold bg-green-100 text-green-700">Available</span>
              </Link>
            );
          }
          return (
            <div key={tile.key}
              aria-disabled="true"
              title={blockedByProfile ? "Complete your student profile first" : undefined}
              className="bg-white rounded-2xl border border-gray-200 p-5 opacity-60 cursor-not-allowed select-none">
              <div className={`w-12 h-12 rounded-xl flex items-center justify-center mb-4 ${tile.color} opacity-70`}>
                <tile.icon size={22} aria-hidden="true" />
              </div>
              <h3 className="font-bold text-gray-900">{tile.title}</h3>
              <p className="text-sm text-gray-700 mt-1">{tile.description}</p>
              <span className="inline-block mt-3 px-2 py-0.5 rounded-full text-xs font-semibold bg-gray-100 text-gray-600">
                {blockedByProfile ? "Complete Profile First" : "Coming Soon"}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function DashboardPage() {
  const user = useUser();
  const role = useRole();

  const { data: notifications = [] } = useQuery({
    queryKey: ["ams-notifications"],
    queryFn: async () => (await api.get("/notifications?unread_only=true")).data,
  });

  if (role === "student" && user) {
    return <StudentDashboard user={user} />;
  }

  // Existing generic staff/admin dashboard — unchanged for every non-student role.
  const cards = [
    { label: "Academic Calendar", icon: CalendarDays, href: "/calendar", desc: "Semesters, exam dates, holidays", color: "bg-blue-50 text-blue-700" },
    { label: "Courses", icon: BookOpen, href: "/courses", desc: "Course catalog & offerings", color: "bg-teal-50 text-teal-700" },
    { label: "Enrollment", icon: ClipboardList, href: "/enrollment", desc: "Course enrollment management", color: "bg-purple-50 text-purple-700" },
    { label: "Grading", icon: BarChart3, href: "/grading", desc: "Grade sheets & result approval", color: "bg-orange-50 text-orange-700" },
    { label: "Research / PG", icon: GraduationCap, href: "/research", desc: "Advisory committees, PhD tracking", color: "bg-green-50 text-green-700" },
    { label: "Notifications", icon: Bell, href: "/notifications", desc: `${notifications.length} unread`, color: "bg-red-50 text-red-700" },
  ];

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900">Welcome back, {user?.full_name?.split(" ")[0]}!</h1>
        <p className="text-gray-700 mt-1">{ROLES[role as keyof typeof ROLES] ?? role} · AVFU Academic Management System</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {cards.map((card) => (
          <Link key={card.href} href={card.href}
            className="bg-white rounded-2xl border border-gray-200 p-5 hover:shadow-md transition-all group hover:border-[#0D6E6E]">
            <div className={`w-12 h-12 rounded-xl flex items-center justify-center mb-4 ${card.color}`}>
              <card.icon size={22} />
            </div>
            <h3 className="font-bold text-gray-900 group-hover:text-[#0D6E6E] transition-colors">{card.label}</h3>
            <p className="text-sm text-gray-700 mt-1">{card.desc}</p>
          </Link>
        ))}
      </div>

      {/* Quick info */}
      <div className="mt-8 bg-white rounded-2xl border border-gray-200 p-5">
        <h2 className="font-bold text-gray-800 mb-3">Your Profile</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
          <div><p className="text-gray-700">Name</p><p className="font-semibold">{user?.full_name}</p></div>
          <div><p className="text-gray-700">Role</p><p className="font-semibold">{ROLES[role as keyof typeof ROLES] ?? role}</p></div>
          <div><p className="text-gray-700">Email</p><p className="font-semibold truncate">{user?.email}</p></div>
          <div><p className="text-gray-700">Designation</p><p className="font-semibold">{user?.designation ?? "—"}</p></div>
        </div>
      </div>
    </div>
  );
}
