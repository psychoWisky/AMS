"use client";
import { usePathname, useRouter } from "next/navigation";
import { useRole } from "@/stores/auth.store";
import { cn } from "@/lib/utils";
import {
  LayoutDashboard, CalendarDays, BookOpen, Users, ClipboardList,
  BarChart3, FlaskConical, Bell, Settings, ChevronLeft, ChevronRight, GraduationCap, LogOut,
} from "lucide-react";
import { useAuthStore } from "@/stores/auth.store";
import { api } from "@/services/api";
import { useState } from "react";

const NAV = [
  { label: "Dashboard",       icon: LayoutDashboard, href: "/dashboard",    roles: [] },
  { label: "Academic Calendar",icon: CalendarDays,   href: "/calendar",     roles: ["super_admin","academic_admin","registrar","hod","faculty","student","examiner"] },
  { label: "Courses",         icon: BookOpen,        href: "/courses",      roles: ["super_admin","academic_admin","hod","faculty","student"] },
  { label: "Enrollment",      icon: ClipboardList,   href: "/enrollment",   roles: ["super_admin","academic_admin","hod","faculty","student","registrar"] },
  { label: "Grading",         icon: BarChart3,       href: "/grading",      roles: ["super_admin","academic_admin","hod","faculty","registrar","examiner"] },
  { label: "Research / PG",   icon: FlaskConical,    href: "/research",     roles: ["super_admin","academic_admin","hod","faculty","student","research_supervisor"] },
  { label: "Users",           icon: Users,           href: "/users",        roles: ["super_admin","academic_admin"] },
  { label: "Notifications",   icon: Bell,            href: "/notifications",roles: [] },
];

export function AMSSidebar({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const pathname = usePathname();
  const role = useRole();
  const clearAuth = useAuthStore((s) => s.clearAuth);
  const router = useRouter();

  const visible = NAV.filter((n) => n.roles.length === 0 || !role || n.roles.includes(role));

  async function logout() {
    clearAuth();
    router.push("/login");
  }

  return (
    <aside style={{ width: collapsed ? 64 : 220 }}
      className="fixed left-0 top-0 h-full bg-white border-r border-gray-200 z-30 flex flex-col overflow-hidden transition-all duration-200">
      {/* Brand */}
      <div className="flex items-center gap-3 px-4 h-16 border-b border-gray-200 shrink-0">
        <div className="w-9 h-9 rounded-xl bg-[#0D6E6E] flex items-center justify-center shrink-0">
          <GraduationCap size={18} className="text-white" />
        </div>
        {!collapsed && (
          <div>
            <p className="text-base font-bold text-[#1A1A2E]">AVFU AMS</p>
            <p className="text-sm text-gray-700">Academic System</p>
          </div>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 overflow-y-auto py-3 px-2">
        {visible.map((item) => {
          const active = pathname.startsWith(item.href);
          return (
            <a key={item.href} href={item.href} title={collapsed ? item.label : undefined}
              className={cn("group flex items-center gap-3 px-3 py-2.5 rounded-xl mb-0.5 transition-all relative",
                active ? "bg-[#E6F4F4] text-[#0D6E6E] font-semibold" : "text-gray-700 hover:bg-gray-50 hover:text-[#0D6E6E]")}>
              {active && <div className="absolute left-0 top-1.5 bottom-1.5 w-1 rounded-full bg-[#0D6E6E]" />}
              <item.icon size={18} className={cn("shrink-0", active ? "text-[#0D6E6E]" : "text-gray-600 group-hover:text-[#0D6E6E]")} />
              {!collapsed && <span className="text-base truncate">{item.label}</span>}
            </a>
          );
        })}
      </nav>

      {/* Bottom */}
      <div className="px-2 pb-3 shrink-0 space-y-1">
        <button onClick={logout} title={collapsed ? "Logout" : undefined}
          className="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-gray-700 hover:bg-red-50 hover:text-red-600 transition-colors">
          <LogOut size={18} className="shrink-0" />
          {!collapsed && <span className="text-base">Logout</span>}
        </button>
        <button onClick={onToggle}
          className="w-full flex items-center justify-center gap-2 py-2 text-sm text-gray-600 hover:text-[#0D6E6E] transition-colors">
          {collapsed ? <ChevronRight size={15} /> : <><ChevronLeft size={15} /><span className="text-base">Collapse</span></>}
        </button>
      </div>
    </aside>
  );
}
