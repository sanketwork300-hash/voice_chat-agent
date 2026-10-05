"use client";

import Link from "next/link";
import { CSSProperties, useEffect, useRef } from "react";

import AppHeader, { StatusLine, Tone } from "@/components/AppHeader";
import {
  ActionCard,
  ActivityList,
  PROGRESS_LABEL,
  SourceList,
  ThinkingBubble,
  assistantBubble,
  userBubble,
} from "@/components/Chat";
import { CallStatus, VoiceTurn, useVoiceClient } from "@/lib/useVoiceClient";

const SHOW_DEBUG = process.env.NEXT_PUBLIC_DEBUG_UI === "1";

const STATUS_LABEL: Record<CallStatus, string> = {
  idle: "Not in a call",
  connecting: "Connecting",
  listening: "Listening",
  thinking: "Working on it",
  speaking: "Speaking",
  error: "Problem",
};

const STATUS_TONE: Record<CallStatus, Tone> = {
  idle: "off",
  connecting: "warn",
  listening: "ok",
  thinking: "busy",
  speaking: "busy",
  error: "bad",
};

const WAVE_BARS = 24;

export default function VoicePage() {
  const { status, conversationId, troubleshooting, turns, error, start, stop, level } = useVoiceClient();
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (turns.length > 0) endRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [turns, status]);

  const inCall = status !== "idle" && status !== "error";
  const progress = troubleshooting ? PROGRESS_LABEL[troubleshooting.status] : undefined;
  const compact = turns.length > 0;

  return (
    <div className="flex h-dvh w-full flex-col px-3 sm:px-4">
      <AppHeader
        title="Cloud Support · Voice"
        status={
          <StatusLine tone={STATUS_TONE[status]} live={inCall}>
            <span data-testid="call-status">{STATUS_LABEL[status]}</span>
            {progress && <span data-testid="progress">· {progress}</span>}
            {SHOW_DEBUG && conversationId && (
              <Link href={`/debug?c=${conversationId}`} target="_blank" className="underline hover:text-accent">
                debug
              </Link>
            )}
          </StatusLine>
        }
      />

      <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col overflow-y-auto" aria-live="polite">
        <section
          className={`flex flex-col items-center text-center transition-all duration-700 ${
            compact ? "gap-3 py-5" : "flex-1 justify-center gap-6 py-10"
          }`}
          aria-label="Call"
        >
          <VoiceOrb status={status} level={level} compact={compact} />
          <p className={`rise font-medium tracking-tight ${compact ? "text-sm" : "text-2xl"}`} style={{ "--i": 2 } as CSSProperties}>
            {STATUS_LABEL[status]}
          </p>
          {!compact && (
            <p className="rise max-w-md text-pretty text-muted" style={{ "--i": 3 } as CSSProperties}>
              {inCall
                ? "Go ahead, I'm listening."
                : "Start a call and describe a problem with EC2, S3, Lambda, IAM, or CloudWatch, or ask how something in AWS works. I'll answer out loud; pause when you're done speaking, and feel free to talk over me."}
            </p>
          )}
        </section>

        {compact && (
          <ol className="flex flex-col gap-5 pb-6">
            {turns.map((turn) => (
              <TurnItem key={turn.id} turn={turn} />
            ))}
            {status === "thinking" && turns.at(-1)?.role === "user" && (
              <li className="msg-in-left self-start">
                <ThinkingBubble />
              </li>
            )}
          </ol>
        )}
        {error && (
          <p className="msg-in-left mb-4 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-sm" role="alert">
            {error}
          </p>
        )}
        <div ref={endRef} />
      </main>

      <footer className="flex items-center justify-center pb-6 pt-3">
        {inCall ? (
          <button
            type="button"
            onClick={stop}
            className="flex items-center gap-2 rounded-full border border-danger/60 bg-danger/10 px-6 py-3 font-medium text-danger backdrop-blur transition hover:bg-danger/20 active:scale-95"
          >
            <svg viewBox="0 0 24 24" className="size-4" fill="currentColor" aria-hidden>
              <rect x="6" y="6" width="12" height="12" rx="2" />
            </svg>
            End call
          </button>
        ) : (
          <button
            type="button"
            onClick={start}
            className="rise flex items-center gap-2 rounded-full bg-gradient-to-r from-accent to-accent-2 px-7 py-3 font-medium text-on-accent shadow-[0_0_40px_-8px_var(--glow)] transition hover:brightness-110 active:scale-95"
            style={{ "--i": 4 } as CSSProperties}
          >
            <svg viewBox="0 0 24 24" className="size-4" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden>
              <rect x="9" y="3" width="6" height="11" rx="3" />
              <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
            </svg>
            {status === "error" ? "Try again" : "Start call"}
          </button>
        )}
      </footer>
    </div>
  );
}

/** The call's centrepiece: an orb whose glow and rings follow whoever is speaking. */
function VoiceOrb({
  status,
  level,
  compact,
}: {
  status: CallStatus;
  level: (side: "local" | "remote") => number;
  compact: boolean;
}) {
  const orbRef = useRef<HTMLDivElement>(null);
  const barsRef = useRef<(HTMLSpanElement | null)[]>([]);
  const live = status === "listening" || status === "speaking";

  useEffect(() => {
    if (!live || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let frame = 0;
    let smooth = 0;
    let phase = 0;
    const tick = () => {
      const target = Math.max(level("local"), level("remote"));
      smooth += (target - smooth) * 0.25;
      phase += 0.15;
      orbRef.current?.style.setProperty("--level", smooth.toFixed(3));
      barsRef.current.forEach((bar, i) => {
        const shape = 0.35 + 0.65 * Math.abs(Math.sin(phase + i * 0.55)) * Math.sin((Math.PI * (i + 1)) / (WAVE_BARS + 1));
        bar?.style.setProperty("--h", (smooth * shape).toFixed(3));
      });
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    const orb = orbRef.current;
    const bars = barsRef.current;
    return () => {
      cancelAnimationFrame(frame);
      orb?.style.setProperty("--level", "0");
      bars.forEach((bar) => bar?.style.setProperty("--h", "0"));
    };
  }, [live, level]);

  return (
    <div className="rise flex flex-col items-center gap-4" style={{ "--i": 1 } as CSSProperties} aria-hidden>
      <div ref={orbRef} className={`orb ${compact ? "w-20" : "w-48 sm:w-56"}`} data-state={status}>
        <div className="orb-arc" />
        <div className="orb-ring" />
        <div className="orb-core" />
      </div>
      <div className={`wave flex h-8 items-center gap-[3px] transition-opacity duration-500 ${live ? "opacity-100" : "opacity-0"} ${compact ? "hidden" : ""}`}>
        {Array.from({ length: WAVE_BARS }, (_, i) => (
          <span key={i} ref={(el) => void (barsRef.current[i] = el)} />
        ))}
      </div>
    </div>
  );
}

function TurnItem({ turn }: { turn: VoiceTurn }) {
  if (turn.role === "user") {
    return <li className={`msg-in-right ${userBubble}`}>{turn.text}</li>;
  }
  return (
    <li className="msg-in-left flex max-w-[88%] flex-col gap-2 self-start">
      {turn.activities && turn.activities.length > 0 && <ActivityList activities={turn.activities} />}
      {turn.text && (
        <div className={assistantBubble}>
          {turn.text}
          {turn.interrupted && <span className="ml-2 text-sm text-muted">— interrupted</span>}
        </div>
      )}
      {!turn.text && turn.interrupted && <p className="px-2 text-sm text-muted">Interrupted before replying.</p>}
      {turn.sources && <SourceList sources={turn.sources} />}
      {turn.action && <ActionCard action={turn.action} canDecide={false} />}
      {SHOW_DEBUG && turn.latency && (
        <p className="px-2 font-mono text-[11px] text-muted">
          {turn.latency.stt !== undefined && <>transcribe {turn.latency.stt.toFixed(1)} s · </>}
          {turn.latency.first_token !== undefined && <>first word {turn.latency.first_token.toFixed(1)} s · </>}
          {turn.latency.first_audio !== undefined && <>first audio {turn.latency.first_audio.toFixed(1)} s · </>}
          {turn.latency.total !== undefined && <>total {turn.latency.total.toFixed(1)} s</>}
        </p>
      )}
    </li>
  );
}
