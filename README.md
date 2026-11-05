<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/docs/title-dark.svg">
    <img src="assets/docs/title-light.svg" alt="Belle" width="240">
  </picture>
</h1>

<p align="center">
  <strong>Belle rings when it matters</strong><br>
  An AI coworker for team chat that answers from team knowledge, welcomes new members, digests shared reading, and stays quiet the rest of the time.
</p>

<p align="center">
  <strong>
    <a href="#impact-in-numbers">Numbers</a> ·
    <a href="#team-usage-examples">Examples</a> ·
    <a href="docs/installation.md">Install</a> ·
    <a href="docs/integrations/jiuwenswarm.md">Configure</a> ·
    <a href="docs/skills.md">Skills</a>
  </strong>
</p>

## Get started

- [Install the Python package](docs/installation.md).
- [Use Belle with JiuwenSwarm](docs/integrations/jiuwenswarm.md) for the matching JiuwenSwarm build branches and a Slack configuration example.
- [Run Belle on an OpenCode server](docs/backends/opencode.md) (experimental) for setup and deployment requirements.
- [Run Belle on a Docker Agent server](docs/backends/docker-agent.md) (experimental) for setup and deployment requirements.
- [Run Belle with an agent that speaks the Agent Client Protocol](docs/backends/acp.md) (experimental).
- [Import skills and test probes](docs/skills.md).
- [Configure Belle](docs/config.md), [content tools](docs/tools.md), [task models](docs/task-models.md), and [conversation scopes](docs/scopes.md).
- Connect a platform: [Slack](docs/platforms/slack.md), [Mattermost](docs/platforms/mattermost.md) (experimental), [Matrix](docs/platforms/matrix.md) (experimental, with end-to-end encrypted rooms) or [Discord](docs/platforms/discord.md) (experimental). See [Platforms](docs/platforms/README.md).
- [Compare the platforms and the harnesses](docs/feature-matrix.md): what works where.
- [Understand the Second Brain](docs/second-brain.md): what it ingests, how it recalls, and who sees what.
- [Run the tests](docs/testing.md) for the core or a chosen backend.
- [View local status with the operator TUI](docs/tui.md).

## Belle in a nutshell

Belle sits between a team's chat platforms and one agent harness. It decides when to speak and what each conversation may use. The harness runs each turn with the chat model, and Belle calls task models for decisions and document chores.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/docs/nutshell-dark.svg">
  <img src="assets/docs/nutshell-light.svg" alt="Belle between the chat platforms (Slack, Discord, Mattermost, Matrix) and one harness (JiuwenSwarm, OpenCode v2, Docker Agent, ACP), with its chat and task models." width="880">
</picture>

## How Belle works with a team

### One Belle for the team

Five people should not mean five agents. When each person brings an assistant, one question in a shared room sets off a flood of answers. The team shares one Belle instead, which answers once.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/docs/one-belle-dark.svg">
  <img src="assets/docs/one-belle-light.svg" alt="Two planning rooms. In the first, each person brings an assistant, and one question sets off 38 messages, 5 of them from people. In the second, the team shares Belle, which answers once." width="880">
</picture>

### Quiet until it can help

In a shared channel, Belle reads along and answers when it has something useful to add. Saying nothing is a decision it makes, the same as answering. See the [ambient mode numbers](#ambient-mode).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/docs/ambient-dark.svg">
  <img src="assets/docs/ambient-light.svg" alt="Four people plan lunch, and Belle stays quiet. Then Eve asks where the travel policy is, and Belle answers." width="680">
</picture>

### It knows who is in the room

Rules for each channel and each person say what Belle may do, and where. Before Belle posts to a conversation that has more readers than the one the content came from, it asks a person. On Slack, [`enforce_audience: false`](docs/platforms/slack.md#the-audience-rule) turns this check off. Second Brain recall has no such check: see [Second Brain privacy](docs/second-brain.md#privacy-and-audience). A rule can also make an action wait for one person's approval. In Ops, only David approves. Belle turns down Bob's click, and the request waits for David.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/docs/boundaries-dark.svg">
  <img src="assets/docs/boundaries-light.svg" alt="In Pricing, Belle asks which version to post and asks before posting to a wider audience. In Ops, Bob's approval is turned down, David approves, and Belle restarts the pods." width="880">
</picture>

### On a schedule or in the background

Belle posts scheduled reports, such as a morning digest of what the channel decided. Long work runs in the background: Belle says it started, answers other questions meanwhile, and posts the result when it finishes.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/docs/background-dark.svg">
  <img src="assets/docs/background-light.svg" alt="In Product, Belle posts a 09:00 digest and answers a question about it. In Release, Belle reviews crash reports in the background, answers other questions meanwhile, and posts the result." width="880">
</picture>

## Impact in numbers

### Second Brain

We tested the Second Brain on the [τ-knowledge](https://taubench.com/blog/tau-knowledge.html) benchmark, which evaluates how well an agent performs the tasks of a customer service chatbot. We used the benchmark's harness with two sets of tools. The first is the default All Tools: a shell and two document retrieval tools, one BM25 and one embeddings-based. The other is the Second Brain, with no shell and no other way to read the documents.

The results using the `muse-spark-1.3` LLM are below:

| Arm | Score |
| --- | --- |
| All Tools | 54.6% (53/97) |
| Second Brain | **56.7% (55/97)** |

We have also tested the Second Brain on 500 Confluence documents from [EnterpriseRAG-Bench](https://arxiv.org/abs/2605.05253). 64 questions were asked using `muse-spark-1.3`. Four arms were evaluated:

1. Question, no documents: Shows that the model cannot get the answer from its weights
2. Question + Golden Document(s) in the prompt: Measures the ceiling the LLM can reach
3. Baseline: We use OpenSearch, the same as the original EnterpriseRAG-Bench paper
4. Second Brain

We use the same EnterpriseRAG-Bench LLM-as-a-Judge script to score the outcomes.

| Arm | Correct | Correctness | Completeness | Combined | Doc recall |
| --- | --- | --- | --- | --- | --- |
| No Documents | 0/64 | 0.0% | 7.7% | 0.0% | 0.0% |
| Gold Documents | **57/64** | **89.1%** | 87.9% | **87.1%** | 100.0% |
| Baseline (RAG) | **57/64** | **89.1%** | 74.3% | 71.8% | 91.9% |
| Second Brain | **57/64** | **89.1%** | **90.0%** | _85.2%_ | **97.6%** |

In the table above, Correct counts the questions that got the right answer, on a yes/no basis. Completeness is the average share of the expected information that the final answer includes. Combined is the average Completeness when each wrong answer counts as a completeness of 0, so a wrong answer gets no partial credit.

On this benchmark, the Second Brain matched the baseline's 57/64 correct answers with more complete answers (90.0% against 74.3%), and its combined score, 85.2%, is close to the 87.1% reached with the gold documents in the prompt.

### Ambient Mode

We test how ambient mode performs against a set of 100 conversations. In half of them it is expected to stay silent (negative verdict) and in the other half it has to speak. We further divide it into easy and hard cases.

```
# Easy

Gus: who can write up the action items from the retro?
Ivo: I can't, I'm on call
Gus: ok then this one goes to belle: top 5 action items from the retro doc please

## Expect the agent to react

# Hard

Ivo: the cert on api.example.com expires Oct 3
Ben: I'll rotate it Monday then
Ben: Monday the 5th, I mean

## Expect the agent to reply with a warning: Oct 3 is a Saturday, so rotating on the 5th is two days after expiry

```

We run each prompt three times and measure which was the most frequent outcome.

| | False positives Easy | False negatives Easy | False positives Hard | False negatives Hard | Accuracy |
| --- | --- | --- | --- | --- | --- |
| **Performance** | 1/25 (4%) | 1/25 (4%) | 2/25 (8%) | 7/25 (28%) | **89%** |

Errors lean toward silence: 8 false negatives against 3 false positives. When in doubt, the agent stays silent rather than adding noise to the channel.

## Team usage examples

The examples below show intended workflows. Follow the linked setup guides for configuration and deployment requirements.

### Onboarding new members

The `welcome-onboarding` skill writes a welcome message for a new member, from what Belle knows about the team and the project. With JiuwenSwarm, the [example welcome rule](docs/integrations/jiuwenswarm.md#4-belle-config) detects new members and sends them a message.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/docs/welcome-onboarding-dark.svg">
  <img src="assets/docs/welcome-onboarding-light.svg" alt="Belle welcomes a new member, Charlie, with an orientation about the team and about Belle." width="680">
</picture>

### Reading materials

Use the `reading-digest` skill to share articles and papers with the team. Belle posts the link with a one-sentence summary, then outlines the main points in a thread reply. Each member can then decide whether to read it in full.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/docs/reading-digest-dark.svg">
  <img src="assets/docs/reading-digest-light.svg" alt="Belle shares a paper with a one-sentence summary, then posts the key points and the method in a thread reply." width="680">
</picture>

### Following team tasks

The `slack-daily-update` skill reports the status of the tasks in a Slack List, one row per person.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/docs/daily-update-dark.svg">
  <img src="assets/docs/daily-update-light.svg" alt="Belle's daily update, with one row per person. Alice's row opens to show her tasks, then Bob's row." width="600">
</picture>
