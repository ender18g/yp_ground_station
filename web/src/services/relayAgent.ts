/** Client for the optional local relay agent (services/relay_agent.py). */

const AGENT_BASE_URL = "http://127.0.0.1:5757";

export interface AgentSerialPort {
  device: string;
  description: string;
}

async function agentFetch(path: string, init?: RequestInit): Promise<Response> {
  return fetch(`${AGENT_BASE_URL}${path}`, init);
}

export async function isAgentAvailable(): Promise<boolean> {
  try {
    const response = await agentFetch("/health");
    return response.ok;
  } catch {
    return false;
  }
}

export async function listAgentPorts(): Promise<AgentSerialPort[]> {
  const response = await agentFetch("/ports");
  if (!response.ok) throw new Error(`Failed to list ports: ${response.status}`);
  return response.json();
}

export async function startAgentRelay(port: string, baud: number, tcpPort: number): Promise<void> {
  const response = await agentFetch("/relays", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ port, baud, tcp_port: tcpPort }),
  });
  if (!response.ok) throw new Error(`Failed to start relay: ${response.status}`);
}

export async function stopAgentRelay(tcpPort: number): Promise<void> {
  const response = await agentFetch(`/relays/${tcpPort}`, { method: "DELETE" });
  if (!response.ok) throw new Error(`Failed to stop relay: ${response.status}`);
}
