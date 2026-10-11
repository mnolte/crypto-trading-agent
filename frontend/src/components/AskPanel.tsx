import { useState } from "react";
import { useLocation } from "react-router-dom";
import type { AskContext, AskMessageOut } from "../api/client";
import {
  useAsk,
  useAskConversation,
  useAskConversations,
} from "../api/hooks";
import { formatEur } from "../lib/format";
import { Button } from "./Button";
import { EmptyState } from "./EmptyState";

/** The Ask side panel: read-only questions over stored records. Every answer
 * cites the records it used; the panel can never trade or change settings. */
export function AskPanel() {
  const location = useLocation();
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [error, setError] = useState<string | null>(null);

  const { data: list } = useAskConversations();
  const { data: conversation } = useAskConversation(conversationId);
  const ask = useAsk();

  const context: AskContext = { page: location.pathname.replace("/", "") || "dashboard" };

  const submit = () => {
    const q = question.trim();
    if (!q || ask.isPending) return;
    setError(null);
    ask.mutate(
      { question: q, conversation_id: conversationId, context },
      {
        onSuccess: (out) => {
          setConversationId(out.conversation_id);
          setQuestion("");
        },
        onError: (e) => setError(e instanceof Error ? e.message : String(e)),
      },
    );
  };

  const used = list?.questions_today ?? 0;
  const limit = list?.daily_limit ?? 100;
  const messages = conversation?.messages ?? [];

  return (
    <>
      <div className="ask-head">
        <button
          type="button"
          className="ask-new"
          onClick={() => setConversationId(null)}
        >
          New conversation
        </button>
        <div className="ask-convos">
          {(list?.items ?? []).map((c) => (
            <button
              key={c.id}
              type="button"
              className={
                c.id === conversationId
                  ? "ask-convo ask-convo--on"
                  : "ask-convo"
              }
              onClick={() => setConversationId(c.id)}
            >
              {c.title}
            </button>
          ))}
        </div>
      </div>
      <div className="card__meta">
        Looking at {context.page} · Read-only · {used} of {limit} today
      </div>
      {messages.length === 0 && (
        <EmptyState
          title="Ask about stored records"
          footer={'Try "Why didn\'t you buy SOL yesterday?"'}
        />
      )}
      {messages.map((m, i) => (
        <Message key={i} msg={m} />
      ))}
      {ask.isPending && <div className="card__meta">Thinking…</div>}
      {error && <div className="field__msg">{error}</div>}
      <div className="ask-input">
        <input
          className="input"
          placeholder="Your question"
          value={question}
          disabled={used >= limit}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
        />
        <Button
          variant="primary"
          disabled={ask.isPending || !question.trim() || used >= limit}
          onClick={submit}
        >
          Ask
        </Button>
      </div>
      {used >= limit && (
        <div className="field__msg">
          The Ask limit is {limit} questions a day — it resets at midnight.
        </div>
      )}
    </>
  );
}

function Message({ msg }: { msg: AskMessageOut }) {
  if (msg.role === "user") {
    return <div className="ask-msg ask-msg--user">{msg.text}</div>;
  }
  return (
    <div className="ask-msg ask-msg--assistant">
      <div className="ask-msg__text">{msg.text}</div>
      {msg.citations.length > 0 && (
        <div className="ask-citations">
          {msg.citations.map((c) => (
            <a key={c.id} className="ask-cite" href={c.href}>
              {c.label}
            </a>
          ))}
        </div>
      )}
      <div className="card__meta">
        {msg.records_used > 0
          ? `Answered from ${msg.records_used} stored records · every number above comes from those records, not from the model`
          : "No model answer — the records did not cover it"}
        {msg.model ? ` · ${msg.model}` : ""}
        {msg.call_ids.length > 0 ? ` · ${msg.call_ids.length} calls` : ""}
        {msg.cost_eur != null ? ` · ${formatEur(msg.cost_eur)}` : ""}
      </div>
    </div>
  );
}
