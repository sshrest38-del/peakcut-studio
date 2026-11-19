/**
 * Cloudflare Pages Worker for PeakCut
 * Proxies API and media requests to the Python Streamlit/FastAPI backend,
 * while serving the 100% pixel-perfect PeakCut UI on Cloudflare's global edge network.
 */
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    
    // Set your Streamlit / PeakCut backend server address in Cloudflare Pages Environment Variables:
    // BACKEND_URL = "https://your-server.streamlit.app" or your public tunnel address.
    const backend = (env.BACKEND_URL || "http://127.0.0.1:8765").replace(/\/+$/, "");

    // 1. Proxy API, media, uploads, and auth calls to the Python backend
    const isBackendRoute = 
      url.pathname.startsWith("/api/") ||
      url.pathname.startsWith("/media/") ||
      url.pathname.startsWith("/oauth/") ||
      url.pathname.startsWith("/stream/") ||
      url.pathname.startsWith("/auth/") ||
      url.pathname === "/healthz";

    if (isBackendRoute) {
      const backendTarget = new URL(url.pathname + url.search, backend);
      const reqHeaders = new Headers(request.headers);
      
      reqHeaders.set("Host", backendTarget.host);
      reqHeaders.set("X-Forwarded-Host", url.host);
      reqHeaders.set("X-Forwarded-Proto", url.protocol.replace(":", ""));
      reqHeaders.set("Origin", backend);

      try {
        const backendResponse = await fetch(backendTarget.toString(), {
          method: request.method,
          headers: reqHeaders,
          body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
          redirect: "manual"
        });

        // Copy response headers and add CORS headers if needed
        const respHeaders = new Headers(backendResponse.headers);
        respHeaders.set("Access-Control-Allow-Origin", url.origin);
        respHeaders.set("Access-Control-Allow-Credentials", "true");
        respHeaders.set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS");
        respHeaders.set("Access-Control-Allow-Headers", "*");

        return new Response(backendResponse.body, {
          status: backendResponse.status,
          statusText: backendResponse.statusText,
          headers: respHeaders
        });
      } catch (err) {
        return new Response(JSON.stringify({
          error: "Backend Server Unreachable",
          detail: "Could not connect to Python backend at: " + backend,
          message: err.message
        }), {
          status: 502,
          headers: { "Content-Type": "application/json" }
        });
      }
    }

    // 2. SPA Route handling for PeakCut App
    // /app, /app/videos, /app/creators, /app/settings all load the main SPA shell (index.html)
    if (url.pathname === "/app" || url.pathname.startsWith("/app/")) {
      const indexReq = new Request(new URL("/index.html", request.url), request);
      return env.ASSETS.fetch(indexReq);
    }

    // 3. Fallback to Cloudflare Pages static assets (landing.html, auth.html, brand logos, etc.)
    return env.ASSETS.fetch(request);
  }
};
