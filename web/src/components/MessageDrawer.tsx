import { MessageSquare, X } from "lucide-react";
import { useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import type { VehicleType } from "../types";
import styles from "./MessageDrawer.module.css";

export interface StreamMessage {
  id: string;
  receivedAt: number;
  vehicle_id: string;
  vehicle_type: VehicleType;
  topic: string;
  type: string;
  stamp: number;
  msg: Record<string, unknown>;
}

interface MessageDrawerProps {
  messages: StreamMessage[];
  filteredMessages: StreamMessage[];
  filters: string[];
  width: number;
  onClose: () => void;
  onResize: (width: number) => void;
  onFiltersChange: (filters: string[]) => void;
}

export function MessageDrawer({ messages, filteredMessages, filters, width, onClose, onResize, onFiltersChange }: MessageDrawerProps) {
  const [selectedMessage, setSelectedMessage] = useState<StreamMessage | null>(null);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const clampedWidth = Math.max(360, Math.min(width, Math.floor(window.innerWidth * 0.82)));
  const filterControls = useMemo(() => {
    const controls: Array<{ depth: number; options: string[]; value: string }> = [];
    for (let depth = 0; depth < 8; depth += 1) {
      const options = topicOptions(messages, filters, depth);
      const selected = filters[depth] ?? "all";
      if (options.length === 0) break;
      controls.push({ depth, options, value: selected });
      if (selected === "all") break;
    }
    return controls;
  }, [messages, filters]);

  const startResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    const startX = event.clientX;
    const startWidth = clampedWidth;
    const move = (moveEvent: PointerEvent) => onResize(Math.max(360, Math.min(startWidth + startX - moveEvent.clientX, window.innerWidth - 96)));
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  const updateFilter = (depth: number, value: string) => onFiltersChange([...filters.slice(0, depth), value]);
  const toggleMessage = (message: StreamMessage) => setSelectedMessage((current) => {
    if (current?.id === message.id) { window.setTimeout(() => messageListRef.current?.scrollTo({ top: 0, behavior: "smooth" }), 0); return null; }
    return message;
  });

  return (
    <aside className={styles.drawer} style={{ width: clampedWidth }}>
      <div className={styles.resizeHandle} onPointerDown={startResize} />
      <div className={styles.header}>
        <div className="panel-title"><MessageSquare size={18} /><strong>Messages</strong></div>
        <div className={styles.count}>{filteredMessages.length} / {messages.length}</div>
        <button className="icon-button" title="Close messages" onClick={onClose}><X size={19} /></button>
      </div>
      <div className={styles.filterStack}>
        {filterControls.map((control) => (
          <label key={control.depth}><span>{filterLabel(control.depth)}</span><select value={control.value} onChange={(event) => updateFilter(control.depth, event.target.value)}><option value="all">All</option>{control.options.map((option) => <option key={option} value={option}>{option}</option>)}</select></label>
        ))}
      </div>
      <div className={styles.list} ref={messageListRef}>
        {selectedMessage && <article className={styles.pinned}><div className={styles.pinnedHeader}><div><strong>{selectedMessage.topic}</strong><span>{new Date(selectedMessage.receivedAt).toLocaleTimeString()}</span></div><button className="icon-button" title="Close message" onClick={() => setSelectedMessage(null)}><X size={17} /></button></div><div className={styles.rowMeta}><span>{selectedMessage.type}</span><span>{selectedMessage.vehicle_id}</span></div><pre className={styles.jsonView}>{jsonSyntaxHighlight(selectedMessage.msg)}</pre></article>}
        {filteredMessages.length === 0 ? <div className={styles.empty}>No messages match the current filter.</div> : filteredMessages.map((message) => <button key={message.id} className={`${styles.row} ${selectedMessage?.id === message.id ? styles.selected : ""}`} onClick={() => toggleMessage(message)}><div className={styles.rowTop}><strong>{message.topic}</strong><span>{new Date(message.receivedAt).toLocaleTimeString()}</span></div><div className={styles.rowMeta}><span>{message.type}</span><span>{message.vehicle_id}</span></div></button>)}
      </div>
    </aside>
  );
}

function topicParts(topic: string): string[] { return topic.split("/").filter(Boolean); }
function topicOptions(messages: StreamMessage[], filters: string[], depth: number): string[] {
  const values = new Set<string>();
  for (const message of messages) { const parts = topicParts(message.topic); if (filters.slice(0, depth).every((filter, index) => filter === "all" || parts[index] === filter) && parts[depth]) values.add(parts[depth]); }
  return Array.from(values).sort((a, b) => a.localeCompare(b));
}
function filterLabel(depth: number): string { return ["Topic root", "Vehicle ID", "Message topic", "Subtopic"][depth] ?? `Level ${depth + 1}`; }
function jsonSyntaxHighlight(value: unknown): ReactNode {
  const json = JSON.stringify(value, null, 2);
  const parts = json.split(/("(?:\\u[\da-fA-F]{4}|\\[^u]|[^\\"])*(?:\s*:)?|\btrue\b|\bfalse\b|\bnull\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g);
  return parts.map((part, index) => { if (!part) return null; let className = styles.jsonPunctuation; if (/^"/.test(part)) className = /:$/.test(part) ? styles.jsonKey : styles.jsonString; else if (/true|false/.test(part)) className = styles.jsonBoolean; else if (/null/.test(part)) className = styles.jsonNull; else if (/^-?\d/.test(part)) className = styles.jsonNumber; return <span key={`${part}-${index}`} className={className}>{part}</span>; });
}
