"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ReactNode, Suspense, useCallback, useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_BACKEND_HTTP_URL ?? "http://localhost:8010";
const REFRESH_MS = 2000;

type ConversationRow = { conversation_id: string; channel: string; status: string; intent: string | null; updated_at: string };

type ToolCall = {
  id: string;
  turn: number;
  stage: string;
  requested_by: string;
  tool: string;
  args: Record<string, unknown>;
  read_only: boolean;
  ok: boolean;
  result: unknown;
  error: { error: string; detail: string } | null;
  latency_ms: number;
};

type AgentState = {
  conversation_id: string;
  channel: string;
  status: string;
  turn: number;
  service: string | null;
  intent: string | null;
  problem_statement: string;
  entities: Record<string, string>;
  missing_slots: string[];
  retrieved_documents: {
    n: number;
    chunk_id: string;
    service: string;
    topic: string;
    section: string;
    source_url: string;
    score: number;
    cited: boolean;
  }[];
  tool_calls: ToolCall[];
  root_cause_code: string | null;
  evidence: string[];
  proposed_resolution_code: string | null;
  pending_action: { action_id: string; tool: string; args: unknown; summary: string; confirmed: boolean; expires_at: string } | null;
  outcome: string | null;
  failed_tool_calls: number;
  turns_without_progress: number;
  clarification_attempts: number;
  escalation_reason: string | null;
  latency_ms: Record<string, number>;
  messages: { role: string; text: string }[];
};

type AuditEntry = { at: string; event: string; data: unknown };
type Escalation = {
  id: number;
  conversation_id: string;
  at: string;
  trigger: string;
  reason: string;
  channel: string;
  service: string | null;
  intent: string | null;
  handoff: {
    problem_statement: string;
    turn: number;
    entities: Record<string, string>;
    root_cause: string | null;
    proposed_resolution: string | null;
    checks: { tool: string; ok: boolean; finding: string | null }[];
    failed_tool_calls: number;
    turns_without_progress: number;
    clarification_attempts: number;
    last_customer_message: string;
  };
};
type Detail = { state: AgentState; audit: AuditEntry[]; escalations: Escalation[] };
type SystemInfo = { hardware: Record<string, unknown>; selection: Record<string, unknown>; llm_model: string };

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API}${path}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path} returned ${response.status}`);
  return response.json() as Promise<T>;
}

export default function DebugPage() {
  return (
    <Suspense>
      <Debug />
    </Suspense>
  );
}

function Debug() {
  const router = useRouter();
  const selected = useSearchParams().get("c");
  const [rows, setRows] = useState<ConversationRow[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [escalations, setEscalations] = useState<Escalation[]>([]);
  const [system, setSystem] = useState<SystemInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(true);

  const refresh = useCallback(async () => {
    try {
      setRows(await getJson<ConversationRow[]>("/api/debug/conversations"));
      setDetail(selected ? await getJson<Detail>(`/api/debug/conversations/${selected}`) : null);
      if (!selected) setEscalations(await getJson<Escalation[]>("/api/debug/escalations?limit=20"));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [selected]);

  useEffect(() => {
    getJson<SystemInfo>("/api/system").then(setSystem).catch(() => setSystem(null));
  }, []);

  useEffect(() => {
    // Fetching on mount and on an interval is the point of this effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    if (!live) return;
    const timer = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh, live]);

  return (
    <div className="mx-auto flex min-h-dvh w-full max-w-7xl flex-col gap-4 p-4 text-sm">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-3">
        <div>
          <h1 className="text-lg font-semibold">Agent debug</h1>
          <p className="text-muted">
            What the agent knew and did, per conversation.{" "}
            <Link href="/" className="underline">
              Back to chat
            </Link>
          </p>
        </div>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />
          Refresh every {REFRESH_MS / 1000} s
        </label>
      </header>

      {error && (
        <p className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2" role="alert">
          Could not load debug data from {API}: {error}. Is the backend running with DEBUG_UI=1?
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-[18rem_1fr]">
        <nav aria-label="Conversations" className="flex flex-col gap-1">
          <h2 className="font-semibold">Conversations</h2>
          {rows.length === 0 && <p className="text-muted">None yet.</p>}
          {rows.map((row) => (
            <button
              key={row.conversation_id}
              type="button"
              onClick={() => router.replace(`/debug?c=${row.conversation_id}`)}
              className={`rounded-lg border px-3 py-2 text-left hover:bg-surface ${
                row.conversation_id === selected ? "border-accent" : "border-border"
              }`}
            >
              <span className="block font-mono text-xs">{row.conversation_id.slice(0, 12)}</span>
              <span className="block">
                {row.status} · {row.intent ?? "no intent"}
              </span>
              <span className="block text-xs text-muted">{new Date(row.updated_at).toLocaleTimeString()}</span>
            </button>
          ))}
        </nav>

        <main className="flex min-w-0 flex-col gap-4">
          {detail ? (
            <Conversation detail={detail} />
          ) : (
            <>
              <p className="text-muted">Select a conversation.</p>
              <Section title={`Recent escalations (${escalations.length})`}>
                {escalations.length === 0 ? (
                  <p className="text-muted">None.</p>
                ) : (
                  <ol className="flex flex-col gap-1">
                    {escalations.map((e) => (
                      <li key={e.id} className="flex flex-wrap gap-x-3 border-t border-border pt-1 first:border-0">
                        <span className="font-mono text-xs text-muted">{new Date(e.at).toLocaleString()}</span>
                        <button type="button" className="font-mono text-xs underline" onClick={() => router.replace(`/debug?c=${e.conversation_id}`)}>
                          {e.conversation_id.slice(0, 12)}
                        </button>
                        <span className="rounded bg-warning/20 px-1 text-xs">{e.trigger}</span>
                        <span>{e.reason}</span>
                        <span className="text-muted">
                          {e.channel} · {e.service ?? "no service"} · {e.intent ?? "no intent"}
                        </span>
                      </li>
                    ))}
                  </ol>
                )}
              </Section>
            </>
          )}
          {system && (
            <Section title="Hardware and models">
              <Fields
                fields={{ ...system.hardware, tier: system.selection.tier, reason: system.selection.reason, llm_model: system.llm_model }}
              />
            </Section>
          )}
        </main>
      </div>
    </div>
  );
}

const ACTION_EVENTS = new Set([
  "action_proposed", "action_confirmed", "action_declined", "action_cancelled", "action_expired", "action_refused",
  "action_executed", "action_verified", "action_failed",
]);

function Conversation({ detail }: { detail: Detail }) {
  const { state, audit, escalations } = detail;
  const actionHistory = audit.filter((entry) => ACTION_EVENTS.has(entry.event));
  return (
    <>
      <Section title="State">
        <Fields
          fields={{
            status: state.status,
            service: state.service,
            intent: state.intent,
            turn: state.turn,
            channel: state.channel,
            outcome: state.outcome,
            escalation_reason: state.escalation_reason,
            problem_statement: state.problem_statement,
          }}
        />
      </Section>

      <div className="grid gap-4 md:grid-cols-2">
        <Section title="Entities and missing details">
          <Fields fields={{ ...state.entities, missing_slots: state.missing_slots.join(", ") || "none" }} />
        </Section>
        <Section title="Diagnosis">
          <Fields
            fields={{
              root_cause: state.root_cause_code,
              evidence: state.evidence.join(", ") || null,
              resolution: state.proposed_resolution_code,
            }}
          />
        </Section>
      </div>

      <Section title={`Tool calls (${state.tool_calls.length})`}>
        {state.tool_calls.length === 0 ? (
          <p className="text-muted">None.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead className="text-xs uppercase text-muted">
                <tr>
                  {["id", "turn", "stage", "asked by", "tool", "arguments", "result", "ms"].map((h) => (
                    <th key={h} className="px-2 py-1 font-medium">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {state.tool_calls.map((call) => (
                  <tr key={call.id} className="border-t border-border align-top">
                    <td className="px-2 py-1 font-mono">
                      {call.id}
                      {state.evidence.includes(call.id) && <span title="Evidence for the diagnosis"> ★</span>}
                    </td>
                    <td className="px-2 py-1">{call.turn}</td>
                    <td className="px-2 py-1">{call.stage}</td>
                    <td className="px-2 py-1">{call.requested_by}</td>
                    <td className="px-2 py-1 font-mono">
                      {call.tool}
                      {!call.read_only && <span className="ml-1 rounded bg-warning/20 px-1 text-xs">write</span>}
                    </td>
                    <td className="px-2 py-1">
                      <Json value={call.args} />
                    </td>
                    <td className="px-2 py-1">
                      <span className={call.ok ? "text-success" : "text-danger"}>{call.ok ? "ok" : call.error?.error}</span>
                      <Json value={call.ok ? call.result : call.error?.detail} />
                    </td>
                    <td className="px-2 py-1 text-right">{call.latency_ms}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section title={`Retrieved documents, this turn (${state.retrieved_documents.length})`}>
        {state.retrieved_documents.length === 0 ? (
          <p className="text-muted">None.</p>
        ) : (
          <ol className="flex flex-col gap-1">
            {state.retrieved_documents.map((doc) => (
              <li key={doc.chunk_id} className="flex flex-wrap gap-x-3">
                <span className="font-mono">[{doc.n}]</span>
                <span className="font-mono">{doc.score.toFixed(3)}</span>
                <span className="text-muted">{doc.service}</span>
                <a href={doc.source_url} target="_blank" rel="noreferrer" className="underline">
                  {doc.section}
                </a>
                {doc.cited && <span className="rounded bg-success/20 px-1 text-xs">cited</span>}
                <span className="font-mono text-xs text-muted">{doc.chunk_id}</span>
              </li>
            ))}
          </ol>
        )}
      </Section>

      <div className="grid gap-4 md:grid-cols-2">
        <Section title="Pending action and its history">
          {state.pending_action ? <Json value={state.pending_action} open /> : <p className="text-muted">Nothing pending.</p>}
          {actionHistory.length > 0 && (
            <ol className="mt-2 flex flex-col gap-1 border-t border-border pt-2">
              {actionHistory.map((entry, index) => {
                const data = entry.data as { action_id?: string; summary?: string; via?: string; verified?: boolean; ok?: boolean };
                return (
                  <li key={index} className="flex flex-wrap gap-x-2">
                    <span className="font-mono text-xs text-muted">{new Date(entry.at).toLocaleTimeString()}</span>
                    <span className="font-mono">{entry.event.replace("action_", "")}</span>
                    {data.action_id && <span className="font-mono text-xs text-muted">{data.action_id.slice(0, 8)}</span>}
                    {data.via && <span className="text-muted">via {data.via}</span>}
                    {data.verified !== undefined && <span className="text-muted">verified: {String(data.verified)}</span>}
                    {data.ok !== undefined && <span className="text-muted">ok: {String(data.ok)}</span>}
                    {data.summary && <span className="basis-full text-muted">{data.summary}</span>}
                  </li>
                );
              })}
            </ol>
          )}
        </Section>
        <Section title="Escalation rules and last-turn latency">
          <Fields
            fields={{
              failed_tool_calls: `${state.failed_tool_calls} (escalates at 3)`,
              turns_without_progress: `${state.turns_without_progress} (escalates at 4)`,
              clarification_attempts: `${state.clarification_attempts} (escalates after 2)`,
              escalation_reason: state.escalation_reason,
              ...Object.fromEntries(Object.entries(state.latency_ms).map(([k, v]) => [`${k} (ms)`, v])),
            }}
          />
        </Section>
      </div>

      <Section title={`Escalations (${escalations.length})`}>
        {escalations.length === 0 ? (
          <p className="text-muted">None.</p>
        ) : (
          escalations.map((e) => (
            <div key={e.id} className="border-t border-border pt-2 first:border-0 first:pt-0">
              <p className="flex flex-wrap gap-x-3">
                <span className="font-mono text-xs text-muted">{new Date(e.at).toLocaleString()}</span>
                <span className="rounded bg-warning/20 px-1 text-xs">{e.trigger}</span>
                <span>{e.reason}</span>
              </p>
              <p className="mt-1 text-xs font-semibold uppercase tracking-wide text-muted">Handoff for the engineer</p>
              <Fields
                fields={{
                  problem: e.handoff.problem_statement || null,
                  last_message: e.handoff.last_customer_message,
                  turn: e.handoff.turn,
                  ...e.handoff.entities,
                  root_cause: e.handoff.root_cause,
                  proposed_resolution: e.handoff.proposed_resolution,
                }}
              />
              {e.handoff.checks.length > 0 && (
                <ul className="mt-1 flex flex-col gap-0.5">
                  {e.handoff.checks.map((c, i) => (
                    <li key={i} className="flex gap-2">
                      <span className={c.ok ? "text-success" : "text-danger"}>{c.ok ? "✓" : "✕"}</span>
                      <span className="font-mono">{c.tool}</span>
                      <span className="text-muted">{c.finding}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))
        )}
      </Section>

      <Section title={`Audit log (${audit.length})`}>
        <ol className="flex flex-col gap-1">
          {audit.map((entry, index) => (
            <li key={index} className="flex flex-wrap gap-x-3 border-t border-border pt-1 first:border-0">
              <span className="font-mono text-xs text-muted">{new Date(entry.at).toLocaleTimeString()}</span>
              <span className="font-mono">{entry.event}</span>
              <Json value={entry.data} />
            </li>
          ))}
        </ol>
      </Section>

      <Section title="Transcript">
        <ol className="flex flex-col gap-2">
          {state.messages.map((message, index) => (
            <li key={index}>
              <span className="font-semibold">{message.role}: </span>
              <span className="whitespace-pre-wrap">{message.text}</span>
            </li>
          ))}
        </ol>
      </Section>
    </>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-xl border border-border p-4">
      <h2 className="mb-2 font-semibold">{title}</h2>
      {children}
    </section>
  );
}

function Fields({ fields }: { fields: Record<string, unknown> }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
      {Object.entries(fields).map(([key, value]) => (
        <div key={key} className="contents">
          <dt className="text-muted">{key}</dt>
          <dd className="break-words font-mono">{value === null || value === undefined || value === "" ? "—" : String(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function Json({ value, open = false }: { value: unknown; open?: boolean }) {
  if (value === null || value === undefined) return null;
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  if (text.length <= 60 && !text.includes("\n")) return <code className="font-mono text-xs"> {text}</code>;
  return (
    <details open={open} className="font-mono text-xs">
      <summary className="cursor-pointer text-muted">{text.replace(/\s+/g, " ").slice(0, 60)}…</summary>
      <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap rounded bg-surface p-2">{text}</pre>
    </details>
  );
}
