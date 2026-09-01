import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Vinext turns this into dist/standalone/server.js for the production image.
  output: "standalone",
};

export default nextConfig;
