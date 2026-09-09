"use client";

import { AlertTriangle, ArrowUpRight, Bot, CheckCircle2, Clock3, Send, Sparkles, Truck } from "lucide-react";
import { type SyntheticEvent, useEffect, useState } from "react";
import { AppShell } from "@/components/app-shell";
import { DispatchMap } from "@/components/dispatch-map";
import { api, fixtureMapState, type MapState } from "@/lib/api";
import { useWebMcpTool } from "@/hooks/use-webmcp";

const askDispatcherTool = {
  name: "ask_dispatcher",
  title: "Ask Master Dispatcher",
  description: "Send a dispatch question and show the evidence-bound response in the console.",
  inputSchema: {
    type: "object",
    properties: { message: { type: "string", minLength: 1, maxLength: 4000 } },
    required: ["message"],
    additionalProperties: false,
  },
  annotations: { readOnlyHint: false, untrustedContentHint: false },
};

export default function DispatcherPage() {
  const [data, setData] = useState<MapState>(fixtureMapState());
  const [source, setSource] = useState<"LIVE API" | "FIXTURE MODE">("FIXTURE MODE");
  const [message, setMessage] = useState("");
  const [reply, setReply] = useState("I’m monitoring 10 vehicles and 40 stops. No hard constraint violations are present.");
  const [busy, setBusy] = useState(false);
  useWebMcpTool(askDispatcherTool, async (input) => {
    const message = (input as { message?: unknown }).message;
    if (typeof message !== "string" || !message.trim()) throw new Error("message is required");
    const result = await api<{ reply: string }>("/dispatcher/messages", {
      method: "POST",
      body: JSON.stringify({ message, conversation_id: "dispatcher-webmcp" }),
    });
    setReply(result.reply);
    return { status: "completed", reply: result.reply };
  });
  useEffect(() => { api<MapState>("/map/state").then((value) => { setData(value); setSource("LIVE API"); }).catch(() => setSource("FIXTURE MODE")); }, []);
  async function sendMessage(event: SyntheticEvent<HTMLFormElement>) { event.preventDefault(); if (!message.trim()) return; setBusy(true); try { const result = await api<{ reply: string }>("/dispatcher/messages", { method: "POST", body: JSON.stringify({ message, conversation_id: "dispatcher-ui" }) }); setReply(result.reply); } catch { setReply("The API is offline, so I kept the current validated fixture plan unchanged."); } finally { setMessage(""); setBusy(false); } }
  return <AppShell><div className="dashboard-grid">
    <section className="map-panel panel"><div className="panel-heading map-heading"><div><span className="eyebrow">FLEET TOPOLOGY</span><h1>Tuas / Jurong live plan</h1></div><span className="mode-chip"><span />{source}</span></div><DispatchMap data={data} /><div className="map-metrics"><div><Truck size={15} /><span><strong>10</strong> VEHICLES</span></div><div><Clock3 size={15} /><span><strong>40</strong> STOPS</span></div><div><CheckCircle2 size={15} /><span><strong>0</strong> HARD VIOLATIONS</span></div></div></section>
    <aside className="right-rail"><section className="panel plan-card"><div className="panel-heading"><div><span className="eyebrow">ACTIVE CANDIDATE</span><h2>Plan v{data.plan.version}</h2></div><span className="verified-badge">VALIDATED</span></div><div className="cost-number">{data.plan.objective_cost.toFixed(1)} <small>km</small></div><p>Estimated fleet distance</p><div className="metric-row"><span>On-time confidence</span><strong>94.2%</strong></div><div className="metric-row"><span>Workload spread</span><strong>± 6 min</strong></div><button className="primary-action">Compare plan <ArrowUpRight size={15} /></button></section>
      <section className="panel alert-card"><div className="panel-heading"><div><span className="eyebrow">LIVE SIGNALS</span><h2>Operations feed</h2></div><span className="count-badge">03</span></div><div className="feed-item amber"><AlertTriangle size={17} /><div><strong>Heavy rain watch</strong><span>Jurong West · confidence 78%</span></div><time>2m</time></div><div className="feed-item"><Sparkles size={17} /><div><strong>Plan recalculated</strong><span>Saved 8.4 km · no reassignment</span></div><time>5m</time></div><div className="feed-item"><CheckCircle2 size={17} /><div><strong>LTA snapshot fresh</strong><span>1,248 speed-band links</span></div><time>7m</time></div></section></aside>
    <section className="panel route-strip"><div className="panel-heading"><div><span className="eyebrow">ROUTE LOAD</span><h2>Assignments</h2></div><span className="muted-label">SORTED BY ETA RISK</span></div><div className="route-table">{data.plan.routes.slice(0, 5).map((route, index) => <div className="route-row" key={route.vehicle_id}><span className={`route-color color-${index}`} /><strong>{route.vehicle_id}</strong><span>{route.driver_id}</span><div className="load-track"><i style={{ width: `${68 + index * 5}%` }} /></div><span>{route.stops.length} stops</span><strong>{route.duration_minutes} min</strong></div>)}</div></section>
    <section className="panel agent-console"><div className="agent-header"><div className="bot-mark"><Bot size={18} /></div><div><span className="eyebrow">MASTER DISPATCHER</span><h2>Command console</h2></div><span className="online-label">ONLINE</span></div><div className="agent-reply"><p>{reply}</p><span>Evidence: plan:{data.plan.plan_id.slice(0, 8)} · policy trace available</span></div><form onSubmit={sendMessage} className="command-form"><input aria-label="Message Master Dispatcher" value={message} onChange={(event) => setMessage(event.target.value)} placeholder="Ask about routes, trade-offs, or disruptions…" /><button disabled={busy || !message.trim()} aria-label="Send command"><Send size={16} /></button></form></section>
  </div></AppShell>;
}
