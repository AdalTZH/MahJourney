export type Coordinate = { lat: number; lon: number };
export type Truck = { vehicle_id: string; driver_id: string; depot_id?: string; position: Coordinate; phase: string; completed_stops: number; total_stops: number };
export type Stop = { stop_id: string; sequence: number; location: Coordinate; eta_minute: number; departure_minute: number; demand: number };
export type Route = { vehicle_id: string; driver_id: string; stops: Stop[]; distance_km: number; duration_minutes: number; geometry?: Coordinate[] };
export type MapState = { clock: { scenario_id: string; current_minute: number; playing: boolean; speed: 1 | 5 | 20 }; trucks: Truck[]; plan: { plan_id: string; version: number; status: string; objective_cost: number; routes: Route[]; hard_violations: string[] } };

// Resolve the backend base URL robustly across runtimes. In this vinext/Vite
// setup, client code cannot rely on `process.env` being defined in the browser,
// so we prefer Vite's `import.meta.env` (statically inlined into the bundle),
// then fall back to `process.env` (server/build), then a local-dev default.
// Each accessor is guarded so an undefined env object can never throw here.
function resolveApiBase(): string {
  const viteEnv =
    (typeof import.meta !== "undefined" &&
      (import.meta as unknown as { env?: Record<string, string | undefined> }).env) ||
    undefined;
  const fromVite = viteEnv?.VITE_API_BASE_URL ?? viteEnv?.NEXT_PUBLIC_API_BASE_URL;
  const fromProcess =
    typeof process !== "undefined" && process.env
      ? (process.env.VITE_API_BASE_URL ?? process.env.NEXT_PUBLIC_API_BASE_URL)
      : undefined;
  return fromVite ?? fromProcess ?? "/api/v1";
}

export const API_BASE = resolveApiBase();

// Thrown instead of a generic Error when the backend rejects a request for
// lack of (or an expired) admin session, so callers can distinguish "please
// log in" from every other kind of API failure.
export class UnauthorizedError extends Error {
  constructor() {
    super("not authenticated");
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set("Content-Type", "application/json");
  // Always send the session cookie, including to a different-origin API host
  // in local dev (frontend on :3000, backend on :8000) — same-origin in
  // production once both sit behind the same Caddy host.
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers, credentials: "include" });
  if (response.status === 401) throw new UnauthorizedError();
  if (!response.ok) throw new Error(`API ${response.status}: ${await response.text()}`);
  return response.json() as Promise<T>;
}

export type DispatchStep = { node: string; label: string };

/**
 * Directive the backend sends to steer the UI after a turn completes.
 *
 *   navigate       — push the dispatcher to a different page, optionally
 *                    opening a specific tab (e.g. "drafts" on /dispatcher).
 *   activate_plan  — activate a specific plan version immediately; the frontend
 *                    calls POST /plans/activate then refreshes the map.
 *   refresh        — re-fetch live data without navigating anywhere.
 */
export type UiDirective =
  | { action: "navigate"; path: string; tab?: string }
  | { action: "activate_plan"; plan_id: string; version: number }
  | { action: "refresh"; target: "map" | "drafts" };

// `evidence_references` is the real, de-duplicated union of the workers'
// evidence for this turn. It is EMPTY for a conversational reply (greeting,
// capability question, clarification) that ran no worker — so the UI must only
// show an evidence line when this is non-empty, never a hardcoded default.
export type DispatchFinal = {
  reply: string;
  generated_plan: unknown;
  evidence_references?: string[];
  /** Optional instruction for the frontend to navigate or execute an action. */
  ui_directive?: UiDirective | null;
};

// Stream a dispatcher turn via Server-Sent Events, invoking `onStep` for each
// real graph node as it runs and returning the final reply payload. Uses a POST
// fetch + manual SSE frame parsing (EventSource is GET-only). Falls back to
// throwing on transport/stream errors so the caller can degrade gracefully.
export async function streamDispatch(
  message: string,
  conversationId: string,
  onStep: (step: DispatchStep) => void,
): Promise<DispatchFinal> {
  const response = await fetch(`${API_BASE}/dispatcher/messages/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ message, conversation_id: conversationId }),
  });
  if (response.status === 401) throw new UnauthorizedError();
  if (!response.ok || !response.body) throw new Error(`API ${response.status}`);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let final: DispatchFinal | null = null;
  let streamError: string | null = null;

  const handleFrame = (frame: string) => {
    const lines = frame.split("\n");
    let event = "message";
    let data = "";
    for (const line of lines) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (!data) return;
    const parsed = JSON.parse(data);
    if (event === "step") onStep(parsed as DispatchStep);
    else if (event === "final") final = parsed as DispatchFinal;
    else if (event === "error") streamError = (parsed as { message?: string }).message ?? "error";
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep = buffer.indexOf("\n\n");
    while (sep !== -1) {
      handleFrame(buffer.slice(0, sep));
      buffer = buffer.slice(sep + 2);
      sep = buffer.indexOf("\n\n");
    }
  }
  if (buffer.trim()) handleFrame(buffer);

  if (streamError) throw new Error(streamError);
  if (!final) throw new Error("stream ended without a final reply");
  return final;
}

export type SessionStatus = { authenticated: boolean; username: string | null };
export async function fetchSessionStatus(): Promise<SessionStatus> {
  const response = await fetch(`${API_BASE}/auth/session`, { credentials: "include" });
  if (!response.ok) return { authenticated: false, username: null };
  return response.json() as Promise<SessionStatus>;
}

export async function login(username: string, password: string): Promise<void> {
  const response = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) {
    const message = response.status === 429 ? "Too many attempts. Try again shortly." : "Invalid username or password.";
    throw new Error(message);
  }
}

export async function logout(): Promise<void> {
  await fetch(`${API_BASE}/auth/logout`, { method: "POST", credentials: "include" });
}

export type DispatchResult = { plan_id: string; version: number; sent: Record<string, boolean> };
// Resends an already-ACTIVE plan's route messages to each driver's Telegram.
// Safe to call again if a driver missed the original message or enrolled late.
export async function dispatchPlanToDrivers(planId: string, version: number): Promise<DispatchResult> {
  return api<DispatchResult>(`/plans/${planId}/versions/${version}/dispatch`, { method: "POST" });
}

// Activates a CANDIDATE/VALIDATED plan (making it the fleet's live plan).
// By default this also sends each route's driver their stops over Telegram;
// pass dispatch=false to activate only (the dispatcher can then send from the
// Plan Detail tab). Rejected with 409 if the plan carries hard violations.
export async function activatePlan(planId: string, version: number, dispatch = true): Promise<MapState["plan"]> {
  return api<MapState["plan"]>("/plans/activate", { method: "POST", body: JSON.stringify({ plan_id: planId, version, dispatch }) });
}

// Sends the current plan to drivers regardless of its current status:
// activates it first if it's not yet ACTIVE, then calls the dispatch
// endpoint either way so the caller gets back a per-driver `sent` map
// (activation itself dispatches internally but doesn't return that map).
export async function sendPlanToDrivers(plan: MapState["plan"]): Promise<DispatchResult> {
  const target = plan.status === "ACTIVE" ? plan : await activatePlan(plan.plan_id, plan.version);
  return dispatchPlanToDrivers(target.plan_id, target.version);
}

// A pending candidate plan awaiting the dispatcher's decision. Same shape as
// the live plan, plus created_at so the drafts list can show how recent each is.
export type DraftPlan = MapState["plan"] & { created_at?: string };

// Fetch the candidate plans that have been generated but not yet activated
// (nor rejected). These are what the Draft Plans tab reviews — the live map and
// Plan Detail show only the ACTIVE plan, so drafts live here.
export async function fetchDraftPlans(): Promise<DraftPlan[]> {
  return api<DraftPlan[]>("/plans/drafts");
}

// Reject a draft so it leaves the drafts list without ever going live. The
// active plan is unaffected. Rejected with 409 if the plan is the active one.
export async function rejectPlan(planId: string, version: number): Promise<DraftPlan> {
  return api<DraftPlan>("/plans/reject", { method: "POST", body: JSON.stringify({ plan_id: planId, version }) });
}

// --- Scenario Laboratory disruption injection --------------------------------
// The Scenario page lets the user draw an arbitrary polygon zone on the map and
// drives a state machine (Idle -> Previewed -> Applying -> Applied) off two real
// backend calls: preview (no side effects — just shows what would happen) and
// apply (actually injects the disruption and re-times the live plan).

export type DisruptionType = "ROAD_CLOSURE" | "URGENT_ORDER" | "TRUCK_BREAKDOWN" | "HEAVY_RAIN";

// Only these two are wired to real planning effects on the backend; the other
// two DisruptionType values are accepted by the type for completeness (e.g. the
// WebMCP tool schema) but rejected with 400 by preview/apply.
export type SupportedDisruptionType = "ROAD_CLOSURE" | "HEAVY_RAIN";

// A disruption zone is described EITHER by a drawn `polygon` (>=3 points) OR,
// for ROAD_CLOSURE only, by a selected `road_path` polyline (>=2 points) that
// the backend buffers into a thin corridor. Exactly one of the two is sent.
export type PolygonDisruptionRequest = {
  disruption_type: SupportedDisruptionType;
  polygon?: Coordinate[];
  road_path?: Coordinate[];
  buffer_m?: number;
  severity?: string;
  effective_minute: number;
};

// The real road polyline between two clicked endpoints, from OneMap's driving
// route. Used to draw the selected road and as the `road_path` of a road-closure
// disruption.
export type RoadPathResult = { road_path: Coordinate[]; distance_km: number };

// Route a start->end pair to a real road polyline. Throws if OneMap is
// unavailable (backend replies 503), letting the caller fall back to a straight
// segment between the two points.
export async function fetchRoadPath(
  start: Coordinate,
  end: Coordinate,
  scenarioId = "demo",
): Promise<RoadPathResult> {
  return api<RoadPathResult>(`/scenario/${scenarioId}/road-path`, {
    method: "POST",
    body: JSON.stringify({ start, end }),
  });
}

// Both preview and apply return the same "what did this zone affect" shape;
// apply additionally returns the injected event.
export type AffectedIds = { vehicle_ids: string[]; order_ids: string[] };

export type PreviewDisruptionResult = {
  disruption_type: string;
  affected: AffectedIds;
  candidate_routes: { vehicle_id: string; geometry: Coordinate[]; stops: Stop[] }[];
};

export type ApplyDisruptionResult = {
  disruption_type: string;
  affected: AffectedIds;
  event: { event_id: string; scenario_id: string; event_type: string; effective_minute: number; payload: Record<string, unknown> };
  // For a ROAD_CLOSURE that could be rerouted mid-route: the proposed reroute
  // (a dotted-line suggestion drawn on the scenario map only). Null when no
  // reroute was produced (non-closure, or every affected vehicle fell back to
  // re-time-in-place). This candidate is SCENARIO-ONLY — it is not persisted to
  // shared plan state, so it never appears in the dispatcher's Draft Plans tab
  // and can never become the Overview's active plan. Its routes are delivered
  // inline here rather than fetched from /plans/drafts.
  reroute_candidate: {
    plan_id: string;
    version: number;
    // The vehicles that actually received a new route (subset of affected).
    // Use this — not affected.vehicle_ids — to filter which candidate routes
    // to draw as the dotted suggestion. affected includes vehicles that merely
    // cross the zone but fell back to re-time; rerouted_vehicle_ids is only
    // those that got a genuinely re-sequenced route.
    rerouted_vehicle_ids: string[];
    // The candidate's full routes (with geometry) inline, so the scenario map
    // draws the dotted suggestion without any backend round-trip.
    routes: Route[];
  } | null;
};

// Preview: compute affected vehicles/orders + a candidate re-timed route for
// each, WITHOUT injecting anything — the live plan and scenario events are
// completely untouched by calling this.
export async function previewDisruption(
  body: PolygonDisruptionRequest,
  scenarioId = "demo",
): Promise<PreviewDisruptionResult> {
  return api<PreviewDisruptionResult>(`/scenario/${scenarioId}/preview-disruption`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// Apply: actually inject the drawn zone as a real disruption event and re-time
// the live plan to reflect it.
export async function applyDisruption(
  body: PolygonDisruptionRequest,
  scenarioId = "demo",
): Promise<ApplyDisruptionResult> {
  return api<ApplyDisruptionResult>(`/scenario/${scenarioId}/apply-disruption`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// Disruption injection is only meaningful when the API has returned an active
// plan, so the Scenario page gates its controls on this predicate.
export function isRealPlan(data: MapState): boolean {
  return Boolean(data.plan.plan_id) && data.plan.status !== "NO_ACTIVE_PLAN";
}

// --- Telegram driver enrollment ---------------------------------------------
// Enrolling a driver is a two-step handshake: the dispatcher issues a
// short-lived token for a driver id, and the driver redeems it by opening a
// t.me deep link (or sending `/start enroll_<token>`) to the fleet's bot. Once
// redeemed, the driver's Telegram account is bound so they receive their route
// messages on plan activation/dispatch.

export type ReadyStatus = {
  status: string;
  mode: string;
  missing_live_credentials: boolean;
  persistence: string;
  data_source: string;
  telegram_bot_username: string;
};

// Backend readiness/config probe. Exposes the Telegram bot username so the UI
// can build a t.me enrollment deep link.
export async function fetchReady(): Promise<ReadyStatus> {
  return api<ReadyStatus>("/ready");
}

export type Driver = { driver_id: string } & Record<string, unknown>;

export async function fetchDrivers(): Promise<Driver[]> {
  return api<Driver[]>("/drivers");
}

export type EnrollmentToken = { token: string; expires_in_seconds: number };

// Issue a fresh, short-lived enrollment token for a driver. The raw token is
// only returned here (never stored), so surface it to the dispatcher right away.
export async function issueEnrollmentToken(driverId: string): Promise<EnrollmentToken> {
  return api<EnrollmentToken>("/telegram/enrollment-tokens", {
    method: "POST",
    body: JSON.stringify({ driver_id: driverId }),
  });
}

// Build the driver-facing Telegram deep link that redeems an enrollment token.
// Returns null when the bot username isn't configured, so callers can fall back
// to showing the raw `/start enroll_<token>` command instead.
export function enrollmentDeepLink(botUsername: string, token: string): string | null {
  const handle = botUsername.replace(/^@/, "").trim();
  if (!handle) return null;
  return `https://t.me/${handle}?start=enroll_${token}`;
}

export type SuspendResult = { driver_id: string; status: string };

// Revoke a driver's binding so they stop receiving route messages.
export async function suspendDriver(driverId: string): Promise<SuspendResult> {
  return api<SuspendResult>(`/telegram/drivers/${encodeURIComponent(driverId)}/suspend`, {
    method: "POST",
  });
}

// A driver currently bound to a Telegram account. `suspended` means the link
// still exists but the driver is muted (no route messages until re-activated).
export type LinkedDriver = { driver_id: string; telegram_user_id: number; suspended: boolean };

// List the drivers whose Telegram accounts are linked. Drivers that were never
// enrolled don't appear here.
export async function fetchTelegramDrivers(): Promise<LinkedDriver[]> {
  return api<LinkedDriver[]>("/telegram/drivers");
}

export type ReactivateResult = { driver_id: string; status: string; was_suspended: boolean };

// Lift a driver's suspension so they receive route messages again. The Telegram
// binding stays intact.
export async function reactivateDriver(driverId: string): Promise<ReactivateResult> {
  return api<ReactivateResult>(`/telegram/drivers/${encodeURIComponent(driverId)}/reactivate`, {
    method: "POST",
  });
}

export type UnlinkResult = { driver_id: string; status: string; was_linked: boolean };

// Remove a driver's Telegram binding entirely, freeing the id to be enrolled
// again from scratch.
export async function unlinkDriver(driverId: string): Promise<UnlinkResult> {
  return api<UnlinkResult>(`/telegram/drivers/${encodeURIComponent(driverId)}/unlink`, {
    method: "POST",
  });
}

/**
 * Delete all persisted messages for a conversation so the agent starts fresh.
 */
export async function clearConversation(conversationId: string): Promise<void> {
  await api(`/dispatcher/conversations/${encodeURIComponent(conversationId)}`, {
    method: "DELETE",
  });
}

export type ConversationHistoryMessage = {
  role: "user" | "assistant";
  content: string;
  created_at: string;
};

/**
 * Fetch persisted conversation messages from the backend so the chat panel
 * can be seeded with history after a page refresh.
 * Returns an empty array when persistence is off or the conversation has no messages.
 */
export async function fetchConversationHistory(
  conversationId: string,
  limit = 100,
): Promise<ConversationHistoryMessage[]> {
  try {
    return await api<ConversationHistoryMessage[]>(
      `/dispatcher/conversations/${encodeURIComponent(conversationId)}/messages?limit=${limit}`,
    );
  } catch {
    return [];
  }
}



export const depot = { lat: 1.3214, lon: 103.6783 };
export function emptyMapState(): MapState {
  return {
    clock: { scenario_id: "demo", current_minute: 480, playing: false, speed: 1 },
    trucks: [],
    plan: {
      plan_id: "",
      version: 0,
      status: "NO_ACTIVE_PLAN",
      objective_cost: 0,
      routes: [],
      hard_violations: [],
    },
  };
}
