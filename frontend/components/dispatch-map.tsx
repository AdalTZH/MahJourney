"use client";

import * as maplibregl from "maplibre-gl";
import type { GeoJSONSource, Map as MapLibreMap } from "maplibre-gl";
import type { FeatureCollection } from "geojson";
import { useEffect, useRef } from "react";
import type { MapState } from "@/lib/api";

maplibregl.setWorkerUrl("/maplibre-worker/maplibre-gl-worker.mjs");

const colors = ["#38e8d4", "#75a7ff", "#f2bb5b", "#c084fc", "#34d399", "#fb7185", "#67e8f9", "#a3e635", "#f97316", "#94a3b8"];
function routeGeoJson(data: MapState): FeatureCollection { return { type: "FeatureCollection", features: data.plan.routes.flatMap((route, index) => { const geometry = route.geometry ?? []; return geometry.length >= 2 ? [{ type: "Feature" as const, properties: { vehicle_id: route.vehicle_id, color: colors[index % colors.length] }, geometry: { type: "LineString" as const, coordinates: geometry.map((point) => [point.lon, point.lat]) } }] : []; }) }; }
function truckGeoJson(data: MapState): FeatureCollection { return { type: "FeatureCollection", features: data.trucks.map((truck, index) => ({ type: "Feature", properties: { ...truck, color: colors[index % colors.length] }, geometry: { type: "Point", coordinates: [truck.position.lon, truck.position.lat] } })) }; }
function fleetBounds(data: MapState) { const bounds = new maplibregl.LngLatBounds(); data.plan.routes.forEach((route) => { const points = route.geometry?.length ? route.geometry : route.stops.map((stop) => stop.location); points.forEach((point) => bounds.extend([point.lon, point.lat])); }); data.trucks.forEach((truck) => bounds.extend([truck.position.lon, truck.position.lat])); return bounds; }

export function DispatchMap({ data }: { data: MapState }) {
  const host = useRef<HTMLDivElement>(null);
  const map = useRef<MapLibreMap | null>(null);
  const latestData = useRef(data);
  useEffect(() => { latestData.current = data; }, [data]);
  useEffect(() => {
    if (!host.current || map.current) return;
    const instance = new maplibregl.Map({ container: host.current, center: [103.733, 1.318], zoom: 12.2, minZoom: 11, maxBounds: [[103.535, 1.144], [104.502, 1.494]], attributionControl: false, style: { version: 8, sources: { onemap: { type: "raster", tiles: ["https://www.onemap.gov.sg/maps/tiles/Night/{z}/{x}/{y}.png"], tileSize: 256, attribution: "© OneMap / Singapore Land Authority" } }, layers: [{ id: "onemap-night", type: "raster", source: "onemap", paint: { "raster-opacity": 0.72, "raster-saturation": -0.35 } }] } });
    instance.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
    instance.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-left");
    instance.on("load", () => { instance.addSource("routes", { type: "geojson", data: routeGeoJson(latestData.current) }); instance.addLayer({ id: "route-glow", type: "line", source: "routes", paint: { "line-color": ["get", "color"], "line-width": 12, "line-opacity": 0.28 } }); instance.addLayer({ id: "routes", type: "line", source: "routes", paint: { "line-color": ["get", "color"], "line-width": 3.4, "line-opacity": 1 } }); instance.addSource("trucks", { type: "geojson", data: truckGeoJson(latestData.current) }); instance.addLayer({ id: "truck-halo", type: "circle", source: "trucks", paint: { "circle-radius": 15, "circle-color": ["get", "color"], "circle-opacity": 0.32 } }); instance.addLayer({ id: "trucks", type: "circle", source: "trucks", paint: { "circle-radius": 7.5, "circle-color": ["get", "color"], "circle-stroke-color": "#e8fffb", "circle-stroke-width": 2 } }); const bounds = fleetBounds(latestData.current); if (!bounds.isEmpty()) instance.fitBounds(bounds, { padding: { top: 115, right: 45, bottom: 45, left: 45 }, maxZoom: 13, duration: 0 }); });
    map.current = instance;
    return () => { instance.remove(); map.current = null; };
  }, []);
  useEffect(() => { const instance = map.current; if (!instance?.isStyleLoaded()) return; void (instance.getSource("routes") as GeoJSONSource | undefined)?.setData(routeGeoJson(data)); void (instance.getSource("trucks") as GeoJSONSource | undefined)?.setData(truckGeoJson(data)); }, [data]);
  return <div ref={host} className="dispatch-map" aria-label="Live fleet map centered on Tuas and Jurong" />;
}
