"use client";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useUser, useRole } from "@/stores/auth.store";
import { ROLES } from "@/lib/utils";
import { GraduationCap, BookOpen, Users, CalendarDays, ClipboardList, BarChart3, Bell } from "lucide-react";
import Link from "next/link";

export default function DashboardPage() {
  const user = useUser();
  const role = useRole();

  const { data: notifications = [] } = useQuery({
    queryKey: ["ams-notifications"],
    queryFn: async () => (await api.get("/notifications?unread_only=true")).data,
  });

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
