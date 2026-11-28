const HTML_PAGE = HTML_TEMPLATE.replace(
  "__SHOWCASE_DATA_JSON__",
  () => JSON.stringify(SHOWCASE_DATA).replace(/</g, "\\u003c")
);

const SECURITY_HEADERS = {
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "no-referrer",
  "Cache-Control": "public, max-age=60"
};

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload, null, 2), {
    status,
    headers: {
      ...SECURITY_HEADERS,
      "Content-Type": "application/json; charset=utf-8",
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, OPTIONS"
    }
  });
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method.toUpperCase();

    console.log(JSON.stringify({
      ts: new Date().toISOString(),
      service: "flagrewind-showcase",
      method,
      path
    }));

    if (method === "OPTIONS" && path.startsWith("/api/")) {
      return jsonResponse({ ok: true }, 204);
    }
    if (method !== "GET" && method !== "HEAD") {
      return jsonResponse({ error: "Method not allowed; read-only showcase." }, 405);
    }

    if (path === "/" || path === "/index.html") {
      return new Response(HTML_PAGE, {
        status: 200,
        headers: {
          ...SECURITY_HEADERS,
          "Content-Type": "text/html; charset=utf-8",
          "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
        }
      });
    }
    if (path === "/health") {
      return jsonResponse({
        ok: true,
        service: "flagrewind-showcase",
        mode: "read-only",
        synthetic_demo: true,
        heldout_tag: "drills-heldout-v1",
        verification: "local run; GitHub Actions blocked by an account billing hold",
        unapproved_writes: 0,
        repo: "https://github.com/Shashankcodelover/flagrewind"
      });
    }
    if (path === "/api/proof") {
      return jsonResponse(SHOWCASE_DATA.proof);
    }
    if (path === "/api/scenarios") {
      return jsonResponse({
        heldout_tag: "drills-heldout-v1",
        count: SHOWCASE_DATA.scenarios.length,
        scenarios: SHOWCASE_DATA.scenarios
      });
    }
    if (path === "/api/ablation") {
      return jsonResponse(SHOWCASE_DATA.ablation);
    }
    if (path === "/api/runs") {
      return jsonResponse({ runs: SHOWCASE_DATA.run_ledger });
    }
    return jsonResponse({ error: "Not found" }, 404);
  }
};
