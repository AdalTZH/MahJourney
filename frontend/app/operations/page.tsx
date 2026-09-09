"use client";

import { Activity, BarChart3, Bot, CloudRain, Database, KeyRound, Link2, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { AppShell } from "@/components/app-shell";
import { api } from "@/lib/api";

type Integration = { integration: string; status: string; message?: string; age_seconds?: number; record_count?: number };
type Audit = { valid: boolean; events: { sequence: number; event_type: string; event_hash: string }[] };
type ForecastGate = { status: string; synchronized_days: number; required_days: number };
type Evaluation = { scenario_count: number; scenario_mix: { golden: number; disruption: number; adversarial: number }; hard_constraint_compliance: number; policy_compliance: number; zero_infeasible_automatic_executions: boolean; median_cost_improvement_percent: number; traffic_free_ortools_comparison: string };
const initial: Integration[] = [{ integration: "OneMap", status: "CHECKING" }, { integration: "LTA DataMall", status: "CHECKING" }, { integration: "NEA / data.gov.sg", status: "CHECKING" }, { integration: "OpenAI Responses", status: "CHECKING" }];
export default function OperationsPage() {
  const [integrations, setIntegrations] = useState(initial);
  const [audit, setAudit] = useState<Audit>({ valid: false, events: [] });
  const [gate, setGate] = useState<ForecastGate>({ status: "EXPERIMENTAL", synchronized_days: 0, required_days: 14 });
  const [evaluation, setEvaluation] = useState<Evaluation | null>(null);
  const [evaluating, setEvaluating] = useState(false);
  useEffect(() => {
    const refresh = () => Promise.all([api<Integration[]>("/operations/integrations"), api<Audit>("/operations/audit"), api<ForecastGate>("/operations/forecast-gate")]).then(([health, chain, forecast]) => { setIntegrations(health); setAudit(chain); setGate(forecast); }).catch(() => setIntegrations(initial.map((item) => ({ ...item, status: "UNAVAILABLE" }))));
    void refresh(); const timer = window.setInterval(refresh, 30_000); return () => window.clearInterval(timer);
  }, []);
  const recentAudit = audit.events.slice(-4).reverse();
  const progress = Math.min(100, gate.synchronized_days / gate.required_days * 100);
  const runEvaluation = async () => { setEvaluating(true); try { setEvaluation(await api<Evaluation>("/evaluations/run", { method: "POST" })); } finally { setEvaluating(false); } };
  return <AppShell><div className="operations-layout"><section className="operations-heading"><span className="eyebrow">SYSTEM OBSERVABILITY</span><h1>Operations control</h1><p>External freshness, agent handoffs, policy outcomes, and tamper-evident audit state.</p></section><section className="health-grid">{integrations.map((item, index) => { const Icon = [Link2, Activity, CloudRain, Bot][index] ?? Database; const detail = item.age_seconds === undefined ? item.message : `${item.record_count ?? 0} records · ${item.age_seconds}s ago`; return <article className="panel health-card" key={item.integration}><Icon size={19} /><div><span>{item.integration}</span><strong>{item.status}</strong>{detail && <small>{detail}</small>}</div><i className={["NOT_CONFIGURED", "FETCH_FAILED", "STALE", "UNAVAILABLE"].includes(item.status) ? "warning" : ""} /></article>; })}</section>
    <section className="panel agent-trace"><div className="panel-heading"><div><span className="eyebrow">AGENT BOUNDARY</span><h2>Master-mediated execution</h2></div><span className="verified-badge">NO HIDDEN CoT</span></div><div className="trace-flow"><div><Bot size={18} /><strong>Master Dispatcher</strong><span>sole memory access</span></div><b>→</b><div><Activity size={18} /><strong>Worker agent</strong><span>bounded tool grant</span></div><b>→</b><div><ShieldCheck size={18} /><strong>Policy engine</strong><span>deterministic authority</span></div></div></section>
    <section className="panel audit-table"><div className="panel-heading"><div><span className="eyebrow">AUDIT INTEGRITY</span><h2>HMAC-SHA256 chain</h2></div><span className={audit.valid ? "online-label" : "verified-badge"}>{audit.valid ? "VERIFIED" : "INVALID"}</span></div>{recentAudit.map((event) => <div className="audit-row" key={event.sequence}><span>{String(event.sequence).padStart(3, "0")}</span><strong>{event.event_type}</strong><code>{`${event.event_hash.slice(0, 5)}…${event.event_hash.slice(-4)}`}</code><em>{audit.valid ? "chain valid" : "check failed"}</em></div>)}</section>
    <section className="panel gate-card"><KeyRound size={20} /><span className="eyebrow">FORECAST GATE</span><h2>Weather Markov remains {gate.status.toLowerCase()}</h2><p>Operational enablement requires 14 synchronized days plus Brier-score and ETA-MAE improvements over traffic-only and persistence baselines.</p><div className="gate-progress"><i style={{ width: `${progress}%` }} /></div><span>Day {gate.synchronized_days} / {gate.required_days} · synchronized LTA v4 and NEA rainfall evidence</span></section>
    <section className="panel evaluation-card"><div><BarChart3 size={20} /><span className="eyebrow">EVALUATION HARNESS</span><h2>Golden, disruption, and adversarial scenarios</h2><p>{evaluation ? `${evaluation.scenario_count} cases · ${evaluation.scenario_mix.golden}/${evaluation.scenario_mix.disruption}/${evaluation.scenario_mix.adversarial} mix` : "Run the deterministic acceptance suite on demand."}</p></div>{evaluation && <div className="evaluation-metrics"><span><b>{(evaluation.hard_constraint_compliance * 100).toFixed(0)}%</b> hard constraints</span><span><b>{(evaluation.policy_compliance * 100).toFixed(0)}%</b> policy</span><span><b>{evaluation.median_cost_improvement_percent}%</b> vs greedy</span><span className={evaluation.zero_infeasible_automatic_executions ? "pass" : "fail"}><b>{evaluation.zero_infeasible_automatic_executions ? "PASS" : "FAIL"}</b> fail-closed</span><span className="pending"><b>{evaluation.traffic_free_ortools_comparison.replaceAll("_", " ")}</b> traffic-aware gate</span></div>}<button type="button" disabled={evaluating} onClick={() => void runEvaluation()}>{evaluating ? "RUNNING…" : "RUN EVALUATION"}</button></section></div></AppShell>;
}
