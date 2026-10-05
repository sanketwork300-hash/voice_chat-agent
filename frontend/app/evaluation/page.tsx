"use client";

import AppHeader from "@/components/AppHeader";
import { useRouter, useSearchParams } from "next/navigation";
import { CSSProperties, Fragment, ReactNode, Suspense, useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_BACKEND_HTTP_URL ?? "http://localhost:8010";

type Rate = { passed: number; applicable: number; rate: number | null };

type Metrics = {
  scenarios: number;
  rates: Record<string, Rate>;
  by_service: Record<string, { passed: number; scenarios: number }>;
  write_safety_violations: number;
  errors: number;
  escalation_rate: number | null;
  scenario_seconds: { p50: number; p95: number };
  turn_seconds: { p50: number; p95: number };
  turns_per_scenario: number;
};

type Run = {
  run_id: string;
  started_at: string;
  subset: string;
  model: string;
  scenario_count: number;
  wall_clock_s: number;
  metrics: Metrics;
};

type Turn = { customer: string; agent: string; latency_s: number; status: string };

type Result = {
  scenario_id: string;
  service: string;
  variation: string;
  passed: boolean;
  checks: Record<string, boolean | null>;
  scenario: { expected_root_causes: string[]; expected_resolution: string | null; intents: string[] };
  outcome: {
    turns: Turn[];
    intent: string | null;
    status: string | null;
    root_cause: string | null;
    resolution: string | null;
    result: string | null;
    escalation_reason: string | null;
    tool_calls: { id: string; tool: string; requested_by: string; stage: string; ok: boolean; error: { error: string } | null }[];
    writes: { tool: string; args: Record<string, unknown> }[];
    post_condition: boolean | null;
    error: string | null;
    duration_s: number;
  };
};

// The checks shown as tiles, in the order of the spec's report.
const METRICS: [string, string][] = [
  ["intent", "Intent accuracy"],
  ["tool_selection", "Tool selection by the model"],
  ["tool_coverage", "Tool coverage after required checks"],
  ["tool_arguments", "Tool argument accuracy"],
  ["diagnosis", "Diagnosis accuracy"],
  ["resolution", "Resolution accuracy"],
  ["unsupported", "Unsupported detection"],
];

// Services are always listed in this order, whatever order a run stored them in.
const SERVICE_ORDER = ["ec2", "s3", "lambda", "iam", "cloudwatch", "none"];

const CHECK_LABELS: Record<string, string> = {
  intent: "intent",
  tool_coverage: "tool coverage",
  tool_arguments: "tool arguments",
  diagnosis: "diagnosis",
  resolution: "resolution",
  unsupported: "unsupported detection",
  write_safe: "write safety",
};

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API}${path}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path} returned ${response.status}`);
  return response.json() as Promise<T>;
}

const percent = (rate: number | null) => (rate === null ? "n/a" : `${Math.round(rate * 100)}%`);

export default function EvaluationPage() {
  return (
    <Suspense>
      <Evaluation />
    </Suspense>
  );
}

function Evaluation() {
  const router = useRouter();
  const selected = useSearchParams().get("run");
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [detail, setDetail] = useState<{ run: Run; results: Result[] } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getJson<Run[]>("/api/evaluation/runs")
      .then(setRuns)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  const runId = selected ?? runs?.[0]?.run_id ?? null;
  useEffect(() => {
    if (!runId) return;
    getJson<{ run: Run; results: Result[] }>(`/api/evaluation/runs/${runId}`)
      .then(setDetail)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [runId]);

  return (
    <div className="mx-auto flex min-h-dvh w-full max-w-6xl flex-col gap-6 px-3 pb-8 text-sm sm:px-4">
      <AppHeader title="Evaluation" wide />
      <p className="rise text-muted" style={{ "--i": 1 } as CSSProperties}>
        Scenario runs against the agent with the real model and FakeCloud. Every number is computed from a stored run.
      </p>

      {error && (
        <p className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2" role="alert">
          Could not load evaluation data from {API}: {error}. Is the backend running with DEBUG_UI=1?
        </p>
      )}
      {runs?.length === 0 && (
        <p className="text-muted">
          No runs stored yet. Run <code className="font-mono">make evaluate-smoke</code> to create one.
        </p>
      )}

      {runs && runs.length > 0 && (
        <label className="rise flex flex-wrap items-center gap-2" style={{ "--i": 2 } as CSSProperties}>
          <span className="text-muted">Run</span>
          <select
            value={runId ?? ""}
            onChange={(e) => router.replace(`/evaluation?run=${e.target.value}`)}
            className="glass max-w-full rounded-lg px-2.5 py-1.5 outline-none focus:border-accent"
          >
            {runs.map((run) => (
              <option key={run.run_id} value={run.run_id}>
                {new Date(run.started_at).toLocaleString()} · {run.subset} · {run.scenario_count} scenarios ·{" "}
                {run.model}
              </option>
            ))}
          </select>
        </label>
      )}

      {detail && detail.run.run_id === runId && <RunView run={detail.run} results={detail.results} />}
    </div>
  );
}

function RunView({ run, results }: { run: Run; results: Result[] }) {
  const { metrics } = run;
  const passed = metrics.rates.passed;
  const violations = metrics.write_safety_violations;
  return (
    <>
      <section className="rise grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]" aria-label="Summary" style={{ "--i": 3 } as CSSProperties}>
        <div className="glass relative overflow-hidden rounded-2xl p-5">
          <div className="pointer-events-none absolute -right-16 -top-16 size-48 rounded-full bg-accent/15 blur-3xl" aria-hidden />
          <p className="text-muted">Scenarios passed</p>
          <p className="bg-gradient-to-r from-accent to-accent-2 bg-clip-text text-6xl font-semibold leading-tight tracking-tight text-transparent">
            {passed.passed}
            <span className="text-2xl font-normal text-muted [-webkit-text-fill-color:var(--muted)]"> of {passed.applicable}</span>
          </p>
          <Meter rate={passed.rate} label="Scenarios passed" />
          <p className="mt-3 text-muted">
            {run.subset} set · {run.model} · {(run.wall_clock_s / 60).toFixed(1)} minutes
          </p>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <StatusTile
            label="Write-safety violations"
            value={String(violations)}
            good={violations === 0}
            note={violations === 0 ? "No change was made without confirmation" : "A change was made without confirmation"}
          />
          <Tile label="Escalation rate" value={percent(metrics.escalation_rate)} note="Conversations handed to a human" />
          <Tile
            label="Time per scenario"
            value={`${metrics.scenario_seconds.p50.toFixed(0)} s`}
            note={`median; 95th percentile ${metrics.scenario_seconds.p95.toFixed(0)} s`}
          />
          <Tile
            label="Turns per scenario"
            value={metrics.turns_per_scenario.toFixed(1)}
            note={metrics.errors === 0 ? "No scenario ended in an error" : `${metrics.errors} scenario(s) ended in an error`}
          />
        </div>
      </section>

      <section aria-label="Accuracy by check" className="rise" style={{ "--i": 4 } as CSSProperties}>
        <h2 className="mb-2 font-semibold">Accuracy by check</h2>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {METRICS.map(([key, label]) => {
            const rate = metrics.rates[key];
            return (
              <div key={key} className="glass rounded-2xl p-4">
                <p className="text-muted">{label}</p>
                <p className="text-2xl font-semibold">{percent(rate.rate)}</p>
                <Meter rate={rate.rate} label={label} />
                <p className="mt-1 text-xs text-muted">
                  {rate.applicable === 0 ? "Does not apply to this run" : `${rate.passed} of ${rate.applicable} scenarios`}
                </p>
              </div>
            );
          })}
        </div>
      </section>

      <section aria-label="Passed by service" className="rise" style={{ "--i": 5 } as CSSProperties}>
        <h2 className="mb-2 font-semibold">Passed by service</h2>
        <table className="w-full max-w-xl text-left">
          <tbody>
            {SERVICE_ORDER.filter((service) => service in metrics.by_service).map((service) => {
              const counts = metrics.by_service[service];
              return (
                <tr key={service} className="border-t border-border">
                  <th scope="row" className="w-32 py-1.5 pr-3 font-normal">
                    {service === "none" ? "out of domain" : service}
                  </th>
                  <td className="w-24 py-1.5 pr-3 tabular-nums">
                    {counts.passed} of {counts.scenarios}
                  </td>
                  <td className="py-1.5">
                    <Meter rate={counts.passed / counts.scenarios} label={`${service} scenarios passed`} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>

      <ResultsTable results={results} />
    </>
  );
}

/** A ratio against its limit: one hue, with the unfilled track in a lighter step of the same hue. */
function Meter({ rate, label }: { rate: number | null; label: string }) {
  const value = rate === null ? 0 : Math.round(rate * 100);
  return (
    <div
      className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-accent/15"
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={value}
      title={rate === null ? "Does not apply" : `${value}%`}
    >
      <div className="meter-fill h-full rounded-full" style={{ width: `${value}%` }} />
    </div>
  );
}

function Tile({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="glass rounded-2xl p-4">
      <p className="text-muted">{label}</p>
      <p className="text-2xl font-semibold tracking-tight">{value}</p>
      <p className="text-xs text-muted">{note}</p>
    </div>
  );
}

/** A tile whose state matters: the state is given by an icon and words, with color only as reinforcement. */
function StatusTile({ label, value, good, note }: { label: string; value: string; good: boolean; note: string }) {
  return (
    <div className={`glass rounded-2xl p-4 ${good ? "" : "!border-danger"}`}>
      <p className="text-muted">{label}</p>
      <p className="flex items-baseline gap-2 text-2xl font-semibold">
        {value}
        <span className={`text-sm font-medium ${good ? "text-success" : "text-danger"}`}>
          {good ? "✓ OK" : "✕ Failed"}
        </span>
      </p>
      <p className="text-xs text-muted">{note}</p>
    </div>
  );
}

function ResultsTable({ results }: { results: Result[] }) {
  const [onlyFailed, setOnlyFailed] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const failed = results.filter((r) => !r.passed).length;
  const shown = onlyFailed ? results.filter((r) => !r.passed) : results;

  return (
    <section aria-label="Scenarios" className="rise" style={{ "--i": 6 } as CSSProperties}>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-semibold">Scenarios</h2>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={onlyFailed} onChange={(e) => setOnlyFailed(e.target.checked)} />
          Only failed ({failed})
        </label>
      </div>
      <div className="glass overflow-x-auto rounded-2xl p-2">
        <table className="w-full text-left">
          <thead className="text-xs text-muted">
            <tr>
              {["Scenario", "Service", "Customer behaviour", "Result", "Failed checks", "Root cause found", "Turns", "Seconds"].map(
                (heading) => (
                  <th key={heading} scope="col" className="px-2 py-1 font-medium">
                    {heading}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {shown.map((result) => {
              const failedChecks = Object.entries(CHECK_LABELS)
                .filter(([key]) => result.checks[key] === false)
                .map(([, label]) => label);
              const isOpen = open === result.scenario_id;
              return (
                <Fragment key={result.scenario_id}>
                  <tr className="border-t border-border align-top transition-colors hover:bg-surface-2/50">
                    <td className="px-2 py-1.5">
                      <button
                        type="button"
                        onClick={() => setOpen(isOpen ? null : result.scenario_id)}
                        aria-expanded={isOpen}
                        className="font-mono underline"
                      >
                        {result.scenario_id}
                      </button>
                    </td>
                    <td className="px-2 py-1.5">{result.service === "none" ? "out of domain" : result.service}</td>
                    <td className="px-2 py-1.5">{result.variation.replaceAll("_", " ")}</td>
                    <td className="px-2 py-1.5 whitespace-nowrap">
                      <span className={result.passed ? "text-success" : "text-danger"}>{result.passed ? "✓" : "✕"}</span>{" "}
                      {result.passed ? "Pass" : "Fail"}
                    </td>
                    <td className="px-2 py-1.5">{result.outcome.error ?? failedChecks.join(", ")}</td>
                    <td className="px-2 py-1.5 font-mono text-xs">{result.outcome.root_cause ?? "—"}</td>
                    <td className="px-2 py-1.5 tabular-nums">{result.outcome.turns.length}</td>
                    <td className="px-2 py-1.5 tabular-nums">{result.outcome.duration_s.toFixed(1)}</td>
                  </tr>
                  {isOpen && (
                    <tr className="border-t border-border bg-surface-2/40">
                      <td colSpan={8} className="px-3 py-3">
                        <ScenarioDetail result={result} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ScenarioDetail({ result }: { result: Result }) {
  const { outcome, scenario } = result;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div>
        <DetailHeading>Conversation</DetailHeading>
        <ol className="flex flex-col gap-2">
          {outcome.turns.map((turn, index) => (
            <li key={index}>
              <p>
                <span className="font-semibold">Customer: </span>
                {turn.customer}
              </p>
              <p className="whitespace-pre-wrap">
                <span className="font-semibold">Agent: </span>
                {turn.agent}
              </p>
              <p className="font-mono text-xs text-muted">
                {turn.status} · {turn.latency_s.toFixed(1)} s
              </p>
            </li>
          ))}
        </ol>
      </div>
      <div className="flex flex-col gap-3">
        <div>
          <DetailHeading>Expected and found</DetailHeading>
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 font-mono text-xs">
            <dt className="text-muted">intent</dt>
            <dd>
              {outcome.intent ?? "none"} (expected {scenario.intents.join(" or ")})
            </dd>
            <dt className="text-muted">root cause</dt>
            <dd>
              {outcome.root_cause ?? "none"} (expected {scenario.expected_root_causes.join(" or ") || "none"})
            </dd>
            <dt className="text-muted">resolution</dt>
            <dd>
              {outcome.resolution ?? "none"} (expected {scenario.expected_resolution ?? "any"})
            </dd>
            <dt className="text-muted">ended as</dt>
            <dd>
              {outcome.status}
              {outcome.result ? `, ${outcome.result}` : ""}
              {outcome.escalation_reason ? `: ${outcome.escalation_reason}` : ""}
            </dd>
            <dt className="text-muted">FakeCloud afterwards</dt>
            <dd>
              {outcome.post_condition === null ? "not checked" : outcome.post_condition ? "as expected" : "not as expected"}
            </dd>
          </dl>
        </div>
        <div>
          <DetailHeading>Tool calls</DetailHeading>
          <ol className="font-mono text-xs">
            {outcome.tool_calls.map((call) => (
              <li key={call.id}>
                {call.id} {call.tool} · {call.stage} · asked by {call.requested_by} ·{" "}
                {call.ok ? "ok" : call.error?.error}
              </li>
            ))}
            {outcome.tool_calls.length === 0 && <li className="text-muted">None.</li>}
          </ol>
        </div>
        <div>
          <DetailHeading>Changes made to FakeCloud</DetailHeading>
          <ol className="font-mono text-xs">
            {outcome.writes.map((write, index) => (
              <li key={index}>
                {write.tool} {JSON.stringify(write.args)}
              </li>
            ))}
            {outcome.writes.length === 0 && <li className="text-muted">None.</li>}
          </ol>
        </div>
      </div>
    </div>
  );
}

function DetailHeading({ children }: { children: ReactNode }) {
  return <h3 className="mb-1 text-xs font-semibold text-accent">{children}</h3>;
}
