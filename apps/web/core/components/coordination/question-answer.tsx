import { useState } from "react";
import { Check, Pencil, Send, X } from "lucide-react";
import { Button } from "@plane/propel/button";

type Props = {
  question: string;
  answer: string;
  canAnswer: boolean;
  busy: boolean;
  onSubmit: (text: string) => Promise<boolean>;
};

export function QuestionAnswer({ question, answer, canAnswer, busy, onSubmit }: Props) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  if (!canAnswer || (answer && !editing))
    return (
      <>
        {answer && <p className="break-words whitespace-pre-wrap">{answer}</p>}
        {canAnswer && (
          <Button
            variant="secondary"
            disabled={busy}
            onClick={() => {
              setDraft(answer);
              setEditing(true);
            }}
          >
            <Pencil className="size-4" />
            Edit answer
          </Button>
        )}
      </>
    );

  async function save() {
    if (await onSubmit(draft)) {
      setEditing(false);
      setDraft("");
    }
  }

  return (
    <form
      className="space-y-2"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <textarea
        aria-label={`Answer: ${question}`}
        className="w-full min-w-0 rounded border border-subtle bg-surface-1 px-3 py-2 text-body-sm-regular disabled:opacity-60"
        rows={2}
        value={draft}
        disabled={busy}
        onChange={(event) => setDraft(event.target.value)}
      />
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" disabled={busy || !draft.trim()}>
          {answer ? <Check className="size-4" /> : <Send className="size-4" />}
          {answer ? "Save answer" : "Post answer"}
        </Button>
        {answer && (
          <Button variant="secondary" disabled={busy} onClick={() => setEditing(false)}>
            <X className="size-4" />
            Cancel
          </Button>
        )}
      </div>
    </form>
  );
}
