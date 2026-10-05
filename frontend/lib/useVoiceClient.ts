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

/** Loudness meters on the two audio tracks, read by the page's animation loop rather than through React state. */
class LevelMeter {
  private context: AudioContext | null = null;
  private analysers: Partial<Record<"local" | "remote", AnalyserNode>> = {};
  private buffer = new Uint8Array(512);

  attach(side: "local" | "remote", track: MediaStreamTrack) {
    try {
      this.context ??= new AudioContext();
      void this.context.resume().catch(() => undefined);
      const analyser = this.context.createAnalyser();
      analyser.fftSize = 512;
      // Analysed only; the page plays the agent's audio through its own element.
      this.context.createMediaStreamSource(new MediaStream([track])).connect(analyser);
      this.analysers[side] = analyser;
    } catch {
      // Without Web Audio the call still works; the orb just does not follow the voices.
    }
  }

  /** Loudness from 0 to 1 (RMS of the waveform, scaled so speech fills most of the range). */
  read(side: "local" | "remote"): number {
    const analyser = this.analysers[side];
    if (!analyser) return 0;
    analyser.getByteTimeDomainData(this.buffer);
    let sum = 0;
    for (const sample of this.buffer) sum += ((sample - 128) / 128) ** 2;
    return Math.min(1, Math.sqrt(sum / this.buffer.length) * 4);
  }

  close() {
    void this.context?.close().catch(() => undefined);
    this.context = null;
    this.analysers = {};
  }
}

export function useVoiceClient() {
  const [state, dispatch] = useReducer(reducer, initial);
  const clientRef = useRef<PipecatClient | null>(null);
  const speakerRef = useRef<HTMLAudioElement | null>(null); // plays the agent's audio track
  const meterRef = useRef(new LevelMeter());

  const stop = useCallback(() => {
    clientRef.current?.disconnect();
    clientRef.current = null;
    if (speakerRef.current) speakerRef.current.srcObject = null;
    meterRef.current.close();
    dispatch({ type: "ended" });
  }, []);

  const level = useCallback((side: "local" | "remote") => meterRef.current.read(side), []);

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
          if (track.kind !== "audio") return;
          meterRef.current.attach(participant?.local ? "local" : "remote", track);
          if (participant?.local) return;
          const speaker = speakerRef.current ?? (speakerRef.current = new Audio());
          speaker.autoplay = true;
          speaker.srcObject = new MediaStream([track]);
          void speaker.play().catch(() => undefined); // autoplay is allowed after the Start call click
        },
        onBotReady: () => dispatch({ type: "status", status: "listening" }),
        onDisconnected: () => {
          clientRef.current = null;
          meterRef.current.close();
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

  useEffect(() => {
    const meter = meterRef.current;
    return () => {
      void clientRef.current?.disconnect();
      meter.close();
    };
  }, []);

  return { ...state, start, stop, level };
}

function describe(error: unknown): string {
  if (error instanceof Error) {
    if (error.name === "NotAllowedError") return "Microphone access was denied. Allow the microphone and try again.";
    return error.message;
  }
  if (typeof error === "object" && error && "message" in error) return String((error as { message: unknown }).message);
  return "The voice connection failed. Is the backend running with voice enabled?";
}
