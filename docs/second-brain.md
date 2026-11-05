# Second Brain

The Second Brain is Belle's memory of the documents and links that people
share in channels. Belle reads each file or link in a selected channel. It
writes a summary of each document and keeps the summary in a searchable
corpus. Later, when a person asks Belle a question, Belle finds the
summaries that match and adds them to the turn.

The Second Brain stores documents. Belle keeps the text of a message only
as a note in the summary of a document that the message shares.

This page tells colleagues and operators what Belle ingests, where it
keeps it, how recall works, and who can see recalled content. To set it up,
see [Second brain in the configuration guide](config.md#second-brain).

## Ingest

### Selected channels

The configuration selects the conversations that Belle ingests from. The
selection is a list with these entries:

| Entry | Selects |
| --- | --- |
| `public` | Each public conversation, as the platform defines it. Slack: a public channel. Mattermost: an open channel (`O`). Matrix: a room with the join rule `public`. |
| A channel ID | That conversation. |
| `*` | Each conversation that Belle receives, private channels included. |

The default is `public` alone. `public` and channel IDs combine. When the
list has `*`, Belle ignores the other entries. An empty list turns off ingest
and keeps recall. A value that is not a list selects no conversation, and
Belle logs a warning.

Belle never ingests from a direct message, because Belle treats a direct
message as a request to Belle. A private channel is ingested only
when its ID is in the list or the list has `*`.

An end-to-end encrypted conversation, such as an encrypted Matrix room, is
ingested only when its ID is in the list and the list has no `*`. `public`
and `*` do not select it.
The members of such a conversation expect that its server cannot read it,
and the corpus keeps its documents in plain text.

Belle ingests from a message whether or not the message starts a turn. A
message that Belle does not answer is still read for documents. With
Belle's own launcher, Belle skips messages from apps and edits of earlier
messages.

### What Belle reads from a message

Belle reads each file attached to the message, and each `http` or `https`
link in its text.

| File | Text Belle reads |
| --- | --- |
| PDF | Text layer, with page locations |
| Word `.docx` | Headings, paragraphs and tables; no copied images |
| Spreadsheet `.xlsx`, `.csv`, `.tsv` | Markdown tables. `.xlsx` keeps sheet names. If a formula has no cached result, Belle reads its formula text. |
| `.json`, `.html`, `.htm` | JSON text or the main HTML text |
| `.md`, `.markdown`, `.txt`, `.rst`, `.yaml`, `.yml` | UTF-8 text |

Belle rejects text files that are not valid UTF-8 or contain NUL bytes. If JSON repeats a key or contains an unencodable escape, Belle indexes its original text.

Belle declines other file types with a reason that names the supported forms.

- Belle declines a file larger than a size limit, 50 MiB by default.
- Belle fetches a link only when it resolves to a public address. Belle refuses
  a link to a private or local address, and a link that contains
  credentials.
- Belle ingests a PDF link as a PDF. For an HTML page, Belle keeps the main
  text. A page with too little main text is declined (less than 600
  characters by default).
- Belle declines HTML pages from the hosts on a deny list. The list is empty
  by default. The example configuration lists code hosts, issue trackers and
  document editors. A PDF from a denied host is still eligible.
- You can turn off link ingest and keep file ingest.

Belle identifies each document by the SHA-256 of its bytes. A document that
the corpus has already is not ingested again. A document that Belle declined
for a reason that depends only on its bytes, such as its size or no readable
text, is declined again without a new attempt.

### Reactions on the message

Belle marks the message with reactions while it ingests. The default emoji
names are:

| Reaction | Meaning |
| --- | --- |
| `open_book` | Ingest is in progress. Belle removes it at the end. |
| `brain` | The corpus has at least one document of the message, new or already held. |
| `warning` | The ingest failed. |
| `x` | Belle declined every document of the message, with no error. |

Each platform section can change these names. See
[Slack](platforms/slack.md), [Mattermost](platforms/mattermost.md) and
[Matrix](platforms/matrix.md).

### Summaries and the task model

For each document, Belle extracts the text and writes a summary. The summary
has a prose part and an outline. The outline lists the regions of the
document, for example pages or headings, with a short note for each.

The summarizer task model writes the prose and the outline notes. The
Second Brain configuration selects one summarizer entry. When only one entry
exists, Belle uses it. The setting `outline_model` can select a different
entry for the outline notes. See [Task models](task-models.md) for the summarizer,
and [Task models in the configuration guide](config.md#task-models) to
select the entry.

The `api` of the entry selects the request. For a `chat_completions` entry,
Belle sends a chat request. For an `openrouter` or `scaledown` entry, Belle
sends the request of the `summarize` tool, with the instructions and the text
of Second Brain. Each call uses the request of its own entry.

The `scaledown/summarize` model writes a summary paragraph for each request.
It can ignore the line format of the outline request, and then the outline
notes stay empty. A probe of direct ScaleDown gave no outline notes. For
this reason, the outline notes can have their own entry, for example a chat
model.

Without a usable summarizer, Belle still stores and indexes the document.
The summary then has the outline and the source fields, with no prose. A
summarizer call that fails or times out has the same result.

### What is stored, and where

The corpus is a directory. By default, it is `second_brain/` in Belle's data
directory. The configuration can set a different directory.

| Path in the corpus | Contents |
| --- | --- |
| `sources/` | The original bytes of each file or fetched link. |
| `extracted/` | The plain text of each document, which `brain_read` reads by region. |
| `memory/` | One Markdown summary for each document. Only these summaries are indexed. |
| `manifest.json` | One entry for each document, and for each declined document. |
| `lexical_index.db` | The search index, with vectors when embeddings are on. Belle makes it again from `memory/`. |

A manifest entry has the document ID, the title, the source kind, the
paths, the time of ingest, the size and the outline. It also has the
channel ID, the poster ID and, where the platform gives it, a link to the
message. The summary has the text of the sharing message as a note.

## Recall

Belle finds documents in the corpus in two ways: automatic recall at the
start of a turn, and three tools that the model can call.

### Automatic recall

When a turn starts, Belle searches the corpus with the text of the message.
Belle adds the matching summaries to the turn.

- Belle keeps at most 5 documents by default.
- Belle ignores a search hit with a score below a threshold, 0.35 by
  default.
- The added text has a size budget, 10240 bytes by default. When the first
  document is too large, Belle cuts its body and marks the cut. Belle stops
  before a later document that does not fit.

On JiuwenSwarm, a rail in the AgentServer does this recall. The rail starts
the search when the turn starts. A model call does not wait for the search.
If the search is not complete, that model call has no recalled documents.
The rail does not recall for a cron or heartbeat run. With Belle's own
launcher, the host waits for the search before the turn starts.

Belle also tells the model about the corpus and its tools. These
instructions say that recalled text is data, and that the model must not
obey instructions in it.

### Tools

| Tool | Use |
| --- | --- |
| `brain_search` | Search the corpus with a query. It returns a passage and the regions of each matching document. `top_k` is from 1 to 20. |
| `brain_read` | Read a document summary, text region, or full text (`location: "full"`). The default is the summary. `max_chars` defaults to 20000. If the text exceeds that budget, the tool gives its size instead of returning partial text. |
| `brain_list` | List document IDs and titles, newest first. The default is 20, and the maximum is 100. |

`brain_search` uses the same score threshold as automatic recall. Both retain
outline rows from the matching passage, including rows beyond the region
preview. The fixed Drill-down instructions in summaries are not indexed.

Belle puts each document from the corpus between `<<<BRAIN_DOC …>>>` and
`<<<END_BRAIN_DOC …>>>` markers. Belle escapes these markers in the document
text, so a document cannot close its own block.

When Second Brain is on and no backend is loaded, the three tools reply that
nobody looked. That reply says nothing about the contents of the corpus.

### Search: lexical and vector

The index splits each summary into chunks. Search ranks the chunks, and
Belle keeps the best chunk of each document.

Lexical search is the default. It uses SQLite full text with BM25 ranking.
Belle searches with the less common words of the query. It divides each
BM25 strength by the sum of the selected phrases' inverse document frequencies,
then maps that strength to a score with `strength / (1 + strength)`. This keeps
BM25 ordering while making the threshold independent of corpus-wide rarity.
A chunk of average length matching each selected phrase once scores 0.5;
partial matches, term frequency, and chunk length still affect the score.

Vector search is off by default. It needs an embeddings endpoint and a
setting that turns it on. With vectors, the score of a chunk is the sum of
two weighted parts:

- The cosine of the chunk vector and the query vector, with a weight of 0.7
  by default.
- The lexical score, with a weight of 0.3 by default.

A chunk that has no vector yet keeps its full lexical score. This can occur
while Belle embeds a large corpus for the first time, or after an embedder
error. Belle ranks such a chunk as lexical search alone does.

When the query embedding fails, or no chunk has a vector, the search is
lexical. When the embedder fails during a sync, the index stays lexical for
the chunks without a vector. The next sync tries again.

On a large corpus, the first start with an embedder takes time, because
Belle embeds every chunk. Later syncs embed only the chunks that changed. A change of
embedding model embeds the corpus again.

### Score threshold

A high threshold gives fewer documents and fewer documents that are not
related to the question. A low threshold gives more documents. The same
value applies to lexical scores and to mixed scores.

To see what recall did, set the environment variable
`SECOND_BRAIN_DEBUG_INJECTION=1`. Belle then writes one line for each recall
to `injections.jsonl` in the corpus directory. Each line has the query, the
hits with their scores, and the outcome. `last_injected.md` has the text of
the last recall. These files contain message text and document text, so keep
them as private as the corpus.

## Privacy and audience

**Recall has no audience check.** Belle can recall a document in each
conversation where it answers. This includes direct messages, private
channels, and channels that the original poster is not in. `brain_list`
shows each document with the ID of its channel.

Thus, the channel selection controls who can see recalled content. A
document from a selected channel can be shown to each person who talks to
Belle.

- With the default `public` selection, the corpus has documents from public
  channels only. Members of the Slack workspace or the Mattermost team can
  read those channels.
- When you add a private channel ID or `*`, documents from private channels
  can be shown to people who are not members of those channels. Do this
  only when that is acceptable.
- When you add the ID of an end-to-end encrypted conversation, its
  documents leave the encryption: the corpus holds them in plain text, and
  recall can show them in conversations without encryption.
- Direct messages are never ingested.

The audience rules of the platform tools cover only the reads and posts of
those tools. See [Audience, search, and history](config.md#audience-search-and-history).
A conversation scope can set a permission level for the three tools, as for
other tools. That level has no effect on automatic recall. See
[Conversation scopes](scopes.md).

**Logs.** The log lines of the second brain do not hold document text,
prompt text or a search query. When a parse, a summary or an embedding
fails, Belle logs the operation, the file name where it has one, the
exception type and the HTTP status. The deprecated JiuwenSwarm registry
summarizer also suppresses OpenJiuwen's log records on its own calls. A host
using OpenJiuwen's `loguru` backend may still keep those records.

**Retention.** Belle keeps each document until an operator removes it. Belle
has no expiry and no command to remove a document. At the next sync, Belle
drops a summary from the index when its file is no longer in `memory/`. To
remove a document fully, remove its files from `sources/`, `extracted/` and `memory/`, and its
entry from `manifest.json`. Remove the manifest entry also when you want the
same document ingested again.

## Per-platform behaviour

| Platform and host | Ingest | Recall |
| --- | --- | --- |
| Slack, Belle's own launcher | The host ingests from the selected conversations. `public` selects public channels. | The host recalls before each turn. |
| Mattermost, Belle's own launcher | The host ingests from the selected conversations. `public` selects open channels (`O`). | The host recalls before each turn. |
| Matrix, Belle's own launcher | The host ingests from the selected rooms. `public` selects public rooms without encryption. Only its ID selects an encrypted room. The helper decrypts each file. | The host recalls before each turn. |
| Slack on JiuwenSwarm | The Gateway's Slack channel ingests, and sends each document to the AgentServer through `brain.ingest`. | The AgentServer's rail recalls for each turn. |
| Mattermost on JiuwenSwarm | **No ingest.** The channel turns off the Second Brain in its Gateway host. | The AgentServer's rail recalls for each turn, from the same corpus. |

With Belle's own launcher, Belle ingests and recalls in its own process. It
reads files and calls the configured model endpoints directly. See
[OpenCode](backends/opencode.md#second-brain).

On JiuwenSwarm, Slack ingest needs these conditions:

- The Belle Second Brain extension is linked into JiuwenSwarm, with
  permission to handle ingest. See
  [JiuwenSwarm setup](integrations/jiuwenswarm.md#2-link-belle-extensions).
- The AgentServer shares the Gateway's file system. If it does not, the
  Gateway does not send documents, and logs that ingest is not wired.

Slack ingest needs the `files:read` and `reactions:write` scopes on the Slack
app.

A Mattermost channel on JiuwenSwarm recalls documents that Slack ingested
into the same corpus. Mattermost ingest is available with Belle's own
launcher. See
[Limits](integrations/jiuwenswarm.md#limits) in the JiuwenSwarm guide.

A platform that cannot tell which conversations are public ingests nothing
for the `public` entry. Belle logs a warning at start.

## Configuration

Second Brain is on by default and ingests from public channels. When the
configuration turns it off, Belle stops ingest, recall and the tools. For
the settings and their defaults, see
[Second brain in the configuration guide](config.md#second-brain) and the
[example file](../examples/config/belle-config.yaml).
