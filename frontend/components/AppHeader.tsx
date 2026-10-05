"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode } from "react";

const SHOW_DEBUG = process.env.NEXT_PUBLIC_DEBUG_UI === "1";

const MODES = [
  { href: "/", label: "Chat" },
  { href: "/voice", label: "Voice" },
];

const TOOLS = [
  { href: "/debug", label: "Debug" },
  { href: "/evaluation", label: "Evaluation" },
];

export type Tone = "ok" | "busy" | "warn" | "bad" | "off";

const TONE: Record<Tone, string> = {
  ok: "text-success",
  busy: "text-accent",
  warn: "text-warning",
  bad: "text-danger",
  off: "text-muted",
};

/** The status line under the product name: a coloured dot, then words, so colour is never the only signal. */
export function StatusLine({ tone, live, children }: { tone: Tone; live?: boolean; children: ReactNode }) {
  return (
    <p className="flex items-center gap-x-2 overflow-hidden whitespace-nowrap text-xs text-muted" role="status">
      <span className={`signal-dot inline-block size-1.5 shrink-0 rounded-full bg-current ${TONE[tone]}`} data-live={live} aria-hidden />
      {children}
    </p>
  );
}

export default function AppHeader({
  title = "Cloud Support",
  status,
  actions,
  wide = false,
}: {
  title?: string;
  status?: ReactNode;
  actions?: ReactNode;
  wide?: boolean;
}) {
  const pathname = usePathname();
  const links = SHOW_DEBUG || TOOLS.some((t) => t.href === pathname) ? [...MODES, ...TOOLS] : MODES;
  return (
    <header className="rise sticky top-0 z-20 pt-3">
      <div className={`glass mx-auto flex w-full flex-wrap items-center gap-3 rounded-2xl px-3 py-2.5 sm:px-4 ${wide ? "" : "max-w-4xl"}`}>
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <Link href="/" className="relative grid size-8 shrink-0 place-items-center" aria-label="Cloud Support home">
            <span className="brand-mark absolute inset-0 rounded-[10px] opacity-90" aria-hidden />
            <span className="absolute inset-[2px] rounded-[8px] bg-background" aria-hidden />
            <svg viewBox="0 0 24 24" className="relative size-4 text-accent" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
              <path d="M4 12h2l2-5 3 10 3-8 2 3h4" />
            </svg>
          </Link>
          <div className="min-w-0">
            <h1 className="truncate text-sm font-semibold tracking-tight">{title}</h1>
            {status}
          </div>
        </div>

        <nav aria-label="Pages" className="order-last flex w-full gap-1 rounded-xl bg-surface-2/70 p-1 md:order-none md:w-auto">
          {links.map((link) => {
            const active = pathname === link.href;
            return (
              <Link
                key={link.href}
                href={link.href}
                aria-current={active ? "page" : undefined}
                className={`flex-1 rounded-lg px-3 py-1.5 text-center text-xs font-medium transition-colors md:flex-none ${
                  active
                    ? "bg-surface text-foreground shadow-[0_0_0_1px_var(--border),0_0_18px_-6px_var(--glow)]"
                    : "text-muted hover:text-foreground"
                }`}
              >
                {link.label}
              </Link>
            );
          })}
        </nav>

        {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
      </div>
    </header>
  );
}

/** A quiet header button: an outline that lights up on hover. */
export const ghostButton =
  "rounded-lg border border-border px-2.5 py-1.5 text-xs font-medium text-muted transition hover:border-accent/60 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-35 disabled:hover:border-border disabled:hover:text-muted";
