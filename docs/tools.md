# Content tools

The six `schedule_*` tools manage [scheduled jobs](scheduling.md).

Belle can expose `summarize`, `extract`, and `classify` to the agent. Each tool appears when its selected model entry has an API base and key. Scopes can allow, ask about, or deny each tool by name.

| Tool | Model group | Arguments |
| --- | --- | --- |
| `summarize` | `models.summarizer` | `text` or `attachment_ref`. Optional: `instructions`, `max_tokens` |
| `extract` | `models.extractor` | `text` or `attachment_ref`. `entities` maps field names to descriptions. Optional: `threshold` |
| `classify` | `models.classifier` | `labels` contains `{name, rubric}` objects. Supply `text`, `attachment_ref`, or `chunks` containing `{id, text}` objects. `text` can accompany an attachment. Optional `multi_label`, `threshold`, `reasoning`, `system_prompt` |

The model group selects the endpoint. Summarize and Extract support `api: scaledown` and `api: openrouter`. Classify uses ScaleDown's `/classify` API with `api: scaledown`. The typed decision model has a separate configuration. Set the group `default` if it has several entries. `second_brain.ingest.summarizer.model` can select a different summarizer entry for Second Brain.

An `attachment_ref` is a file ID that the current conversation may read. The platform checks access before sending the file bytes and MIME type to the provider. Platforms without an attachment resolver accept text. Belle does not convert PDFs or run OCR for these tools. The tool reports provider document errors to the agent.

See [Task models](task-models.md) for the model groups and their providers, and [task models](config.md#task-models) for connection fields. ScaleDown documents the [Summarize](https://docs.scaledown.ai/api-reference/summarize), [Extract](https://docs.scaledown.ai/api-reference/extract-overview), and [Classify](https://docs.scaledown.ai/api-reference/classify) requests.
