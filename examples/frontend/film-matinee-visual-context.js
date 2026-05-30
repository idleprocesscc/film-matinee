// Film-matinee visual context helper for film-matinee.
//
// Builds an image-first "film-matinee sheet" for the current playback window:
// keyframes + color bands + short subtitle anchors, plus a plain-text
// subtitle sidecar for the model. This module keeps sampling off the visible
// player by using a detached video element.

const DEFAULTS = {
  baseUrl: "/film-matinee",
  windowSec: 90,
  minSheetSec: 60,
  maxSheetSec: 600,
  targetKeyframesPerSheet: 12,
  rowSec: 30,
  sampleStepSec: 1,
  sheetWidth: 1600,
  rowHeight: 168,
  maxRowsPerSheet: 3,
  keyframesPerRow: 4,
  maxKeyframes: 12,
  keyframeWidth: 230,
  keyframeHeight: 130,
  bandPixelsPerSecond: 3,
  minBandWidth: 10,
  minSegmentSec: 4,
  maxSegmentSec: 18,
  visualEventSensitivity: 1.35,
  microEventSensitivity: 1.6,
  microEventLookaheadSec: 2,
  minMicroKeyframeGapSec: 2,
  lowInfoLuma: 0.035,
  highInfoLuma: 0.97,
  maxAnchorDistanceSec: 6,
  anchorMaxChars: 24,
  anchorMaxWords: 6,
  anchorFontPx: 12,
  jpegQuality: 0.72,
  includeCurrentFrame: false,
  currentFrameWidth: 960,
  seekTimeoutMs: 7000,
};

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function stripBaseUrl(baseUrl) {
  return String(baseUrl || "/film-matinee").replace(/\/+$/, "");
}

function fmtTime(seconds) {
  const total = Math.max(0, Math.round(seconds || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function cleanSubtitleText(text) {
  return String(text || "")
    .replace(/<[^>]+>/g, "")
    .replace(/\{\\[^}]+\}/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function cueId(cue, index) {
  const tenths = Math.max(0, Math.round((cue.start || 0) * 10));
  return `S${String(index + 1).padStart(2, "0")}_${tenths}`;
}

function shortCueLabel(id) {
  return String(id || "").split("_")[0] || "S";
}

function normalizeCues(cues = []) {
  return cues.map((cue, index) => ({
    id: cue.id || cueId(cue, index),
    start: Number(cue.start) || 0,
    end: Number(cue.end) || Number(cue.start) || 0,
    text: cleanSubtitleText(cue.text),
  })).filter((cue) => cue.text);
}

function truncateOpening(text, { maxChars = 18, maxWords = 4 } = {}) {
  const cleaned = cleanSubtitleText(text);
  if (!cleaned) return "";

  const hasCjk = /[\u3400-\u9fff]/.test(cleaned);
  if (!hasCjk && /\s/.test(cleaned)) {
    const words = cleaned.split(/\s+/).slice(0, maxWords).join(" ");
    return `${words.replace(/[,.!?;:，。！？；：]+$/, "")}...`;
  }

  const chars = Array.from(cleaned).slice(0, maxChars).join("");
  return `${chars.replace(/[,.!?;:，。！？；：]+$/, "")}...`;
}

function pickSubtitleAnchor(time, cues, options) {
  const maxDistance = options.maxAnchorDistanceSec;
  const nearby = cues
    .map((cue) => {
      const inside = time >= cue.start && time <= cue.end;
      const distance = inside ? 0 : Math.min(Math.abs(time - cue.start), Math.abs(time - cue.end));
      return { cue, distance };
    })
    .filter((item) => item.distance <= maxDistance)
    .sort((a, b) => a.distance - b.distance)[0];

  if (!nearby) return null;
  const text = truncateOpening(nearby.cue.text, {
    maxChars: options.anchorMaxChars,
    maxWords: options.anchorMaxWords,
  });
  if (!text) return null;
  return {
    id: nearby.cue.id,
    text,
    distance: Number(nearby.distance.toFixed(2)),
  };
}

function waitForEvent(target, eventName, timeoutMs) {
  return new Promise((resolve, reject) => {
    let timer = null;
    const describe = () => {
      const parts = [];
      if ("readyState" in target) parts.push(`readyState=${target.readyState}`);
      if ("networkState" in target) parts.push(`networkState=${target.networkState}`);
      if (target.error) parts.push(`mediaError=${target.error.code}`);
      return parts.length ? ` (${parts.join(", ")})` : "";
    };
    const cleanup = () => {
      target.removeEventListener(eventName, onEvent);
      target.removeEventListener("error", onError);
      if (timer) clearTimeout(timer);
    };
    const onEvent = () => {
      cleanup();
      resolve();
    };
    const onError = () => {
      cleanup();
      reject(new Error(`video ${eventName} failed${describe()}`));
    };

    target.addEventListener(eventName, onEvent, { once: true });
    target.addEventListener("error", onError, { once: true });
    timer = setTimeout(() => {
      cleanup();
      reject(new Error(`timed out waiting for ${eventName}${describe()}`));
    }, timeoutMs);
  });
}

async function createSamplerVideo(src, options) {
  const video = document.createElement("video");
  video.preload = "auto";
  video.muted = true;
  video.playsInline = true;
  video.crossOrigin = "anonymous";
  video.style.position = "fixed";
  video.style.left = "-9999px";
  video.style.top = "0";
  video.style.width = "1px";
  video.style.height = "1px";
  video.style.opacity = "0";
  video.style.pointerEvents = "none";
  document.body.appendChild(video);
  try {
    const metadataReady = waitForEvent(video, "loadedmetadata", options.seekTimeoutMs);
    video.src = src;
    video.load();
    await metadataReady;
    return video;
  } catch (error) {
    video.removeAttribute("src");
    video.load();
    video.remove();
    throw error;
  }
}

async function seekVideo(video, time, options) {
  const duration = Number.isFinite(video.duration) ? video.duration : time;
  const target = clamp(time, 0, Math.max(0, duration - 0.05));
  if (Math.abs(video.currentTime - target) < 0.035) {
    if (video.readyState >= 2) return;
    await waitForEvent(video, "loadeddata", options.seekTimeoutMs);
    return;
  }
  const wait = waitForEvent(video, "seeked", options.seekTimeoutMs);
  video.currentTime = target;
  await wait;
}

function drawContained(ctx, source, x, y, width, height) {
  const sourceWidth = source.videoWidth || source.naturalWidth || width;
  const sourceHeight = source.videoHeight || source.naturalHeight || height;
  const sourceRatio = sourceWidth / (sourceHeight || 1);
  const boxRatio = width / (height || 1);

  let drawWidth = width;
  let drawHeight = height;
  if (sourceRatio > boxRatio) {
    drawHeight = width / sourceRatio;
  } else {
    drawWidth = height * sourceRatio;
  }
  const dx = x + (width - drawWidth) / 2;
  const dy = y + (height - drawHeight) / 2;
  ctx.fillStyle = "#050505";
  ctx.fillRect(x, y, width, height);
  ctx.drawImage(source, dx, dy, drawWidth, drawHeight);
}

function averageColorFromVideo(video, canvas, ctx) {
  ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
  const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height);
  let r = 0;
  let g = 0;
  let b = 0;
  const count = data.length / 4;
  for (let i = 0; i < data.length; i += 4) {
    r += data[i];
    g += data[i + 1];
    b += data[i + 2];
  }
  return [
    Math.round(r / count),
    Math.round(g / count),
    Math.round(b / count),
  ];
}

function colorDistance(a, b) {
  if (!a || !b) return 0;
  const dr = a[0] - b[0];
  const dg = a[1] - b[1];
  const db = a[2] - b[2];
  return Math.sqrt(dr * dr + dg * dg + db * db);
}

function rgbCss(rgb) {
  return `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
}

function luminance(rgb) {
  return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255;
}

function readableTextColor(rgb) {
  return luminance(rgb) > 0.55 ? "#111" : "#f6f6f6";
}

function average(values) {
  if (!values.length) return 0;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function quantile(values, q) {
  if (!values.length) return 0;
  const sorted = values.slice().sort((a, b) => a - b);
  const index = clamp(Math.floor((sorted.length - 1) * q), 0, sorted.length - 1);
  return sorted[index];
}

function rgbSaturation(r, g, b) {
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  return max <= 0 ? 0 : (max - min) / max;
}

function visualStatsFromVideo(video, canvas, ctx) {
  ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
  const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height);
  let r = 0;
  let g = 0;
  let b = 0;
  let lumaSum = 0;
  let lumaSq = 0;
  let saturationSum = 0;
  const lumas = [];
  const count = data.length / 4;

  for (let i = 0; i < data.length; i += 4) {
    const pr = data[i];
    const pg = data[i + 1];
    const pb = data[i + 2];
    const y = (0.2126 * pr + 0.7152 * pg + 0.0722 * pb) / 255;
    r += pr;
    g += pg;
    b += pb;
    lumaSum += y;
    lumaSq += y * y;
    saturationSum += rgbSaturation(pr, pg, pb);
    lumas.push(y);
  }

  let edge = 0;
  let edgeCount = 0;
  for (let y = 0; y < canvas.height; y += 1) {
    for (let x = 0; x < canvas.width; x += 1) {
      const current = lumas[y * canvas.width + x];
      if (x + 1 < canvas.width) {
        edge += Math.abs(current - lumas[y * canvas.width + x + 1]);
        edgeCount += 1;
      }
      if (y + 1 < canvas.height) {
        edge += Math.abs(current - lumas[(y + 1) * canvas.width + x]);
        edgeCount += 1;
      }
    }
  }

  const luma = lumaSum / count;
  const variance = Math.max(0, lumaSq / count - luma * luma);
  return {
    rgb: [Math.round(r / count), Math.round(g / count), Math.round(b / count)],
    luma,
    contrast: Math.sqrt(variance),
    edge: edgeCount ? edge / edgeCount : 0,
    saturation: saturationSum / count,
  };
}

function frameQuality(sample, options) {
  const notBlack = clamp((sample.luma - options.lowInfoLuma) / 0.18, 0, 1);
  const notWhite = clamp((options.highInfoLuma - sample.luma) / 0.18, 0, 1);
  const lumaScore = Math.min(notBlack, notWhite);
  const contrastScore = clamp(sample.contrast / 0.16, 0, 1);
  const edgeScore = clamp(sample.edge / 0.12, 0, 1);
  const saturationScore = clamp(sample.saturation / 0.35, 0, 1);
  return clamp(
    0.4 * lumaScore + 0.25 * contrastScore + 0.25 * edgeScore + 0.1 * saturationScore,
    0,
    1,
  );
}

function isLowInformationFrame(sample, options) {
  return (
    sample.luma < options.lowInfoLuma && sample.contrast < 0.025 && sample.edge < 0.02
  ) || (
    sample.luma > options.highInfoLuma && sample.contrast < 0.025 && sample.edge < 0.02
  );
}

async function sampleVisualFrames(video, from, to, options) {
  const colorCanvas = document.createElement("canvas");
  colorCanvas.width = 32;
  colorCanvas.height = 18;
  const colorCtx = colorCanvas.getContext("2d", { willReadFrequently: true });
  const samples = [];
  const step = Math.max(0.25, Number(options.sampleStepSec) || 1);

  for (let t = from; t <= to + 0.001; t += step) {
    await seekVideo(video, t, options);
    const stats = visualStatsFromVideo(video, colorCanvas, colorCtx);
    const prev = samples[samples.length - 1];
    const sample = {
      t: Number(t.toFixed(3)),
      ...stats,
    };
    sample.delta = prev ? colorDistance(sample.rgb, prev.rgb) : 0;
    sample.change = prev
      ? sample.delta
        + Math.abs(sample.luma - prev.luma) * 220
        + Math.abs(sample.contrast - prev.contrast) * 90
        + Math.abs(sample.edge - prev.edge) * 90
      : 0;
    sample.quality = frameQuality(sample, options);
    sample.lowInformation = isLowInformationFrame(sample, options);
    samples.push(sample);
  }

  return samples;
}

function sampleAtOrNear(samples, time) {
  let best = samples[0] || null;
  let bestDistance = Infinity;
  for (const sample of samples) {
    const d = Math.abs(sample.t - time);
    if (d < bestDistance) {
      best = sample;
      bestDistance = d;
    }
  }
  return best;
}

function samplesInRange(samples, from, to) {
  return samples.filter((sample) => sample.t >= from - 0.001 && sample.t <= to + 0.001);
}

function averageRgb(samples) {
  if (!samples.length) return [0, 0, 0];
  return [
    Math.round(average(samples.map((sample) => sample.rgb[0]))),
    Math.round(average(samples.map((sample) => sample.rgb[1]))),
    Math.round(average(samples.map((sample) => sample.rgb[2]))),
  ];
}

function visualChangeThreshold(samples, options) {
  const changes = samples.slice(1).map((sample) => sample.change).filter(Number.isFinite);
  if (!changes.length) return Infinity;
  const median = quantile(changes, 0.5);
  const q75 = quantile(changes, 0.75);
  const q90 = quantile(changes, 0.9);
  const spread = Math.max(1, q75 - median);
  return Math.min(q90, median + spread * options.visualEventSensitivity);
}

function chooseCutSamples(samples, from, to, options) {
  const threshold = visualChangeThreshold(samples, options);
  const minGap = Math.max(2, options.minSegmentSec);
  const cuts = [];

  for (const sample of samples.slice(1)) {
    if (sample.change < threshold) continue;
    if (sample.t - from < options.minSegmentSec || to - sample.t < options.minSegmentSec) continue;
    if (cuts.length && sample.t - cuts[cuts.length - 1].t < minGap) {
      if (sample.change > cuts[cuts.length - 1].change) cuts[cuts.length - 1] = sample;
      continue;
    }
    cuts.push(sample);
  }

  return cuts;
}

function splitLongSegment(segment, samples, options) {
  const maxDuration = Math.max(options.maxSegmentSec, options.minSegmentSec * 2);
  const result = [];
  const queue = [segment];

  while (queue.length) {
    const current = queue.shift();
    if (current.end - current.start <= maxDuration) {
      result.push(current);
      continue;
    }

    const candidates = samplesInRange(
      samples,
      current.start + options.minSegmentSec,
      current.end - options.minSegmentSec,
    ).sort((a, b) => b.change - a.change);
    const best = candidates[0];
    const cut = best?.t || current.start + (current.end - current.start) / 2;
    queue.push({ start: current.start, end: cut });
    queue.push({ start: cut, end: current.end });
  }

  return result;
}

function buildVisualSegments(samples, from, to, options) {
  const cuts = chooseCutSamples(samples, from, to, options);
  const base = [];
  let start = from;
  for (const cut of cuts) {
    base.push({ start, end: cut.t, cutScore: cut.change });
    start = cut.t;
  }
  base.push({ start, end: to, cutScore: 0 });
  return base.flatMap((segment) => splitLongSegment(segment, samples, options))
    .filter((segment) => segment.end - segment.start >= Math.min(1, options.minSegmentSec));
}

function subtitleProximityScore(time, cues, options) {
  if (!cues.length) return 0;
  const maxDistance = options.maxAnchorDistanceSec;
  let best = Infinity;
  for (const cue of cues) {
    const distance = time >= cue.start && time <= cue.end
      ? 0
      : Math.min(Math.abs(time - cue.start), Math.abs(time - cue.end));
    best = Math.min(best, distance);
  }
  return best <= maxDistance ? 1 - best / maxDistance : 0;
}

function representativeScore(sample, segmentSamples, cues, selectedTimes, options) {
  const segmentRgb = averageRgb(segmentSamples);
  const representativeness = 1 - clamp(colorDistance(sample.rgb, segmentRgb) / 160, 0, 1);
  const lumas = segmentSamples.map((item) => item.luma);
  const segmentLuma = average(lumas);
  const lumaRepresentative = 1 - clamp(Math.abs(sample.luma - segmentLuma) / 0.35, 0, 1);
  const transitionPenalty = clamp(sample.change / Math.max(1, quantile(segmentSamples.map((item) => item.change), 0.9)), 0, 1);
  const subtitleScore = subtitleProximityScore(sample.t, cues, options);
  const diversity = selectedTimes.length
    ? clamp(Math.min(...selectedTimes.map((time) => Math.abs(time - sample.t))) / options.minSegmentSec, 0, 1)
    : 1;

  return (
    0.42 * sample.quality
    + 0.18 * representativeness
    + 0.12 * lumaRepresentative
    + 0.14 * subtitleScore
    + 0.08 * diversity
    - 0.12 * transitionPenalty
    - (sample.lowInformation ? 0.5 : 0)
  );
}

function pickSegmentKeyframe(segment, samples, cues, selectedTimes, options) {
  const duration = Math.max(0.001, segment.end - segment.start);
  const pad = Math.min(1.5, duration * 0.18);
  let segmentSamples = samplesInRange(samples, segment.start + pad, segment.end - pad);
  if (!segmentSamples.length) segmentSamples = samplesInRange(samples, segment.start, segment.end);
  if (!segmentSamples.length) return null;

  const useful = segmentSamples.filter((sample) => !sample.lowInformation);
  const candidates = useful.length ? useful : segmentSamples;
  const scored = candidates
    .map((sample) => ({
      time: sample.t,
      score: representativeScore(sample, segmentSamples, cues, selectedTimes, options),
      reason: "segment-representative",
    }))
    .sort((a, b) => b.score - a.score);

  return scored[0] || null;
}

function chooseMicroEventSamples(samples, from, to, options) {
  const threshold = visualChangeThreshold(samples, {
    ...options,
    visualEventSensitivity: options.microEventSensitivity,
  });
  const events = [];
  for (const sample of samples.slice(1)) {
    if (sample.change < threshold) continue;
    if (sample.t < from || sample.t > to) continue;
    const previous = events[events.length - 1];
    if (previous && sample.t - previous.t < options.minMicroKeyframeGapSec) {
      if (sample.change > previous.change) events[events.length - 1] = sample;
      continue;
    }
    events.push(sample);
  }
  return events;
}

function pickPostEventKeyframe(eventSample, samples, cues, selectedTimes, options) {
  const start = eventSample.t + Math.max(0.001, options.sampleStepSec * 0.5);
  const end = eventSample.t + options.microEventLookaheadSec;
  let candidates = samplesInRange(samples, start, end).filter((sample) => !sample.lowInformation);
  if (!candidates.length) candidates = samplesInRange(samples, eventSample.t, end);
  if (!candidates.length) return null;

  const scored = candidates
    .map((sample) => {
      const distancePenalty = clamp((sample.t - eventSample.t) / Math.max(0.001, options.microEventLookaheadSec), 0, 1);
      const diversity = selectedTimes.length
        ? clamp(Math.min(...selectedTimes.map((time) => Math.abs(time - sample.t))) / options.minMicroKeyframeGapSec, 0, 1)
        : 1;
      return {
        time: sample.t,
        score: 0.62 * sample.quality
          + 0.18 * subtitleProximityScore(sample.t, cues, options)
          + 0.14 * diversity
          - 0.12 * distancePenalty
          - (sample.lowInformation ? 0.5 : 0),
        reason: "micro-event",
        eventTime: eventSample.t,
      };
    })
    .sort((a, b) => b.score - a.score);

  return scored[0] || null;
}

function pickKeyframeSelections(samples, from, to, cues, rows, options) {
  const segments = buildVisualSegments(samples, from, to, options);
  const maxByRows = rows.length * Math.max(1, options.keyframesPerRow);
  const maxCount = Math.max(2, Number(options.maxKeyframes) || maxByRows);
  const selected = [];

  for (const segment of segments) {
    const pick = pickSegmentKeyframe(segment, samples, cues, selected.map((item) => item.time), options);
    if (pick) selected.push({ ...pick, segment });
  }

  const selectedTimes = () => selected.map((item) => item.time);
  for (const eventSample of chooseMicroEventSamples(samples, from, to, options)) {
    if (selected.length >= maxCount) break;
    if (selectedTimes().some((time) => Math.abs(time - eventSample.t) < options.minMicroKeyframeGapSec)) continue;
    const pick = pickPostEventKeyframe(eventSample, samples, cues, selectedTimes(), options);
    if (!pick) continue;
    if (selectedTimes().some((time) => Math.abs(time - pick.time) < options.minMicroKeyframeGapSec)) continue;
    selected.push({
      ...pick,
      segment: {
        start: eventSample.t,
        end: Math.min(to, eventSample.t + options.microEventLookaheadSec),
      },
    });
  }

  if (!selected.length && samples.length) {
    const best = samples
      .map((sample) => ({
        time: sample.t,
        score: sample.quality - (sample.lowInformation ? 0.5 : 0),
        reason: "fallback-quality",
        segment: { start: from, end: to },
      }))
      .sort((a, b) => b.score - a.score)[0];
    if (best) selected.push(best);
  }

  const deduped = [];
  for (const item of selected.sort((a, b) => b.score - a.score)) {
    if (deduped.length >= maxCount) break;
    if (deduped.every((existing) => Math.abs(existing.time - item.time) >= Math.max(1, options.minMicroKeyframeGapSec))) {
      deduped.push(item);
    }
  }

  return deduped.sort((a, b) => a.time - b.time);
}

async function captureKeyframes(video, keyframeSelections, cues, options) {
  const frameCanvas = document.createElement("canvas");
  frameCanvas.width = options.keyframeWidth * 2;
  frameCanvas.height = options.keyframeHeight * 2;
  const frameCtx = frameCanvas.getContext("2d");
  const frames = [];

  for (let i = 0; i < keyframeSelections.length; i += 1) {
    const selection = keyframeSelections[i];
    const time = typeof selection === "number" ? selection : selection.time;
    await seekVideo(video, time, options);
    frameCtx.clearRect(0, 0, frameCanvas.width, frameCanvas.height);
    drawContained(frameCtx, video, 0, 0, frameCanvas.width, frameCanvas.height);
    frames.push({
      id: `K${i + 1}`,
      time,
      score: typeof selection === "number" ? null : Number(selection.score.toFixed(3)),
      reason: typeof selection === "number" ? "fixed-time" : selection.reason,
      segment: typeof selection === "number" ? null : selection.segment,
      image: frameCanvas.toDataURL("image/jpeg", options.jpegQuality),
      subtitleAnchor: pickSubtitleAnchor(time, cues, options),
    });
  }

  return frames;
}

function loadImage(dataUrl) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("failed to load generated keyframe"));
    image.src = dataUrl;
  });
}

function roundRect(ctx, x, y, width, height, radius) {
  const r = Math.min(radius, width / 2, height / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + width, y, x + width, y + height, r);
  ctx.arcTo(x + width, y + height, x, y + height, r);
  ctx.arcTo(x, y + height, x, y, r);
  ctx.arcTo(x, y, x + width, y, r);
  ctx.closePath();
}

function rectPath(ctx, x, y, width, height) {
  ctx.beginPath();
  ctx.rect(x, y, width, height);
}

function drawFittedText(ctx, text, x, y, maxWidth) {
  let value = text;
  while (value.length > 4 && ctx.measureText(value).width > maxWidth) {
    value = `${value.slice(0, -4)}...`;
  }
  ctx.fillText(value, x, y);
}

function splitRows(from, to, options) {
  const rowSec = Math.max(10, Number(options.rowSec) || 30);
  const rows = [];
  for (let start = from; start < to - 0.001; start += rowSec) {
    rows.push({ start, end: Math.min(to, start + rowSec) });
  }
  return rows;
}

function drawColorBand(ctx, samples, rowStart, rowEnd, x, y, width, height) {
  rectPath(ctx, x, y, width, height);
  ctx.fillStyle = "#161616";
  ctx.fill();
  ctx.save();
  ctx.clip();

  const rowSamples = samplesInRange(samples, rowStart, rowEnd);
  if (!rowSamples.length) {
    ctx.fillStyle = "#222";
    ctx.fillRect(x, y, width, height);
    ctx.restore();
    return;
  }

  const duration = Math.max(0.001, rowEnd - rowStart);
  for (let i = 0; i < rowSamples.length; i += 1) {
    const sample = rowSamples[i];
    const next = rowSamples[i + 1];
    const t0 = clamp(sample.t, rowStart, rowEnd);
    const t1 = clamp(next ? next.t : sample.t + 1, rowStart, rowEnd);
    const sx = x + ((t0 - rowStart) / duration) * width;
    const sw = Math.max(1, ((t1 - t0) / duration) * width + 1);
    ctx.fillStyle = rgbCss(sample.rgb);
    ctx.fillRect(sx, y, sw, height);
  }

  ctx.restore();
  ctx.strokeStyle = "rgba(255,255,255,0.18)";
  ctx.lineWidth = 1;
  rectPath(ctx, x, y, width, height);
  ctx.stroke();
}

function drawSubtitleMarkers(ctx, cues, rowStart, rowEnd, x, y, width) {
  const duration = Math.max(0.001, rowEnd - rowStart);
  ctx.fillStyle = "rgba(255,255,255,0.48)";
  for (const cue of cues) {
    if (cue.end < rowStart || cue.start > rowEnd) continue;
    const start = clamp(cue.start, rowStart, rowEnd);
    const end = clamp(cue.end, rowStart, rowEnd);
    const sx = x + ((start - rowStart) / duration) * width;
    const ex = x + ((end - rowStart) / duration) * width;
    roundRect(ctx, sx, y, Math.max(4, ex - sx), 4, 2);
    ctx.fill();
  }
}

function compactRowsFromKeyframes(keyframes, from, to, options) {
  const perRow = Math.max(1, Math.floor(options.keyframesPerRow || 3));
  const rows = [];
  for (let i = 0; i < keyframes.length; i += perRow) {
    const frames = keyframes.slice(i, i + perRow);
    if (!frames.length) continue;
    rows.push({
      start: Math.max(from, frames[0].segment?.start ?? frames[0].time),
      end: Math.min(to, frames[frames.length - 1].segment?.end ?? frames[frames.length - 1].time),
      frames,
    });
  }
  return rows.length ? rows : [{ start: from, end: to, frames: [] }];
}

function layoutCompactRow(row, left, contentWidth, options) {
  const frames = row.frames || [];
  if (!frames.length) return [];

  const gapCount = Math.max(0, frames.length - 1);
  const pxPerSecond = Math.max(0.5, Number(options.bandPixelsPerSecond) || 2);
  const minBandWidth = Math.max(4, Number(options.minBandWidth) || 10);
  const bandWidths = [];
  for (let i = 0; i < gapCount; i += 1) {
    const duration = Math.max(0.001, frames[i + 1].time - frames[i].time);
    bandWidths.push(Math.max(minBandWidth, duration * pxPerSecond));
  }
  const totalFrameWidth = frames.length * options.keyframeWidth;
  const requested = totalFrameWidth + bandWidths.reduce((sum, width) => sum + width, 0);
  const available = contentWidth;
  let scale = requested > available ? available / requested : 1;
  const frameWidth = Math.max(96, options.keyframeWidth * scale);
  const frameHeight = Math.round(frameWidth * options.keyframeHeight / options.keyframeWidth);
  const scaledBands = bandWidths.map((width) => Math.max(minBandWidth * 0.6, width * scale));
  const totalWidth = frames.length * frameWidth + scaledBands.reduce((sum, width) => sum + width, 0);
  let x = left + Math.max(0, (available - totalWidth) / 2);

  return frames.map((frame, index) => {
    const item = {
      frame,
      frameX: x,
      frameY: 0,
      frameWidth,
      frameHeight,
      bandAfter: scaledBands[index] || 0,
    };
    x += frameWidth + (scaledBands[index] || 0);
    return item;
  });
}

async function renderSheet({ filmTitle, from, to, rows, samples, keyframes, cues, options }) {
  const width = options.sheetWidth;
  const left = 34;
  const right = 34;
  const contentWidth = width - left - right;
  const top = 28;
  const bottom = 18;
  const compactRows = compactRowsFromKeyframes(keyframes, from, to, options);
  const rowHeight = Math.max(
    options.rowHeight,
    options.keyframeHeight + 48,
  );
  const height = top + compactRows.length * rowHeight + bottom;
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");

  ctx.fillStyle = "#0b0b0c";
  ctx.fillRect(0, 0, width, height);

  ctx.fillStyle = "#f3f0e8";
  ctx.font = "600 22px system-ui, -apple-system, BlinkMacSystemFont, sans-serif";
  drawFittedText(ctx, filmTitle || "Film", left, 24, width - left - right - 180);
  ctx.fillStyle = "rgba(243,240,232,0.7)";
  ctx.font = "14px system-ui, -apple-system, BlinkMacSystemFont, sans-serif";
  ctx.textAlign = "right";
  ctx.fillText(`${fmtTime(from)}-${fmtTime(to)}`, width - right, 24);
  ctx.textAlign = "left";

  const images = await Promise.all(keyframes.map((frame) => loadImage(frame.image)));
  const frameById = new Map(keyframes.map((frame, index) => [frame.id, { ...frame, imageEl: images[index] }]));

  let globalFrameIndex = 0;
  for (let rowIndex = 0; rowIndex < compactRows.length; rowIndex += 1) {
    const row = compactRows[rowIndex];
    const rowTop = top + rowIndex * rowHeight;
    const frameY = rowTop + 26;
    const labelY = rowTop + 18;
    const rowItems = layoutCompactRow(row, left, contentWidth, options)
      .map((item) => ({ ...item, frameY }));
    const markerY = frameY + (rowItems[0]?.frameHeight || options.keyframeHeight) + 30;

    ctx.fillStyle = "rgba(255,255,255,0.08)";
    ctx.font = "12px system-ui, -apple-system, BlinkMacSystemFont, sans-serif";
    ctx.fillText(`${fmtTime(row.start)}-${fmtTime(row.end)}`, left, labelY);

    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(left, frameY + (rowItems[0]?.frameHeight || options.keyframeHeight) / 2);
    ctx.lineTo(left + contentWidth, frameY + (rowItems[0]?.frameHeight || options.keyframeHeight) / 2);
    ctx.stroke();

    for (let itemIndex = 0; itemIndex < rowItems.length - 1; itemIndex += 1) {
      const item = rowItems[itemIndex];
      const next = rowItems[itemIndex + 1];
      const gapX = item.frameX + item.frameWidth;
      const gapEnd = next.frameX;
      const gapWidth = gapEnd - gapX;
      if (gapWidth > 10) {
        drawColorBand(
          ctx,
          samples,
          item.frame.time,
          next.frame.time,
          gapX,
          frameY,
          gapWidth,
          item.frameHeight,
        );
      }
    }

    drawSubtitleMarkers(ctx, cues, row.start, row.end, left, markerY, contentWidth);

    for (const item of rowItems) {
      const { frame } = item;
      globalFrameIndex += 1;
      const frameImage = frameById.get(frame.id);
      const center = item.frameX + item.frameWidth / 2;
      const { frameX } = item;

      ctx.strokeStyle = "rgba(255,255,255,0.36)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(center, frameY - 8);
      ctx.lineTo(center, frameY + item.frameHeight + 8);
      ctx.stroke();

      ctx.save();
      rectPath(ctx, frameX, frameY, item.frameWidth, item.frameHeight);
      ctx.clip();
      drawContained(ctx, frameImage.imageEl, frameX, frameY, item.frameWidth, item.frameHeight);
      ctx.restore();

      ctx.strokeStyle = "rgba(255,255,255,0.42)";
      rectPath(ctx, frameX, frameY, item.frameWidth, item.frameHeight);
      ctx.stroke();

      const chipSample = sampleAtOrNear(samples, frame.time);
      const chipColor = chipSample?.rgb || [24, 24, 24];
      const chipText = `K${globalFrameIndex} ${fmtTime(frame.time)}`;
      ctx.font = "700 11px system-ui, -apple-system, BlinkMacSystemFont, sans-serif";
      const chipWidth = Math.min(item.frameWidth - 12, ctx.measureText(chipText).width + 14);
      ctx.fillStyle = rgbCss(chipColor);
      roundRect(ctx, frameX + 8, frameY + 8, chipWidth, 20, 5);
      ctx.fill();
      ctx.fillStyle = readableTextColor(chipColor);
      ctx.fillText(chipText, frameX + 15, frameY + 22);

      if (frame.subtitleAnchor) {
        ctx.fillStyle = "#f3f0e8";
        ctx.font = `600 ${options.anchorFontPx}px system-ui, -apple-system, BlinkMacSystemFont, sans-serif`;
        drawFittedText(
          ctx,
          `${shortCueLabel(frame.subtitleAnchor.id)} "${frame.subtitleAnchor.text}"`,
          frameX,
          frameY + item.frameHeight + 21,
          item.frameWidth,
        );
      }
    }

    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.beginPath();
    ctx.moveTo(left, rowTop + rowHeight - 4);
    ctx.lineTo(width - right, rowTop + rowHeight - 4);
    ctx.stroke();
  }

  return canvas.toDataURL("image/png");
}

function makeSidecar(cues, from, to) {
  const lines = cues.map((cue) => (
    `${cue.id} ${fmtTime(cue.start)}-${fmtTime(cue.end)}: ${cue.text}`
  ));
  return [
    `[字幕 ${fmtTime(from)}-${fmtTime(to)}]`,
    ...lines,
    "[/字幕]",
  ].join("\n");
}

async function fetchSubtitles(baseUrl, filmId, from, to) {
  const url = `${baseUrl}/sync/${encodeURIComponent(filmId)}?from=${from}&to=${to}`;
  const response = await fetch(url);
  if (!response.ok) throw new Error(`subtitle sync failed: ${response.status}`);
  const data = await response.json();
  return normalizeCues(data.subtitles || []);
}

function imagePartFromDataUrl(dataUrl) {
  const match = String(dataUrl).match(/^data:([^;]+);base64,(.+)$/);
  if (!match) return null;
  return { media_type: match[1], data: match[2] };
}

function cuesInRange(cues, from, to) {
  return cues.filter((cue) => cue.end >= from && cue.start <= to);
}

function compactSamples(samples) {
  return samples.map((sample) => ({
    t: sample.t,
    rgb: sample.rgb,
    delta: Number(sample.delta.toFixed(2)),
    change: Number(sample.change.toFixed(2)),
    luma: Number(sample.luma.toFixed(3)),
    quality: Number(sample.quality.toFixed(3)),
  }));
}

function selectionEnd(selection, fallback) {
  const segmentEnd = Number(selection?.segment?.end);
  if (Number.isFinite(segmentEnd)) return segmentEnd;
  const time = Number(selection?.time);
  return Number.isFinite(time) ? time : fallback;
}

function chooseAdaptiveEnd(from, candidateTo, selections, options) {
  const duration = candidateTo - from;
  if (duration <= 0) return candidateTo;

  const minEnd = Math.min(candidateTo, from + Math.max(0, Number(options.minSheetSec) || 0));
  const maxKeyframes = Math.max(2, Math.floor(Number(options.maxKeyframes) || DEFAULTS.maxKeyframes));
  const targetKeyframes = clamp(
    Math.floor(Number(options.targetKeyframesPerSheet) || Math.min(12, maxKeyframes)),
    2,
    maxKeyframes,
  );
  const sorted = selections
    .filter((selection) => Number.isFinite(selection.time))
    .sort((a, b) => a.time - b.time);

  if (!sorted.length) return candidateTo;

  let end = candidateTo;
  if (sorted.length >= maxKeyframes) {
    end = selectionEnd(sorted[maxKeyframes - 1], sorted[maxKeyframes - 1].time);
  } else if (sorted.length >= targetKeyframes) {
    end = selectionEnd(sorted[targetKeyframes - 1], sorted[targetKeyframes - 1].time);
  }

  if (end < minEnd) end = minEnd;
  const step = Math.max(0.25, Number(options.sampleStepSec) || 1);
  const aligned = Math.ceil(end / step) * step;
  return clamp(Number(aligned.toFixed(3)), minEnd, candidateTo);
}

export function createFilmMatineeVisualContext(filmMatineePlayer, initOptions = {}) {
  const options = { ...DEFAULTS, ...initOptions };
  options.baseUrl = stripBaseUrl(options.baseUrl);
  const cache = new Map();

  async function buildWindowSheet(buildOptions = {}) {
    const opts = { ...options, ...buildOptions };
    opts.baseUrl = stripBaseUrl(opts.baseUrl);

    const st = opts.status || filmMatineePlayer.status();
    if (!st?.id) return null;

    const currentTime = Number(opts.currentTime ?? st.ts ?? 0);
    const to = Number(opts.to ?? currentTime);
    const from = Math.max(0, Number(opts.from ?? (to - opts.windowSec)));
    const cacheKey = [
      st.id,
      from.toFixed(1),
      to.toFixed(1),
      opts.rowSec,
      opts.sampleStepSec,
      opts.sheetWidth,
    ].join(":");
    if (cache.has(cacheKey)) return cache.get(cacheKey);

    const streamUrl = `${opts.baseUrl}/stream/${encodeURIComponent(st.id)}`;
    const cues = await fetchSubtitles(opts.baseUrl, st.id, from, to);
    const sampler = await createSamplerVideo(streamUrl, opts);

    try {
      const samples = await sampleVisualFrames(sampler, from, to, opts);
      const rows = splitRows(from, to, opts);
      const keyframeSelections = pickKeyframeSelections(samples, from, to, cues, rows, opts);
      const keyframes = await captureKeyframes(sampler, keyframeSelections, cues, opts);
      const sheetDataUrl = await renderSheet({
        filmTitle: st.title || st.id,
        from,
        to,
        rows,
        samples,
        keyframes,
        cues,
        options: opts,
      });

      const result = {
        mode: "window",
        filmId: st.id,
        title: st.title || st.id,
        timeRange: [from, to],
        nextFrom: null,
        duration: to - from,
        sheet: {
          dataUrl: sheetDataUrl,
          ...imagePartFromDataUrl(sheetDataUrl),
        },
        sidecar: makeSidecar(cues, from, to),
        subtitles: cues,
        keyframes,
        samples: compactSamples(samples),
      };
      cache.set(cacheKey, result);
      return result;
    } finally {
      sampler.removeAttribute("src");
      sampler.load();
      sampler.remove();
    }
  }

  async function buildAdaptiveSheet(buildOptions = {}) {
    const opts = { ...options, ...buildOptions };
    opts.baseUrl = stripBaseUrl(opts.baseUrl);

    const st = opts.status || filmMatineePlayer.status();
    if (!st?.id) return null;

    const currentTime = Number(opts.currentTime ?? st.ts ?? 0);
    const requestedFrom = Number(opts.from);
    let from = Math.max(0, Number.isFinite(requestedFrom)
      ? requestedFrom
      : currentTime - opts.windowSec);
    const streamUrl = `${opts.baseUrl}/stream/${encodeURIComponent(st.id)}`;
    const sampler = await createSamplerVideo(streamUrl, opts);

    try {
      const duration = Number.isFinite(sampler.duration) ? sampler.duration : null;
      if (duration !== null) from = clamp(from, 0, duration);

      const explicitTo = Number(opts.to);
      const maxSheetSec = Math.max(1, Number(opts.maxSheetSec) || DEFAULTS.maxSheetSec);
      let candidateTo = Number.isFinite(explicitTo) ? explicitTo : from + maxSheetSec;
      if (duration !== null) candidateTo = Math.min(candidateTo, duration);
      candidateTo = Math.max(from, candidateTo);

      const cacheKey = [
        "adaptive",
        st.id,
        from.toFixed(1),
        candidateTo.toFixed(1),
        opts.targetKeyframesPerSheet,
        opts.maxKeyframes,
        opts.sampleStepSec,
        opts.sheetWidth,
      ].join(":");
      if (cache.has(cacheKey)) return cache.get(cacheKey);

      const candidateCues = await fetchSubtitles(opts.baseUrl, st.id, from, candidateTo);
      const candidateSamples = await sampleVisualFrames(sampler, from, candidateTo, opts);
      const analysisRows = splitRows(from, candidateTo, opts);
      const analysisMaxKeyframes = Math.max(
        Number(opts.maxKeyframes) || 0,
        (Number(opts.targetKeyframesPerSheet) || DEFAULTS.targetKeyframesPerSheet) + 8,
        Math.ceil((candidateTo - from) / Math.max(1, Number(opts.maxSegmentSec) || DEFAULTS.maxSegmentSec))
          + (Number(opts.targetKeyframesPerSheet) || DEFAULTS.targetKeyframesPerSheet),
      );
      const analysisOptions = {
        ...opts,
        maxKeyframes: analysisMaxKeyframes,
      };
      const candidateSelections = pickKeyframeSelections(
        candidateSamples,
        from,
        candidateTo,
        candidateCues,
        analysisRows,
        analysisOptions,
      );
      const to = chooseAdaptiveEnd(from, candidateTo, candidateSelections, opts);
      const cues = cuesInRange(candidateCues, from, to);
      const samples = samplesInRange(candidateSamples, from, to);
      const rows = splitRows(from, to, opts);
      const keyframeSelections = pickKeyframeSelections(samples, from, to, cues, rows, opts);
      const keyframes = await captureKeyframes(sampler, keyframeSelections, cues, opts);
      const sheetDataUrl = await renderSheet({
        filmTitle: st.title || st.id,
        from,
        to,
        rows,
        samples,
        keyframes,
        cues,
        options: opts,
      });

      const hasMore = duration !== null
        ? to < duration - 0.001
        : to < candidateTo - 0.001;
      const result = {
        mode: "adaptive",
        filmId: st.id,
        title: st.title || st.id,
        timeRange: [from, to],
        candidateTimeRange: [from, candidateTo],
        nextFrom: hasMore ? to : null,
        duration: to - from,
        sheet: {
          dataUrl: sheetDataUrl,
          ...imagePartFromDataUrl(sheetDataUrl),
        },
        sidecar: makeSidecar(cues, from, to),
        subtitles: cues,
        keyframes,
        samples: compactSamples(samples),
        adaptive: {
          targetKeyframes: Math.min(
            Math.max(2, Number(opts.targetKeyframesPerSheet) || DEFAULTS.targetKeyframesPerSheet),
            Math.max(2, Number(opts.maxKeyframes) || DEFAULTS.maxKeyframes),
          ),
          maxKeyframes: Math.max(2, Number(opts.maxKeyframes) || DEFAULTS.maxKeyframes),
          candidateKeyframes: candidateSelections.length,
          candidateDuration: Number((candidateTo - from).toFixed(3)),
        },
      };
      cache.set(cacheKey, result);
      return result;
    } finally {
      sampler.removeAttribute("src");
      sampler.load();
      sampler.remove();
    }
  }

  async function collect(buildOptions = {}) {
    const st = filmMatineePlayer.status();
    if (!st?.id) return { textPrefix: "", images: [], visual: null };

    const useAdaptive = Boolean(buildOptions.adaptive ?? options.adaptive);
    const visual = useAdaptive
      ? await buildAdaptiveSheet({ ...buildOptions, status: st })
      : await buildWindowSheet({ ...buildOptions, status: st });
    if (!visual) return { textPrefix: "", images: [], visual: null };

    const unitLabel = visual.mode === "adaptive" ? "视觉单元" : "视觉窗口";
    const lines = [
      `[正在和你看 ${visual.title}，${unitLabel} ${fmtTime(visual.timeRange[0])}-${fmtTime(visual.timeRange[1])}]`,
      "附图是 film-matinee sheet：按从左到右、从上到下阅读；关键帧之间的色带压缩经过的时间，越长只代表时间越久；关键帧下方短句只是字幕锚点，完整字幕见 sidecar。",
      visual.sidecar,
      "",
    ];
    const images = [];
    if (visual.sheet?.media_type && visual.sheet?.data) {
      images.push({ label: "film-matinee-sheet", media_type: visual.sheet.media_type, data: visual.sheet.data });
    }

    if (buildOptions.includeCurrentFrame ?? options.includeCurrentFrame) {
      const currentFrame = filmMatineePlayer.snapshot?.({
        width: options.currentFrameWidth,
        quality: options.jpegQuality,
      });
      const image = currentFrame ? imagePartFromDataUrl(currentFrame) : null;
      if (image) images.push({ label: "current-frame", ...image });
    }

    return {
      textPrefix: lines.join("\n"),
      images,
      visual,
    };
  }

  return {
    buildWindowSheet,
    buildAdaptiveSheet,
    collect,
    clearCache() {
      cache.clear();
    },
  };
}
