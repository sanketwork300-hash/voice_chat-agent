"use client";

import Link from "next/link";
import { useEffect, useRef } from "react";

import { ActivityList, OUTCOME_LABEL, PROGRESS_LABEL, SourceList } from "@/components/Chat";
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

const STATUS_DOT: Record<CallStatus, string> = {
  idle: "bg-border",
  connecting: "bg-warning animate-pulse",
  listening: "bg-success",
  thinking: "bg-accent animate-pulse",
  speaking: "bg-accent",
  error: "bg-danger",
};

export default function VoicePage() {
  const { status, conversationId, troubleshooting, turns, error, start, stop } = useVoiceClient();
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [turns, status]);

  const inCall = status !== "idle" && status !== "error";
  const progress = troubleshooting ? PROGRESS_LABEL[troubleshooting.status] : undefined;

  return (
    <div className="mx-auto flex h-dvh w-full max-w-3xl flex-col px-4">
      <header className="flex items-center justify-between gap-4 border-b border-border py-4">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold">Cloud Support Assistant · Voice</h1>
          <p className="flex flex-wrap items-center gap-x-2 text-sm text-muted" role="status">
            <span className={`inline-block size-2 rounded-full ${STATUS_DOT[status]}`} aria-hidden />
            <span data-testid="call-status">{STATUS_LABEL[status]}</span>
            {progress && <span data-testid="progress">· {progress}</span>}
            {SHOW_DEBUG && conversationId && (
              <Link href={`/debug?c=${conversationId}`} target="_blank" className="text-xs underline">
                debug
              </Link>
            )}
          </p>
        </div>
        <Link href="/" className="shrink-0 text-sm underline">
          Switch to chat
        </Link>
      </header>

      <main className="flex-1 overflow-y-auto py-6" aria-live="polite">
        {turns.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
            <p className="max-w-md text-muted">
              Start a call and describe a problem with EC2, S3, Lambda, IAM, or CloudWatch, or ask how something in
              AWS works. I&apos;ll answer out loud; pause when you&apos;re done speaking, and feel free to talk over me.
            </p>
            {inCall && <p className="text-sm text-muted">Go ahead, I&apos;m listening.</p>}
          </div>
        ) : (
          <ol className="flex flex-col gap-4">
            {turns.map((turn) => (
              <TurnItem key={turn.id} turn={turn} />
            ))}
            {status === "thinking" && turns.at(-1)?.role === "user" && (
              <li className="self-start rounded-2xl bg-surface px-4 py-3 text-muted">
                <span className="animate-pulse">Thinking…</span>
              </li>
            )}
          </ol>
        )}
        {error && (
          <p className="mt-4 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-sm" role="alert">
            {error}
          </p>
        )}
        <div ref={endRef} />
      </main>

      <footer className="flex items-center justify-center gap-4 border-t border-border py-5">
        {inCall ? (
          <button
            type="button"
            onClick={stop}
            className="rounded-full border border-danger px-6 py-3 font-medium text-danger hover:bg-danger/10"
          >
            End call
          </button>
        ) : (
          <button
            type="button"
            onClick={start}
            className="rounded-full bg-accent px-6 py-3 font-medium text-on-accent"
          >
            {status === "error" ? "Try again" : "Start call"}
          </button>
        )}
      </footer>
    </div>
  );
}

function TurnItem({ turn }: { turn: VoiceTurn }) {
  if (turn.role === "user") {
    return <li className="max-w-[85%] self-end rounded-2xl bg-accent px-4 py-3 text-on-accent">{turn.text}</li>;
  }
  return (
    <li className="flex max-w-[85%] flex-col gap-2 self-start">
      {turn.activities && turn.activities.length > 0 && <ActivityList activities={turn.activities} />}
      {turn.text && (
        <div className="rounded-2xl bg-surface px-4 py-3">
          {turn.text}
          {turn.interrupted && <span className="ml-2 text-sm text-muted">— interrupted</span>}
        </div>
      )}
      {!turn.text && turn.interrupted && <p className="px-2 text-sm text-muted">Interrupted before replying.</p>}
      {turn.sources && <SourceList sources={turn.sources} />}
      {turn.action && (
        <div className="rounded-xl border border-accent/50 px-4 py-3" aria-label="Proposed change">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted">Proposed change</p>
          <p className="mt-1">{turn.action.summary}</p>
          <p className="mt-2 text-sm text-muted">
            {turn.action.outcome ? OUTCOME_LABEL[turn.action.outcome] : "Say “yes” to apply it or “no” to leave it."}
          </p>
        </div>
      )}
      {SHOW_DEBUG && turn.latency && (
        <p className="px-2 font-mono text-xs text-muted">
          {turn.latency.stt !== undefined && <>transcribe {turn.latency.stt.toFixed(1)} s · </>}
          {turn.latency.first_token !== undefined && <>first word {turn.latency.first_token.toFixed(1)} s · </>}
          {turn.latency.first_audio !== undefined && <>first audio {turn.latency.first_audio.toFixed(1)} s · </>}
          {turn.latency.total !== undefined && <>total {turn.latency.total.toFixed(1)} s</>}
        </p>
      )}
    </li>
  );
}
