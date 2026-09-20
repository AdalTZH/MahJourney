"use client";

import { AlertTriangle, ArrowUpRight, Bot, CheckCircle2, ChevronDown, Clock3, MapPin, Mic, MicOff, Package, Search, Send, SendHorizontal, Sparkles, Truck, Volume2, VolumeX } from "lucide-react";
import { type SyntheticEvent, useEffect, useMemo, useRef, useState } from "react";
import { AppShell } from "@/components/app-shell";
import { DispatchMap } from "@/components/dispatch-map";
import { api, fixtureMapState, sendPlanToDrivers, type MapState } from "@/lib/api";
import { useSpeech } from "@/hooks/use-speech";
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

type Signal = { integration: string; status: string; age_seconds?: number; record_count?: number };

export default function DispatcherPage() {
  const [data, setData] = useState<MapState>(fixtureMapState());
  const [source, setSource] = useState<"LIVE API" | "FIXTURE MODE">("FIXTURE MODE");
  const [signals, setSignals] = useState<Signal[]>([]);
  const [message, setMessage] = useState("");
  const [reply, setReply] = useState("Monitoring the live plan. Ask about routes, trade-offs, or disruptions.");
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<"overview" | "plan">("overview");
  const [expandedVehicles, setExpandedVehicles] = useState<Set<string>>(new Set());
  const [selectedVehicleId, setSelectedVehicleId] = useState<string | null>(null);
  const [voiceReplyEnabled, setVoiceReplyEnabled] = useState(false);
  const speech = useSpeech();
  const lastSpokenReply = useRef("");
  const [planQuery, setPlanQuery] = useState("");
  const [planSort, setPlanSort] = useState<"stops" | "duration" | "vehicle">("stops");
  const [dispatching, setDispatching] = useState(false);
  const [dispatchStatus, setDispatchStatus] = useState<string | null>(null);
  async function sendToDrivers() {
    if (dispatching) return;
    setDispatching(true);
    setDispatchStatus(null);
    try {
      const wasActive = data.plan.status === "ACTIVE";
      const result = await sendPlanToDrivers(data.plan);
      if (!wasActive) setData((current) => ({ ...current, plan: { ...current.plan, status: "ACTIVE" } }));
      const entries = Object.entries(result.sent);
      const sentCount = entries.filter(([, ok]) => ok).length;
      const activatedPrefix = wasActive ? "" : "Plan activated. ";
      setDispatchStatus(entries.length === 0 ? `${activatedPrefix}No routes to dispatch.` : `${activatedPrefix}Sent to ${sentCount} of ${entries.length} drivers on Telegram.`);
    } catch (error) {
      setDispatchStatus(error instanceof Error ? error.message : "Failed to send plan to drivers.");
    } finally {
      setDispatching(false);
    }
  }
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
  useEffect(() => {
    const refresh = () => api<Signal[]>("/operations/integrations").then((items) => setSignals(items.filter((item) => item.age_seconds !== undefined))).catch(() => setSignals([]));
    void refresh(); const timer = window.setInterval(refresh, 30_000); return () => window.clearInterval(timer);
  }, []);
  // While listening, the input mirrors the live transcript; once it stops the
  // transcript is copied into `message` so the recognized text stays visible
  // (and editable) instead of disappearing.
  const commandValue = speech.listening ? speech.transcript : message;
  // Speak newly received replies aloud when hands-free mode is on.
  const speakRef = useRef(speech.speak);
  useEffect(() => { speakRef.current = speech.speak; }, [speech.speak]);
  useEffect(() => {
    if (!voiceReplyEnabled || !reply || reply === lastSpokenReply.current) return;
    lastSpokenReply.current = reply;
    speakRef.current(reply);
  }, [reply, voiceReplyEnabled]);
  async function submitCommand(rawText: string) {
    const outgoing = rawText.trim();
    if (!outgoing || busy) return;
    speech.clearTranscript();
    setMessage("");
    setBusy(true);
    try {
      const result = await api<{ reply: string }>("/dispatcher/messages", { method: "POST", body: JSON.stringify({ message: outgoing, conversation_id: "dispatcher-ui" }) });
      setReply(result.reply);
    } catch { setReply("The API is offline, so I kept the current validated fixture plan unchanged."); }
    finally { setBusy(false); }
  }
  function sendMessage(event: SyntheticEvent<HTMLFormElement>) { event.preventDefault(); void submitCommand(speech.listening ? speech.transcript : message); }
  function toggleMic() {
    if (speech.listening) { setMessage(speech.transcript); speech.stopListening(); return; }
    setMessage("");
    // Auto-submit when the spoken phrase finalizes — fully hands-free. The
    // recognized text is kept in the input until the send completes so it
    // never just disappears.
    speech.listen((finalText) => { setMessage(finalText); void submitCommand(finalText); });
  }
  const vehicleCount = data.plan.routes.length;
  const stopCount = data.plan.routes.reduce((total, route) => total + route.stops.length, 0);
  const violationCount = data.plan.hard_violations?.length ?? 0;
  const activeRoutes = data.plan.routes.filter((route) => route.stops.length > 0);
  const standbyCount = data.plan.routes.length - activeRoutes.length;
  const activeDurations = activeRoutes.map((route) => route.duration_minutes);
  const workloadSpread = activeDurations.length > 1 ? Math.round((Math.max(...activeDurations) - Math.min(...activeDurations)) / 2) : 0;
  const formatDuration = (minutes: number) => { const h = Math.floor(minutes / 60); const m = minutes % 60; return h > 0 ? `${h}h ${m}m` : `${m}m`; };
  const signalTone = (status: string) => (["FRESH", "CONFIGURED"].includes(status) ? "" : "amber");
  const signalAge = (seconds?: number) => (seconds === undefined ? "" : seconds < 90 ? "live" : seconds < 3600 ? `${Math.round(seconds / 60)}m` : `${Math.round(seconds / 3600)}h`);
  const clockToTime = (minute: number) => `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;
  const toggleVehicle = (id: string) => setExpandedVehicles((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const maxStops = Math.max(1, ...data.plan.routes.map((route) => route.stops.length));
  const selectedRoute = selectedVehicleId ? data.plan.routes.find((route) => route.vehicle_id === selectedVehicleId) ?? null : null;
  const selectedTruck = selectedVehicleId ? data.trucks.find((truck) => truck.vehicle_id === selectedVehicleId) ?? null : null;
  const planRoutes = useMemo(() => {
    const query = planQuery.trim().toLowerCase();
    const filtered = data.plan.routes.filter((route) => !query || route.vehicle_id.toLowerCase().includes(query) || route.driver_id.toLowerCase().includes(query));
    const sorted = [...filtered];
    if (planSort === "stops") sorted.sort((a, b) => b.stops.length - a.stops.length);
    else if (planSort === "duration") sorted.sort((a, b) => b.duration_minutes - a.duration_minutes);
    else sorted.sort((a, b) => a.vehicle_id.localeCompare(b.vehicle_id));
    return sorted;
  }, [data.plan.routes, planQuery, planSort]);
  return <AppShell><div className="dispatcher-tabs" role="tablist" aria-label="Dispatcher views">
    <button role="tab" aria-selected={tab === "overview"} className={tab === "overview" ? "tab-button active" : "tab-button"} onClick={() => setTab("overview")}>Overview</button>
    <button role="tab" aria-selected={tab === "plan"} className={tab === "plan" ? "tab-button active" : "tab-button"} onClick={() => setTab("plan")}>Plan Detail</button>
  </div>
  {tab === "overview" ? <div className="dashboard-grid">
    <section className="map-panel panel"><div className="panel-heading map-heading"><div><span className="eyebrow">FLEET TOPOLOGY</span><h1>Live fleet plan</h1></div><span className="mode-chip"><span />{source}</span></div><DispatchMap data={data} selectedVehicleId={selectedVehicleId} onSelectVehicle={setSelectedVehicleId} /><div className="map-metrics"><div><Truck size={15} /><span><strong>{vehicleCount}</strong> VEHICLES</span></div><div><Clock3 size={15} /><span><strong>{stopCount}</strong> STOPS</span></div><div><CheckCircle2 size={15} /><span><strong>{violationCount}</strong> HARD VIOLATIONS</span></div></div></section>
    <aside className="right-rail"><section className="panel plan-card"><div className="panel-heading"><div><span className="eyebrow">ACTIVE CANDIDATE</span><h2>Plan v{data.plan.version}</h2></div><span className="verified-badge">{data.plan.status}</span></div><div className="cost-number">{data.plan.objective_cost.toFixed(1)} <small>km</small></div><p>Estimated fleet distance</p><div className="metric-row"><span>Assigned stops</span><strong>{stopCount}</strong></div><div className="metric-row"><span>Workload spread</span><strong>± {formatDuration(workloadSpread)}</strong></div><button className="primary-action">Compare plan <ArrowUpRight size={15} /></button>
      <button className="primary-action secondary-action" disabled={dispatching || violationCount > 0} onClick={sendToDrivers} title={violationCount > 0 ? "This plan has hard violations and cannot be activated or dispatched" : data.plan.status === "ACTIVE" ? "Resend route messages to enrolled drivers on Telegram" : "Activate this plan and send route messages to enrolled drivers on Telegram"}>{dispatching ? "Sending…" : data.plan.status === "ACTIVE" ? "Send to drivers" : "Activate & send to drivers"} <SendHorizontal size={15} /></button>
      {dispatchStatus && <p className="dispatch-status">{dispatchStatus}</p>}
      </section>
      <section className="panel alert-card"><div className="panel-heading"><div><span className="eyebrow">LIVE SIGNALS</span><h2>Operations feed</h2></div><span className="count-badge">{String(signals.length).padStart(2, "0")}</span></div>{signals.length === 0 ? <div className="feed-item"><Sparkles size={17} /><div><strong>No live snapshots yet</strong><span>Collector warming up or credentials pending</span></div></div> : signals.map((signal) => <div className={`feed-item ${signalTone(signal.status)}`} key={signal.integration}>{signal.status === "FRESH" ? <CheckCircle2 size={17} /> : <AlertTriangle size={17} />}<div><strong>{signal.integration} · {signal.status}</strong><span>{(signal.record_count ?? 0).toLocaleString()} records</span></div><time>{signalAge(signal.age_seconds)}</time></div>)}</section></aside>
    <section className="panel vehicle-inspector">
      {!selectedRoute ? <div className="vehicle-inspector-empty"><MapPin size={22} /><p>Click a vehicle on the map to inspect its route.</p><span className="muted-label">{activeRoutes.length} active · {standbyCount} standby</span></div> : <>
        <div className="panel-heading"><div><span className="eyebrow">VEHICLE INSPECTOR</span><h2>{selectedRoute.vehicle_id} <span className="muted-label">Driver {selectedRoute.driver_id}</span></h2></div><button className="icon-button" onClick={() => setSelectedVehicleId(null)} aria-label="Close vehicle inspector">✕</button></div>
        <div className="plan-detail-summary compact">
          <div className="plan-detail-stat"><Package size={14} /><div><strong>{selectedRoute.stops.length}</strong><span>stops</span></div></div>
          <div className="plan-detail-stat"><MapPin size={14} /><div><strong>{selectedRoute.distance_km.toFixed(1)}</strong><span>km</span></div></div>
          <div className="plan-detail-stat"><Clock3 size={14} /><div><strong>{formatDuration(selectedRoute.duration_minutes)}</strong><span>duration</span></div></div>
          {selectedTruck && <div className="plan-detail-stat"><Truck size={14} /><div><strong>{selectedTruck.phase.replace("_", " ")}</strong><span>phase</span></div></div>}
        </div>
        {selectedRoute.stops.length === 0 ? <div className="vehicle-inspector-empty"><p>This vehicle is on standby — no stops in the current plan.</p></div> : <div className="plan-detail-stops flush">
          <table className="plan-detail-table">
            <thead><tr><th>#</th><th>Stop</th><th>ETA</th><th>Departs</th><th>Service</th><th>Qty</th></tr></thead>
            <tbody>
              {selectedRoute.stops.map((stop) => { const done = selectedTruck ? data.clock.current_minute >= stop.departure_minute : false; return <tr key={stop.stop_id} className={done ? "is-done" : ""}>
                <td className="plan-detail-seq">{stop.sequence}</td>
                <td className="plan-detail-stop-id">{stop.stop_id}</td>
                <td>{clockToTime(stop.eta_minute)}</td>
                <td>{clockToTime(stop.departure_minute)}</td>
                <td className="muted-label">{stop.departure_minute - stop.eta_minute}m</td>
                <td>{stop.demand}</td>
              </tr>; })}
            </tbody>
          </table>
        </div>}
      </>}
    </section>
    <section className="panel agent-console">
      <div className="agent-header">
        <div className="bot-mark"><Bot size={18} /></div>
        <div><span className="eyebrow">MASTER DISPATCHER</span><h2>Command console</h2></div>
        {speech.synthesisSupported && <button type="button" className={voiceReplyEnabled ? "voice-toggle active" : "voice-toggle"} onClick={() => { setVoiceReplyEnabled((value) => !value); if (voiceReplyEnabled) speech.stopSpeaking(); }} aria-pressed={voiceReplyEnabled} title={voiceReplyEnabled ? "Hands-free replies on — click to mute" : "Enable spoken replies"}>{voiceReplyEnabled ? <Volume2 size={14} /> : <VolumeX size={14} />}<span>{speech.speaking ? "SPEAKING…" : voiceReplyEnabled ? "VOICE ON" : "VOICE OFF"}</span></button>}
        <span className="online-label">ONLINE</span>
      </div>
      <div className="agent-reply"><p>{reply}</p><span>Evidence: plan:{data.plan.plan_id.slice(0, 8)} · policy trace available</span></div>
      <form onSubmit={sendMessage} className="command-form">
        {speech.recognitionSupported && <button type="button" className={speech.listening ? "mic-button listening" : "mic-button"} onClick={toggleMic} aria-label={speech.listening ? "Stop voice input" : "Start voice input"} aria-pressed={speech.listening}>{speech.listening ? <Mic size={16} /> : <MicOff size={16} className="mic-idle" />}</button>}
        <input aria-label="Message Master Dispatcher" value={commandValue} onChange={(event) => setMessage(event.target.value)} readOnly={speech.listening} placeholder={speech.listening ? "Listening…" : "Ask about routes, trade-offs, or disruptions…"} />
        <button disabled={busy || !commandValue.trim()} aria-label="Send command"><Send size={16} /></button>
      </form>
      {speech.error && <p className="voice-error">{speech.error}</p>}
    </section>
  </div> : <div className="plan-detail-page">
    <section className="panel plan-detail">
      <div className="panel-heading plan-detail-heading">
        <div><span className="eyebrow">PLAN DETAIL</span><h1>Plan v{data.plan.version} <span className="plan-detail-id">{data.plan.plan_id.slice(0, 8)}</span></h1></div>
        <div className="plan-detail-heading-actions">
          <span className={data.plan.status === "VALIDATED" ? "verified-badge" : "verified-badge warn"}>{data.plan.status}</span>
          <button className="primary-action secondary-action" disabled={dispatching || violationCount > 0} onClick={sendToDrivers} title={violationCount > 0 ? "This plan has hard violations and cannot be activated or dispatched" : data.plan.status === "ACTIVE" ? "Resend route messages to enrolled drivers on Telegram" : "Activate this plan and send route messages to enrolled drivers on Telegram"}>{dispatching ? "Sending…" : data.plan.status === "ACTIVE" ? "Send to drivers" : "Activate & send to drivers"} <SendHorizontal size={15} /></button>
        </div>
      </div>
      {dispatchStatus && <p className="dispatch-status plan-detail-dispatch-status">{dispatchStatus}</p>}
      <div className="plan-detail-summary">
        <div className="plan-detail-stat"><Truck size={15} /><div><strong>{data.plan.routes.length}</strong><span>vehicles</span></div></div>
        <div className="plan-detail-stat"><Package size={15} /><div><strong>{stopCount}</strong><span>stops</span></div></div>
        <div className="plan-detail-stat"><MapPin size={15} /><div><strong>{data.plan.objective_cost.toFixed(1)}</strong><span>km total</span></div></div>
        <div className="plan-detail-stat"><Clock3 size={15} /><div><strong>± {formatDuration(workloadSpread)}</strong><span>spread</span></div></div>
        {violationCount > 0 && <div className="plan-detail-stat warn"><AlertTriangle size={15} /><div><strong>{violationCount}</strong><span>violations</span></div></div>}
      </div>
      <div className="plan-detail-toolbar">
        <div className="search-field"><Search size={14} /><input value={planQuery} onChange={(event) => setPlanQuery(event.target.value)} placeholder="Search vehicle or driver…" aria-label="Search routes" /></div>
        <fieldset className="sort-field" aria-label="Sort routes by"><legend className="sr-only">Sort routes by</legend>
          {([["stops", "Stops"], ["duration", "Duration"], ["vehicle", "Vehicle"]] as const).map(([value, label]) => <button key={value} type="button" className={planSort === value ? "sort-chip active" : "sort-chip"} onClick={() => setPlanSort(value)}>{label}</button>)}
        </fieldset>
        <span className="plan-detail-count">{planRoutes.length} of {data.plan.routes.length}</span>
      </div>
      <div className="plan-detail-list">
        {planRoutes.length === 0 && <div className="plan-detail-empty">No vehicles match “{planQuery}”.</div>}
        {planRoutes.map((route, index) => {
          const standby = route.stops.length === 0;
          const expanded = expandedVehicles.has(route.vehicle_id);
          const loadPercent = Math.round((route.stops.length / maxStops) * 100);
          return <div className={`plan-detail-route${standby ? " is-standby" : ""}`} key={route.vehicle_id}>
            <button className="plan-detail-route-header" onClick={() => toggleVehicle(route.vehicle_id)} aria-expanded={expanded}>
              <span className={`route-color color-${index % 10}`} aria-hidden="true" />
              <span className="plan-detail-route-id"><strong>{route.vehicle_id}</strong><span className="muted-label">Driver {route.driver_id}</span></span>
              {standby ? <span className="standby-pill">STANDBY</span> : <>
                <div className="plan-detail-route-load"><div className="load-track"><i style={{ width: `${loadPercent}%` }} /></div><span>{route.stops.length} stops</span></div>
                <span className="plan-detail-route-metric">{route.distance_km.toFixed(1)} km</span>
                <span className="plan-detail-route-metric strong">{formatDuration(route.duration_minutes)}</span>
              </>}
              <ChevronDown size={16} className={`chevron${expanded ? " open" : ""}`} />
            </button>
            {expanded && !standby && <div className="plan-detail-stops">
              <table className="plan-detail-table">
                <thead><tr><th>#</th><th>Stop</th><th>ETA</th><th>Departs</th><th>Service</th><th>Qty</th></tr></thead>
                <tbody>
                  {route.stops.map((stop) => <tr key={stop.stop_id}>
                    <td className="plan-detail-seq">{stop.sequence}</td>
                    <td className="plan-detail-stop-id">{stop.stop_id}</td>
                    <td>{clockToTime(stop.eta_minute)}</td>
                    <td>{clockToTime(stop.departure_minute)}</td>
                    <td className="muted-label">{stop.departure_minute - stop.eta_minute}m</td>
                    <td>{stop.demand}</td>
                  </tr>)}
                </tbody>
              </table>
            </div>}
          </div>;
        })}
      </div>
    </section>
  </div>}
  </AppShell>;
}
