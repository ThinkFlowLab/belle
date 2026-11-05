// The Belle relay. Each hook posts its event to Belle and applies the fields
// of the answer. The plugin holds no policy: it names a hook, forwards the
// event, waits, and applies. A failed round trip throws, so a hook never
// continues with an unexamined default.
//
//   hook             answer fields the relay applies
//   prompt           text, skills, agents; reject (message) refuses the
//                    prompt; agent and model switch the session first;
//                    response (true) relays this turn's provider responses
//   context          tools (names to keep), system_append, messages_drop (ids),
//                    options (merged)
//   execute.before   deny (message), tool, input
//   evaluate         effect, message
//   http.response    body (replacement provider response body). Asked only
//                    on a turn whose prompt answer set `response`, because
//                    reading the body buffers it and stops streaming.
//
// The prompt and context events reach Belle with each file's contents replaced
// by {"omitted": <length of the contents>}. OpenCode puts every file of the
// session into each context event, and Belle answers neither hook from them.
//
// It ships as package data (belle_agent.integrations.opencode.plugin), so the
// plugin and the Python adapter version together; the image copies it into the
// project plugin directory.

const URL = process.env.BELLE_RELAY_URL
const TOKEN = process.env.BELLE_RELAY_TOKEN ?? ""

// A JSON.stringify replacer that stubs file contents. They sit in the `data` of
// a prompt's file or a media source, a `data:` URI of a file or tool result, and
// the `text` of a text file inlined into the history.
function omitFiles(this: any, key: string, value: unknown) {
  if (typeof value !== "string") return value
  const file =
    (key === "data" && ("mime" in this || "mediaType" in this)) ||
    (key === "uri" && value.startsWith("data:")) ||
    (key === "text" && this.metadata?.attachment !== undefined)
  return file ? { omitted: value.length } : value
}

async function ask(hook: string, event: unknown, replacer?: (this: any, key: string, value: unknown) => unknown) {
  // `timeout: false`: without it the runtime aborted a round trip that Belle
  // held for 900 seconds after 357 seconds ("The operation timed out"). With
  // it the same hold returned. A person may take that long to answer.
  const sessionID = (event as { sessionID?: string })?.sessionID
  const response = await fetch(`${URL}/hook/${hook}`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${TOKEN}`,
      ...(sessionID ? { "x-belle-session": sessionID } : {}),
    },
    body: JSON.stringify(event, replacer),
    timeout: false,
  } as RequestInit)
  if (!response.ok) throw new Error(`belle relay ${hook}: HTTP ${response.status}`)
  return (await response.json()) as any
}

export default {
  id: "belle.relay",
  async setup(ctx: any) {
    // Sessions whose provider response bodies Belle asked for on this turn.
    const buffered = new Set<string>()

    await ctx.session.hook("prompt", async (event: any) => {
      const { reject, agent, model, response, ...prompt } = await ask("prompt", event, omitFiles)
      if (reject) throw new Error(reject)
      if (response) buffered.add(event.sessionID)
      else buffered.delete(event.sessionID)
      if (agent) await ctx.session.switchAgent({ sessionID: event.sessionID, agent })
      if (model) await ctx.session.switchModel({ sessionID: event.sessionID, model })
      Object.assign(event.prompt, prompt)
    })

    await ctx.session.hook("context", async (event: any) => {
      const answer = await ask("context", { ...event, tools: Object.keys(event.tools) }, omitFiles)
      if (answer.tools)
        for (const name of Object.keys(event.tools)) if (!answer.tools.includes(name)) delete event.tools[name]
      for (const text of answer.system_append ?? []) event.system.push({ type: "text", text })
      for (let i = event.messages.length - 1; i >= 0; i--)
        if ((answer.messages_drop ?? []).includes(event.messages[i].id)) event.messages.splice(i, 1)
      Object.assign(event.options, answer.options ?? {})
    })

    await ctx.tool.hook("execute.before", async (event: any) => {
      const answer = await ask("execute.before", event)
      if (answer.deny) throw new Error(answer.deny)
      if (answer.tool) event.tool = answer.tool
      if (answer.input) event.input = answer.input
    })

    await ctx.permission.hook("evaluate", async (event: any) => {
      Object.assign(event, await ask("evaluate", event))
    })

    await ctx.session.hook("http.response", async (event: any) => {
      if (event.kind !== "primary" || !buffered.has(event.sessionID)) return
      const body = await event.response.clone().text()
      const answer = await ask("http.response", { sessionID: event.sessionID, body })
      if (typeof answer.body === "string")
        event.response = new Response(answer.body, { status: 200, headers: event.response.headers })
    })
  },
}
