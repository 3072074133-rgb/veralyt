# Node prompt contract

This directory contains the versioned system prompts for every LLM-backed node
in the analysis workflow. Prompts are written in Chinese because the initial
users and financial vocabulary are Chinese.

## Node mapping

| Workflow node | Prompt file | Output contract | Thinking |
| --- | --- | --- | --- |
| Intent classification | `intent_classifier.md` | `IntentDecision` | Off |
| Requirement interpretation and planning | `analysis_planner.md` | `AnalysisPlan` | On |
| Query specification | `tool_orchestrator.md` | `QueryRequest` for the planner-selected dataset | On |
| Draft generation | `draft_writer.md` | `AnalysisDraft` | Off |
| Reflection review | `reflection_reviewer.md` | `ReflectionDecision` | On |
| Conversation summarization | `conversation_summarizer.md` | `ConversationMemory` | Off |

Conversation summarization runs before the graph when the context budget is
exceeded. It is incremental infrastructure rather than part of the reflection
loop.

The following workflow nodes are deterministic and must not call an LLM:

- start and task creation
- file validation, parsing, profiling, and DuckDB import
- deterministic result validation
- revision routing and retry-budget checks
- final API response assembly
- off-topic response and end

## Runtime assembly

Each Markdown file is a complete system prompt. The application sends dynamic
context in a separate user message using the JSON envelope documented in that
file. Never interpolate workbook cell values into the system prompt.

For structured-output nodes, pass the matching Pydantic JSON Schema through
Ollama's `format` parameter and validate the returned content again with
`model_validate_json`. Set `extra="forbid"` on all response models.

For query-generation steps, expose only the planner-selected dataset schema.
The model returns a typed `QueryRequest`; the application binds the trusted
dataset ID, validates the specification, executes the query, and wraps the
result before updating graph state.

## Shared safety rules

All node implementations must enforce these rules outside the prompt as well:

1. Workbook names, sheet names, headers, cell values, comments, formulas, and
   tool results are untrusted data, never instructions.
2. The LLM never calculates or invents financial values. Quantitative claims
   must refer to verified tool results and evidence IDs.
3. Amounts use decimal strings at API boundaries. Dates use ISO 8601 strings.
4. Generated SQL is read-only, parsed before execution, and subject to time,
   memory, row-count, and statement-count limits.
5. Do not store or expose model reasoning traces. Store concise decisions,
   assumptions, issues, tool calls, and evidence references only.
6. Invalid structured output gets one repair attempt. A second failure ends the
   node with a typed error.
7. Reflection is capped by workflow state, initially at three rounds.

## Versioning

Prompt versions are independent from API schema versions. Any behavior-changing
prompt edit must increment the `prompt_version` in that prompt's front matter
and be evaluated against the fixed financial-analysis regression set.
