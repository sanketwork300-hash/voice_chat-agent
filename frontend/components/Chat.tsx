"use client";

import Link from "next/link";
import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";

import { Activity, ChatMessage, ConnectionStatus, PendingAction, Sources, useChatSocket } from "@/lib/useChatSocket";

const MAX_MESSAGE_CHARS = 2000;
const SHOW_DEBUG = process.env.NEXT_PUBLIC_DEBUG_UI === "1";

const STATUS_LABEL: Record<ConnectionStatus, string> = {
  connecting: "Connecting",
  idle: "Online",
  thinking: "Working",
  disconnected: "Reconnecting",
};

const STATUS_DOT: Record<ConnectionStatus, string> = {
  connecting: "bg-warning",
  idle: "bg-success",
  thinking: "bg-accent animate-pulse",
  disconnected: "bg-danger",
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

const ACTIVITY_MARK: Record<Activity["state"], { symbol: string; className: string; label: string }> = {
  running: { symbol: "•", className: "text-accent animate-pulse", label: "In progress" },
  ok: { symbol: "✓", className: "text-success", label: "Done" },
  warn: { symbol: "!", className: "text-warning", label: "Attention" },
  error: { symbol: "✕", className: "text-danger", label: "Failed" },
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
  "I can't SSH into my EC2 server",
  "My Lambda function demo-report-generator keeps timing out",
  "What are the rules for naming an S3 bucket?",
];

export default function Chat() {
  const { status, model, conversationId, troubleshooting, messages, send, decide, escalate, clear } = useChatSocket();
  const [draft, setDraft] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
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
    <div className="mx-auto flex h-dvh w-full max-w-3xl flex-col px-4">
      <header className="flex items-center justify-between gap-4 border-b border-border py-4">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold">Cloud Support Assistant</h1>
          <p className="flex flex-wrap items-center gap-x-2 text-sm text-muted" role="status">
            <span className={`inline-block size-2 rounded-full ${STATUS_DOT[status]}`} aria-hidden />
            {STATUS_LABEL[status]}
            {progress && <span data-testid="progress">· {progress}</span>}
            {SHOW_DEBUG && model && <span className="font-mono text-xs">· {model}</span>}
            {SHOW_DEBUG && conversationId && (
              <Link href={`/debug?c=${conversationId}`} target="_blank" className="text-xs underline">
                debug
              </Link>
            )}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Link href="/voice" className="text-sm underline">
            Switch to voice
          </Link>
          <button
            type="button"
            onClick={escalate}
            disabled={!idle || messages.length === 0 || escalated}
            title="Hand this conversation to a support engineer"
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface disabled:cursor-not-allowed disabled:opacity-40"
          >
            Talk to a human
          </button>
          <button
            type="button"
            onClick={clear}
            disabled={!idle || messages.length === 0}
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface disabled:cursor-not-allowed disabled:opacity-40"
          >
            Clear conversation
          </button>
        </div>
      </header>

      <main className="flex-1 overflow-y-auto py-6" aria-live="polite">
        {messages.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
            <p className="max-w-md text-muted">
              Describe a problem with EC2, S3, Lambda, IAM, or CloudWatch and I&apos;ll check your resources and
              help you fix it, or ask how something in AWS works.
            </p>
            <div className="flex flex-wrap justify-center gap-2">
              {SUGGESTIONS.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  onClick={() => submit(suggestion)}
                  disabled={!idle}
                  className="rounded-full border border-border px-3 py-1.5 text-sm hover:bg-surface disabled:opacity-40"
                >
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <ol className="flex flex-col gap-4">
            {messages.map((message) => (
              <MessageItem key={message.id} message={message} canDecide={idle} onDecide={decide} />
            ))}
            {waitingForReply && (
              <li className="self-start rounded-2xl bg-surface px-4 py-3 text-muted">
                <span className="animate-pulse">Thinking…</span>
              </li>
            )}
          </ol>
        )}
        <div ref={endRef} />
      </main>

      <form onSubmit={onSubmit} className="flex items-end gap-2 border-t border-border py-4">
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
          placeholder="Type your message…"
          className="flex-1 resize-none rounded-xl border border-border bg-background px-4 py-3 outline-none focus:border-accent"
        />
        <button
          type="submit"
          disabled={!canSend}
          className="rounded-xl bg-accent px-5 py-3 font-medium text-on-accent disabled:cursor-not-allowed disabled:opacity-40"
        >
          Send
        </button>
      </form>
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
      <li className="self-center rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-sm" role="alert">
        {message.text}
      </li>
    );
  }
  if (message.role === "user") {
    return (
      <li className="max-w-[85%] self-end whitespace-pre-wrap rounded-2xl bg-accent px-4 py-3 text-on-accent">
        {message.text}
      </li>
    );
  }
  return (
    <li className="flex max-w-[85%] flex-col gap-2 self-start">
      {message.activities && message.activities.length > 0 && <ActivityList activities={message.activities} />}
      {message.text && (
        <div className="markdown rounded-2xl bg-surface px-4 py-3">
          <Markdown>{message.text}</Markdown>
        </div>
      )}
      {message.sources && <SourceList sources={message.sources} />}
      {message.action && <ActionCard action={message.action} canDecide={canDecide} onDecide={onDecide} />}
      {SHOW_DEBUG && message.latency && (
        <p className="px-2 font-mono text-xs text-muted">
          first token {(message.latency.first_token / 1000).toFixed(1)} s · total{" "}
          {(message.latency.total / 1000).toFixed(1)} s
        </p>
      )}
    </li>
  );
}

export function ActivityList({ activities }: { activities: Activity[] }) {
  return (
    <ul className="flex flex-col gap-1 rounded-xl border border-border px-4 py-3 text-sm" aria-label="Checks">
      {activities.map((activity) => {
        const mark = ACTIVITY_MARK[activity.state];
        return (
          <li key={activity.id} className="flex gap-2">
            <span className={`w-4 shrink-0 text-center font-semibold ${mark.className}`} aria-label={mark.label}>
              {mark.symbol}
            </span>
            <span>
              <span className="text-muted">{activity.label}</span>
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
    <div className="px-2 text-sm" aria-label="Sources">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted">
        {sources.kind === "cited" ? "Sources" : "Related documentation"}
      </p>
      <ol className="mt-1 flex flex-col gap-0.5">
        {documents.map((doc) => (
          <li key={doc.n} className="flex gap-2">
            <span className="w-6 shrink-0 text-muted">[{doc.n}]</span>
            <a href={doc.url} target="_blank" rel="noreferrer" className="text-accent underline">
              {doc.section}
            </a>
          </li>
        ))}
      </ol>
      <p className="mt-1 text-xs text-muted">AWS documentation, snapshot from {year}.</p>
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
  onDecide: (actionId: string, confirm: boolean) => void;
}) {
  return (
    <div className="rounded-xl border border-accent/50 px-4 py-3" aria-label="Proposed change">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted">Proposed change</p>
      <p className="mt-1">{action.summary}</p>
      {action.outcome ? (
        <p className="mt-2 text-sm text-muted">{OUTCOME_LABEL[action.outcome]}</p>
      ) : (
        <div className="mt-3 flex gap-2">
          <button
            type="button"
            disabled={!canDecide}
            onClick={() => onDecide(action.action_id, true)}
            className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-on-accent disabled:opacity-40"
          >
            Confirm
          </button>
          <button
            type="button"
            disabled={!canDecide}
            onClick={() => onDecide(action.action_id, false)}
            className="rounded-lg border border-border px-4 py-2 text-sm hover:bg-surface disabled:opacity-40"
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}
