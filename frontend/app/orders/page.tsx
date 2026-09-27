"use client";

import { AlertTriangle, CalendarClock, Package, RefreshCw, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { AppShell } from "@/components/app-shell";
import { api, type Coordinate } from "@/lib/api";

// Mirrors the backend `Order` domain model (see backend/mahjourney/domain.py).
// GET /orders returns either raw repository rows or `Order.model_dump()`, so
// every field beyond the identifiers is treated as optional here.
type Order = {
  order_id: string;
  address?: string;
  postal_code?: string;
  location?: Coordinate;
  demand?: number;
  service_seconds?: number;
  window_start_minute?: number;
  window_end_minute?: number;
  cargo_tags?: string[];
  weight_kg?: number;
  volume_m3?: number;
  quantity?: number;
  delivery_area?: string;
  special_handling?: string;
  priority_level?: number;
  customer_name?: string;
  contact_phone?: string;
  assigned_depot_id?: string;
  assignment_note?: string;
};

// Minutes-from-midnight -> "HH:MM" for the delivery window columns.
function formatMinute(minute?: number): string {
  if (minute === undefined || Number.isNaN(minute)) return "—";
  const hours = Math.floor(minute / 60) % 24;
  const mins = minute % 60;
  return `${String(hours).padStart(2, "0")}:${String(mins).padStart(2, "0")}`;
}

const PRIORITY_LABELS: Record<number, string> = { 1: "CRITICAL", 2: "HIGH", 3: "STANDARD", 4: "LOW", 5: "DEFERRED" };

function priorityLabel(level?: number): string {
  if (level === undefined) return "—";
  return PRIORITY_LABELS[level] ?? `P${level}`;
}

export default function OrdersPage() {
  const [orders, setOrders] = useState<Order[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [query, setQuery] = useState("");
  const [planning, setPlanning] = useState(false);
  const [planStatus, setPlanStatus] = useState<string | null>(null);

  // Kick off a fleet plan for the current order book. This calls the backend's
  // plan generator (POST /plans/generate), which builds routes from the orders
  // currently loaded server-side; the result surfaces on the Dispatcher tab.
  const planSchedule = () => {
    setPlanning(true);
    setPlanStatus(null);
    api<{
      plan_id: string;
      version: number;
      status: string;
      generation_ms: number;
      hard_violations: string[];
    }>("/plans/generate", {
      method: "POST",
      body: JSON.stringify({}),
    })
      .then((plan) => {
        // generation_ms is the backend's wall-clock time to build + persist the
        // plan; show it in seconds so the operator can see how long planning
        // took (it tracks the OR-Tools search budget).
        const seconds = (plan.generation_ms / 1000).toFixed(1);
        const violations = plan.hard_violations ?? [];
        // Surface unassigned orders explicitly — a lower OR-Tools budget can
        // leave a tight order unplanned, which is exactly the kind of thing an
        // operator needs to see rather than have buried.
        const unassigned = violations.filter((v) => v.startsWith("unassigned stop"));
        let message = `Plan ${plan.plan_id} v${plan.version} generated in ${seconds}s. Open the Dispatcher tab to review.`;
        if (unassigned.length > 0) {
          message += ` Warning: ${unassigned.length} order(s) could not be assigned (${unassigned
            .map((v) => v.replace("unassigned stop ", ""))
            .join(", ")}).`;
        } else if (violations.length > 0) {
          message += ` Warning: ${violations.length} constraint violation(s) — plan needs review.`;
        }
        setPlanStatus(message);
      })
      .catch(() => setPlanStatus("Couldn't generate a schedule. Check the planner service and try again."))
      .finally(() => setPlanning(false));
  };

  const load = () => {
    setState("loading");
    api<Order[]>("/orders")
      .then((data) => {
        setOrders(data);
        setState("ready");
      })
      .catch(() => setState("error"));
  };

  useEffect(() => {
    let active = true;
    api<Order[]>("/orders")
      .then((data) => {
        if (!active) return;
        setOrders(data);
        setState("ready");
      })
      .catch(() => {
        if (active) setState("error");
      });
    return () => {
      active = false;
    };
  }, []);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return orders;
    return orders.filter((order) =>
      [order.order_id, order.address, order.customer_name, order.delivery_area, order.assigned_depot_id]
        .filter(Boolean)
        .some((field) => String(field).toLowerCase().includes(needle)),
    );
  }, [orders, query]);

  // Today's date, formatted for the plan button label (e.g. "25 Sep 2026").
  const today = useMemo(
    () => new Date().toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }),
    [],
  );

  return (
    <AppShell>
      <div className="operations-layout">
        <section className="operations-heading">
          <span className="eyebrow">ORDER INTAKE</span>
          <h1>Orders</h1>
          <p>Incoming delivery orders.</p>
        </section>

        <section className="panel" style={{ gridColumn: "1 / -1", display: "flex", flexDirection: "column" }}>
          <div className="panel-heading plan-detail-heading">
            <div>
              <span className="eyebrow">ORDER BOOK</span>
              <h1>
                {state === "ready" ? `${filtered.length} of ${orders.length} orders` : "Orders"}
              </h1>
            </div>
            <div className="plan-detail-heading-actions">
              <button type="button" className="icon-button" onClick={load} aria-label="Pull latest orders" title="Pull latest orders">
                <RefreshCw size={15} />
              </button>
              <button
                type="button"
                className="primary-action secondary-action"
                onClick={planSchedule}
                disabled={state !== "ready" || orders.length === 0 || planning}
                aria-label={`Plan delivery schedule for ${today}`}
              >
                <CalendarClock size={13} />
                {planning ? "Planning…" : `Plan delivery schedule for ${today}`}
              </button>
            </div>
          </div>

          <div className="plan-detail-toolbar">
            <div className="search-field">
              <Search size={14} />
              <input
                type="text"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Filter by id, address, customer…"
                aria-label="Filter orders"
              />
            </div>
          </div>

          {planStatus && <p className="dispatch-status plan-detail-dispatch-status">{planStatus}</p>}

          {state === "loading" && (
            <div className="plan-detail-empty">
              <RefreshCw size={20} className="spin" style={{ marginBottom: 8 }} />
              <p style={{ margin: 0 }}>Loading orders…</p>
            </div>
          )}

          {state === "error" && (
            <div className="plan-detail-empty">
              <AlertTriangle size={22} color="#e0a34a" style={{ marginBottom: 8 }} />
              <p style={{ margin: "0 0 12px" }}>Couldn&apos;t load orders. Check the backend connection and try again.</p>
              <button type="button" className="sort-chip" onClick={load}>
                Retry
              </button>
            </div>
          )}

          {state === "ready" && filtered.length === 0 && (
            <div className="plan-detail-empty">
              <Package size={22} color="#3f6269" style={{ marginBottom: 8 }} />
              <p style={{ margin: 0 }}>
                {orders.length === 0
                  ? "No orders yet. Ingested delivery orders will appear here as they arrive."
                  : "No matching orders. Try a different search term."}
              </p>
            </div>
          )}

          {state === "ready" && filtered.length > 0 && (
            <div className="plan-detail-list" style={{ overflowX: "auto" }}>
              <table className="plan-detail-table">
                <thead>
                  <tr>
                    {["Order ID", "Customer", "Postal code", "Delivery area", "Delivery window", "Order qty", "Priority"].map((heading) => (
                      <th key={heading}>{heading}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((order) => (
                    <tr key={order.order_id}>
                      <td className="plan-detail-stop-id">{order.order_id}</td>
                      <td>{order.customer_name || "—"}</td>
                      <td>{order.postal_code ? `S${order.postal_code}` : "—"}</td>
                      <td>{order.delivery_area || "—"}</td>
                      <td>
                        {formatMinute(order.window_start_minute)}–{formatMinute(order.window_end_minute)}
                      </td>
                      <td>{order.demand ?? order.quantity ?? "—"}</td>
                      <td>{priorityLabel(order.priority_level)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </AppShell>
  );
}
