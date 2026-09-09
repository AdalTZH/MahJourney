"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity, Map, RadioTower, Route, ShieldCheck } from "lucide-react";
import type { ReactNode } from "react";

const navigation = [{ href: "/dispatcher", label: "Dispatcher", icon: Route }, { href: "/scenario", label: "Scenario", icon: Map }, { href: "/operations", label: "Operations", icon: Activity }];

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  return <div className="app-shell">
    <header className="topbar">
      <div className="brand-lockup"><div className="brand-mark"><RadioTower size={17} /></div><div><strong>MAHJOURNEY</strong><span>DISPATCH INTELLIGENCE</span></div></div>
      <nav aria-label="Primary navigation">{navigation.map(({ href, label, icon: Icon }) => <Link key={href} className={pathname === href ? "nav-link active" : "nav-link"} href={href}><Icon size={15} />{label}</Link>)}</nav>
      <div className="system-state"><span className="signal-dot" />SYSTEM NOMINAL</div>
    </header>
    <main>{children}</main>
    <footer className="statusbar"><span><ShieldCheck size={13} />AUDIT CHAIN VERIFIED</span><span>SGT · ASIA/SINGAPORE</span><span>MARKOV: EXPERIMENTAL</span></footer>
  </div>;
}

