import L from "leaflet";
import { useEffect, useRef, useState } from "react";
import { WMSTileLayer, useMap } from "react-leaflet";
import { createPortal } from "react-dom";
import type { Vehicle } from "../../types";
import { formatHeading, formatKnots, kmhToKnots, metersPerSecondToKnots } from "../../utils/formatters";
import { haversineMeters } from "../../utils/geo";

const WEATHER_RADAR_WMS_URL = "https://mapservices.weather.noaa.gov/eventdriven/services/radar/radar_base_reflectivity/MapServer/WMSServer";
const WEATHER_RADAR_REFRESH_MS = 10 * 60 * 1000;
const WEATHER_RADAR_OPACITY = 0.48;
const TRANSPARENT_TILE_DATA_URL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=";
const WIND_OVERLAY_REFRESH_MS = 15 * 60 * 1000;
const WIND_OVERLAY_ATTRIBUTION = "Wind &copy; Open-Meteo";
const WIND_SAMPLE_COLUMNS = 4;
const WIND_SAMPLE_ROWS = 3;
const WIND_FETCH_TIMEOUT_MS = 7000;

type WindSample = { id: string; latitude: number; longitude: number; speedKmh: number; directionDeg: number };
type ProjectedWindSample = WindSample & { x: number; y: number };
type WindState = { samples: WindSample[]; projectedSamples: ProjectedWindSample[] };

type Props = { yp?: Vehicle; showVectors: boolean; onToggleVectors: () => void };

export function WeatherRadarLayer() {
  const [bucket, setBucket] = useState(() => Math.floor(Date.now() / WEATHER_RADAR_REFRESH_MS));
  useEffect(() => {
    const interval = window.setInterval(() => setBucket(Math.floor(Date.now() / WEATHER_RADAR_REFRESH_MS)), WEATHER_RADAR_REFRESH_MS);
    return () => window.clearInterval(interval);
  }, []);
  return <WMSTileLayer key={`weather-radar-${bucket}`} url={`${WEATHER_RADAR_WMS_URL}?radar_cache=${bucket}`} layers="1" format="image/png" transparent opacity={WEATHER_RADAR_OPACITY} attribution="Radar &copy; NOAA/NWS" errorTileUrl={TRANSPARENT_TILE_DATA_URL} eventHandlers={{ tileerror: (event) => { const tile = (event as unknown as { tile?: HTMLImageElement }).tile; if (tile) tile.src = TRANSPARENT_TILE_DATA_URL; } }} />;
}

export function WindLayer({ yp, showVectors, onToggleVectors }: Props) {
  const map = useMap();
  const [windState, setWindState] = useState<WindState>({ samples: [], projectedSamples: [] });
  const sampleKeyRef = useRef("");
  const samplesRef = useRef<WindSample[]>([]);

  useEffect(() => { samplesRef.current = windState.samples; }, [windState.samples]);
  useEffect(() => {
    if (!showVectors) return;
    map.attributionControl.addAttribution(WIND_OVERLAY_ATTRIBUTION);
    return () => { map.attributionControl.removeAttribution(WIND_OVERLAY_ATTRIBUTION); };
  }, [map, showVectors]);
  useEffect(() => {
    let cancelled = false;
    let controller: AbortController | null = null;
    const refresh = () => {
      const points = windSamplePoints(map);
      const key = `${Math.floor(Date.now() / WIND_OVERLAY_REFRESH_MS)}:${points.map((point) => `${point.latitude.toFixed(2)},${point.longitude.toFixed(2)}`).join("|")}`;
      if (key === sampleKeyRef.current) { setWindState((current) => ({ ...current, projectedSamples: projectWindSamples(map, samplesRef.current) })); return; }
      sampleKeyRef.current = key;
      controller?.abort(); controller = new AbortController();
      fetchWindSamples(points, controller.signal).then((samples) => { if (!cancelled) { samplesRef.current = samples; setWindState({ samples, projectedSamples: projectWindSamples(map, samples) }); } });
    };
    const project = () => setWindState((current) => ({ ...current, projectedSamples: projectWindSamples(map, samplesRef.current) }));
    refresh(); map.on("moveend zoomend resize", refresh); map.on("move zoom", project);
    const interval = window.setInterval(refresh, WIND_OVERLAY_REFRESH_MS);
    return () => { cancelled = true; controller?.abort(); window.clearInterval(interval); map.off("moveend zoomend resize", refresh); map.off("move zoom", project); };
  }, [map]);

  const wind = windState.samples.length ? windState.samples[Math.floor(windState.samples.length / 2)] : null;
  const ypReadout = readoutForYp(yp);
  if (!wind && !ypReadout) return null;
  return createPortal(<>
    {showVectors && windState.projectedSamples.length > 0 && <div className="wind-overlay" aria-hidden="true"><svg width="100%" height="100%" focusable="false">{windState.projectedSamples.map((sample) => <g key={sample.id} transform={`translate(${sample.x.toFixed(1)} ${sample.y.toFixed(1)}) rotate(${(sample.directionDeg + 180) % 360})`}><line className="wind-arrow-line" x1="0" y1="13" x2="0" y2={windArrowTipY(sample.speedKmh)} /><path className="wind-arrow-head" d={`M -5 ${windArrowTipY(sample.speedKmh) + 7} L 0 ${windArrowTipY(sample.speedKmh)} L 5 ${windArrowTipY(sample.speedKmh) + 7}`} /><circle className="wind-arrow-dot" cx="0" cy="13" r="2.4" /></g>)}</svg></div>}
    <button className="wind-readout" type="button" title="Toggle wind vectors" onClick={onToggleVectors}>{ypReadout && <ReadoutRow label="YP" heading={formatHeading(ypReadout.headingDeg)} speed={formatKnots(ypReadout.speedKts)} />}{wind && <ReadoutRow label="Wind" heading={formatHeading(wind.directionDeg)} speed={formatKnots(kmhToKnots(wind.speedKmh))} />}</button>
  </>, map.getContainer());
}
function ReadoutRow({ label, heading, speed }: { label: string; heading: string; speed: string }) { return <span className="readout-row"><span className="readout-label">{label}:</span><span className="readout-heading">{heading}</span><span className="readout-at">@</span><span className="readout-speed">{speed} kts</span></span>; }
function windSamplePoints(map: L.Map) { const size = map.getSize(); const points: Array<{ latitude: number; longitude: number }> = []; for (let row = 0; row < WIND_SAMPLE_ROWS; row += 1) for (let column = 0; column < WIND_SAMPLE_COLUMNS; column += 1) { const point = map.containerPointToLatLng([(column + 0.5) / WIND_SAMPLE_COLUMNS * size.x, (row + 0.5) / WIND_SAMPLE_ROWS * size.y]); points.push({ latitude: point.lat, longitude: point.lng }); } return points; }
function projectWindSamples(map: L.Map, samples: WindSample[]): ProjectedWindSample[] { return samples.map((sample) => { const point = map.latLngToContainerPoint([sample.latitude, sample.longitude]); return { ...sample, x: point.x, y: point.y }; }); }
async function fetchWindSamples(points: Array<{ latitude: number; longitude: number }>, signal: AbortSignal): Promise<WindSample[]> { const results = await Promise.allSettled(points.map((point, index) => fetchWindSample(point.latitude, point.longitude, index, signal))); return results.flatMap((result) => result.status === "fulfilled" && result.value ? [result.value] : []); }
async function fetchWindSample(latitude: number, longitude: number, index: number, signal: AbortSignal): Promise<WindSample | null> { const timeoutController = new AbortController(); const timeout = window.setTimeout(() => timeoutController.abort(), WIND_FETCH_TIMEOUT_MS); const abort = () => timeoutController.abort(); signal.addEventListener("abort", abort, { once: true }); try { const params = new URLSearchParams({ latitude: latitude.toFixed(4), longitude: longitude.toFixed(4), current: "wind_speed_10m,wind_direction_10m", wind_speed_unit: "kmh", timezone: "UTC" }); const response = await fetch(`https://api.open-meteo.com/v1/forecast?${params}`, { signal: timeoutController.signal }); if (!response.ok) return null; const payload = await response.json() as { current?: { wind_speed_10m?: number; wind_direction_10m?: number } }; const speedKmh = Number(payload.current?.wind_speed_10m); const directionDeg = Number(payload.current?.wind_direction_10m); return Number.isFinite(speedKmh) && Number.isFinite(directionDeg) ? { id: `${index}-${latitude.toFixed(3)}-${longitude.toFixed(3)}`, latitude, longitude, speedKmh, directionDeg } : null; } catch { return null; } finally { window.clearTimeout(timeout); signal.removeEventListener("abort", abort); } }
function windArrowTipY(speedKmh: number) { return -Math.min(30, Math.max(14, 11 + speedKmh * 0.55)); }
function readoutForYp(yp?: Vehicle) { if (!yp) return null; const headingDeg = Number.isFinite(yp.heading) ? yp.heading : undefined; const history = yp.history; if (!history || history.length < 2) return headingDeg == null ? null : { headingDeg, speedKts: undefined }; const recent = [...history].reverse(); const latest = recent.find((point) => point.stamp != null); const previous = latest ? recent.find((point) => point !== latest && point.stamp != null && latest.stamp! - point.stamp! > 0.1) : undefined; const speedKts = latest && previous && latest.stamp != null && previous.stamp != null ? metersPerSecondToKnots(haversineMeters(previous.latitude, previous.longitude, latest.latitude, latest.longitude) / (latest.stamp - previous.stamp)) : undefined; return headingDeg == null && speedKts == null ? null : { headingDeg, speedKts }; }
