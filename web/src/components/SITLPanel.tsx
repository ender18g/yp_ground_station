import { useEffect, useRef, useState } from "react";
import { Cable, CheckCircle2, CircleDashed, Loader2, Plus, Radio, RotateCcw, Trash2 } from "lucide-react";

import { connectSITL, listSerialPorts } from "../api";
import type { SerialPortInfo, SITLBridge } from "../api";

interface SITLPanelProps {
  bridges: Record<string, SITLBridge>;
  onConnect: (url: string, vehicleId: string) => void;
  onDisconnect: (vehicleId: string) => void;
}

export function SITLPanel({ bridges, onConnect: _onConnect, onDisconnect }: SITLPanelProps) {
  const [tab, setTab] = useState<"network" | "radio">("network");
  const [url, setUrl] = useState("");
  const [netVehicleId, setNetVehicleId] = useState("");
  const [netCameraHost, setNetCameraHost] = useState("");
  const [netError, setNetError] = useState<string | null>(null);
  const [netConnecting, setNetConnecting] = useState(false);
  const [serialPorts, setSerialPorts] = useState<SerialPortInfo[]>([]);
  const [portsLoading, setPortsLoading] = useState(false);
  const [portsLoaded, setPortsLoaded] = useState(false);
  const [selectedPort, setSelectedPort] = useState("");
  const [manualPort, setManualPort] = useState("");
  const [baud, setBaud] = useState("57600");
  const [radioVehicleId, setRadioVehicleId] = useState("");
  const [radioCameraHost, setRadioCameraHost] = useState("");
  const [radioError, setRadioError] = useState<string | null>(null);
  const [radioConnecting, setRadioConnecting] = useState(false);
  const [relayComPort, setRelayComPort] = useState("COM12");
  const [relayTcpPort, setRelayTcpPort] = useState("5762");
  const [relayCopied, setRelayCopied] = useState(false);

  const refreshPorts = () => {
    setPortsLoading(true);
    listSerialPorts()
      .then((ports) => {
        setSerialPorts(ports);
        setPortsLoaded(true);
        if (ports.length > 0 && !selectedPort) setSelectedPort(ports[0].device);
      })
      .finally(() => setPortsLoading(false));
  };

  const prevTab = useRef(tab);
  useEffect(() => {
    if (tab === "radio" && prevTab.current !== "radio") refreshPorts();
    prevTab.current = tab;
  }, [tab]);

  const handleNetConnect = async () => {
    const trimUrl = url.trim();
    if (!trimUrl) { setNetError("MAVLink URL is required"); return; }
    const validPrefixes = ["tcp:", "tcpin:", "tcpout:", "udpin:", "udpout:", "udpbcast:", "serial:"];
    if (!validPrefixes.some((prefix) => trimUrl.toLowerCase().startsWith(prefix))) {
      setNetError(`URL must start with: ${validPrefixes.join(", ")}`);
      return;
    }
    setNetError(null);
    setNetConnecting(true);
    const result = await connectSITL(trimUrl, netVehicleId.trim() || undefined, netCameraHost.trim() || undefined).catch((error) => ({ ok: false as const, error: String(error) }));
    setNetConnecting(false);
    if (!result.ok) setNetError(result.error ?? "Connection failed");
    else { setUrl(""); setNetVehicleId(""); setNetCameraHost(""); }
  };

  const handleRadioConnect = async () => {
    const port = (selectedPort || manualPort).trim();
    if (!port) { setRadioError("Select or enter a serial port"); return; }
    const baudNum = parseInt(baud, 10);
    if (!baudNum || baudNum <= 0) { setRadioError("Invalid baud rate"); return; }
    const mavUrl = `serial:${port}:${baudNum}`;
    setRadioError(null);
    setRadioConnecting(true);
    const result = await connectSITL(mavUrl, radioVehicleId.trim() || undefined, radioCameraHost.trim() || undefined).catch((error) => ({ ok: false as const, error: String(error) }));
    setRadioConnecting(false);
    if (!result.ok) setRadioError(result.error ?? "Connection failed");
    else { setRadioVehicleId(""); setRadioCameraHost(""); }
  };

  const bridgeList = Object.values(bridges);

  return (
    <div className="sitl-panel">
      <div className="panel-title"><Cable size={17} /><strong>Connections</strong></div>
      <div className="sitl-tabs">
        <button className={tab === "network" ? "sitl-tab active" : "sitl-tab"} onClick={() => setTab("network")}><Cable size={13} /> Network</button>
        <button className={tab === "radio" ? "sitl-tab active" : "sitl-tab"} onClick={() => setTab("radio")}><Radio size={13} /> RFD-900</button>
      </div>
      {tab === "network" && (
        <div className="sitl-form">
          <label className="sitl-field-label">MAVLink URL</label>
          <input className="sitl-input" type="text" placeholder="tcp:localhost:5760" value={url} onChange={(event) => setUrl(event.target.value)} onKeyDown={(event) => event.key === "Enter" && !netConnecting && handleNetConnect()} spellCheck={false} />
          <label className="sitl-field-label">Vehicle ID <span className="sitl-optional">(optional)</span></label>
          <input className="sitl-input" type="text" placeholder="auto-generated from URL" value={netVehicleId} onChange={(event) => setNetVehicleId(event.target.value)} onKeyDown={(event) => event.key === "Enter" && !netConnecting && handleNetConnect()} spellCheck={false} />
          <label className="sitl-field-label">Camera Host <span className="sitl-optional">(optional)</span></label>
          <input className="sitl-input" type="text" placeholder="defaults to MAVLink URL host, if any" value={netCameraHost} onChange={(event) => setNetCameraHost(event.target.value)} onKeyDown={(event) => event.key === "Enter" && !netConnecting && handleNetConnect()} spellCheck={false} />
          {netError && <div className="sitl-error">{netError}</div>}
          <button className="sitl-connect-btn" onClick={handleNetConnect} disabled={netConnecting}>{netConnecting ? <Loader2 size={15} className="sitl-spin" /> : <Plus size={15} />}{netConnecting ? "Connecting…" : "Connect"}</button>
          <div className="sitl-hint">Examples: <code>tcp:localhost:5760</code> · <code>udpin:0.0.0.0:14551</code></div>
        </div>
      )}
      {tab === "radio" && (
        <div className="sitl-form">
          {portsLoaded && !portsLoading && serialPorts.length === 0 && (
            <div className="sitl-windows-hint">
              <strong>No serial ports visible to Docker</strong>
              <p>Docker Desktop on Windows cannot access COM ports directly. Run this relay script on your Windows machine to bridge the COM port over TCP:</p>
              <div className="sitl-relay-inputs"><input className="sitl-input sitl-relay-field" value={relayComPort} onChange={(event) => setRelayComPort(event.target.value)} spellCheck={false} title="Windows COM port" placeholder="COM12" /><input className="sitl-input sitl-relay-field" value={relayTcpPort} onChange={(event) => setRelayTcpPort(event.target.value)} spellCheck={false} title="TCP port" placeholder="5762" /></div>
              <code className="sitl-relay-cmd">python services/com_tcp_relay.py --port {relayComPort} --baud {baud} --tcp-port {relayTcpPort}</code>
              <div className="sitl-relay-actions">
                <button className="sitl-relay-copy-btn" onClick={() => { navigator.clipboard.writeText(`python services/com_tcp_relay.py --port ${relayComPort} --baud ${baud} --tcp-port ${relayTcpPort}`); setRelayCopied(true); setTimeout(() => setRelayCopied(false), 2000); }}>{relayCopied ? "Copied!" : "Copy command"}</button>
                <button className="sitl-relay-use-btn" onClick={() => { setUrl(`tcp:host.docker.internal:${relayTcpPort}`); setTab("network"); }}>Connect via Network tab →</button>
              </div>
            </div>
          )}
          <label className="sitl-field-label">Serial Port <button className="sitl-refresh-btn" title="Refresh port list" onClick={refreshPorts} disabled={portsLoading}>{portsLoading ? <Loader2 size={12} className="sitl-spin" /> : <RotateCcw size={12} />}</button></label>
          {serialPorts.length > 0 ? <select className="sitl-input" value={selectedPort} onChange={(event) => setSelectedPort(event.target.value)}>{serialPorts.map((port) => <option key={port.device} value={port.device}>{port.device}{port.description && port.description !== "n/a" ? ` — ${port.description}` : ""}</option>)}</select> : <input className="sitl-input" type="text" placeholder="/dev/ttyUSB0" value={manualPort} onChange={(event) => setManualPort(event.target.value)} spellCheck={false} />}
          <label className="sitl-field-label">Baud Rate</label>
          <select className="sitl-input" value={baud} onChange={(event) => setBaud(event.target.value)}><option value="57600">57600 (RFD-900 default)</option><option value="115200">115200</option><option value="9600">9600</option><option value="38400">38400</option></select>
          <label className="sitl-field-label">Vehicle ID <span className="sitl-optional">(optional)</span></label>
          <input className="sitl-input" type="text" placeholder="auto-generated" value={radioVehicleId} onChange={(event) => setRadioVehicleId(event.target.value)} onKeyDown={(event) => event.key === "Enter" && !radioConnecting && handleRadioConnect()} spellCheck={false} />
          <label className="sitl-field-label">Camera Host <span className="sitl-optional">(optional)</span></label>
          <input className="sitl-input" type="text" placeholder="e.g. 192.168.1.50" value={radioCameraHost} onChange={(event) => setRadioCameraHost(event.target.value)} onKeyDown={(event) => event.key === "Enter" && !radioConnecting && handleRadioConnect()} spellCheck={false} />
          {radioError && <div className="sitl-error">{radioError}</div>}
          <button className="sitl-connect-btn" onClick={handleRadioConnect} disabled={radioConnecting}>{radioConnecting ? <Loader2 size={15} className="sitl-spin" /> : <Radio size={15} />}{radioConnecting ? "Connecting…" : "Connect Radio"}</button>
          <div className="sitl-hint">Requires the server container to have the USB device passed through via <code>devices:</code> in docker-compose (Linux hosts only).</div>
        </div>
      )}
      {bridgeList.length > 0 && <div className="sitl-bridge-list">{bridgeList.map((bridge) => <div key={bridge.vehicle_id} className={`sitl-bridge-row sitl-status-${bridge.status}`}><div className="sitl-bridge-icon">{bridge.status === "connected" && <CheckCircle2 size={15} />}{bridge.status === "connecting" && <Loader2 size={15} className="sitl-spin" />}{(bridge.status === "error" || bridge.status === "disconnected") && <CircleDashed size={15} />}</div><div className="sitl-bridge-info"><strong>{bridge.vehicle_id}</strong><span className="sitl-bridge-meta">{bridge.frame ? `${bridge.frame}` : bridge.url}{bridge.autopilot ? ` · ${bridge.autopilot}` : ""}</span>{bridge.status === "error" && bridge.error && <span className="sitl-bridge-error">{bridge.error}</span>}</div><button className="sitl-disconnect-btn" title="Disconnect" onClick={() => onDisconnect(bridge.vehicle_id)}><Trash2 size={14} /></button></div>)}</div>}
      {bridgeList.length === 0 && <div className="sitl-empty">No active connections.</div>}
    </div>
  );
}
