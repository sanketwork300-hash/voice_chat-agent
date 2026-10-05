"use client";

import { PipecatClient, type TranscriptData } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";
import { useCallback, useEffect, useReducer, useRef } from "react";

import type { ActionOutcome, Activity, PendingAction, Sources, Troubleshooting } from "@/lib/useChatSocket";

const OFFER_URL = `${process.env.NEXT_PUBLIC_BACKEND_HTTP_URL ?? "http://localhost:8010"}/api/voice/offer`;

export type CallStatus = "idle" | "connecting" | "listening" | "thinking" | "speaking" | "error";

export type VoiceLatency = { stt?: number; first_token?: number; first_audio?: number; total?: number };

export type VoiceTurn = {
  id: number;
  role: "user" | "assistant";
  text: string;
  activities?: Activity[];
  action?: PendingAction;
  sources?: Sources;
  latency?: VoiceLatency;
  interrupted?: boolean; // the customer spoke over this reply, so it was cut short
};

type State = {
  status: CallStatus;
  conversationId: string | null;
  troubleshooting: Troubleshooting | null;
  turns: VoiceTurn[];
  error: string | null;
};

// The agent's chat events, forwarded over the voice connection as server messages.
type AgentEvent =
  | { type: "state"; status: string; service: string | null; intent: string | null }
  | ({ type: "activity" } & Activity)
  | ({ type: "sources" } & Sources)
  | { type: "pending_action"; action_id: string; summary: string }
  | { type: "action_resolved"; action_id: string; outcome: ActionOutcome }
  | { type: "escalated"; trigger: string; reason: string };

type ServerMessage = { conversation_id?: string; latency?: VoiceLatency; agent?: AgentEvent };

type Action =
  | { type: "status"; status: CallStatus }
  | { type: "user_said"; text: string }
  | { type: "bot_said"; text: string }
  | { type: "server"; message: ServerMessage }
  | { type: "failed"; message: string }
  | { type: "interrupted" }
  | { type: "ended" };

const initial: State = { status: "idle", conversationId: null, troubleshooting: null, turns: [], error: null };

let nextId = 1;

function withAssistantTurn(state: State, change: (turn: VoiceTurn) => VoiceTurn): State {
  const last = state.turns.at(-1);
  if (last?.role === "assistant" && last.latency === undefined && !last.interrupted) {
    return { ...state, turns: [...state.turns.slice(0, -1), change(last)] };
  }
  return { ...state, turns: [...state.turns, change({ id: nextId++, role: "assistant", text: "" })] };
}

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "status":
      return { ...state, status: action.status, error: null };
    case "user_said":
      return { ...state, status: "thinking", turns: [...state.turns, { id: nextId++, role: "user", text: action.text }] };
    case "bot_said":
      return withAssistantTurn(state, (t) => ({ ...t, text: t.text ? `${t.text} ${action.text}` : action.text }));
    case "server": {
      const { conversation_id, latency, agent } = action.message;
      if (conversation_id) return { ...state, conversationId: conversation_id };
      if (latency) return withAssistantTurn(state, (t) => ({ ...t, latency }));
      if (!agent) return state;
      switch (agent.type) {
        case "state":
          return { ...state, troubleshooting: { status: agent.status, service: agent.service, intent: agent.intent } };
        case "activity":
          return withAssistantTurn(state, (t) => ({
            ...t,
            activities: [...(t.activities ?? []).filter((a) => a.id !== agent.id),
              { id: agent.id, label: agent.label, state: agent.state, detail: agent.detail }],
          }));
        case "sources":
          return withAssistantTurn(state, (t) => ({ ...t, sources: { kind: agent.kind, documents: agent.documents } }));
        case "pending_action":
          return withAssistantTurn(state, (t) => ({ ...t, action: { action_id: agent.action_id, summary: agent.summary } }));
        case "action_resolved":
          return {
            ...state,
            turns: state.turns.map((t) =>
              t.action?.action_id === agent.action_id ? { ...t, action: { ...t.action, outcome: agent.outcome } } : t,
            ),
          };
      }
      return state;
    }
    case "failed":
      return { ...state, status: "error", error: action.message };
    case "interrupted": {
      // The customer started speaking while the agent was replying or still working on the reply.
      const last = state.turns.at(-1);
      const turns =
        last?.role === "assistant" && last.latency === undefined
          ? [...state.turns.slice(0, -1), { ...last, interrupted: true }]
          : state.turns;
      return { ...state, status: "listening", turns };
    }
    case "ended":
      return { ...state, status: "idle", troubleshooting: null };
  }
}

export function useVoiceClient() {
  const [state, dispatch] = useReducer(reducer, initial);
  const clientRef = useRef<PipecatClient | null>(null);
  const speakerRef = useRef<HTMLAudioElement | null>(null); // plays the agent's audio track

  const stop = useCallback(() => {
    clientRef.current?.disconnect();
    clientRef.current = null;
    if (speakerRef.current) speakerRef.current.srcObject = null;
    dispatch({ type: "ended" });
  }, []);

  const start = useCallback(async () => {
    if (clientRef.current) return;
    dispatch({ type: "status", status: "connecting" });
    const client = new PipecatClient({
      transport: new SmallWebRTCTransport({ webrtcRequestParams: { endpoint: OFFER_URL } }),
      enableMic: true,
      enableCam: false,
      callbacks: {
        onTrackStarted: (track, participant) => {
          // The transport hands over the agent's audio track; nothing plays it unless the page does.
          if (track.kind !== "audio" || participant?.local) return;
          const speaker = speakerRef.current ?? (speakerRef.current = new Audio());
          speaker.autoplay = true;
          speaker.srcObject = new MediaStream([track]);
          void speaker.play().catch(() => undefined); // autoplay is allowed after the Start call click
        },
        onBotReady: () => dispatch({ type: "status", status: "listening" }),
        onDisconnected: () => {
          clientRef.current = null;
          dispatch({ type: "ended" });
        },
        onUserTranscript: (data: TranscriptData) => {
          if (data.final && data.text.trim()) dispatch({ type: "user_said", text: data.text.trim() });
        },
        onBotTtsText: (data) => dispatch({ type: "bot_said", text: data.text }),
        onUserStartedSpeaking: () => dispatch({ type: "interrupted" }),
        onBotStartedSpeaking: () => dispatch({ type: "status", status: "speaking" }),
        onBotStoppedSpeaking: () => dispatch({ type: "status", status: "listening" }),
        onServerMessage: (data: ServerMessage) => dispatch({ type: "server", message: data }),
        onError: (message) => dispatch({ type: "failed", message: describe(message.data) }),
      },
    });
    clientRef.current = client;
    try {
      await client.connect();
    } catch (error) {
      clientRef.current = null;
      dispatch({ type: "failed", message: describe(error) });
    }
  }, []);

  useEffect(
    () => () => {
      void clientRef.current?.disconnect();
    },
    [],
  );

  return { ...state, start, stop };
}

function describe(error: unknown): string {
  if (error instanceof Error) {
    if (error.name === "NotAllowedError") return "Microphone access was denied. Allow the microphone and try again.";
    return error.message;
  }
  if (typeof error === "object" && error && "message" in error) return String((error as { message: unknown }).message);
  return "The voice connection failed. Is the backend running with voice enabled?";
}
