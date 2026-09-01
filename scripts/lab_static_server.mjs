// Local public-assets-only preview; no directory listings or website mutation.
import fs from "node:fs/promises";
import path from "node:path";
import http from "node:http";

export async function startStaticServer(directory, port = 0) {
  const root = await fs.realpath(directory);
  const policy = await fs.readFile(path.join(root, "_headers"), "utf8");
  const commonHeaders = {};
  let inCommonRule = false;
  for (const line of policy.split(/\r?\n/)) {
    if (line && !/^\s/.test(line)) inCommonRule = line.trim() === "/*";
    else if (inCommonRule && /^\s+[^:]+:/.test(line)) {
      const boundary = line.indexOf(":");
      commonHeaders[line.slice(0, boundary).trim()] = line.slice(boundary + 1).trim();
    }
  }
  const mime = { ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8", ".json": "application/json", ".xml": "application/xml", ".txt": "text/plain; charset=utf-8" };
  const server = http.createServer(async (request, response) => {
    if (!["GET", "HEAD"].includes(request.method)) { response.writeHead(405); response.end(); return; }
    try {
      const pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
      if (pathname.includes("\\") || pathname.includes("\0") || pathname.split("/").some(part => part.startsWith("."))) {
        response.writeHead(400); response.end(); return;
      }
      let destination = path.resolve(root, "." + pathname);
      if (destination !== root && !destination.startsWith(root + path.sep)) throw new Error("outside release");
      let status = 200;
      try {
        const stat = await fs.stat(destination);
        if (stat.isDirectory()) destination = path.join(destination, "index.html");
        destination = await fs.realpath(destination);
        if (!destination.startsWith(root + path.sep)) throw new Error("outside release");
      } catch {
        destination = path.join(root, "404.html"); status = 404;
      }
      const content = await fs.readFile(destination);
      response.writeHead(status, { ...commonHeaders, "Content-Type": mime[path.extname(destination)] || "application/octet-stream",
        "Content-Length": content.length, "Cache-Control": "no-store" });
      response.end(request.method === "HEAD" ? undefined : content);
    } catch {
      response.writeHead(404, { "Content-Type": "text/plain" }); response.end("Not found");
    }
  });
  await new Promise((resolve, reject) => { server.once("error", reject); server.listen(port, "127.0.0.1", resolve); });
  return { server, baseUrl: "http://127.0.0.1:" + server.address().port };
}

