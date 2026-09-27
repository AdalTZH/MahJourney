"use client";

import { AlertTriangle, Bot, CheckCircle2, ChevronDown, Clock3, Copy, Link2, Link2Off, MapPin, Package, RefreshCw, Search, SendHorizontal, Sparkles, Truck, UserCheck, UserPlus, UserX } from "lucide-react";
import { type SyntheticEvent, useCallback, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { AppShell } from "@/components/app-shell";
import { DispatchMap } from "@/components/dispatch-map";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";
import { activatePlan, api, type DraftPlan, emptyMapState, enrollmentDeepLink, fetchDraftPlans, fetchDrivers, fetchReady, fetchTelegramDrivers, issueEnrollmentToken, type LinkedDriver, type MapState, reactivateDriver, rejectPlan, sendPlanToDrivers, suspendDriver, unlinkDriver } from "@/lib/api";
import { useChatContext } from "@/lib/chat-context";
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
type TabId = "overview" | "plan" | "drafts" | "enroll";

const VALID_TABS: TabId[] = ["overview", "plan", "drafts", "enroll"];


export default function DispatcherPage() {
  const [data, setData] = useState<MapState>(emptyMapState());
  const [source, setSource] = useState<"LIVE API" | "OFFLINE">("OFFLINE");
  const [signals, setSignals] = useState<Signal[]>([]);
  const router = useRouter();
  const searchParams = useSearchParams();

  // Chat state lives in ChatContext (global, survives page navigation).
  const { messages: chatMessages, busy: chatBusy, setOpen: setChatOpen, submit } = useChatContext();

  // Tab can be set by the agent via ?tab= in the URL (e.g. the agent navigates
  // the dispatcher to /dispatcher?tab=drafts after generating a plan).
  const paramTab = searchParams.get("tab") as TabId | null;
  const [tab, setTab] = useState<TabId>(
    paramTab && VALID_TABS.includes(paramTab) ? paramTab : "overview",
  );

  // Keep the tab in sync if the URL changes while the page is mounted (e.g.
  // the agent fires a second directive mid-session).
  useEffect(() => {
    const t = searchParams.get("tab") as TabId | null;
    if (!t || !VALID_TABS.includes(t)) return;
    const timer = window.setTimeout(() => {
      setTab(t);
      // Strip the query param so the URL stays clean after the tab activates.
      router.replace("/dispatcher", { scroll: false });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [router, searchParams]);
  const [expandedVehicles, setExpandedVehicles] = useState<Set<string>>(new Set());
  const [selectedVehicleId, setSelectedVehicleId] = useState<string | null>(null);
  const [refreshingMap, setRefreshingMap] = useState(false);
  const [draftPlans, setDraftPlans] = useState<DraftPlan[]>([]);
  const [draftsLoading, setDraftsLoading] = useState(false);
  const [draftActionId, setDraftActionId] = useState<string | null>(null);
  const [draftStatus, setDraftStatus] = useState<string | null>(null);
  // Which draft routes are expanded to show their stop breakdown. Keyed by
  // `${plan_id}:${version}:${vehicle_id}` so the same vehicle appearing across
  // different drafts (or in the Plan Detail tab) expands independently.
  const [expandedDraftRoutes, setExpandedDraftRoutes] = useState<Set<string>>(new Set());
  const toggleDraftRoute = (key: string) => setExpandedDraftRoutes((current) => { const next = new Set(current); if (next.has(key)) next.delete(key); else next.add(key); return next; });
  const [stacked, setStacked] = useState(false);
  useEffect(() => {
    const mql = window.matchMedia("(max-width: 980px)");
    const onChange = () => setStacked(mql.matches);
    onChange();
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);
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
  // --- Telegram driver enrollment tab state ---------------------------------
  const [drivers, setDrivers] = useState<string[]>([]);
  const [botUsername, setBotUsername] = useState("");
  const [enrollDriverId, setEnrollDriverId] = useState("");
  const [enrolling, setEnrolling] = useState(false);
  const [enrollError, setEnrollError] = useState<string | null>(null);
  const [issued, setIssued] = useState<{ driverId: string; token: string; expiresInSeconds: number } | null>(null);
  const [copied, setCopied] = useState(false);
  const [suspendingId, setSuspendingId] = useState<string | null>(null);
  const [reactivatingId, setReactivatingId] = useState<string | null>(null);
  const [unlinkingId, setUnlinkingId] = useState<string | null>(null);
  const [enrollNotice, setEnrollNotice] = useState<string | null>(null);
  const [linkedDrivers, setLinkedDrivers] = useState<LinkedDriver[]>([]);
  const [refreshingRoster, setRefreshingRoster] = useState(false);
  // Reload the set of Telegram-linked drivers so the roster reflects who's
  // currently bound (and whether they're suspended). Called on tab open and
  // after any enroll/suspend/unlink action.
  const reloadLinkedDrivers = useCallback(() => {
    void fetchTelegramDrivers().then(setLinkedDrivers).catch(() => setLinkedDrivers([]));
  }, []);
  // Manual roster refresh: re-pulls both the fleet driver list and their live
  // link status so the table reflects enrollments made outside this session
  // (e.g. a driver who just opened their link) without a full page reload.
  const refreshRoster = useCallback(async () => {
    setRefreshingRoster(true);
    try {
      const [fleet, linked] = await Promise.all([
        fetchDrivers().catch(() => null),
        fetchTelegramDrivers().catch(() => null),
      ]);
      if (fleet) setDrivers(Array.from(new Set(fleet.map((row) => row.driver_id))).sort());
      if (linked) setLinkedDrivers(linked);
    } finally {
      setRefreshingRoster(false);
    }
  }, []);
  // Look up a driver's link status by id for the roster badges.
  const linkByDriver = useMemo(() => {
    const map = new Map<string, LinkedDriver>();
    for (const record of linkedDrivers) map.set(record.driver_id, record);
    return map;
  }, [linkedDrivers]);
  // Load the fleet's driver ids and the bot username once, when the enroll tab
  // is first opened, so the picker is populated and deep links can be built.
  useEffect(() => {
    if (tab !== "enroll" || drivers.length > 0) return;
    void fetchDrivers()
      .then((rows) => setDrivers(Array.from(new Set(rows.map((row) => row.driver_id))).sort()))
      .catch(() => setDrivers([]));
  }, [tab, drivers.length]);
  useEffect(() => {
    if (tab !== "enroll" || botUsername) return;
    void fetchReady().then((ready) => setBotUsername(ready.telegram_bot_username ?? "")).catch(() => setBotUsername(""));
  }, [tab, botUsername]);
  // Refresh the linked-driver roster whenever the enroll tab is shown.
  useEffect(() => {
    if (tab !== "enroll") return;
    reloadLinkedDrivers();
  }, [tab, reloadLinkedDrivers]);
  const deepLink = issued ? enrollmentDeepLink(botUsername, issued.token) : null;
  const startCommand = issued ? `/start enroll_${issued.token}` : "";
  async function issueEnrollment(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    const driverId = enrollDriverId.trim();
    if (!driverId || enrolling) return;
    setEnrolling(true);
    setEnrollError(null);
    setIssued(null);
    setCopied(false);
    setEnrollNotice(null);
    try {
      const result = await issueEnrollmentToken(driverId);
      setIssued({ driverId, token: result.token, expiresInSeconds: result.expires_in_seconds });
      reloadLinkedDrivers();
    } catch (error) {
      setEnrollError(error instanceof Error ? error.message : "Failed to issue enrollment token.");
    } finally {
      setEnrolling(false);
    }
  }
  async function copyEnrollLink() {
    const text = deepLink ?? startCommand;
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }
  async function suspendEnrolledDriver(driverId: string) {
    if (suspendingId) return;
    setSuspendingId(driverId);
    setEnrollNotice(null);
    setEnrollError(null);
    try {
      await suspendDriver(driverId);
      setEnrollNotice(`Suspended ${driverId}. They will stop receiving route messages.`);
      if (issued?.driverId === driverId) setIssued(null);
      reloadLinkedDrivers();
    } catch (error) {
      setEnrollError(error instanceof Error ? error.message : "Failed to suspend driver.");
    } finally {
      setSuspendingId(null);
    }
  }
  async function reactivateEnrolledDriver(driverId: string) {
    if (reactivatingId) return;
    setReactivatingId(driverId);
    setEnrollNotice(null);
    setEnrollError(null);
    try {
      await reactivateDriver(driverId);
      setEnrollNotice(`Reactivated ${driverId}. They will receive route messages again.`);
      reloadLinkedDrivers();
    } catch (error) {
      setEnrollError(error instanceof Error ? error.message : "Failed to reactivate driver.");
    } finally {
      setReactivatingId(null);
    }
  }
  async function unlinkEnrolledDriver(driverId: string) {
    if (unlinkingId) return;
    setUnlinkingId(driverId);
    setEnrollNotice(null);
    setEnrollError(null);
    try {
      await unlinkDriver(driverId);
      setEnrollNotice(`Unlinked ${driverId}. Their Telegram account is no longer bound — issue a new link to re-enroll them.`);
      if (issued?.driverId === driverId) setIssued(null);
      reloadLinkedDrivers();
    } catch (error) {
      setEnrollError(error instanceof Error ? error.message : "Failed to unlink driver.");
    } finally {
      setUnlinkingId(null);
    }
  }
  useWebMcpTool(askDispatcherTool, async (input) => {
    const message = (input as { message?: unknown }).message;
    if (typeof message !== "string" || !message.trim()) throw new Error("message is required");
    await submit(message.trim());
    const lastAgentMsg = chatMessages.filter((m) => m.role === "agent").at(-1);
    return { status: "completed", reply: lastAgentMsg?.text ?? "" };
  });
  // Pull the current live plan/fleet from the backend. The map is otherwise
  // fetched only once on mount (no polling/websocket), so after a new plan is
  // generated elsewhere — e.g. the Orders tab — this is how the dispatcher
  // pulls it in without a full page reload. Used both on mount and by the
  // manual refresh button on the map header.
  const refreshMap = useCallback(async () => {
    setRefreshingMap(true);
    try {
      const value = await api<MapState>("/map/state");
      setData(value);
      setSource("LIVE API");
    } catch {
      setSource("OFFLINE");
    } finally {
      setRefreshingMap(false);
    }
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => void refreshMap(), 0);
    return () => window.clearTimeout(timer);
  }, [refreshMap]);
  // Load the pending candidate plans for the Draft Plans tab. Kept separate
  // from the map/Plan Detail data, which show only the ACTIVE plan.
  const refreshDrafts = useCallback(async () => {
    setDraftsLoading(true);
    try {
      setDraftPlans(await fetchDraftPlans());
    } catch {
      setDraftPlans([]);
    } finally {
      setDraftsLoading(false);
    }
  }, []);
  // Pull drafts on mount and whenever the Draft Plans tab is opened, so a
  // candidate generated on the Orders tab shows up without a page reload.
  useEffect(() => {
    const timer = window.setTimeout(() => void refreshDrafts(), 0);
    return () => window.clearTimeout(timer);
  }, [refreshDrafts]);
  useEffect(() => {
    if (tab !== "drafts") return;
    const timer = window.setTimeout(() => void refreshDrafts(), 0);
    return () => window.clearTimeout(timer);
  }, [tab, refreshDrafts]);
  // Activate a draft: it becomes the live plan WITHOUT dispatching to drivers
  // (the dispatcher sends explicitly from the Plan Detail tab), then we refresh
  // both the map (new active plan) and the drafts list (this
  // one leaves it).
  const activateDraft = async (plan: DraftPlan) => {
    if (draftActionId) return;
    setDraftActionId(plan.plan_id);
    setDraftStatus(null);
    try {
      await activatePlan(plan.plan_id, plan.version, false);
      setDraftStatus(`Plan ${plan.plan_id.slice(0, 8)} activated. Send it to drivers from the Plan Detail tab.`);
      await Promise.all([refreshDrafts(), refreshMap()]);
    } catch (error) {
      setDraftStatus(error instanceof Error ? error.message : "Failed to activate the draft.");
    } finally {
      setDraftActionId(null);
    }
  };
  // Reject a draft: it is marked superseded and drops out of the list. The
  // active plan is untouched.
  const rejectDraft = async (plan: DraftPlan) => {
    if (draftActionId) return;
    setDraftActionId(plan.plan_id);
    setDraftStatus(null);
    try {
      await rejectPlan(plan.plan_id, plan.version);
      setDraftStatus(`Plan ${plan.plan_id.slice(0, 8)} rejected.`);
      await refreshDrafts();
    } catch (error) {
      setDraftStatus(error instanceof Error ? error.message : "Failed to reject the draft.");
    } finally {
      setDraftActionId(null);
    }
  };
  useEffect(() => {
    const refresh = () => api<Signal[]>("/operations/integrations").then((items) => setSignals(items.filter((item) => item.age_seconds !== undefined))).catch(() => setSignals([]));
    void refresh(); const timer = window.setInterval(refresh, 30_000); return () => window.clearInterval(timer);
  }, []);
  const vehicleCount = data.plan.routes.length;
  const stopCount = data.plan.routes.reduce((total, route) => total + route.stops.length, 0);
  const violationCount = data.plan.hard_violations?.length ?? 0;
  // The backend returns status "NO_ACTIVE_PLAN" with no routes when nothing has
  // been activated yet (e.g. a fresh start). The map shows the ACTIVE plan
  // only, so an unactivated candidate never appears here.
  const noActivePlan = source === "LIVE API" && data.plan.status === "NO_ACTIVE_PLAN";
  const activeRoutes = data.plan.routes.filter((route) => route.stops.length > 0);
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
    <button role="tab" aria-selected={tab === "drafts"} className={tab === "drafts" ? "tab-button active" : "tab-button"} onClick={() => setTab("drafts")}>Draft Plans{draftPlans.length > 0 ? ` (${draftPlans.length})` : ""}</button>
    <button role="tab" aria-selected={tab === "enroll"} className={tab === "enroll" ? "tab-button active" : "tab-button"} onClick={() => setTab("enroll")}>Enroll Drivers</button>
  </div>
  {tab === "overview" ? <div className="dispatch-overview">
    <ResizablePanelGroup orientation={stacked ? "vertical" : "horizontal"} className="overview-split">
      <ResizablePanel defaultSize="70%" minSize="55%" maxSize="80%" className="overview-left">
        <section className={selectedRoute ? "map-panel panel has-inspector" : "map-panel panel"}><div className="panel-heading map-heading"><div><span className="eyebrow">FLEET TOPOLOGY</span><h1>Live fleet plan{noActivePlan && <span className="muted-label" style={{ marginLeft: 10, fontSize: 12 }}>— no active plan yet</span>}</h1></div><div style={{ display: "flex", alignItems: "center", gap: 10 }}><button type="button" className="icon-button" onClick={() => void refreshMap()} disabled={refreshingMap} aria-label="Refresh fleet plan" title="Reload the current plan from the server"><RefreshCw size={15} className={refreshingMap ? "spin" : undefined} /></button><span className="mode-chip"><span />{source}</span></div></div><DispatchMap data={data} selectedVehicleId={selectedVehicleId} onSelectVehicle={setSelectedVehicleId} /><div className="map-metrics"><div><Truck size={15} /><span><strong>{vehicleCount}</strong> VEHICLES</span></div><div><Clock3 size={15} /><span><strong>{stopCount}</strong> STOPS</span></div><div><CheckCircle2 size={15} /><span><strong>{violationCount}</strong> HARD VIOLATIONS</span></div></div></section>
        {selectedRoute && <section className="panel vehicle-inspector">
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
        </section>}
      </ResizablePanel>
      <ResizableHandle withHandle className="overview-handle" />
      <ResizablePanel defaultSize="30%" minSize="20%" className="overview-right">
        <aside className="master-console">
          <section className="panel plan-card"><div className="panel-heading"><div><span className="eyebrow">ACTIVE CANDIDATE</span><h2>Plan v{data.plan.version}</h2></div><span className="verified-badge">{data.plan.status}</span></div><div className="cost-number">{data.plan.objective_cost.toFixed(1)} <small>km</small></div><p>Estimated fleet distance</p><div className="plan-card-metrics"><div className="metric-row"><span>Assigned stops</span><strong>{stopCount}</strong></div><div className="metric-row"><span>Workload spread</span><strong>± {formatDuration(workloadSpread)}</strong></div></div>
          </section>
          <section className="panel alert-card"><div className="panel-heading"><div><span className="eyebrow">LIVE SIGNALS</span><h2>Operations feed</h2></div><span className="count-badge">{String(signals.length).padStart(2, "0")}</span></div><div className="alert-feed">{signals.length === 0 ? <div className="feed-item"><Sparkles size={17} /><div><strong>No live snapshots yet</strong><span>Collector warming up or credentials pending</span></div></div> : signals.map((signal) => <div className={`feed-item ${signalTone(signal.status)}`} key={signal.integration}>{signal.status === "FRESH" ? <CheckCircle2 size={17} /> : <AlertTriangle size={17} />}<div><strong>{signal.integration} · {signal.status}</strong><span>{(signal.record_count ?? 0).toLocaleString()} records</span></div><time>{signalAge(signal.age_seconds)}</time></div>)}</div></section>
          <section className="panel agent-console">
            <div className="agent-header">
              <div className="bot-mark"><Bot size={18} /></div>
              <div><span className="eyebrow">MASTER DISPATCHER</span><h2>Command console</h2></div>
              <span className="online-label" style={{ marginLeft: "auto" }}>{chatBusy ? "THINKING…" : "ONLINE"}</span>
            </div>
            <div style={{ padding: "18px 16px", display: "flex", flexDirection: "column", gap: 10, flex: 1 }}>
              <p style={{ margin: 0, fontSize: 11, color: "#7fa8ad", lineHeight: 1.6 }}>
                The master agent is available on every page. Use the floating{" "}
                <strong style={{ color: "#3bd4c5" }}>AGENT</strong> button in the
                bottom-right corner — your conversation history is always preserved.
              </p>
              <button
                type="button"
                className="primary-action"
                style={{ alignSelf: "flex-start", display: "inline-flex", alignItems: "center", gap: 6 }}
                onClick={() => setChatOpen(true)}
              >
                <Bot size={14} /> Open chat
              </button>
              {chatMessages.length > 1 && (
                <p style={{ margin: 0, fontSize: 10, color: "#4f8b8f", fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace" }}>
                  {chatMessages.length - 1} message{chatMessages.length !== 2 ? "s" : ""} in history
                </p>
              )}
            </div>
          </section>
        </aside>
      </ResizablePanel>
    </ResizablePanelGroup>
  </div> : tab === "plan" ? <div className="plan-detail-page">
    <section className="panel plan-detail">
      <div className="panel-heading plan-detail-heading">
        <div><span className="eyebrow">PLAN DETAIL — ACTIVE PLAN</span><h1>{noActivePlan ? "No active plan" : <>Plan v{data.plan.version} <span className="plan-detail-id">{data.plan.plan_id.slice(0, 8)}</span></>}</h1></div>
        <div className="plan-detail-heading-actions">
          {!noActivePlan && <span className={data.plan.status === "VALIDATED" ? "verified-badge" : "verified-badge warn"}>{data.plan.status}</span>}
          {/* Plan Detail shows the live plan. Activation happens on the Draft
              Plans tab (which no longer sends to drivers); sending route
              messages to drivers is done here, on the active plan. */}
          {data.plan.status === "ACTIVE" && <button className="primary-action" disabled={dispatching || violationCount > 0} onClick={sendToDrivers} title="Send route messages to enrolled drivers on Telegram">{dispatching ? "Sending…" : "Send to drivers"} <SendHorizontal size={15} /></button>}
        </div>
      </div>
      {noActivePlan && <p className="dispatch-status plan-detail-dispatch-status">No plan is active yet. Generate one on the Orders tab, then activate it under Draft Plans.</p>}
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
  </div> : tab === "drafts" ? <div className="plan-detail-page">
    <section className="panel plan-detail">
      <div className="panel-heading plan-detail-heading">
        <div><span className="eyebrow">DRAFT PLANS — PENDING REVIEW</span><h1>Draft plans <span className="muted-label">{draftPlans.length} pending</span></h1></div>
        <div className="plan-detail-heading-actions">
          <button type="button" className="icon-button" onClick={() => void refreshDrafts()} disabled={draftsLoading} aria-label="Refresh draft plans" title="Reload pending draft plans"><RefreshCw size={15} className={draftsLoading ? "spin" : undefined} /></button>
        </div>
      </div>
      {draftStatus && <p className="dispatch-status plan-detail-dispatch-status">{draftStatus}</p>}
      {draftsLoading && draftPlans.length === 0 ? <div className="plan-detail-empty">Loading draft plans…</div>
        : draftPlans.length === 0 ? <div className="plan-detail-empty">No draft plans pending. Generate one on the Orders tab; it will appear here for review before you activate it.</div>
        : <div className="plan-detail-list">
          {draftPlans.map((plan) => {
            const stops = plan.routes.reduce((total, route) => total + route.stops.length, 0);
            const violations = plan.hard_violations?.length ?? 0;
            const busyThis = draftActionId === plan.plan_id;
            return <div className="plan-detail-route" key={`${plan.plan_id}-${plan.version}`}>
              <div className="plan-detail-route-header" style={{ cursor: "default" }}>
                <span className="plan-detail-route-id"><strong>Plan v{plan.version}</strong><span className="muted-label">{plan.plan_id.slice(0, 8)}</span></span>
                <span className={violations > 0 ? "verified-badge warn" : "verified-badge"}>{plan.status}</span>
                <span className="plan-detail-route-metric">{plan.routes.length} vehicles</span>
                <span className="plan-detail-route-metric">{stops} stops</span>
                <span className="plan-detail-route-metric strong">{plan.objective_cost.toFixed(1)} km</span>
                {violations > 0 && <span className="plan-detail-route-metric" style={{ color: "#e0a34a" }}><AlertTriangle size={13} /> {violations}</span>}
              </div>
              {/* Approve/reject actions sit at the top of each draft card so the
                  dispatcher can act without scrolling past the full route and
                  stop breakdown below. */}
              <div className="plan-detail-stops" style={{ display: "flex", gap: 10, padding: "10px 16px", alignItems: "center" }}>
                <button type="button" className="primary-action" disabled={busyThis || violations > 0} onClick={() => void activateDraft(plan)} title={violations > 0 ? "This plan has hard violations and cannot be activated" : "Activate this plan as the live plan (send to drivers from the Plan Detail tab)"}>{busyThis ? "Working…" : "Activate plan"} <CheckCircle2 size={15} /></button>
                <button type="button" className="primary-action secondary-action" disabled={busyThis} onClick={() => void rejectDraft(plan)} title="Reject this draft — it will not go live">{busyThis ? "Working…" : "Reject"} <UserX size={15} /></button>
                {violations > 0 && <span className="muted-label">Cannot activate: {violations} hard violation(s).</span>}
              </div>
              {/* Per-vehicle breakdown for this draft, mirroring the Plan Detail
                  tab so the dispatcher can review routes and stops before
                  activating. Each vehicle row expands to its stop table. */}
              <div className="plan-detail-list" style={{ padding: "0 12px 4px" }}>
                {plan.routes.length === 0 && <div className="plan-detail-empty">This draft has no routes.</div>}
                {plan.routes.map((route, index) => {
                  const standby = route.stops.length === 0;
                  const routeKey = `${plan.plan_id}:${plan.version}:${route.vehicle_id}`;
                  const expanded = expandedDraftRoutes.has(routeKey);
                  const draftMaxStops = Math.max(1, ...plan.routes.map((r) => r.stops.length));
                  const loadPercent = Math.round((route.stops.length / draftMaxStops) * 100);
                  return <div className={`plan-detail-route${standby ? " is-standby" : ""}`} key={routeKey}>
                    <button className="plan-detail-route-header" onClick={() => toggleDraftRoute(routeKey)} aria-expanded={expanded}>
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
            </div>;
          })}
        </div>}
    </section>
  </div> : <div className="plan-detail-page enroll-page">
    <section className="panel plan-detail">
      <div className="panel-heading plan-detail-heading">
        <div><span className="eyebrow">DRIVER ENROLLMENT</span><h1>Enroll drivers on Telegram</h1></div>
        <span className="verified-badge">{botUsername ? `@${botUsername.replace(/^@/, "")}` : "BOT NOT CONFIGURED"}</span>
      </div>
      <div className="enroll-body">
        <p className="enroll-intro">Issue a one-time enrollment link for a driver, then share it with them. Opening the link in Telegram binds their account so they receive their route stops when a plan is dispatched. Links expire shortly after they are issued.</p>
        <form className="enroll-form" onSubmit={issueEnrollment}>
          <label className="enroll-field">
            <span>Driver ID</span>
            <input
              list="enroll-driver-options"
              value={enrollDriverId}
              onChange={(event) => setEnrollDriverId(event.target.value)}
              placeholder="e.g. DRV-01"
              aria-label="Driver ID to enroll"
            />
            <datalist id="enroll-driver-options">
              {drivers.map((driverId) => <option key={driverId} value={driverId} aria-label={driverId} />)}
            </datalist>
          </label>
          <button className="primary-action enroll-issue" disabled={enrolling || !enrollDriverId.trim()}>
            <UserPlus size={15} /> {enrolling ? "Issuing…" : "Issue enrollment link"}
          </button>
        </form>
        {enrollError && <p className="dispatch-status enroll-error">{enrollError}</p>}
        {enrollNotice && <p className="dispatch-status">{enrollNotice}</p>}
        {issued && <div className="enroll-result">
          <div className="enroll-result-head">
            <span className="eyebrow">ENROLLMENT LINK · {issued.driverId}</span>
            <span className="muted-label">Expires in {Math.round(issued.expiresInSeconds / 60)} min</span>
          </div>
          {deepLink ? <a className="enroll-link" href={deepLink} target="_blank" rel="noreferrer"><Link2 size={14} /> {deepLink}</a>
            : <div className="enroll-link"><Link2 size={14} /> {startCommand}</div>}
          {!deepLink && <p className="enroll-hint">Bot username is not configured, so share this command for the driver to send to the fleet bot.</p>}
          <button type="button" className="primary-action secondary-action enroll-copy" onClick={copyEnrollLink}>
            <Copy size={14} /> {copied ? "Copied" : deepLink ? "Copy link" : "Copy command"}
          </button>
        </div>}
        {drivers.length > 0 && <div className="enroll-roster">
          <div className="enroll-roster-head">
            <span className="eyebrow">FLEET DRIVERS</span>
            <span className="plan-detail-count">{linkByDriver.size} of {drivers.length} linked</span>
            <button type="button" className="sort-chip enroll-refresh" onClick={() => void refreshRoster()} disabled={refreshingRoster} aria-label="Refresh driver link status" title="Refresh driver link status">
              <RefreshCw size={13} className={refreshingRoster ? "spin" : undefined} /> {refreshingRoster ? "Refreshing…" : "Refresh"}
            </button>
          </div>
          <ul className="enroll-driver-list">
            {drivers.map((driverId) => {
              const link = linkByDriver.get(driverId);
              const linked = Boolean(link);
              const suspended = link?.suspended ?? false;
              return <li key={driverId} className="enroll-driver-row">
                <span className="enroll-driver-main">
                  <span className="enroll-driver-id">{driverId}</span>
                  {linked
                    ? <span className={suspended ? "enroll-status suspended" : "enroll-status linked"}>{suspended ? <UserX size={12} /> : <CheckCircle2 size={12} />}{suspended ? "SUSPENDED" : "LINKED"}{link ? <span className="enroll-tg-id">· TG {link.telegram_user_id}</span> : null}</span>
                    : <span className="enroll-status unlinked">NOT LINKED</span>}
                </span>
                <div className="enroll-driver-actions">
                  <button type="button" className="sort-chip" onClick={() => { setEnrollDriverId(driverId); }}>Select</button>
                  {linked && !suspended && <button type="button" className="sort-chip enroll-suspend" disabled={suspendingId === driverId} onClick={() => void suspendEnrolledDriver(driverId)}>
                    <UserX size={13} /> {suspendingId === driverId ? "Suspending…" : "Suspend"}
                  </button>}
                  {linked && suspended && <button type="button" className="sort-chip enroll-reactivate" disabled={reactivatingId === driverId} onClick={() => void reactivateEnrolledDriver(driverId)}>
                    <UserCheck size={13} /> {reactivatingId === driverId ? "Reactivating…" : "Reactivate"}
                  </button>}
                  {linked && <button type="button" className="sort-chip enroll-unlink" disabled={unlinkingId === driverId} onClick={() => void unlinkEnrolledDriver(driverId)}>
                    <Link2Off size={13} /> {unlinkingId === driverId ? "Unlinking…" : "Unlink"}
                  </button>}
                </div>
              </li>;
            })}
          </ul>
        </div>}
      </div>
    </section>
  </div>}
  </AppShell>;
}
