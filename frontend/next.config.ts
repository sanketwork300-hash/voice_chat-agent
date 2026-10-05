import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // A self-contained server for the Docker image (frontend/Dockerfile).
  output: "standalone",
};

export default nextConfig;
