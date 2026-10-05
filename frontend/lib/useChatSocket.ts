"use client";

import { useCallback, useEffect, useReducer, useRef } from "react";

const WS_URL = process.env.NEXT_PUBLIC_BACKEND_WS_URL ?? "ws://localhost:8010/ws/chat";
const RECONNECT_MS = 2000;

export type Latency = { first_token: number; total: number };

export type Activity = {
  id: string;
  label: string;
  state: "running" | "ok" | "warn" | "error";
  detail?: string | null;
};

export type ActionOutcome = "executed" | "declined" | "cancelled" | "expired" | "failed" | "refused";

export type PendingAction = {
  action_id: string;
  summary: string;
  outcome?: ActionOutcome; // set once the action is no longer waiting
};

export type SourceDocument = {
  n: number;
  title: string;
  section: string;
  url: string;
  source_date: string;
  cited: boolean;
};

// "cited": the answer was written from these pages. "related": further reading for a diagnosis.
export type Sources = { kind: "cited" | "related"; documents: SourceDocument[] };

export type ChatMessage = {
  id: number;
  role: "user" | "assistant" | "notice";
  text: string;
  activities?: Activity[];
  action?: PendingAction;
  sources?: Sources;
  streaming?: boolean;
  latency?: Latency;
};

export type ConnectionStatus = "connecting" | "idle" | "thinking" | "disconnected";

export type Troubleshooting = { status: string; service: string | null; intent: string | null };

type State = {
  status: ConnectionStatus;
  model: string | null;
  conversationId: string | null;
  troubleshooting: Troubleshooting | null;
  messages: ChatMessage[];
  nextId: number;
};

type ServerEvent =
  | { type: "ready"; conversation_id: string; model: string }
  | { type: "status"; status: "thinking" | "idle" }
  | { type: "token"; text: string }
  | ({ type: "activity" } & Activity)
  | ({ type: "state" } & Troubleshooting)
  | ({ type: "sources" } & Sources)
  | { type: "pending_action"; action_id: string; summary: string }
  | { type: "action_resolved"; action_id: string; outcome: ActionOutcome }
  | { type: "escalated"; trigger: string; reason: string }
  | { type: "interim"; text: string }
  | { type: "done"; latency_ms: Latency }
  | { type: "cleared" }
  | { type: "error"; message: string };

type Action =
  | { type: "server"; event: ServerEvent }
  | { type: "sent"; text?: string }
  | { type: "closed" };

const initialState: State = {
  status: "connecting",
  model: null,
  conversationId: null,
  troubleshooting: null,
  messages: [],
  nextId: 1,
};

function append(state: State, message: Omit<ChatMessage, "id">): State {
  return {
    ...state,
    messages: [...state.messages, { ...message, id: state.nextId }],
    nextId: state.nextId + 1,
  };
}

/** Apply a change to the assistant message of the turn in progress, creating it if needed. */
function updateTurn(state: State, change: (message: ChatMessage) => ChatMessage): State {
  const last = state.messages.at(-1);
  if (last?.streaming) {
    return { ...state, messages: [...state.messages.slice(0, -1), change(last)] };
  }
  const created = append(state, { role: "assistant", text: "", streaming: true });
  const message = created.messages.at(-1)!;
  return { ...created, messages: [...created.messages.slice(0, -1), change(message)] };
}

function withoutStreaming(messages: ChatMessage[]): ChatMessage[] {
  return messages.filter((m) => !m.streaming);
}

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "sent": {
      const thinking = { ...state, status: "thinking" as const };
      return action.text ? append(thinking, { role: "user", text: action.text }) : thinking;
    }

    case "closed": {
      if (state.status === "disconnected") return state;
      // The server starts a new conversation for every connection.
      const kept = withoutStreaming(state.messages);
      const next = { ...state, status: "disconnected" as const, messages: kept, troubleshooting: null };
      return kept.length > 0
        ? append(next, { role: "notice", text: "Connection lost. The assistant will start a new conversation when it reconnects." })
        : next;
    }

    case "server": {
      const event = action.event;
      switch (event.type) {
        case "ready":
          return { ...state, status: "idle", model: event.model, conversationId: event.conversation_id };
        case "status":
          return { ...state, status: event.status };
        case "state":
          return { ...state, troubleshooting: { status: event.status, service: event.service, intent: event.intent } };
        case "token":
          return updateTurn(state, (m) => ({ ...m, text: m.text + event.text }));
        case "activity": {
          const activity: Activity = { id: event.id, label: event.label, state: event.state, detail: event.detail };
          return updateTurn(state, (m) => {
            const others = (m.activities ?? []).filter((a) => a.id !== activity.id);
            return { ...m, activities: [...others, activity] };
          });
        }
        case "sources":
          return updateTurn(state, (m) => ({ ...m, sources: { kind: event.kind, documents: event.documents } }));
        case "pending_action":
          return updateTurn(state, (m) => ({
            ...m,
            action: { action_id: event.action_id, summary: event.summary },
          }));
        case "action_resolved":
          return {
            ...state,
            messages: state.messages.map((m) =>
              m.action?.action_id === event.action_id ? { ...m, action: { ...m.action, outcome: event.outcome } } : m,
            ),
          };
        case "done":
          return {
            ...state,
            messages: state.messages.map((m) =>
              m.streaming ? { ...m, streaming: false, latency: event.latency_ms } : m,
            ),
          };
        case "cleared":
          return { ...state, messages: [], troubleshooting: null };
        case "escalated": // the reply text and the state event that follow carry what the customer sees
        case "interim": // spoken on a call only
          return state;
        case "error":
          // A partial reply from a failed turn is not part of the conversation.
          return append(
            { ...state, messages: withoutStreaming(state.messages) },
            { role: "notice", text: event.message },
          );
      }
    }
  }
}

export function useChatSocket() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let closedByCleanup = false;
    let retry: ReturnType<typeof setTimeout> | undefined;

    function connect() {
      const socket = new WebSocket(WS_URL);
      socketRef.current = socket;
      socket.onmessage = (message) => {
        dispatch({ type: "server", event: JSON.parse(message.data) as ServerEvent });
      };
      socket.onclose = () => {
        if (closedByCleanup) return;
        dispatch({ type: "closed" });
        retry = setTimeout(connect, RECONNECT_MS);
      };
    }

    connect();
    return () => {
      closedByCleanup = true;
      clearTimeout(retry);
      socketRef.current?.close();
    };
  }, []);

  const post = useCallback((payload: object): boolean => {
    const socket = socketRef.current;
    if (socket?.readyState !== WebSocket.OPEN) return false;
    socket.send(JSON.stringify(payload));
    return true;
  }, []);

  const send = useCallback(
    (text: string) => {
      if (post({ type: "message", text })) dispatch({ type: "sent", text });
    },
    [post],
  );

  const decide = useCallback(
    (actionId: string, confirm: boolean) => {
      if (post({ type: confirm ? "confirm_action" : "cancel_action", action_id: actionId })) {
        dispatch({ type: "sent" });
      }
    },
    [post],
  );

  const escalate = useCallback(() => {
    if (post({ type: "escalate" })) dispatch({ type: "sent" });
  }, [post]);

  const clear = useCallback(() => {
    post({ type: "clear" });
  }, [post]);

  return {
    status: state.status,
    model: state.model,
    conversationId: state.conversationId,
    troubleshooting: state.troubleshooting,
    messages: state.messages,
    send,
    decide,
    escalate,
    clear,
  };
}
