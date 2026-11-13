import { useEffect, useRef, useState } from "react";
import { MessageSquare, Paperclip, Send, X } from "lucide-react";
import { API_BASE_URL } from "@plane/constants";
import { Button } from "@plane/propel/button";
import { Dialog, EDialogWidth } from "@plane/propel/dialog";
import { APIService } from "@/services/api.service";
import { coordinationError } from "./helpers";

type Asset = { id: string; name: string; url: string | null; mime_type?: string };
type Message = {
  id: string;
  role: string;
  author: string;
  body: string;
  state: string;
  created_at: string;
  assets: Asset[];
};
type Conversation = { revision: number; state: string; error: string; closed: boolean; messages: Message[] };
type Props = {
  workspaceSlug: string;
  projectId: string;
  issueId: string;
  canWrite: boolean;
  uploadAttachment: (file: File) => Promise<{ id: string } | undefined>;
};
class ConversationService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }
}
const api = new ConversationService();

export function AgentConversation(props: Props) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button variant="secondary" onClick={() => setOpen(true)} prependIcon={<MessageSquare className="size-4" />}>
        Agent conversation
      </Button>
      {open && <ConversationDialog {...props} onClose={() => setOpen(false)} />}
    </>
  );
}

function ConversationDialog({
  workspaceSlug,
  projectId,
  issueId,
  canWrite,
  uploadAttachment,
  onClose,
}: Props & { onClose: () => void }) {
  const path = `/api/workspaces/${encodeURIComponent(workspaceSlug)}/projects/${projectId}/coordination/work-items/${issueId}/conversation/`;
  const [data, setData] = useState<Conversation>();
  const [draft, setDraft] = useState("");
  const [assets, setAssets] = useState<Asset[]>([]);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [disconnected, setDisconnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const retry = useRef<{ payload: string; id: string } | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let active = true;
    let streamed = false;
    async function load() {
      try {
        const response = await api.get(path);
        if (active && !streamed) setData(response.data as Conversation);
      } catch (failure) {
        if (active) setLoadError(coordinationError(failure));
      }
    }
    void load();
    const stream = new EventSource(`${API_BASE_URL}${path}stream/`, { withCredentials: true });
    stream.addEventListener("message", (event) => {
      streamed = true;
      setData(JSON.parse(event.data) as Conversation);
      setDisconnected(false);
      setLoadError("");
    });
    stream.addEventListener("error", () => setDisconnected(true));
    return () => {
      active = false;
      stream.close();
    };
  }, [path]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "nearest" });
  }, [data]);
  const readOnly = !canWrite || !data || data.closed || data.state === "not_assigned";

  async function send() {
    if (readOnly || busy || (!draft.trim() && assets.length === 0)) return;
    setBusy(true);
    setError("");
    const payload = JSON.stringify({ body: draft.trim(), assets: assets.map((asset) => asset.id) });
    if (retry.current?.payload !== payload) retry.current = { payload, id: crypto.randomUUID() };
    try {
      await api.post(path, { ...JSON.parse(payload), id: retry.current.id });
      setDraft("");
      setAssets([]);
      retry.current = null;
    } catch (failure) {
      setError(coordinationError(failure));
    } finally {
      setBusy(false);
    }
  }

  async function upload(selected: FileList | null) {
    if (!selected || readOnly) return;
    setBusy(true);
    setError("");
    try {
      const results = await Promise.allSettled(
        Array.from(selected).map(async (file) => {
          const result = await uploadAttachment(file);
          if (!result) throw new Error("The attachment could not be uploaded.");
          setAssets((current) => [...current, { id: result.id, name: file.name, url: null }]);
        })
      );
      const failed = results.find((result) => result.status === "rejected");
      if (failed?.status === "rejected") throw failed.reason;
    } catch (failure) {
      setError(coordinationError(failure));
    } finally {
      if (input.current) input.current.value = "";
      setBusy(false);
    }
  }

  return (
    <Dialog open onOpenChange={onClose}>
      <Dialog.Panel
        width={EDialogWidth.XXL}
        className="flex h-[min(44rem,calc(100dvh_-_2rem))] max-w-[calc(100%_-_1rem)] flex-col overflow-hidden"
      >
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-subtle p-4">
          <div className="min-w-0">
            <Dialog.Title>Agent conversation</Dialog.Title>
            <p className="text-body-xs-regular text-tertiary" role="status">
              {data?.closed
                ? data.state === "archived" || data.state === "not_assigned"
                  ? "Archived conversation"
                  : "Archive pending"
                : (data?.state.replaceAll("_", " ") ?? "Connecting")}
              {disconnected && " / Connection lost. Reconnecting..."}
            </p>
          </div>
          <button
            type="button"
            title="Close conversation"
            aria-label="Close conversation"
            className="grid size-8 shrink-0 place-items-center rounded hover:bg-layer-2"
            onClick={onClose}
          >
            <X className="size-4" />
          </button>
        </header>
        <div
          className="min-h-0 flex-1 space-y-5 overflow-y-auto p-4"
          role="log"
          aria-label="Conversation messages"
          aria-live="polite"
        >
          {data?.messages.map((message) => (
            <article key={message.id} className="min-w-0 space-y-2 border-l-2 border-subtle pl-3">
              <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-body-xs-regular text-tertiary">
                <span className="text-body-sm-medium text-primary">{message.author}</span>
                <time dateTime={message.created_at}>{new Date(message.created_at).toLocaleString()}</time>
                <span>{message.state}</span>
              </header>
              <p className="text-body-sm-regular break-words whitespace-pre-wrap">{message.body}</p>
              {message.assets.map((asset) =>
                asset.url ? (
                  <a
                    key={asset.id}
                    href={`${API_BASE_URL}${asset.url}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex flex-col gap-2 text-body-sm-regular break-all text-accent-primary"
                  >
                    {asset.mime_type?.startsWith("image/") && (
                      <img
                        src={`${API_BASE_URL}${asset.url}`}
                        alt={asset.name}
                        loading="lazy"
                        className="aspect-video w-full rounded border border-subtle object-contain"
                      />
                    )}
                    <span className="flex items-center gap-2">
                      <Paperclip className="size-4 shrink-0" />
                      {asset.name}
                    </span>
                  </a>
                ) : (
                  <p key={asset.id} className="text-body-xs-regular text-tertiary">
                    {asset.name} (unavailable)
                  </p>
                )
              )}
            </article>
          ))}
          {data?.messages.length === 0 && <p className="text-body-sm-regular text-tertiary">No conversation yet</p>}
          <div ref={bottom} />
        </div>
        {(error || loadError || data?.error) && (
          <p className="shrink-0 px-4 pb-3 text-body-sm-regular break-words text-danger-primary" role="alert">
            {error || loadError || data?.error}
          </p>
        )}
        {!data?.closed && (
          <form
            className="shrink-0 space-y-2 border-t border-subtle p-4"
            onSubmit={(event) => {
              event.preventDefault();
              void send();
            }}
          >
            <textarea
              aria-label="Message agent"
              rows={3}
              value={draft}
              disabled={readOnly || busy}
              onChange={(event) => setDraft(event.target.value)}
              className="w-full resize-none rounded border border-subtle bg-surface-1 p-3 text-body-sm-regular disabled:opacity-60"
            />
            {assets.map((asset) => (
              <div className="flex min-w-0 items-center gap-2 text-body-xs-regular" key={asset.id}>
                <span className="min-w-0 break-all">{asset.name}</span>
                <button
                  type="button"
                  title="Remove from message"
                  aria-label={`Remove ${asset.name} from message`}
                  disabled={busy}
                  className="grid size-6 shrink-0 place-items-center rounded hover:bg-layer-2"
                  onClick={() => setAssets((current) => current.filter((row) => row.id !== asset.id))}
                >
                  <X className="size-3" />
                </button>
              </div>
            ))}
            <div className="flex items-center justify-between gap-3">
              <input
                ref={input}
                type="file"
                multiple
                className="hidden"
                aria-label="Conversation attachments"
                onChange={(event) => void upload(event.target.files)}
              />
              <button
                type="button"
                title="Attach to work item"
                aria-label="Attach to work item"
                disabled={readOnly || busy}
                className="grid size-8 place-items-center rounded hover:bg-layer-2 disabled:opacity-50"
                onClick={() => input.current?.click()}
              >
                <Paperclip className="size-4" />
              </button>
              <Button
                type="submit"
                disabled={readOnly || busy || (!draft.trim() && assets.length === 0)}
                prependIcon={<Send className="size-4" />}
              >
                Send
              </Button>
            </div>
          </form>
        )}
      </Dialog.Panel>
    </Dialog>
  );
}
