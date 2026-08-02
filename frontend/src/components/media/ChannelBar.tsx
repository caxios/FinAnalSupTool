/**
 * ChannelBar.tsx
 * ──────────────
 * Channel picker + inline manager for the video feeds.
 *
 * A dropdown selects "All my channels" or one specific saved channel; a
 * "Manage channels" toggle reveals an add box (URL / @handle / UC id / name)
 * and a removable list. The list is persisted on the backend (channels.json).
 */

import { useEffect, useState } from "react";
import type { ChannelInfo } from "../../types";
import { getChannels, addChannel, deleteChannel } from "../../api";

interface Props {
  /** Selected channel id, or "all". */
  value: string;
  onChange: (channelId: string) => void;
}

export default function ChannelBar({ value, onChange }: Props) {
  const [channels, setChannels] = useState<ChannelInfo[]>([]);
  const [manageOpen, setManageOpen] = useState(false);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const r = await getChannels();
      setChannels(r.channels);
    } catch {
      /* non-fatal */
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function handleAdd() {
    const v = input.trim();
    if (!v || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await addChannel(v);
      setChannels(r.channels);
      setInput("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not add channel.");
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(id: string) {
    setBusy(true);
    setError(null);
    try {
      const r = await deleteChannel(id);
      setChannels(r.channels);
      if (value === id) onChange("all"); // deselect if the removed one was active
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not remove channel.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="channel-bar">
      <select
        className="channel-select"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="all">All my channels</option>
        {channels.map((c) => (
          <option key={c.channel_id} value={c.channel_id}>
            {c.title}
          </option>
        ))}
      </select>

      <button
        className="channel-manage-btn"
        onClick={() => setManageOpen((o) => !o)}
      >
        {manageOpen ? "Done" : "Manage channels"}
      </button>

      {manageOpen && (
        <div className="channel-manager">
          <div className="channel-add-row">
            <input
              className="channel-input"
              placeholder="Channel URL, @handle, or UC… id"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  handleAdd();
                }
              }}
              disabled={busy}
            />
            <button
              className="channel-add-btn"
              onClick={handleAdd}
              disabled={busy || !input.trim()}
            >
              Add
            </button>
          </div>

          {error && <div className="channel-error">{error}</div>}

          {channels.length === 0 ? (
            <div className="channel-empty">
              No channels yet — add one above (e.g. a YouTube URL or @handle).
            </div>
          ) : (
            <ul className="channel-list">
              {channels.map((c) => (
                <li key={c.channel_id} className="channel-item">
                  <span className="channel-title">{c.title}</span>
                  {c.handle && <span className="channel-handle">@{c.handle}</span>}
                  <button
                    className="channel-remove"
                    onClick={() => handleDelete(c.channel_id)}
                    disabled={busy}
                    title="Remove channel"
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
