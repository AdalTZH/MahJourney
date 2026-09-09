import { copyFile, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const source = resolve(root, "node_modules/maplibre-gl/dist");
const destination = resolve(root, "public/maplibre-worker");

await mkdir(destination, { recursive: true });
await Promise.all([
  copyFile(resolve(source, "maplibre-gl-worker.mjs"), resolve(destination, "maplibre-gl-worker.mjs")),
  copyFile(resolve(source, "maplibre-gl-shared.mjs"), resolve(destination, "maplibre-gl-shared.mjs")),
]);
