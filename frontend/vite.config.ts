import { defineConfig } from "vite";
import { viteStaticCopy } from "vite-plugin-static-copy";

const cesiumSource = "node_modules/cesium/Build/Cesium";
const api = process.env.SSG_API ?? "http://127.0.0.1:8080";

export default defineConfig({
  define: { CESIUM_BASE_URL: JSON.stringify("/cesium") },
  plugins: [
    viteStaticCopy({
      targets: ["Workers", "ThirdParty", "Assets", "Widgets"].map((dir) => ({
        src: `${cesiumSource}/${dir}`,
        rename: { stripBase: 4 },
        dest: "cesium",
      })),
    }),
  ],
  server: { proxy: { "/api": api } },
  build: { chunkSizeWarningLimit: 6000 },
});
