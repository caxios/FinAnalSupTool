# Step 7: Frontend Role-Based Chat Implementation Plan

## Goal
Implement the "Role-Based Chat" feature in the React frontend. Currently, the `ChatPanel` only communicates with the "General Assistant" (Omniscient Bot) because it does not pass an `agent_id` to the backend. This update will add a dropdown to the Chat UI, allowing the user to select specific agents (like the Manager Agent or SEC Filings Agent) to chat with, isolating the context to their specific reports and debate transcripts.

## 1. Update API Client (`frontend/src/api.ts`)
Modify the `askChat` function to accept an optional `agent_id` parameter and include it in the POST request body.

```typescript
export async function askChat(
  question: string,
  history: ChatMessage[],
  agent_id?: string
): Promise<ChatResponse> {
  return fetchJson<ChatResponse>(`${API_BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, history, agent_id }), // Add agent_id here
  });
}
```

## 2. Update Chat Panel UI (`frontend/src/components/ChatPanel.tsx`)
Add a `<select>` dropdown menu to the header of the Chat Panel so the user can switch between agents.

### State Changes
1. Add a new state: `const [selectedAgent, setSelectedAgent] = useState<string>("general");`
2. Add a `useEffect` or `onChange` handler that clears the `messages` array whenever `selectedAgent` changes. (We do not want to mix conversation histories between different agents).

### UI Additions
In the `.chat-header`, next to the title, add a `<select>` element.
The options should be:
- `value="general"`: "General Assistant (All Data)"
- `value="manager"`: "Manager Agent"
- Map over `AGENT_ORDER` and `AGENT_NAMES` (imported from `agentMeta.ts`) to generate options for the 6 field agents.

### API Call Update
Update the `askChat` call inside the `send` function to pass the selected agent:
`const res = await askChat(q, history, selectedAgent === "general" ? undefined : selectedAgent);`

## 3. Style the Dropdown (`frontend/src/index.css`)
Add appropriate CSS classes for the new select dropdown to ensure it matches the dark, glassmorphism aesthetic of the existing application.
- Class suggestion: `.chat-agent-select`
- Needs dark background (`#1e1e1e` or similar), subtle border, and padding.
- Should fit cleanly within the `.chat-header` flex layout.

## 4. Verification
- Open the frontend, open the Chat panel.
- Ensure the default is "General Assistant".
- Change the dropdown to "Manager Agent". Verify that the chat clears.
- Ask the Manager Agent a question like "What was the result of the debate?".
- Inspect the Network tab to ensure `{"agent_id": "manager"}` is being sent in the POST body to `/chat`.
