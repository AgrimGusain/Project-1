import { useState } from "react";
import { Button, Field, Notice, Panel } from "../components/ui";
import { authToken, backendUrl, setConnection } from "../lib/api";
import { useTelemetry } from "../state/telemetry";

export default function Settings() {
  const { reconnect, link } = useTelemetry();
  const [url, setUrl] = useState(backendUrl());
  const [token, setToken] = useState(authToken() ?? "");
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const test = async () => {
    setResult(null);
    try {
      const base = url.trim().replace(/\/+$/, "");
      const res = await fetch(`${base}/api/health`);
      const h = await res.json();
      setResult({ ok: true, text: `Reached rover backend: mode ${h.mode}${h.auth_required ? ", token required" : ""}` });
    } catch {
      setResult({ ok: false, text: "Could not reach the backend at that address." });
    }
  };

  const save = () => {
    setConnection(url, token || null);
    reconnect();
    setResult({ ok: true, text: "Saved. Reconnecting…" });
  };

  return (
    <div className="mx-auto max-w-xl space-y-4">
      <Panel title="Backend connection">
        <div className="space-y-4">
          <Field label="Backend URL" hint="Leave empty when this page is served by the rover itself. Example: http://raspberrypi.local:8000">
            <input className="input font-mono" value={url} placeholder="(same origin)" onChange={(e) => setUrl(e.target.value)} />
          </Field>
          <Field label="Access token" hint="Only needed when the backend has auth.enabled. Stored in this browser only.">
            <input className="input font-mono" type="password" value={token} autoComplete="off" onChange={(e) => setToken(e.target.value)} />
          </Field>
          <div className="flex gap-2">
            <Button variant="primary" onClick={save}>Save &amp; reconnect</Button>
            <Button onClick={test}>Test</Button>
          </div>
          {result && <Notice tone={result.ok ? "ok" : "bad"}>{result.text}</Notice>}
          <div className="text-xs text-fg-muted">Telemetry link: <span className="font-semibold">{link}</span></div>
        </div>
      </Panel>
      <Notice tone="info" title="Network">
        Connect the laptop or tablet to the same Wi-Fi network as the Raspberry Pi. Manual control is protected by a server-side dead-man:
        if this device loses the connection, the rover stops on its own.
      </Notice>
    </div>
  );
}
