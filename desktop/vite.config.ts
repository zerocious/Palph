import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Порт фиксирован: его же указывает src-tauri/tauri.conf.json в devUrl.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
  },
  build: {
    // Tauri сам подставляет современный webview, поэтому не тащим
    // легаси-транспиляцию.
    target: "esnext",
    sourcemap: true,
  },
});
