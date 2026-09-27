"use client";

import { CloudRain, GitBranch, Pause, Play, RotateCcw, Siren } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { AppShell } from "@/components/app-shell";
import { DispatchMap } from "@/components/dispatch-map";
import type { RerouteMeta } from "@/components/dispatch-map";

// Simulation day start (12:30 SGT). The clock and route timings are all
// expressed as minutes-from-midnight, so the sim window opens at 750.
const SIM_START_MINUTE = 750;
import {
  api,
  applyDisruption,
  depot,
  fetchRoadPath,
  emptyMapState,
  isRealPlan,
  previewDisruption,
  type ApplyDisruptionResult,
  type Coordinate,
  type MapState,
  type PolygonDisruptionRequest,
  type PreviewDisruptionResult,
  type Route,
  type SupportedDisruptionType,
} from "@/lib/api";
import { useWebMcpTool } from "@/hooks/use-webmcp";

const injectDisruptionTool = {
  name: "inject_scenario_event",
  title: "Inject scenario event",
  description: "Inject one supported disruption into scenario mode at the visible virtual time.",
  inputSchema: {
    type: "object",
    properties: { event_type: { type: "string", enum: ["ROAD_CLOSURE", "HEAVY_RAIN"] } },
    required: ["event_type"],
    additionalProperties: false,
  },
  annotations: { readOnlyHint: false, untrustedContentHint: false },
};

// Only ROAD_CLOSURE and HEAVY_RAIN are wired to real planning effects on the
// backend, so those are the only two offered in this flow. URGENT_ORDER and
// TRUCK_BREAKDOWN remain valid DisruptionType values elsewhere (e.g. approval
// evaluation), but aren't implemented here yet, so they're left out of the
// scenario toolbox entirely rather than shown disabled.
const SUPPORTED_TYPES: readonly SupportedDisruptionType[] = ["ROAD_CLOSURE", "HEAVY_RAIN"];

const events = [
  { type: "ROAD_CLOSURE" as const, label: "Road closure", icon: Siren },
  { type: "HEAVY_RAIN" as const, label: "Heavy rain", icon: CloudRain },
];

// A small square (~1km) around a point, used as the default zone for the
// WebMCP tool (which has no map to draw on) and as a quick-start size hint
// for the drawing UI.
function squareZoneAround(center: Coordinate, halfSideDegrees = 0.005): Coordinate[] {
  return [
    { lat: center.lat - halfSideDegrees, lon: center.lon - halfSideDegrees },
    { lat: center.lat - halfSideDegrees, lon: center.lon + halfSideDegrees },
    { lat: center.lat + halfSideDegrees, lon: center.lon + halfSideDegrees },
    { lat: center.lat + halfSideDegrees, lon: center.lon - halfSideDegrees },
  ];
}

// The disruption-handling state machine for the map/banner. This is deliberately
// separate from API connectivity — this tracks the lifecycle of one drawn disruption zone:
// draw the shape -> preview what it would affect -> apply it for real.
type InjectionPhase =
  | "IDLE"
  | "DRAWING"
  | "SELECTING_ROAD"
  | "PREVIEWED"
  | "APPLYING"
  | "APPLIED"
  | "FAILED";

type InjectionState = {
  phase: InjectionPhase;
  disruptionType: SupportedDisruptionType | null;
  // The polygon zone (for HEAVY_RAIN, or a polygon-drawn ROAD_CLOSURE) OR the
  // selected road polyline (for a road-selected ROAD_CLOSURE). Exactly one is
  // set per injection; the payload sent to the backend uses whichever is present.
  polygon: Coordinate[] | null;
  roadPath: Coordinate[] | null;
  preview: PreviewDisruptionResult | null;
  applied: ApplyDisruptionResult | null;
  // The mid-route reroute candidate proposed by a road closure: its full routes
  // (with geometry) delivered INLINE in the apply response, plus which vehicles
  // it actually rerouted. Rendered as the dotted suggestion on the scenario map.
  // This is SCENARIO-ONLY — it never becomes an active plan and never appears in
  // the dispatcher's Draft Plans. Null when a closure produced no reroute.
  rerouteCandidate: { routes: Route[] } | null;
  rerouteVehicleIds: string[];
  error: string | null;
};

const IDLE_INJECTION: InjectionState = {
  phase: "IDLE",
  disruptionType: null,
  polygon: null,
  roadPath: null,
  preview: null,
  applied: null,
  rerouteCandidate: null,
  rerouteVehicleIds: [],
  error: null,
};

// Buffer half-width (metres) applied to a selected road to build the closure
// corridor. Matches the backend default; sent explicitly so the two agree.
const ROAD_BUFFER_M = 40;

export default function ScenarioPage() {
  const [data, setData] = useState<MapState>(emptyMapState());
  // Scenario-only adopted reroute: vehicle_id -> rerouted Route. Populated when
  // the user approves a reroute suggestion, and merged over the live plan's
  // routes for display on the scenario map ONLY. Held separately from `data`
  // (which is re-fetched from /map/state on every clock tick) so an approved
  // reroute survives those refreshes without ever being written to the backend.
  const [approvedRoutes, setApprovedRoutes] = useState<Record<string, Route>>({});
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<1 | 5 | 20>(1);
  const [minute, setMinute] = useState(SIM_START_MINUTE);
  const [branches, setBranches] = useState(["demo"]);
  const [injection, setInjection] = useState<InjectionState>(IDLE_INJECTION);
  const [rainSeverity, setRainSeverity] = useState<"MODERATE" | "HEAVY">("HEAVY");
  // Guards against overlapping injections.
  const injecting = useRef(false);
  // Number of vertices placed so far while drawing, and a counter bumped to
  // tell DispatchMap to close the shape (see the Finish button below).
  const [drawPointCount, setDrawPointCount] = useState(0);
  const [finishSignal, setFinishSignal] = useState(0);
  // For ROAD_CLOSURE, the user chooses how to describe the zone: draw a polygon
  // or select a road (two clicks). HEAVY_RAIN is always polygon-drawn.
  const [roadClosureMode, setRoadClosureMode] = useState<"DRAW_ZONE" | "SELECT_ROAD">("SELECT_ROAD");
  // Endpoints placed so far while selecting a road, and a counter bumped to tell
  // DispatchMap to confirm the selection (mirrors the polygon finishSignal).
  const [roadPointCount, setRoadPointCount] = useState(0);
  const [roadConfirmSignal, setRoadConfirmSignal] = useState(0);
  // Vehicle whose stops are shown on the map (click a truck to select). Only
  // active when not drawing/selecting a road — the map suppresses truck clicks
  // during capture.
  const [selectedVehicleId, setSelectedVehicleId] = useState<string | null>(null);

  const realPlan = isRealPlan(data);

  useWebMcpTool(injectDisruptionTool, async (input) => {
    const eventType = (input as { event_type?: unknown }).event_type;
    if (typeof eventType !== "string" || !events.some((event) => event.type === eventType)) throw new Error("unsupported event_type");
    if (!SUPPORTED_TYPES.includes(eventType as SupportedDisruptionType)) throw new Error(`${eventType} is not supported for injection`);
    // The WebMCP tool has no map to draw on, so it uses a small default zone
    // around the depot — the same polygon-based contract as the drawing UI.
    const result = await applyDisruption({
      disruption_type: eventType as SupportedDisruptionType,
      polygon: squareZoneAround(depot),
      severity: eventType === "HEAVY_RAIN" ? "HEAVY" : undefined,
      effective_minute: minute,
    });
    return { status: "applied", event_id: result.event.event_id, effective_minute: minute, affected_vehicle_ids: result.affected.vehicle_ids };
  });

  useEffect(() => {
    if (!playing) return;
    const timer = window.setInterval(() => setMinute((value) => Math.min(1080, value + speed)), 1000);
    return () => window.clearInterval(timer);
  }, [playing, speed]);

  useEffect(() => {
    // Keep syncing the clock and truck positions regardless of the injection
    // phase — the affected-vehicle highlight and reroute overlay are separate
    // props derived from `injection`, not from `data`, so there's no reason to
    // freeze the trucks while drawing/previewing/applying a disruption.
    void api("/scenario/demo", {
      method: "PATCH",
      body: JSON.stringify({ current_minute: minute, playing, speed }),
    })
      .then(() => api<MapState>("/map/state?scenario_id=demo"))
      .then(setData)
      .catch(() => undefined);
  }, [minute, playing, speed]);

  // Step 1: enter capture mode for the chosen disruption type. For ROAD_CLOSURE
  // in "select road" mode this is the two-click road picker; otherwise it's the
  // polygon draw mode. The map takes over click handling until the user
  // confirms (or cancels).
  function startCapture(disruptionType: SupportedDisruptionType) {
    if (!realPlan || injecting.current) return;
    // Clear any vehicle selection so its stop pins / route dimming don't linger
    // over the drawing or road-selection interaction.
    setSelectedVehicleId(null);
    if (disruptionType === "ROAD_CLOSURE" && roadClosureMode === "SELECT_ROAD") {
      setRoadPointCount(0);
      setInjection({ ...IDLE_INJECTION, phase: "SELECTING_ROAD", disruptionType });
      return;
    }
    setDrawPointCount(0);
    setInjection({ ...IDLE_INJECTION, phase: "DRAWING", disruptionType });
  }

  function cancelCapture() {
    setInjection(IDLE_INJECTION);
    setDrawPointCount(0);
    setRoadPointCount(0);
  }

  function finishDrawing() {
    if (drawPointCount < 3) return;
    setFinishSignal((value) => value + 1);
  }

  function confirmRoadSelection() {
    if (roadPointCount < 2) return;
    setRoadConfirmSignal((value) => value + 1);
  }

  // The map reports the two chosen road endpoints here. Fetch the real road
  // polyline between them (falling back to a straight segment if OneMap can't
  // route), keep it on the injection as roadPath, then preview it.
  async function handleRoadSelectComplete(start: Coordinate, end: Coordinate) {
    if (injection.disruptionType !== "ROAD_CLOSURE" || injecting.current) return;
    injecting.current = true;
    setRoadPointCount(0);
    try {
      const roadPath = await fetchRoadPath(start, end)
        .then((result) => (result.road_path.length >= 2 ? result.road_path : [start, end]))
        .catch(() => [start, end]);
      setInjection((prev) => ({ ...prev, phase: "PREVIEWED", roadPath, error: null }));
      await runPreview({ roadPath });
    } finally {
      injecting.current = false;
    }
  }

  // Build the backend request body for the current injection's shape: a drawn
  // polygon, or a selected road (road_path + buffer). severity is only attached
  // for heavy rain.
  function buildDisruptionRequest(
    disruptionType: SupportedDisruptionType,
    shape: { polygon?: Coordinate[]; roadPath?: Coordinate[] },
  ): PolygonDisruptionRequest {
    const base = {
      disruption_type: disruptionType,
      severity: disruptionType === "HEAVY_RAIN" ? rainSeverity : undefined,
      effective_minute: minute,
    };
    if (shape.roadPath) {
      return { ...base, road_path: shape.roadPath, buffer_m: ROAD_BUFFER_M };
    }
    return { ...base, polygon: shape.polygon };
  }

  // Step 2 (shared): preview the current shape (no side effects) so the user
  // sees what it would affect before committing. Does NOT manage
  // `injecting.current` — callers own that guard.
  async function runPreview(shape: { polygon?: Coordinate[]; roadPath?: Coordinate[] }) {
    const disruptionType = injection.disruptionType;
    if (!disruptionType) return;
    const preview = await previewDisruption(
      buildDisruptionRequest(disruptionType, shape),
    ).catch(() => null);
    if (!preview) {
      // Preview failed — do NOT wipe the injection, or the shape the user just
      // placed would vanish from the map. Keep it visible and surface the
      // failure so they can retry or discard it deliberately.
      setInjection((prev) => ({
        ...prev,
        phase: "FAILED",
        preview: null,
        error: "Could not preview this zone. It's still on the map — retry or discard.",
      }));
      return;
    }
    setInjection((prev) => ({ ...prev, phase: "PREVIEWED", preview, error: null }));
  }

  // The map reports the finished polygon here. Immediately preview it.
  async function handlePolygonComplete(polygon: Coordinate[]) {
    const disruptionType = injection.disruptionType;
    if (!disruptionType || injecting.current) return;
    injecting.current = true;
    setDrawPointCount(0);
    // Keep the drawn polygon on the injection state from the start so the zone
    // renders (via confirmedZone) as soon as the shape is finished, and stays
    // put even if the preview call below fails.
    setInjection((prev) => ({ ...prev, phase: "PREVIEWED", polygon, error: null }));
    try {
      await runPreview({ polygon });
    } finally {
      injecting.current = false;
    }
  }

  // Re-run the preview for the shape that's already on the map after a failure,
  // without making the user redraw/reselect it.
  async function retryPreview() {
    const { disruptionType, polygon, roadPath } = injection;
    if (!disruptionType || injecting.current) return;
    if (!polygon && !roadPath) return;
    injecting.current = true;
    try {
      await runPreview(roadPath ? { roadPath } : { polygon: polygon ?? undefined });
    } finally {
      injecting.current = false;
    }
  }

  // Step 3: commit the previewed zone — inject it for real and re-time the
  // live plan, then show the resulting reroute.
  async function applyPreviewedDisruption() {
    const { disruptionType, polygon, roadPath } = injection;
    if (!disruptionType || (!polygon && !roadPath) || injecting.current) return;
    injecting.current = true;
    setInjection((prev) => ({ ...prev, phase: "APPLYING", error: null }));
    try {
      const applied = await applyDisruption(
        buildDisruptionRequest(disruptionType, roadPath ? { roadPath } : { polygon: polygon ?? undefined }),
      ).catch(() => null);
      if (!applied) {
        // Apply failed — fall back to the previewed state (the zone stays on the
        // map) rather than losing the user's work.
        setInjection((prev) => ({
          ...prev,
          phase: "PREVIEWED",
          error: "Could not apply this zone. It's still on the map — retry or discard.",
        }));
        return;
      }
      setBranches((current) => (current.includes(applied.event.event_id) ? current : [...current, applied.event.event_id]));
      // A road closure may propose a mid-route reroute. Its full routes+geometry
      // arrive INLINE in the apply response (the candidate is scenario-only and
      // is never stored, so there is no drafts entry to fetch). Record which
      // vehicles it rerouted (for the banner and the committed-prefix note).
      // Absent for non-closures / re-time-only.
      let rerouteCandidate: { routes: Route[] } | null = null;
      let rerouteVehicleIds: string[] = [];
      if (applied.reroute_candidate) {
        const { rerouted_vehicle_ids, routes } = applied.reroute_candidate;
        rerouteCandidate = { routes };
        // Use the vehicles that actually got a re-sequenced route, not the
        // broader affected set (which includes vehicles that merely cross the
        // zone but fell back to re-time-in-place).
        rerouteVehicleIds = rerouted_vehicle_ids;
      }
      setInjection((prev) => ({
        ...prev,
        phase: "APPLIED",
        applied,
        rerouteCandidate,
        rerouteVehicleIds,
        error: null,
      }));
      // The live plan just changed server-side; refresh the map immediately
      // rather than waiting for the next clock-sync tick.
      await api<MapState>("/map/state?scenario_id=demo").then(setData).catch(() => undefined);
    } finally {
      injecting.current = false;
    }
  }

  // Approve the proposed mid-route reroute — SCENARIO-ONLY. This adopts the
  // suggested route ON THE SCENARIO MAP by merging the rerouted candidate
  // routes over the displayed plan (see `displayData`). It does NOT activate
  // anything on the backend: the reroute never becomes the fleet's live plan
  // and never appears in the dispatcher's drafts. The dotted suggestion is
  // replaced by the now-solid adopted route.
  function approveReroute() {
    const candidate = injection.rerouteCandidate;
    if (!candidate) return;
    const wanted = new Set(injection.rerouteVehicleIds);
    const adopted = candidate.routes.filter((route) => wanted.has(route.vehicle_id));
    setApprovedRoutes((current) => {
      const next = { ...current };
      for (const route of adopted) next[route.vehicle_id] = route;
      return next;
    });
    setInjection((prev) => ({ ...prev, rerouteCandidate: null, rerouteVehicleIds: [] }));
  }

  // Reject the proposed reroute: dismiss the dotted suggestion and keep the
  // original route. Nothing is persisted, so there is no draft to drop and the
  // active plan is untouched.
  function rejectReroute() {
    setInjection((prev) => ({ ...prev, rerouteCandidate: null, rerouteVehicleIds: [] }));
  }

  function clearInjection() {
    setInjection(IDLE_INJECTION);
  }

  async function resetScenario() {
    await api("/scenario/demo/reset", { method: "POST" }).catch(() => undefined);
    setMinute(SIM_START_MINUTE);
    setPlaying(false);
    // Drop any scenario-only adopted reroute so the map returns to baseline.
    setApprovedRoutes({});
    clearInjection();
  }

  async function createBranch() {
    const result = await api<{ scenario_id: string }>("/scenario/demo/branch", {
      method: "POST",
      body: JSON.stringify({ at_minute: minute }),
    }).catch(() => ({ scenario_id: `offline-branch-${branches.length}` }));
    setBranches((current) => [...current, result.scenario_id]);
  }

  const time = `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;

  // The plan actually drawn on the scenario map: the live plan from /map/state
  // with any APPROVED reroute routes merged over their vehicles. This keeps an
  // adopted reroute visible across the per-second /map/state refresh without
  // ever persisting it — the merge is display-only and scenario-local, so the
  // Overview's active plan and the dispatcher's drafts are unaffected.
  const displayData = useMemo<MapState>(() => {
    if (Object.keys(approvedRoutes).length === 0) return data;
    return {
      ...data,
      plan: {
        ...data.plan,
        routes: data.plan.routes.map((route) => approvedRoutes[route.vehicle_id] ?? route),
      },
    };
  }, [data, approvedRoutes]);

  // Map overlay inputs derived from the current injection.
  //
  // Orange highlight visibility rules:
  // - While a reroute candidate is pending (Approve/Reject banner showing):
  //   HIDE the highlight so the dotted suggestion is readable.
  // - On Reject: candidate is cleared but phase stays APPLIED, so the highlight
  //   comes back — the original route still crosses the closure.
  // - On Approve: the vehicle's route is swapped for the rerouted one (which
  //   avoids the closure), so we drop it from the affected set below — no
  //   lingering orange highlight on a route that no longer crosses the zone.
  const reroutePending = injection.rerouteCandidate !== null;
  const affectedVehicleIds = reroutePending
    ? []
    : (injection.preview?.affected.vehicle_ids ?? injection.applied?.affected.vehicle_ids ?? [])
        // A vehicle whose reroute was approved now shows its adopted route, so
        // it's no longer "affected" — exclude it from the highlight.
        .filter((id) => !(id in approvedRoutes));
  // The dotted suggestion line(s). Before Apply it's the preview's "what a full
  // replan could look like". After Apply, for a road closure that actually
  // produced a reroute candidate, it's the REAL proposed route per rerouted
  // vehicle (drawn until the dispatcher approves or rejects). Re-time-only
  // closures / non-closures have no candidate, so no dotted line after Apply.
  let rerouteGeometry: Record<string, Coordinate[]> | undefined;
  // Per-vehicle timing for trimming the dotted line to the road still ahead.
  // For an applied reroute candidate this comes from the candidate route's OWN
  // duration (and shift-start), so the trim is measured along the suggested
  // route itself — not the active plan's route for that vehicle, which is a
  // different length and, late in the sim day, would clamp the line away.
  let rerouteMeta: Record<string, RerouteMeta> | undefined;
  if (injection.preview && (injection.phase === "PREVIEWED" || injection.phase === "APPLYING")) {
    rerouteGeometry = Object.fromEntries(
      injection.preview.candidate_routes.map((route) => [route.vehicle_id, route.geometry]),
    );
  } else if (injection.rerouteCandidate) {
    const wanted = new Set(injection.rerouteVehicleIds);
    const rerouted = injection.rerouteCandidate.routes.filter(
      (route) => wanted.has(route.vehicle_id) && (route.geometry?.length ?? 0) >= 2,
    );
    rerouteGeometry = Object.fromEntries(
      rerouted.map((route): [string, Coordinate[]] => [route.vehicle_id, route.geometry ?? []]),
    );
    rerouteMeta = Object.fromEntries(
      rerouted.map((route): [string, RerouteMeta] => [
        route.vehicle_id,
        {
          // The candidate route is rebuilt from the depot at the vehicle's
          // shift start, so its geometry spans from SIM_START; trim by the
          // candidate's OWN total duration.
          startMinute: SIM_START_MINUTE,
          durationMinutes: Math.max(1, route.duration_minutes ?? 1),
        },
      ]),
    );
  }
  // The drawn zone stays visible (as a persistent translucent overlay, not the
  // in-progress dashed style) for as long as it's attached to the current
  // injection — through preview, applying, and applied — so the user doesn't
  // lose track of where they placed it. Cleared on Discard/Dismiss/Cancel,
  // which reset injection to IDLE_INJECTION (polygon: null).
  const confirmedZone = injection.polygon ?? undefined;
  // The selected road (for a road-selected closure) stays on the map as a red
  // line through preview/applying/applied, same lifecycle as confirmedZone.
  const selectedRoad = injection.roadPath ?? undefined;

  return (
    <AppShell>
      <div className="scenario-layout">
        <section className="panel scenario-map">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">DETERMINISTIC REPLAY</span>
              <h1>Scenario laboratory</h1>
            </div>
            <span className="scenario-time">{time} SGT</span>
          </div>
          <DisruptionBanner
            injection={injection}
            drawPointCount={drawPointCount}
            roadPointCount={roadPointCount}
            onDismiss={clearInjection}
            onApply={applyPreviewedDisruption}
            onRetry={retryPreview}
            onCancel={cancelCapture}
            onFinishDrawing={finishDrawing}
            onConfirmRoad={confirmRoadSelection}
            onApproveReroute={approveReroute}
            onRejectReroute={rejectReroute}
          />
          <DispatchMap
            data={displayData}
            selectedVehicleId={selectedVehicleId}
            onSelectVehicle={setSelectedVehicleId}
            affectedVehicleIds={affectedVehicleIds}
            rerouteGeometry={rerouteGeometry}
            rerouteMeta={rerouteMeta}
            currentMinute={minute}
            drawMode={injection.phase === "DRAWING"}
            onPolygonComplete={handlePolygonComplete}
            onDrawPointCountChange={setDrawPointCount}
            finishSignal={finishSignal}
            confirmedZone={confirmedZone}
            roadSelectMode={injection.phase === "SELECTING_ROAD"}
            onRoadPointCountChange={setRoadPointCount}
            onRoadSelectComplete={handleRoadSelectComplete}
            roadSelectSignal={roadConfirmSignal}
            selectedRoad={selectedRoad}
          />
        </section>
        <aside className="panel event-toolbox">
          <span className="eyebrow">INJECT EVENT</span>
          <h2>Controlled disruptions</h2>
          <p>Draw a zone on the map to affect only the vehicles inside it.</p>
          {!realPlan && <p className="event-waiting">Waiting for live plan data…</p>}
          {injection.phase === "DRAWING" && (
            <p className="event-waiting">Click the map to add points, then use Finish shape above the map.</p>
          )}
          {injection.phase === "SELECTING_ROAD" && (
            <p className="event-waiting">Click the road&apos;s start and end points, then use Confirm selection above the map.</p>
          )}
          <div className="road-closure-mode">
            <span>Road closure input</span>
            {(["SELECT_ROAD", "DRAW_ZONE"] as const).map((value) => (
              <button
                key={value}
                className={roadClosureMode === value ? "speed active" : "speed"}
                onClick={() => setRoadClosureMode(value)}
                disabled={injection.phase !== "IDLE"}
                title={injection.phase !== "IDLE" ? "Finish or cancel the current capture first" : undefined}
              >
                {value === "SELECT_ROAD" ? "Select road" : "Draw zone"}
              </button>
            ))}
          </div>
          {events.map(({ type, label, icon: Icon }) => {
            const supported = SUPPORTED_TYPES.includes(type as SupportedDisruptionType);
            const capturingThis =
              (injection.phase === "DRAWING" || injection.phase === "SELECTING_ROAD") &&
              injection.disruptionType === type;
            const disabled = !supported || !realPlan || (injection.phase !== "IDLE" && !capturingThis);
            return (
              <button
                key={type}
                onClick={() => (capturingThis ? cancelCapture() : startCapture(type as SupportedDisruptionType))}
                disabled={disabled}
                title={!supported ? "Not available in this flow" : !realPlan ? "Waiting for live plan data" : undefined}
              >
                <Icon size={17} />
                <span>{label}</span>
                <b>{capturingThis ? "✕" : "+"}</b>
              </button>
            );
          })}
          {injection.disruptionType === "HEAVY_RAIN" && (
            <div className="severity-picker">
              <span>Severity</span>
              {(["MODERATE", "HEAVY"] as const).map((value) => (
                <button
                  key={value}
                  className={rainSeverity === value ? "speed active" : "speed"}
                  onClick={() => setRainSeverity(value)}
                >
                  {value}
                </button>
              ))}
            </div>
          )}
        </aside>
        <section className="panel timeline-panel">
          <div className="timeline-controls">
            <button className="round-button" onClick={() => setPlaying(!playing)}>{playing ? <Pause size={17} /> : <Play size={17} />}</button>
            {([1, 5, 20] as const).map((value) => <button key={value} onClick={() => setSpeed(value)} className={speed === value ? "speed active" : "speed"}>{value}×</button>)}
            <button className="utility-button" onClick={resetScenario}><RotateCcw size={15} />Reset</button>
            <button className="utility-button" onClick={createBranch}><GitBranch size={15} />Branch</button>
          </div>
        </section>
        <section className="panel branch-panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">SCENARIO TREE</span>
              <h2>{branches.length} deterministic branches</h2>
            </div>
          </div>
          {branches.map((branch, index) => (
            <div className="branch-row" key={branch}>
              <span>{index === 0 ? "LIVE BASELINE" : `BRANCH ${String(index).padStart(2, "0")}`}</span>
              <strong>{branch}</strong>
              <em>{index === 0 ? "source" : `from ${time}`}</em>
            </div>
          ))}
        </section>
      </div>
    </AppShell>
  );
}

// Banner driven by the draw -> preview -> apply lifecycle. Hidden when idle;
// while drawing it just prompts the user, once a preview comes back it shows
// the real affected-vehicle evidence and an Apply/Cancel choice.
function DisruptionBanner({
  injection,
  drawPointCount,
  roadPointCount,
  onDismiss,
  onApply,
  onRetry,
  onCancel,
  onFinishDrawing,
  onConfirmRoad,
  onApproveReroute,
  onRejectReroute,
}: {
  injection: InjectionState;
  drawPointCount: number;
  roadPointCount: number;
  onDismiss: () => void;
  onApply: () => void;
  onRetry: () => void;
  onCancel: () => void;
  onFinishDrawing: () => void;
  onConfirmRoad: () => void;
  onApproveReroute: () => void;
  onRejectReroute: () => void;
}) {
  if (injection.phase === "IDLE") return null;
  const label = injection.disruptionType === "HEAVY_RAIN" ? "Heavy rain" : "Road closure";

  if (injection.phase === "FAILED") {
    return (
      <output className="disruption-banner phase-failed">
        <div className="disruption-banner-body">
          <span className="disruption-banner-tag">Couldn&apos;t preview</span>
          <strong>{label} — zone kept on the map</strong>
          <span className="disruption-banner-detail">
            {injection.error ?? "Something went wrong. The zone is still on the map — retry or discard."}
          </span>
        </div>
        <div className="disruption-banner-actions">
          <button className="disruption-banner-dismiss" onClick={onDismiss}>
            Discard
          </button>
          <button className="disruption-banner-apply" onClick={onRetry}>
            Retry
          </button>
        </div>
      </output>
    );
  }

  if (injection.phase === "DRAWING") {
    return (
      <output className="disruption-banner phase-drawing">
        <div className="disruption-banner-body">
          <span className="disruption-banner-tag">Drawing zone</span>
          <strong>{label} — click the map to add points</strong>
          <span className="disruption-banner-detail">
            {drawPointCount} point{drawPointCount === 1 ? "" : "s"} placed
            {drawPointCount < 3 ? ` — need at least ${3 - drawPointCount} more` : " — ready to finish"}
          </span>
        </div>
        <div className="disruption-banner-actions">
          <button className="disruption-banner-dismiss" onClick={onCancel}>
            Cancel
          </button>
          <button className="disruption-banner-apply" onClick={onFinishDrawing} disabled={drawPointCount < 3}>
            Finish shape
          </button>
        </div>
      </output>
    );
  }

  if (injection.phase === "SELECTING_ROAD") {
    return (
      <output className="disruption-banner phase-drawing">
        <div className="disruption-banner-body">
          <span className="disruption-banner-tag">Selecting road</span>
          <strong>{label} — click the road&apos;s start and end</strong>
          <span className="disruption-banner-detail">
            {roadPointCount === 0
              ? "Click the start point of the road"
              : roadPointCount === 1
                ? "Click the end point of the road"
                : "Both points placed — ready to confirm"}
          </span>
        </div>
        <div className="disruption-banner-actions">
          <button className="disruption-banner-dismiss" onClick={onCancel}>
            Cancel
          </button>
          <button className="disruption-banner-apply" onClick={onConfirmRoad} disabled={roadPointCount < 2}>
            Confirm selection
          </button>
        </div>
      </output>
    );
  }

  if (!injection.preview && injection.phase !== "APPLYING") return null;

  const affected = injection.applied?.affected ?? injection.preview?.affected;
  const vehicleCount = affected?.vehicle_ids.length ?? 0;
  const vehicleList = affected?.vehicle_ids.join(", ") || "no vehicles";
  const phaseNote =
    injection.phase === "APPLYING" ? "Applying…" : injection.phase === "APPLIED" ? "Applied" : "Zone previewed";
  const hasReroute = Boolean(injection.rerouteCandidate);
  const rerouteList = injection.rerouteVehicleIds.join(", ") || "affected vehicles";
  const detail =
    injection.error && injection.phase === "PREVIEWED"
      ? injection.error
      : injection.phase === "APPLIED"
        ? hasReroute
          ? `${rerouteList} — dotted line shows the suggested reroute around the closure (remaining stops re-sequenced; completed and in-progress stops kept).`
          : `${vehicleList} — now running slower through this zone (same stops, later ETAs)`
        : `${vehicleList} — Apply disruption`;

  return (
    <output className={`disruption-banner phase-${injection.phase.toLowerCase()}`}>
      <div className="disruption-banner-body">
        <span className="disruption-banner-tag">{phaseNote}</span>
        <strong>
          {label} — {vehicleCount} vehicle{vehicleCount === 1 ? "" : "s"} affected
        </strong>
        <span className="disruption-banner-detail">{detail}</span>
      </div>
      {injection.phase === "APPLYING" && <span className="disruption-banner-spinner" aria-hidden />}
      {injection.phase === "PREVIEWED" && (
        <div className="disruption-banner-actions">
          <button className="disruption-banner-dismiss" onClick={onCancel}>
            Discard
          </button>
          <button className="disruption-banner-apply" onClick={onApply} disabled={vehicleCount === 0}>
            Apply
          </button>
        </div>
      )}
      {injection.phase === "APPLIED" && hasReroute && (
        <div className="disruption-banner-actions">
          <button className="disruption-banner-dismiss" onClick={onRejectReroute}>
            Reject
          </button>
          <button className="disruption-banner-apply" onClick={onApproveReroute}>
            Approve reroute
          </button>
        </div>
      )}
      {injection.phase === "APPLIED" && !hasReroute && (
        <button className="disruption-banner-dismiss" onClick={onDismiss}>
          Dismiss
        </button>
      )}
    </output>
  );
}
