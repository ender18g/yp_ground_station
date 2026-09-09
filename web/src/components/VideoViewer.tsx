import { Maximize2, Video, X } from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";

export function VideoViewer({
  vehicleId,
  streams,
  onClose,
}: {
  vehicleId: string;
  streams: { label: string; url: string }[];
  onClose: () => void;
}) {
  const [frame, setFrame] = useState(() => ({
    x: Math.max(16, window.innerWidth - 456),
    y: 120,
    width: Math.min(420, window.innerWidth - 32),
    height: 320,
  }));

  // Track which camera URL is currently selected. Defaults to the first camera in the array.
  const [activeUrl, setActiveUrl] = useState<string>(streams[0]?.url || "");

  const dragRef = useRef<{
    mode: "move" | "resize";
    pointerId: number;
    startX: number;
    startY: number;
    frame: typeof frame;
  } | null>(null);

  const videoRef = useRef<HTMLVideoElement | null>(null);

  // The WebRTC logic watches 'activeUrl' so it automatically re-negotiates when the user switches cameras
  useEffect(() => {
    const node = videoRef.current;
    if (!node || !activeUrl) return;

    let pc: RTCPeerConnection | null = new RTCPeerConnection();
    let isActive = true;
    const controller = new AbortController();

    const startWebRTC = async () => {
      try {
        pc!.addTransceiver("video", { direction: "recvonly" });

        pc!.ontrack = (event) => {
          if (node.srcObject !== event.streams[0]) {
            node.srcObject = event.streams[0];
          }
        };

        const offer = await pc!.createOffer();
        await pc!.setLocalDescription(offer);

        const response = await fetch(activeUrl, {
          method: "POST",
          headers: { "Content-Type": "application/sdp" },
          body: offer.sdp,
          signal: controller.signal,
        });

        if (!response.ok)
          throw new Error(`WHEP stream failed: ${response.status}`);

        const answerSdp = await response.text();

        if (isActive && pc) {
          await pc!.setRemoteDescription({ type: "answer", sdp: answerSdp });
        }
      } catch (error) {
        if (
          isActive &&
          !(error instanceof DOMException && error.name === "AbortError")
        ) {
          console.error(`WebRTC Error on ${activeUrl}:`, error);
        }
      }
    };

    startWebRTC();

    return () => {
      isActive = false;
      controller.abort();
      if (pc) {
        pc.close();
        pc = null;
      }
      if (node) node.srcObject = null;
    };
  }, [activeUrl]);

  const updateFrame = (next: typeof frame) => {
    const maxWidth = Math.max(280, window.innerWidth - 24);
    const maxHeight = Math.max(220, window.innerHeight - 24);
    const width = Math.min(maxWidth, Math.max(280, next.width));
    const height = Math.min(maxHeight, Math.max(220, next.height));
    setFrame({
      width,
      height,
      x: Math.min(
        Math.max(12, next.x),
        Math.max(12, window.innerWidth - width - 12),
      ),
      y: Math.min(
        Math.max(12, next.y),
        Math.max(12, window.innerHeight - height - 12),
      ),
    });
  };

  const startDrag = (
    mode: "move" | "resize",
    event: ReactPointerEvent<HTMLElement>,
  ) => {
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      mode,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      frame,
    };
  };

  const moveDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    if (drag.mode === "move") {
      updateFrame({
        ...drag.frame,
        x: drag.frame.x + dx,
        y: drag.frame.y + dy,
      });
      return;
    }
    updateFrame({
      ...drag.frame,
      width: drag.frame.width + dx,
      height: drag.frame.height + dy,
    });
  };

  const endDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null;
  };

  return (
    <section
      className="video-viewer"
      style={{
        left: frame.x,
        top: frame.y,
        width: frame.width,
        height: frame.height,
      }}
      onMouseDown={(event) => event.stopPropagation()}
    >
      <header
        className="video-viewer-header"
        onPointerDown={(event) => startDrag("move", event)}
        onPointerMove={moveDrag}
        onPointerUp={endDrag}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
          <Video size={16} />
          <strong>{vehicleId}</strong>

          {/* Render the dropdown if the vehicle provided more than 1 camera feed */}
          {streams.length > 1 && (
            <select
              value={activeUrl}
              onChange={(e) => setActiveUrl(e.target.value)}
              onPointerDown={(e) => e.stopPropagation()} // Prevents dropdown click from dragging the window
              style={{
                background: "#1e293b",
                color: "white",
                border: "1px solid #475569",
                borderRadius: "4px",
                padding: "2px 6px",
                fontSize: "12px",
                outline: "none",
                cursor: "pointer",
              }}
            >
              {streams.map((stream, index) => (
                <option key={index} value={stream.url}>
                  {stream.label}
                </option>
              ))}
            </select>
          )}
        </div>
        <button
          className="icon-button"
          title="Close stream"
          onPointerDown={(event) => event.stopPropagation()}
          onClick={onClose}
        >
          <X size={17} />
        </button>
      </header>
      <video
        ref={videoRef}
        className="video-viewer-media"
        autoPlay
        muted
        playsInline
      />
      <button
        className="video-resize-handle"
        title="Resize stream"
        onPointerDown={(event) => startDrag("resize", event)}
        onPointerMove={moveDrag}
        onPointerUp={endDrag}
      >
        <Maximize2 size={15} />
      </button>
    </section>
  );
}
