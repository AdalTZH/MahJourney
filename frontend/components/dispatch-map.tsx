"use client";

import * as maplibregl from "maplibre-gl";
import type { GeoJSONSource, Map as MapLibreMap } from "maplibre-gl";
import type { FeatureCollection } from "geojson";
import { useCallback, useEffect, useRef } from "react";
import type { Coordinate, MapState } from "@/lib/api";

// Per-vehicle timing for a reroute candidate's own route, used to trim its
// dotted line to the road still ahead of the truck. Measured along the
// candidate route itself (its own shift window + duration), not the active
// plan's route for the same vehicle.
export type RerouteMeta = { startMinute: number; durationMinutes: number };

// Warning color for a disrupted leg / rerouted path — reads clearly against the
// teal/blue fleet palette below.
const WARNING_COLOR = "#f97316";
const REROUTE_COLOR = "#fbbf24";
// Confirmed disruption zone — a distinct red so the placed zone reads as a
// hazard area, separate from the orange affected-route highlight.
const ZONE_COLOR = "#ef4444";
const EMPTY_FC: FeatureCollection = { type: "FeatureCollection", features: [] };

maplibregl.setWorkerUrl("/maplibre-worker/maplibre-gl-worker.mjs");

const colors = ["#38e8d4", "#75a7ff", "#f2bb5b", "#c084fc", "#34d399", "#fb7185", "#67e8f9", "#a3e635", "#f97316", "#94a3b8"];
// Pin icon ids registered on the map, one per fleet color, so stop markers
// render as a proper teardrop pin instead of a plain dot.
function pinIconId(color: string): string { return `stop-pin-${color.replace("#", "")}`; }
function pinIconSvg(color: string): string {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="34" height="46" viewBox="0 0 34 46">` +
    `<path d="M17 45C17 45 31 27.6 31 17C31 8.4 24.7 2 17 2C9.3 2 3 8.4 3 17C3 27.6 17 45 17 45Z" fill="${color}" stroke="#0b1220" stroke-width="2.5"/>` +
    `<circle cx="17" cy="17" r="7.5" fill="#0b1220"/>` +
    `</svg>`;
}
async function loadPinImage(color: string): Promise<HTMLImageElement> {
  const svg = pinIconSvg(color);
  const url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  const image = new Image();
  image.src = url;
  await image.decode();
  return image;
}
function routeGeoJson(data: MapState): FeatureCollection { return { type: "FeatureCollection", features: data.plan.routes.flatMap((route, index) => { const geometry = route.geometry ?? []; return geometry.length >= 2 ? [{ type: "Feature" as const, properties: { vehicle_id: route.vehicle_id, color: colors[index % colors.length] }, geometry: { type: "LineString" as const, coordinates: geometry.map((point) => [point.lon, point.lat]) } }] : []; }) }; }
// Only vehicles actually deployed for the plan are shown on the map; standby
// vehicles (held in reserve, no stops) are omitted so the map reflects who is
// on the road. Index is kept from the full list so dot colors stay aligned
// with the corresponding route colors.
function isDeployed(truck: MapState["trucks"][number]): boolean { return truck.phase !== "STANDBY" && truck.total_stops > 0; }
// Stops for the currently selected vehicle only. Empty collection when nothing
// is selected so the layer clears itself. Sequence is exposed for the label.
function stopGeoJson(data: MapState, selectedVehicleId?: string | null): FeatureCollection { if (!selectedVehicleId) return { type: "FeatureCollection", features: [] }; const routeIndex = data.plan.routes.findIndex((route) => route.vehicle_id === selectedVehicleId); const route = routeIndex >= 0 ? data.plan.routes[routeIndex] : undefined; if (!route) return { type: "FeatureCollection", features: [] }; const color = colors[routeIndex % colors.length]; return { type: "FeatureCollection", features: route.stops.map((stop) => ({ type: "Feature" as const, properties: { stop_id: stop.stop_id, sequence: stop.sequence, color, colorHex: color.replace("#", "") }, geometry: { type: "Point" as const, coordinates: [stop.location.lon, stop.location.lat] } })) }; }
function truckGeoJson(data: MapState): FeatureCollection { return { type: "FeatureCollection", features: data.trucks.map((truck, index) => ({ truck, index })).filter(({ truck }) => isDeployed(truck)).map(({ truck, index }) => ({ type: "Feature", properties: { ...truck, color: colors[index % colors.length] }, geometry: { type: "Point", coordinates: [truck.position.lon, truck.position.lat] } })) }; }
function fleetBounds(data: MapState) { const bounds = new maplibregl.LngLatBounds(); data.plan.routes.forEach((route) => { const points = route.geometry?.length ? route.geometry : route.stops.map((stop) => stop.location); points.forEach((point) => bounds.extend([point.lon, point.lat])); }); data.trucks.filter(isDeployed).forEach((truck) => bounds.extend([truck.position.lon, truck.position.lat])); return bounds; }

// --- Disruption overlays -----------------------------------------------------
// Highlights the full route of every affected vehicle (as determined by the
// backend's polygon-containment match) in a warning color, on top of the base
// route line, so it's obvious at a glance which vehicles a drawn zone hit.
function affectedRoutesGeoJson(data: MapState, affectedVehicleIds: string[]): FeatureCollection {
  if (!affectedVehicleIds.length) return EMPTY_FC;
  const wanted = new Set(affectedVehicleIds);
  const features: FeatureCollection["features"] = [];
  for (const route of data.plan.routes) {
    if (!wanted.has(route.vehicle_id)) continue;
    const geometry = route.geometry?.length ? route.geometry : route.stops.map((stop) => stop.location);
    if (geometry.length < 2) continue;
    features.push({
      type: "Feature",
      properties: { vehicle_id: route.vehicle_id },
      geometry: { type: "LineString", coordinates: geometry.map((point) => [point.lon, point.lat]) },
    });
  }
  return { type: "FeatureCollection", features };
}

// --- Disruption zone drawing --------------------------------------------------
// A drawn polygon is a simple ordered list of clicked points. It renders as a
// fill + outline while in progress (even with only 1-2 points, so the user
// gets immediate feedback) and is handed back to the caller as plain
// {lat, lon} points once closed.
function drawnPolygonGeoJson(points: Coordinate[]): FeatureCollection {
  if (points.length === 0) return EMPTY_FC;
  const coordinates = points.map((point) => [point.lon, point.lat]);
  const features: FeatureCollection["features"] = [];
  if (points.length >= 3) {
    features.push({
      type: "Feature",
      properties: { kind: "fill" },
      geometry: { type: "Polygon", coordinates: [[...coordinates, coordinates[0]]] },
    });
  }
  if (points.length >= 2) {
    features.push({
      type: "Feature",
      properties: { kind: "outline" },
      geometry: { type: "LineString", coordinates: points.length >= 3 ? [...coordinates, coordinates[0]] : coordinates },
    });
  }
  features.push({
    type: "Feature",
    properties: { kind: "vertices" },
    geometry: { type: "MultiPoint", coordinates },
  });
  return { type: "FeatureCollection", features };
}

// The confirmed selected road as a single red LineString. Empty collection
// (nothing rendered) for fewer than 2 points.
function selectedRoadGeoJson(road: Coordinate[]): FeatureCollection {
  if (road.length < 2) return EMPTY_FC;
  return {
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        properties: { kind: "road" },
        geometry: { type: "LineString", coordinates: road.map((point) => [point.lon, point.lat]) },
      },
    ],
  };
}

// In-progress road selection: the 1-2 clicked endpoints as points, plus a
// dashed straight connector once both are placed (just a placeholder hint —
// the real road geometry is fetched on confirm).
function roadSelectGeoJson(points: Coordinate[]): FeatureCollection {
  if (points.length === 0) return EMPTY_FC;
  const features: FeatureCollection["features"] = points.map((point) => ({
    type: "Feature",
    properties: { kind: "point" },
    geometry: { type: "Point", coordinates: [point.lon, point.lat] },
  }));
  if (points.length >= 2) {
    features.push({
      type: "Feature",
      properties: { kind: "line" },
      geometry: { type: "LineString", coordinates: points.map((point) => [point.lon, point.lat]) },
    });
  }
  return { type: "FeatureCollection", features };
}

function haversineKm(a: Coordinate, b: Coordinate): number {
  const R = 6371;
  const dLat = ((b.lat - a.lat) * Math.PI) / 180;
  const dLon = ((b.lon - a.lon) * Math.PI) / 180;
  const lat1 = (a.lat * Math.PI) / 180;
  const lat2 = (b.lat * Math.PI) / 180;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
}

// The not-yet-traveled portion of a polyline, given progress in [0, 1]. Mirrors
// the backend's point_along_geometry split so the reroute shows only the road
// ahead of the vehicle, not the part it has already driven.
function remainingGeometry(geometry: Coordinate[], progress: number): Coordinate[] {
  if (geometry.length < 2) return geometry;
  if (progress <= 0) return geometry;
  if (progress >= 1) return [geometry[geometry.length - 1]];
  const lengths = geometry.slice(1).map((point, index) => haversineKm(geometry[index], point));
  const total = lengths.reduce((sum, value) => sum + value, 0);
  const target = total * progress;
  let traversed = 0;
  for (let index = 0; index < lengths.length; index += 1) {
    if (traversed + lengths[index] >= target) {
      const local = (target - traversed) / Math.max(lengths[index], 1e-9);
      const start = geometry[index];
      const end = geometry[index + 1];
      const split: Coordinate = {
        lat: start.lat + (end.lat - start.lat) * local,
        lon: start.lon + (end.lon - start.lon) * local,
      };
      return [split, ...geometry.slice(index + 1)];
    }
    traversed += lengths[index];
  }
  return [geometry[geometry.length - 1]];
}

// Rerouted geometry for affected vehicles only, trimmed to the remaining
// (not-yet-traveled) portion at the current virtual minute. Unaffected vehicles
// are never present here, so their route lines are untouched.
function rerouteGeoJson(
  data: MapState,
  rerouteGeometry: Record<string, Coordinate[]> | undefined,
  currentMinute: number,
  rerouteMeta?: Record<string, RerouteMeta>,
): FeatureCollection {
  if (!rerouteGeometry) return EMPTY_FC;
  const features: FeatureCollection["features"] = [];
  for (const [vehicleId, geometry] of Object.entries(rerouteGeometry)) {
    if (geometry.length < 2) continue;
    const meta = rerouteMeta?.[vehicleId];
    const start = meta?.startMinute ?? 480;
    const duration =
      meta?.durationMinutes ??
      data.plan.routes.find((candidate) => candidate.vehicle_id === vehicleId)?.duration_minutes ??
      1;
    const progress = Math.min(1, Math.max(0, (currentMinute - start) / Math.max(1, duration)));
    const remaining = remainingGeometry(geometry, progress);
    if (remaining.length < 2) continue;
    features.push({
      type: "Feature",
      properties: { vehicle_id: vehicleId },
      geometry: { type: "LineString", coordinates: remaining.map((point) => [point.lon, point.lat]) },
    });
  }
  return { type: "FeatureCollection", features };
}

export function DispatchMap({
  data,
  selectedVehicleId,
  onSelectVehicle,
  affectedVehicleIds = [],
  rerouteGeometry,
  rerouteMeta,
  currentMinute = 750,
  drawMode = false,
  onPolygonComplete,
  onDrawPointCountChange,
  finishSignal = 0,
  confirmedZone,
  roadSelectMode = false,
  onRoadPointCountChange,
  onRoadSelectComplete,
  roadSelectSignal = 0,
  selectedRoad,
}: {
  data: MapState;
  selectedVehicleId?: string | null;
  onSelectVehicle?: (vehicleId: string | null) => void;
  // Disruption overlays (Scenario Laboratory). The affected vehicles (whose
  // whole route is highlighted) and the rerouted candidate geometry keyed by
  // vehicle id. Absent/empty on the dispatcher page, which passes none of these.
  affectedVehicleIds?: string[];
  rerouteGeometry?: Record<string, Coordinate[]>;
  // Per-vehicle candidate-route timing, keyed by vehicle id, so the dotted
  // suggestion is trimmed along its OWN route rather than the active plan's.
  rerouteMeta?: Record<string, RerouteMeta>;
  currentMinute?: number;
  // Disruption zone drawing (Scenario Laboratory). While drawMode is true,
  // clicks add polygon vertices instead of selecting a vehicle. Truck-select
  // click handling is suppressed while drawing.
  drawMode?: boolean;
  // Called with the current vertex count after every click while drawing, so
  // the caller can render its own "Finish (N points)" button and enable/
  // disable it based on having at least 3 points.
  onDrawPointCountChange?: (count: number) => void;
  // Bump this (e.g. a counter incremented on every "Finish" click) to close
  // the ring and report the finished points via onPolygonComplete. Finishing
  // with fewer than 3 points is a no-op (nothing is reported, but the partial
  // shape is still cleared).
  finishSignal?: number;
  onPolygonComplete?: (points: Coordinate[]) => void;
  // The zone belonging to the currently previewed/applied disruption (if any),
  // rendered as a persistent translucent red overlay distinct from the
  // in-progress dashed drawing style — this is what stays on the map after
  // the shape is finished, so the user doesn't lose track of where the
  // disruption was placed. Pass undefined/empty to clear it.
  confirmedZone?: Coordinate[];
  // Road selection (Scenario Laboratory, ROAD_CLOSURE only). While
  // roadSelectMode is true, the first two clicks pick the road's start and end
  // endpoints (further clicks are ignored until reset). Truck-select and
  // polygon drawing are suppressed while selecting a road.
  roadSelectMode?: boolean;
  // Called with the number of endpoints placed so far (0, 1, or 2) after each
  // click, so the caller can enable/disable its Confirm button.
  onRoadPointCountChange?: (count: number) => void;
  // Bump this to confirm the selection: reports the two endpoints via
  // onRoadSelectComplete (a no-op if fewer than 2 are placed) and clears the
  // in-progress endpoints.
  roadSelectSignal?: number;
  onRoadSelectComplete?: (start: Coordinate, end: Coordinate) => void;
  // The selected road polyline (from the backend, or a straight fallback),
  // rendered as a persistent red line — this is the road that stays on the map
  // as the confirmed closure. Pass undefined/empty to clear it.
  selectedRoad?: Coordinate[];
}) {
  const host = useRef<HTMLDivElement>(null);
  const map = useRef<MapLibreMap | null>(null);
  const latestData = useRef(data);
  const latestSelect = useRef(onSelectVehicle);
  const latestSelected = useRef(selectedVehicleId);
  const latestOverlay = useRef({ affectedVehicleIds, rerouteGeometry, rerouteMeta, currentMinute, confirmedZone, selectedRoad });
  const latestDrawMode = useRef(drawMode);
  const latestPolygonComplete = useRef(onPolygonComplete);
  const latestPointCountChange = useRef(onDrawPointCountChange);
  const drawPoints = useRef<Coordinate[]>([]);
  const latestRoadSelectMode = useRef(roadSelectMode);
  const latestRoadSelectComplete = useRef(onRoadSelectComplete);
  const latestRoadPointCountChange = useRef(onRoadPointCountChange);
  const roadPoints = useRef<Coordinate[]>([]);
  useEffect(() => { latestData.current = data; }, [data]);
  useEffect(() => { latestSelect.current = onSelectVehicle; }, [onSelectVehicle]);
  useEffect(() => { latestSelected.current = selectedVehicleId; }, [selectedVehicleId]);
  useEffect(() => { latestOverlay.current = { affectedVehicleIds, rerouteGeometry, rerouteMeta, currentMinute, confirmedZone, selectedRoad }; }, [affectedVehicleIds, rerouteGeometry, rerouteMeta, currentMinute, confirmedZone, selectedRoad]);
  useEffect(() => { latestPolygonComplete.current = onPolygonComplete; }, [onPolygonComplete]);
  useEffect(() => { latestPointCountChange.current = onDrawPointCountChange; }, [onDrawPointCountChange]);
  useEffect(() => { latestRoadSelectComplete.current = onRoadSelectComplete; }, [onRoadSelectComplete]);
  useEffect(() => { latestRoadPointCountChange.current = onRoadPointCountChange; }, [onRoadPointCountChange]);
  const clearDrawing = useCallback(() => {
    drawPoints.current = [];
    const instance = map.current;
    if (instance?.isStyleLoaded()) {
      void (instance.getSource("draw-zone") as GeoJSONSource | undefined)?.setData(drawnPolygonGeoJson([]));
    }
    latestPointCountChange.current?.(0);
  }, []);
  useEffect(() => {
    latestDrawMode.current = drawMode;
    if (!drawMode) {
      // Drawing was turned off (cancelled or completed) from outside: clear
      // any in-progress vertices so a stale shape doesn't linger on screen.
      clearDrawing();
    }
  }, [drawMode, clearDrawing]);
  const lastFinishSignal = useRef(finishSignal);
  useEffect(() => {
    if (finishSignal === lastFinishSignal.current) return;
    lastFinishSignal.current = finishSignal;
    if (drawPoints.current.length >= 3) {
      latestPolygonComplete.current?.(drawPoints.current);
    }
    clearDrawing();
  }, [finishSignal, clearDrawing]);
  const clearRoadSelection = useCallback(() => {
    roadPoints.current = [];
    const instance = map.current;
    if (instance?.isStyleLoaded()) {
      void (instance.getSource("road-select") as GeoJSONSource | undefined)?.setData(EMPTY_FC);
    }
    latestRoadPointCountChange.current?.(0);
  }, []);
  useEffect(() => {
    latestRoadSelectMode.current = roadSelectMode;
    if (!roadSelectMode) {
      // Road selection turned off (confirmed or cancelled): clear the
      // in-progress endpoint markers so they don't linger.
      clearRoadSelection();
    }
  }, [roadSelectMode, clearRoadSelection]);
  const lastRoadSignal = useRef(roadSelectSignal);
  useEffect(() => {
    if (roadSelectSignal === lastRoadSignal.current) return;
    lastRoadSignal.current = roadSelectSignal;
    if (roadPoints.current.length >= 2) {
      latestRoadSelectComplete.current?.(roadPoints.current[0], roadPoints.current[1]);
    }
    clearRoadSelection();
  }, [roadSelectSignal, clearRoadSelection]);
  useEffect(() => {
    if (!host.current || map.current) return;
    const instance = new maplibregl.Map({ container: host.current, center: [103.733, 1.318], zoom: 12.2, minZoom: 11, maxBounds: [[103.535, 1.144], [104.502, 1.494]], attributionControl: false, style: { version: 8, glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf", sources: { onemap: { type: "raster", tiles: ["https://www.onemap.gov.sg/maps/tiles/Night/{z}/{x}/{y}.png"], tileSize: 256, attribution: "© OneMap / Singapore Land Authority" } }, layers: [{ id: "onemap-night", type: "raster", source: "onemap", paint: { "raster-opacity": 0.72, "raster-saturation": -0.35 } }] } });
    instance.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
    instance.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-left");
    instance.on("load", async () => {
      instance.addSource("routes", { type: "geojson", data: routeGeoJson(latestData.current) });
      instance.addLayer({ id: "route-glow", type: "line", source: "routes", paint: { "line-color": ["get", "color"], "line-width": 12, "line-opacity": 0.28 } });
      instance.addLayer({ id: "routes", type: "line", source: "routes", paint: { "line-color": ["get", "color"], "line-width": 3.4, "line-opacity": 1 } });
      // Rerouted candidate geometry (dashed amber) for affected vehicles only.
      const overlay = latestOverlay.current;
      instance.addSource("reroute", { type: "geojson", data: rerouteGeoJson(latestData.current, overlay.rerouteGeometry, overlay.currentMinute, overlay.rerouteMeta) });
      instance.addLayer({ id: "reroute-glow", type: "line", source: "reroute", paint: { "line-color": REROUTE_COLOR, "line-width": 12, "line-opacity": 0.3 } });
      instance.addLayer({ id: "reroute", type: "line", source: "reroute", paint: { "line-color": REROUTE_COLOR, "line-width": 4, "line-dasharray": [1.5, 1] } });
      // Affected vehicles' routes highlighted in a warning color on top of the
      // base route line.
      instance.addSource("affected-routes", { type: "geojson", data: affectedRoutesGeoJson(latestData.current, overlay.affectedVehicleIds) });
      instance.addLayer({ id: "affected-routes-glow", type: "line", source: "affected-routes", paint: { "line-color": WARNING_COLOR, "line-width": 14, "line-opacity": 0.35 } });
      instance.addLayer({ id: "affected-routes", type: "line", source: "affected-routes", paint: { "line-color": WARNING_COLOR, "line-width": 5, "line-opacity": 0.95 } });
      // Confirmed disruption zone (persists after the shape is finished, while
      // it's attached to the current preview/applied injection) — translucent
      // red fill + solid outline, visually distinct from the in-progress
      // dashed drawing style below.
      instance.addSource("confirmed-zone", { type: "geojson", data: drawnPolygonGeoJson(latestOverlay.current.confirmedZone ?? []) });
      instance.addLayer({ id: "confirmed-zone-fill", type: "fill", source: "confirmed-zone", filter: ["==", ["get", "kind"], "fill"], paint: { "fill-color": ZONE_COLOR, "fill-opacity": 0.25 } });
      instance.addLayer({ id: "confirmed-zone-outline", type: "line", source: "confirmed-zone", filter: ["==", ["get", "kind"], "outline"], paint: { "line-color": ZONE_COLOR, "line-width": 2, "line-opacity": 0.9 } });
      // Selected road (persistent red line) — the road picked for a road
      // closure, drawn on top of the base routes so the closed road is obvious.
      instance.addSource("selected-road", { type: "geojson", data: selectedRoadGeoJson(latestOverlay.current.selectedRoad ?? []) });
      instance.addLayer({ id: "selected-road-glow", type: "line", source: "selected-road", paint: { "line-color": ZONE_COLOR, "line-width": 12, "line-opacity": 0.3 } });
      // Black casing drawn wider and beneath the red line so the closure reads
      // as a red road with a crisp black outline.
      instance.addLayer({ id: "selected-road-casing", type: "line", source: "selected-road", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#0b1220", "line-width": 9 } });
      instance.addLayer({ id: "selected-road", type: "line", source: "selected-road", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": ZONE_COLOR, "line-width": 5, "line-opacity": 0.95 } });
      // In-progress road-selection endpoints (start + end clicks) shown as red
      // markers until the selection is confirmed.
      instance.addSource("road-select", { type: "geojson", data: EMPTY_FC });
      instance.addLayer({ id: "road-select-line", type: "line", source: "road-select", filter: ["==", ["get", "kind"], "line"], paint: { "line-color": ZONE_COLOR, "line-width": 2.5, "line-dasharray": [2, 1.5] } });
      instance.addLayer({ id: "road-select-points", type: "circle", source: "road-select", filter: ["==", ["get", "kind"], "point"], paint: { "circle-radius": 6, "circle-color": ZONE_COLOR, "circle-stroke-color": "#0b1220", "circle-stroke-width": 1.5 } });
      // In-progress disruption zone the user is currently drawing.
      instance.addSource("draw-zone", { type: "geojson", data: drawnPolygonGeoJson([]) });
      instance.addLayer({ id: "draw-zone-fill", type: "fill", source: "draw-zone", filter: ["==", ["get", "kind"], "fill"], paint: { "fill-color": WARNING_COLOR, "fill-opacity": 0.18 } });
      instance.addLayer({ id: "draw-zone-outline", type: "line", source: "draw-zone", filter: ["==", ["get", "kind"], "outline"], paint: { "line-color": WARNING_COLOR, "line-width": 2.5, "line-dasharray": [2, 1.5] } });
      instance.addLayer({ id: "draw-zone-vertices", type: "circle", source: "draw-zone", filter: ["==", ["get", "kind"], "vertices"], paint: { "circle-radius": 5, "circle-color": WARNING_COLOR, "circle-stroke-color": "#0b1220", "circle-stroke-width": 1.5 } });
      // Register one pin image per fleet color up front so the stops layer
      // below can reference them by id via icon-image.
      await Promise.all(colors.map(async (color) => { const id = pinIconId(color); if (instance.hasImage(id)) return; try { instance.addImage(id, await loadPinImage(color)); } catch { /* fall back to circle rendering below */ } }));
      instance.addSource("stops", { type: "geojson", data: stopGeoJson(latestData.current, latestSelected.current) });
      if (instance.hasImage(pinIconId(colors[0]))) {
        instance.addLayer({ id: "stops", type: "symbol", source: "stops", layout: { "icon-image": ["concat", "stop-pin-", ["get", "colorHex"]], "icon-size": 0.85, "icon-anchor": "bottom", "icon-allow-overlap": true, "text-field": ["to-string", ["get", "sequence"]], "text-size": 11, "text-font": ["Open Sans Bold"], "text-anchor": "bottom", "text-offset": [0, -3.05], "text-allow-overlap": true }, paint: { "text-color": "#e8fffb", "text-halo-color": "#0b1220", "text-halo-width": 1.2 } });
      } else {
        // Pin images failed to register (e.g. decode blocked) — fall back to dots.
        instance.addLayer({ id: "stop-halo", type: "circle", source: "stops", paint: { "circle-radius": 10, "circle-color": ["get", "color"], "circle-opacity": 0.28 } });
        instance.addLayer({ id: "stops", type: "circle", source: "stops", paint: { "circle-radius": 5.5, "circle-color": "#0b1220", "circle-stroke-color": ["get", "color"], "circle-stroke-width": 2.5 } });
      }
      instance.addSource("trucks", { type: "geojson", data: truckGeoJson(latestData.current) });
      instance.addLayer({ id: "truck-halo", type: "circle", source: "trucks", paint: { "circle-radius": 15, "circle-color": ["get", "color"], "circle-opacity": 0.32 } });
      instance.addLayer({ id: "trucks", type: "circle", source: "trucks", paint: { "circle-radius": 7.5, "circle-color": ["get", "color"], "circle-stroke-color": "#e8fffb", "circle-stroke-width": 2 } });
      instance.on("mouseenter", "trucks", () => { instance.getCanvas().style.cursor = "pointer"; });
      instance.on("mouseleave", "trucks", () => { instance.getCanvas().style.cursor = ""; });
      instance.on("click", "trucks", (event) => {
        // Drawing and road selection both take priority over truck selection.
        if (latestDrawMode.current || latestRoadSelectMode.current) return;
        const feature = event.features?.[0];
        const vehicleId = feature?.properties?.vehicle_id as string | undefined;
        if (vehicleId) latestSelect.current?.(vehicleId);
      });
      instance.on("click", (event) => {
        if (latestDrawMode.current) {
          // Add a vertex at the clicked point. Finishing the shape is driven
          // externally via the finishSignal prop (a "Finish" button in the
          // caller's UI), not by a click gesture here.
          drawPoints.current = [...drawPoints.current, { lat: event.lngLat.lat, lon: event.lngLat.lng }];
          void (instance.getSource("draw-zone") as GeoJSONSource | undefined)?.setData(drawnPolygonGeoJson(drawPoints.current));
          latestPointCountChange.current?.(drawPoints.current.length);
          return;
        }
        if (latestRoadSelectMode.current) {
          // Collect up to two endpoints (start, then end). Further clicks are
          // ignored until the selection is confirmed/cancelled. Confirming is
          // driven externally via the roadSelectSignal prop.
          if (roadPoints.current.length >= 2) return;
          roadPoints.current = [...roadPoints.current, { lat: event.lngLat.lat, lon: event.lngLat.lng }];
          void (instance.getSource("road-select") as GeoJSONSource | undefined)?.setData(roadSelectGeoJson(roadPoints.current));
          latestRoadPointCountChange.current?.(roadPoints.current.length);
          return;
        }
        const hits = instance.queryRenderedFeatures(event.point, { layers: ["trucks"] });
        if (hits.length === 0) latestSelect.current?.(null);
      });
      const bounds = fleetBounds(latestData.current); if (!bounds.isEmpty()) instance.fitBounds(bounds, { padding: { top: 115, right: 45, bottom: 45, left: 45 }, maxZoom: 13, duration: 0 });
    });
    map.current = instance;
    // The map lives inside a draggable resizable panel and a flex column that
    // shrinks when the vehicle inspector docks. MapLibre doesn't watch its
    // container, so observe the host and call resize() on any size change.
    const observer = new ResizeObserver(() => { map.current?.resize(); });
    observer.observe(host.current);
    return () => { observer.disconnect(); instance.remove(); map.current = null; };
  }, []);
  useEffect(() => { const instance = map.current; if (!instance?.isStyleLoaded()) return; void (instance.getSource("routes") as GeoJSONSource | undefined)?.setData(routeGeoJson(data)); void (instance.getSource("trucks") as GeoJSONSource | undefined)?.setData(truckGeoJson(data)); void (instance.getSource("stops") as GeoJSONSource | undefined)?.setData(stopGeoJson(data, selectedVehicleId)); }, [data, selectedVehicleId]);
  useEffect(() => {
    const instance = map.current;
    if (!instance?.isStyleLoaded()) return;
    // Update the disruption overlays independently of the base map data so a
    // reroute redraw touches only the affected vehicles' new geometry and
    // highlighted routes, leaving every other route line exactly as it was.
    void (instance.getSource("affected-routes") as GeoJSONSource | undefined)?.setData(affectedRoutesGeoJson(data, affectedVehicleIds));
    void (instance.getSource("reroute") as GeoJSONSource | undefined)?.setData(rerouteGeoJson(data, rerouteGeometry, currentMinute, rerouteMeta));
  }, [data, affectedVehicleIds, rerouteGeometry, rerouteMeta, currentMinute]);
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    const zoneData = drawnPolygonGeoJson(confirmedZone ?? []);
    // Push the confirmed-zone geometry into its source. Unlike the base-map
    // data effects (which re-run every clock tick and self-heal), this effect
    // only fires when `confirmedZone` changes — typically once, right after the
    // user finishes drawing. At that instant the style is often mid-reload
    // (tiles fetching), so `isStyleLoaded()` can be transiently false and the
    // source lookup would return undefined. If we bailed then, the freshly
    // drawn zone would never render (the in-progress draw-zone has just been
    // cleared), which is exactly the "shape disappears after Finish" bug. So
    // apply immediately when possible, otherwise retry once the style settles.
    const apply = () => {
      const source = map.current?.getSource("confirmed-zone") as GeoJSONSource | undefined;
      if (source) {
        void source.setData(zoneData);
        return true;
      }
      return false;
    };
    if (instance.isStyleLoaded() && apply()) return;
    // Style not ready yet (or source not registered): wait for it to settle,
    // then apply. `once("idle")` fires after the style + sources are ready.
    const onIdle = () => {
      apply();
    };
    instance.once("idle", onIdle);
    return () => {
      instance.off("idle", onIdle);
    };
  }, [confirmedZone]);
  useEffect(() => {
    // Push the selected road geometry into its source. Same style-not-ready
    // retry dance as the confirmed zone above: this effect fires once when the
    // road resolves, which can land while the style is mid-reload.
    const instance = map.current;
    if (!instance) return;
    const roadData = selectedRoadGeoJson(selectedRoad ?? []);
    const apply = () => {
      const source = map.current?.getSource("selected-road") as GeoJSONSource | undefined;
      if (source) {
        void source.setData(roadData);
        return true;
      }
      return false;
    };
    if (instance.isStyleLoaded() && apply()) return;
    const onIdle = () => {
      apply();
    };
    instance.once("idle", onIdle);
    return () => {
      instance.off("idle", onIdle);
    };
  }, [selectedRoad]);
  useEffect(() => {
    const instance = map.current;
    if (!instance?.getLayer("trucks")) return;
    // Selected vehicle gets a brighter, larger stroke; everything else stays normal.
    instance.setPaintProperty("trucks", "circle-stroke-width", selectedVehicleId ? ["case", ["==", ["get", "vehicle_id"], selectedVehicleId], 4, 2] : 2);
    instance.setPaintProperty("trucks", "circle-radius", selectedVehicleId ? ["case", ["==", ["get", "vehicle_id"], selectedVehicleId], 9.5, 7.5] : 7.5);
    // When a vehicle is selected, fade the other route lines so the chosen
    // route (and its stops) reads clearly against the rest of the fleet.
    if (instance.getLayer("routes")) instance.setPaintProperty("routes", "line-opacity", selectedVehicleId ? ["case", ["==", ["get", "vehicle_id"], selectedVehicleId], 1, 0.18] : 1);
    if (instance.getLayer("route-glow")) instance.setPaintProperty("route-glow", "line-opacity", selectedVehicleId ? ["case", ["==", ["get", "vehicle_id"], selectedVehicleId], 0.28, 0.06] : 0.28);
  }, [selectedVehicleId, data]);
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    instance.getCanvas().style.cursor = drawMode || roadSelectMode ? "crosshair" : "";
  }, [drawMode, roadSelectMode]);
  return (
    <div
      ref={host}
      className="dispatch-map"
      aria-label={
        drawMode
          ? "Fleet map in disruption zone drawing mode. Click to add points, then use the Finish button to close the shape."
          : roadSelectMode
            ? "Fleet map in road selection mode. Click the road's start and end points, then use the Confirm button."
            : "Live fleet map centered on Tuas and Jurong. Click a vehicle to inspect its route."
      }
    />
  );
}
