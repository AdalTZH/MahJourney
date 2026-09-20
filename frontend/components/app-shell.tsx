"use client";

import Link from "next/link";
import { useRouter, usePathname } from "next/navigation";
import { Activity, LogOut, Map, RadioTower, Route, ShieldCheck } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { fetchSessionStatus, logout } from "@/lib/api";

const navigation = [{ href: "/dispatcher", label: "Dispatcher", icon: Route }, { href: "/scenario", label: "Scenario", icon: Map }, { href: "/operations", label: "Operations", icon: Activity }];

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  // "checking" avoids a flash of protected content before the session check
  // resolves; an unauthenticated visitor is redirected to /login and never
  // sees `children` render.
  const [authState, setAuthState] = useState<"checking" | "authenticated">("checking");
  useEffect(() => {
    let active = true;
    fetchSessionStatus().then((status) => {
      if (!active) return;
      if (status.authenticated) setAuthState("authenticated");
      else router.push("/login");
    });
    return () => { active = false; };
  }, [router]);
  const onLogout = async () => { await logout(); router.push("/login"); };
  if (authState === "checking") return <div className="app-shell" aria-busy="true" />;
  return <div className="app-shell">
    <header className="topbar">
      <div className="brand-lockup"><div className="brand-mark"><RadioTower size={17} /></div><div><strong>MAHJOURNEY</strong><span>DISPATCH INTELLIGENCE</span></div></div>
      <nav aria-label="Primary navigation">{navigation.map(({ href, label, icon: Icon }) => <Link key={href} className={pathname === href ? "nav-link active" : "nav-link"} href={href}><Icon size={15} />{label}</Link>)}</nav>
      <div className="system-state"><span className="signal-dot" />SYSTEM NOMINAL<button type="button" className="logout-button" onClick={() => void onLogout()} aria-label="Sign out"><LogOut size={13} /></button></div>
    </header>
    <main>{children}</main>
    <footer className="statusbar"><span><ShieldCheck size={13} />AUDIT CHAIN VERIFIED</span><span>SGT · ASIA/SINGAPORE</span><span>MARKOV: EXPERIMENTAL</span></footer>
  </div>;
}

