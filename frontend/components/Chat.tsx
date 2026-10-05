"use client";

import Link from "next/link";
import { CSSProperties, FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";

import AppHeader, { StatusLine, Tone, ghostButton } from "@/components/AppHeader";
import { Activity, ChatMessage, ConnectionStatus, PendingAction, Sources, useChatSocket } from "@/lib/useChatSocket";

const MAX_MESSAGE_CHARS = 2000;
const SHOW_DEBUG = process.env.NEXT_PUBLIC_DEBUG_UI === "1";

const STATUS_LABEL: Record<ConnectionStatus, string> = {
  connecting: "Connecting",
  idle: "Online",
  thinking: "Working",
  disconnected: "Reconnecting",
};

const STATUS_TONE: Record<ConnectionStatus, Tone> = {
  connecting: "warn",
  idle: "ok",
  thinking: "busy",
  disconnected: "bad",
};

// The agent's conversation status, in words a customer would use.
export const PROGRESS_LABEL: Record<string, string> = {
  COLLECTING_INFORMATION: "Collecting details",
  DIAGNOSING: "Diagnosing",
  WAITING_FOR_CONFIRMATION: "Waiting for your confirmation",
  EXECUTING: "Applying the fix",
  VERIFYING: "Verifying the fix",
  RESOLVED: "Resolved",
  ESCALATED: "Handed to a support engineer",
};

export const OUTCOME_LABEL: Record<NonNullable<PendingAction["outcome"]>, string> = {
  executed: "Confirmed and applied",
  declined: "Cancelled — nothing was changed",
  cancelled: "Withdrawn — nothing was changed",
  expired: "Expired — nothing was changed",
  failed: "Could not be applied",
  refused: "Not allowed — nothing was changed",
};

const SUGGESTIONS = [
  { service: "EC2", text: "I can't SSH into my EC2 server" },
  { service: "Lambda", text: "My Lambda function demo-report-generator keeps timing out" },
  { service: "S3", text: "What are the rules for naming an S3 bucket?" },
];

const SERVICES = ["EC2", "S3", "Lambda", "IAM", "CloudWatch"];

export default function Chat() {
  const { status, model, conversationId, troubleshooting, messages, send, decide, escalate, clear } = useChatSocket();
  const [draft, setDraft] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (messages.length > 0) endRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [messages]);

  const idle = status === "idle";
  const canSend = idle && draft.trim().length > 0;
  const waitingForReply = status === "thinking" && !messages.at(-1)?.streaming;
  const progress = troubleshooting ? PROGRESS_LABEL[troubleshooting.status] : undefined;
  const escalated = troubleshooting?.status === "ESCALATED";

  function submit(text: string) {
    if (!idle || !text.trim()) return;
    send(text.trim());
    setDraft("");
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    submit(draft);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit(draft);
    }
  }

  return (
    <div className="flex h-dvh w-full flex-col px-3 sm:px-4">
      <AppHeader
        status={
          <StatusLine tone={STATUS_TONE[status]} live={status === "thinking"}>
            {STATUS_LABEL[status]}
            {progress && <span data-testid="progress">· {progress}</span>}
            {SHOW_DEBUG && model && <span className="font-mono text-[11px]">· {model}</span>}
            {SHOW_DEBUG && conversationId && (
              <Link href={`/debug?c=${conversationId}`} target="_blank" className="underline hover:text-accent">
                debug
              </Link>
            )}
          </StatusLine>
        }
        actions={
          <>
            <button
              type="button"
              onClick={escalate}
              disabled={!idle || messages.length === 0 || escalated}
              title="Hand this conversation to a support engineer"
              className={ghostButton}
            >
              Talk to a human
            </button>
            <button type="button" onClick={clear} disabled={!idle || messages.length === 0} className={ghostButton}>
              Clear conversation
            </button>
          </>
        }
      />

      <main className="mx-auto w-full max-w-3xl flex-1 overflow-y-auto py-6" aria-live="polite">
        {messages.length === 0 ? (
          <div className="flex min-h-full flex-col items-center justify-center gap-7 pb-8 text-center">
            <div className="rise relative size-20" style={{ "--i": 1 } as CSSProperties} aria-hidden>
              <div className="orb w-20" data-state="idle">
                <div className="orb-ring" />
                <div className="orb-core" style={{ filter: "none" }} />
              </div>
            </div>
            <div className="rise flex flex-col gap-3" style={{ "--i": 2 } as CSSProperties}>
              <h2 className="text-balance text-3xl font-semibold tracking-tight sm:text-4xl">
                What&apos;s going wrong in your AWS account?
              </h2>
              <p className="mx-auto max-w-md text-pretty text-muted">
                Describe the problem and I&apos;ll check your resources and help you fix it, or ask how something in
                AWS works.
              </p>
              <ul className="mt-1 flex flex-wrap justify-center gap-1.5" aria-label="Supported services">
                {SERVICES.map((service) => (
                  <li key={service} className="rounded-md border border-border px-2 py-0.5 text-xs text-muted">
                    {service}
                  </li>
                ))}
              </ul>
            </div>
            <div className="grid w-full max-w-2xl gap-2 sm:grid-cols-3">
              {SUGGESTIONS.map((suggestion, index) => (
                <button
                  key={suggestion.text}
                  type="button"
                  onClick={() => submit(suggestion.text)}
                  disabled={!idle}
                  style={{ "--i": 3 + index } as CSSProperties}
                  className="rise glass group flex flex-col gap-2 rounded-xl p-3.5 text-left text-sm transition hover:-translate-y-0.5 hover:border-accent/50 disabled:opacity-40"
                >
                  <span className="text-xs font-medium text-accent">{suggestion.service}</span>
                  <span className="text-foreground/90 group-hover:text-foreground">{suggestion.text}</span>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <ol className="flex flex-col gap-5">
            {messages.map((message) => (
              <MessageItem key={message.id} message={message} canDecide={idle} onDecide={decide} />
            ))}
            {waitingForReply && (
              <li className="msg-in-left self-start">
                <ThinkingBubble />
              </li>
            )}
          </ol>
        )}
        <div ref={endRef} />
      </main>

      <form onSubmit={onSubmit} className="mx-auto w-full max-w-3xl pb-4">
        <div className="glass flex items-end gap-2 rounded-2xl p-2 transition focus-within:border-accent/60 focus-within:shadow-[0_0_30px_-10px_var(--glow)]">
          <label htmlFor="message" className="sr-only">
            Message
          </label>
          <textarea
            id="message"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            maxLength={MAX_MESSAGE_CHARS}
            rows={2}
            placeholder="Describe the problem, or ask a question…"
            className="flex-1 resize-none bg-transparent px-3 py-2 outline-none placeholder:text-muted/70"
          />
          <button
            type="submit"
            disabled={!canSend}
            aria-label="Send"
            className="grid size-10 shrink-0 place-items-center rounded-xl bg-accent text-on-accent shadow-[0_0_24px_-6px_var(--glow)] transition hover:brightness-110 active:scale-95 disabled:cursor-not-allowed disabled:opacity-30 disabled:shadow-none"
          >
            <svg viewBox="0 0 24 24" className="size-4" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M12 19V5M5 12l7-7 7 7" />
            </svg>
          </button>
        </div>
        <p className="mt-1.5 px-2 text-[11px] text-muted">
          Enter to send, Shift+Enter for a new line. Nothing in your account changes until you confirm it.
          {draft.length > MAX_MESSAGE_CHARS * 0.9 && (
            <span className="float-right tabular-nums">
              {draft.length} / {MAX_MESSAGE_CHARS}
            </span>
          )}
        </p>
      </form>
    </div>
  );
}

export function ThinkingBubble() {
  return (
    <div className="glass flex items-center gap-3 rounded-2xl rounded-tl-md px-4 py-3">
      <span className="thinking-dots flex items-center gap-1" aria-hidden>
        <span />
        <span />
        <span />
      </span>
      <span className="shimmer-text text-sm">Thinking…</span>
    </div>
  );
}

function MessageItem({
  message,
  canDecide,
  onDecide,
}: {
  message: ChatMessage;
  canDecide: boolean;
  onDecide: (actionId: string, confirm: boolean) => void;
}) {
  if (message.role === "notice") {
    return (
      <li className="msg-in-left self-center rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-sm" role="alert">
        {message.text}
      </li>
    );
  }
  if (message.role === "user") {
    return <li className={`msg-in-right ${userBubble} whitespace-pre-wrap`}>{message.text}</li>;
  }
  return (
    <li className="msg-in-left flex max-w-[88%] flex-col gap-2 self-start">
      {message.activities && message.activities.length > 0 && <ActivityList activities={message.activities} />}
      {message.text && (
        <div className={`markdown ${assistantBubble} ${message.streaming ? "caret" : ""}`}>
          <Markdown>{message.text}</Markdown>
        </div>
      )}
      {message.sources && <SourceList sources={message.sources} />}
      {message.action && <ActionCard action={message.action} canDecide={canDecide} onDecide={onDecide} />}
      {SHOW_DEBUG && message.latency && (
        <p className="px-2 font-mono text-[11px] text-muted">
          first token {(message.latency.first_token / 1000).toFixed(1)} s · total{" "}
          {(message.latency.total / 1000).toFixed(1)} s
        </p>
      )}
    </li>
  );
}

export const userBubble =
  "max-w-[85%] self-end rounded-2xl rounded-tr-md bg-gradient-to-br from-accent to-accent-2 px-4 py-3 text-on-accent shadow-[0_8px_30px_-12px_var(--glow)]";

export const assistantBubble = "glass rounded-2xl rounded-tl-md px-4 py-3 leading-relaxed";

const ACTIVITY_MARK: Record<Activity["state"], { symbol: string; className: string; label: string }> = {
  running: { symbol: "", className: "", label: "In progress" },
  ok: { symbol: "✓", className: "text-success bg-success/15", label: "Done" },
  warn: { symbol: "!", className: "text-warning bg-warning/15", label: "Attention" },
  error: { symbol: "✕", className: "text-danger bg-danger/15", label: "Failed" },
};

export function ActivityList({ activities }: { activities: Activity[] }) {
  return (
    <ul className="flex flex-col gap-1.5 rounded-xl border border-border bg-surface-2/40 px-3.5 py-3 text-sm" aria-label="Checks">
      {activities.map((activity) => {
        const mark = ACTIVITY_MARK[activity.state];
        return (
          <li key={activity.id} className="msg-in-left flex items-start gap-2.5">
            <span className="mt-0.5 grid size-4 shrink-0 place-items-center" aria-label={mark.label}>
              {activity.state === "running" ? (
                <span className="spinner" />
              ) : (
                <span className={`grid size-4 place-items-center rounded-full text-[10px] font-bold ${mark.className}`}>
                  {mark.symbol}
                </span>
              )}
            </span>
            <span>
              <span className={activity.state === "running" ? "shimmer-text" : "text-muted"}>{activity.label}</span>
              {activity.detail && <span> — {activity.detail}</span>}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

export function SourceList({ sources }: { sources: Sources }) {
  // For an answer, list the pages it cites (all retrieved pages if it cited none by number).
  const cited = sources.documents.filter((d) => d.cited);
  const documents = sources.kind === "cited" && cited.length > 0 ? cited : sources.documents;
  const year = documents[0]?.source_date.slice(0, 4);
  return (
    <div className="px-1 text-sm" aria-label="Sources">
      <p className="text-xs font-medium text-muted">{sources.kind === "cited" ? "Sources" : "Related documentation"}</p>
      <ol className="mt-1.5 flex flex-wrap gap-1.5">
        {documents.map((doc) => (
          <li key={doc.n}>
            <a
              href={doc.url}
              target="_blank"
              rel="noreferrer"
              className="flex items-center gap-1.5 rounded-lg border border-border px-2 py-1 text-xs transition hover:border-accent/60 hover:text-accent"
            >
              <span className="tabular-nums text-accent">{doc.n}</span>
              {doc.section}
            </a>
          </li>
        ))}
      </ol>
      <p className="mt-1.5 text-[11px] text-muted">AWS documentation, snapshot from {year}.</p>
    </div>
  );
}

export function ActionCard({
  action,
  canDecide,
  onDecide,
}: {
  action: PendingAction;
  canDecide: boolean;
  onDecide?: (actionId: string, confirm: boolean) => void;
}) {
  const waiting = !action.outcome;
  const applied = action.outcome === "executed";
  return (
    <div
      className={`msg-in-left rounded-xl px-4 py-3.5 ${waiting ? "beam glass" : "border border-border bg-surface-2/40"}`}
      aria-label="Proposed change"
    >
      <p className="flex items-center gap-2 text-xs font-medium text-accent">
        <svg viewBox="0 0 24 24" className="size-3.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
          <path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z" />
        </svg>
        Proposed change
      </p>
      <p className="mt-1.5">{action.summary}</p>
      {action.outcome ? (
        <p className={`mt-2 text-sm ${applied ? "text-success" : "text-muted"}`}>
          {applied ? "✓ " : ""}
          {OUTCOME_LABEL[action.outcome]}
        </p>
      ) : onDecide ? (
        <div className="mt-3 flex gap-2">
          <button
            type="button"
            disabled={!canDecide}
            onClick={() => onDecide(action.action_id, true)}
            className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-on-accent shadow-[0_0_20px_-6px_var(--glow)] transition hover:brightness-110 active:scale-95 disabled:opacity-40"
          >
            Confirm
          </button>
          <button
            type="button"
            disabled={!canDecide}
            onClick={() => onDecide(action.action_id, false)}
            className="rounded-lg border border-border px-4 py-2 text-sm transition hover:border-accent/50 active:scale-95 disabled:opacity-40"
          >
            Cancel
          </button>
        </div>
      ) : (
        <p className="mt-2 text-sm text-muted">Say “yes” to apply it or “no” to leave it.</p>
      )}
    </div>
  );
}
