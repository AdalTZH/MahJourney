export type Coordinate = { lat: number; lon: number };
export type Truck = { vehicle_id: string; driver_id: string; depot_id?: string; position: Coordinate; phase: string; completed_stops: number; total_stops: number };
export type Stop = { stop_id: string; sequence: number; location: Coordinate; eta_minute: number; departure_minute: number; demand: number };
export type Route = { vehicle_id: string; driver_id: string; stops: Stop[]; distance_km: number; duration_minutes: number; geometry?: Coordinate[] };
export type MapState = { clock: { scenario_id: string; current_minute: number; playing: boolean; speed: 1 | 5 | 20 }; trucks: Truck[]; plan: { plan_id: string; version: number; status: string; objective_cost: number; routes: Route[]; hard_violations: string[] } };

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1";

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

// Activates a CANDIDATE/VALIDATED plan (making it the fleet's live plan) and,
// as part of activation, sends each route's driver their stops over Telegram.
// Rejected with 409 if the plan carries hard violations.
export async function activatePlan(planId: string, version: number): Promise<MapState["plan"]> {
  return api<MapState["plan"]>("/plans/activate", { method: "POST", body: JSON.stringify({ plan_id: planId, version }) });
}

// Sends the current plan to drivers regardless of its current status:
// activates it first if it's not yet ACTIVE, then calls the dispatch
// endpoint either way so the caller gets back a per-driver `sent` map
// (activation itself dispatches internally but doesn't return that map).
export async function sendPlanToDrivers(plan: MapState["plan"]): Promise<DispatchResult> {
  const target = plan.status === "ACTIVE" ? plan : await activatePlan(plan.plan_id, plan.version);
  return dispatchPlanToDrivers(target.plan_id, target.version);
}

export const depot = { lat: 1.3214, lon: 103.6783 };
export function fixtureMapState(): MapState {
  const centers = [[1.331, 103.704], [1.343, 103.722], [1.315, 103.696], [1.304, 103.714], [1.327, 103.746], [1.349, 103.754], [1.296, 103.742], [1.312, 103.768], [1.338, 103.782], [1.288, 103.781]];
  const routes: Route[] = centers.map(([lat, lon], index) => ({ vehicle_id: `TRK-${String(index + 1).padStart(2, "0")}`, driver_id: `DRV-${String(index + 1).padStart(2, "0")}`, distance_km: 9.4 + index * 1.1, duration_minutes: 112 + index * 4, stops: Array.from({ length: 4 }, (_, stopIndex) => ({ stop_id: `ORD-${String(index * 4 + stopIndex + 1).padStart(3, "0")}`, sequence: stopIndex + 1, location: { lat: lat + stopIndex * 0.0018, lon: lon + (stopIndex % 2 ? -1 : 1) * 0.002 }, eta_minute: 500 + stopIndex * 28, departure_minute: 505 + stopIndex * 28, demand: 1 })) }));
  return { clock: { scenario_id: "demo", current_minute: 548, playing: false, speed: 1 }, trucks: routes.map((route, index) => ({ vehicle_id: route.vehicle_id, driver_id: route.driver_id, position: route.stops[1].location, phase: index === 3 ? "SERVICING" : "EN_ROUTE", completed_stops: index % 3, total_stops: 4 })), plan: { plan_id: "fixture-plan", version: 1, status: "VALIDATED", objective_cost: 124.6, routes, hard_violations: [] } };
}
