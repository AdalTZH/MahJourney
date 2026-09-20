"use client";

import { CloudRain, FastForward, GitBranch, Pause, Play, RotateCcw, Siren, Truck } from "lucide-react";
import { useEffect, useState } from "react";
import { AppShell } from "@/components/app-shell";
import { DispatchMap } from "@/components/dispatch-map";
import { api, depot, fixtureMapState, type MapState } from "@/lib/api";
import { useWebMcpTool } from "@/hooks/use-webmcp";

const injectDisruptionTool = {
  name: "inject_scenario_event",
  title: "Inject scenario event",
  description: "Inject one supported disruption into scenario mode at the visible virtual time.",
  inputSchema: {
    type: "object",
    properties: { event_type: { type: "string", enum: ["ROAD_CLOSURE", "URGENT_ORDER", "TRUCK_BREAKDOWN", "HEAVY_RAIN"] } },
    required: ["event_type"],
    additionalProperties: false,
  },
  annotations: { readOnlyHint: false, untrustedContentHint: false },
};

const events = [{ type: "ROAD_CLOSURE", label: "Road closure", icon: Siren }, { type: "URGENT_ORDER", label: "Urgent order", icon: FastForward }, { type: "TRUCK_BREAKDOWN", label: "Truck breakdown", icon: Truck }, { type: "HEAVY_RAIN", label: "Heavy rain", icon: CloudRain }];
export default function ScenarioPage() {
  const [data, setData] = useState<MapState>(fixtureMapState()); const [playing, setPlaying] = useState(false); const [speed, setSpeed] = useState<1 | 5 | 20>(1); const [minute, setMinute] = useState(548); const [branches, setBranches] = useState(["demo"]);
  useWebMcpTool(injectDisruptionTool, async (input) => {
    const eventType = (input as { event_type?: unknown }).event_type;
    if (typeof eventType !== "string" || !events.some((event) => event.type === eventType)) throw new Error("unsupported event_type");
    const webMcpPayload: Record<string, unknown> = { source: "webmcp" };
    if (eventType === "ROAD_CLOSURE") { webMcpPayload.lat = depot.lat; webMcpPayload.lon = depot.lon; webMcpPayload.radius_km = 1.5; }
    if (eventType === "HEAVY_RAIN") webMcpPayload.severity = "HEAVY";
    const result = await api<{ event_id: string }>("/disruptions", { method: "POST", body: JSON.stringify({ scenario_id: "demo", event_type: eventType, effective_minute: minute, payload: webMcpPayload }) });
    return { status: "injected", event_id: result.event_id, effective_minute: minute };
  });
  useEffect(() => { if (!playing) return; const timer = window.setInterval(() => setMinute((value) => Math.min(1080, value + speed)), 1000); return () => window.clearInterval(timer); }, [playing, speed]);
  useEffect(() => {
    void api("/scenario/demo", {
      method: "PATCH",
      body: JSON.stringify({ current_minute: minute, playing, speed }),
    })
      .then(() => api<MapState>("/map/state?scenario_id=demo"))
      .then(setData)
      .catch(() => undefined);
  }, [minute, playing, speed]);
  async function inject(type: string) {
    // ROAD_CLOSURE needs a real location to affect planning: it defaults to
    // the map's current center. HEAVY_RAIN is fleet-wide and needs none.
    const payload: Record<string, unknown> = { source: "dispatcher-demo" };
    if (type === "ROAD_CLOSURE") { payload.lat = depot.lat; payload.lon = depot.lon; payload.radius_km = 1.5; }
    if (type === "HEAVY_RAIN") payload.severity = "HEAVY";
    await api("/disruptions", { method: "POST", body: JSON.stringify({ scenario_id: "demo", event_type: type, effective_minute: minute, payload }) }).catch(() => undefined);
    await api<MapState>("/map/state?scenario_id=demo").then(setData).catch(() => undefined);
  }
  async function resetScenario() {
    await api("/scenario/demo/reset", { method: "POST" }).catch(() => undefined);
    setMinute(480);
    setPlaying(false);
  }
  async function createBranch() {
    const result = await api<{ scenario_id: string }>("/scenario/demo/branch", {
      method: "POST",
      body: JSON.stringify({ at_minute: minute }),
    }).catch(() => ({ scenario_id: `offline-branch-${branches.length}` }));
    setBranches((current) => [...current, result.scenario_id]);
  }
  const time = `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;
  return <AppShell><div className="scenario-layout"><section className="panel scenario-map"><div className="panel-heading"><div><span className="eyebrow">DETERMINISTIC REPLAY</span><h1>Scenario laboratory</h1></div><span className="scenario-time">{time} SGT</span></div><DispatchMap data={data} /></section><aside className="panel event-toolbox"><span className="eyebrow">INJECT EVENT</span><h2>Controlled disruptions</h2><p>Events branch the timeline without touching live operations.</p>{events.map(({ type, label, icon: Icon }) => <button key={type} onClick={() => inject(type)}><Icon size={17} /><span>{label}</span><b>+</b></button>)}</aside>
    <section className="panel timeline-panel"><div className="timeline-controls"><button className="round-button" onClick={() => setPlaying(!playing)}>{playing ? <Pause size={17} /> : <Play size={17} />}</button>{([1, 5, 20] as const).map((value) => <button key={value} onClick={() => setSpeed(value)} className={speed === value ? "speed active" : "speed"}>{value}×</button>)}<button className="utility-button" onClick={resetScenario}><RotateCcw size={15} />Reset</button><button className="utility-button" onClick={createBranch}><GitBranch size={15} />Branch</button></div><input aria-label="Scenario time" type="range" min="480" max="1080" value={minute} onChange={(event) => setMinute(Number(event.target.value))} /><div className="time-labels"><span>08:00</span><span>12:00</span><span>15:00</span><span>18:00</span></div></section>
    <section className="panel branch-panel"><div className="panel-heading"><div><span className="eyebrow">SCENARIO TREE</span><h2>{branches.length} deterministic branches</h2></div></div>{branches.map((branch, index) => <div className="branch-row" key={branch}><span>{index === 0 ? "LIVE BASELINE" : `BRANCH ${String(index).padStart(2, "0")}`}</span><strong>{branch}</strong><em>{index === 0 ? "source" : `from ${time}`}</em></div>)}</section></div></AppShell>;
}
