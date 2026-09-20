"use client";

import * as maplibregl from "maplibre-gl";
import type { GeoJSONSource, Map as MapLibreMap } from "maplibre-gl";
import type { FeatureCollection } from "geojson";
import { useEffect, useRef } from "react";
import type { MapState } from "@/lib/api";

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

export function DispatchMap({ data, selectedVehicleId, onSelectVehicle }: { data: MapState; selectedVehicleId?: string | null; onSelectVehicle?: (vehicleId: string | null) => void }) {
  const host = useRef<HTMLDivElement>(null);
  const map = useRef<MapLibreMap | null>(null);
  const latestData = useRef(data);
  const latestSelect = useRef(onSelectVehicle);
  const latestSelected = useRef(selectedVehicleId);
  useEffect(() => { latestData.current = data; }, [data]);
  useEffect(() => { latestSelect.current = onSelectVehicle; }, [onSelectVehicle]);
  useEffect(() => { latestSelected.current = selectedVehicleId; }, [selectedVehicleId]);
  useEffect(() => {
    if (!host.current || map.current) return;
    const instance = new maplibregl.Map({ container: host.current, center: [103.733, 1.318], zoom: 12.2, minZoom: 11, maxBounds: [[103.535, 1.144], [104.502, 1.494]], attributionControl: false, style: { version: 8, glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf", sources: { onemap: { type: "raster", tiles: ["https://www.onemap.gov.sg/maps/tiles/Night/{z}/{x}/{y}.png"], tileSize: 256, attribution: "© OneMap / Singapore Land Authority" } }, layers: [{ id: "onemap-night", type: "raster", source: "onemap", paint: { "raster-opacity": 0.72, "raster-saturation": -0.35 } }] } });
    instance.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
    instance.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-left");
    instance.on("load", async () => {
      instance.addSource("routes", { type: "geojson", data: routeGeoJson(latestData.current) });
      instance.addLayer({ id: "route-glow", type: "line", source: "routes", paint: { "line-color": ["get", "color"], "line-width": 12, "line-opacity": 0.28 } });
      instance.addLayer({ id: "routes", type: "line", source: "routes", paint: { "line-color": ["get", "color"], "line-width": 3.4, "line-opacity": 1 } });
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
        const feature = event.features?.[0];
        const vehicleId = feature?.properties?.vehicle_id as string | undefined;
        if (vehicleId) latestSelect.current?.(vehicleId);
      });
      instance.on("click", (event) => {
        const hits = instance.queryRenderedFeatures(event.point, { layers: ["trucks"] });
        if (hits.length === 0) latestSelect.current?.(null);
      });
      const bounds = fleetBounds(latestData.current); if (!bounds.isEmpty()) instance.fitBounds(bounds, { padding: { top: 115, right: 45, bottom: 45, left: 45 }, maxZoom: 13, duration: 0 });
    });
    map.current = instance;
    return () => { instance.remove(); map.current = null; };
  }, []);
  useEffect(() => { const instance = map.current; if (!instance?.isStyleLoaded()) return; void (instance.getSource("routes") as GeoJSONSource | undefined)?.setData(routeGeoJson(data)); void (instance.getSource("trucks") as GeoJSONSource | undefined)?.setData(truckGeoJson(data)); void (instance.getSource("stops") as GeoJSONSource | undefined)?.setData(stopGeoJson(data, selectedVehicleId)); }, [data, selectedVehicleId]);
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
  return <div ref={host} className="dispatch-map" aria-label="Live fleet map centered on Tuas and Jurong. Click a vehicle to inspect its route." />;
}
