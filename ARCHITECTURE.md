# co-writer-bot architecture

## Natural-language execution boundary

Free-form messages use native LLM function calling. The model does not return an
`intent`, and application code does not convert an intent back into a bracket command.

```text
Slack event
  -> deterministic dedup / bracket command / pending-card handling
  -> context snapshot (thread, work, episode, attachments)
  -> native LLM tool calling
       -> respond_with_answer
       -> ask_for_clarification
       -> one or more ordered, whitelisted executable functions
  -> JSON-schema and domain validation
  -> start the fully validated plan immediately
  -> show a Stop button carrying the exact run id
  -> registered adapter invokes the domain operation
  -> Stop consumes that run id and cancels only the current thread job
```

The safety target is not perfect language understanding. It is that a mistaken model
interpretation cannot escape the declared function schemas. Valid calls start immediately;
an exact code-generated run ID prevents stale Stop buttons from affecting another job.

## Ownership

- `bot/tool_router.py`: Slack-independent model boundary and
  `answer / clarification / tool_call` parsing.
- `bot/tool_router_slack.py`: immediate execution, progress cards, and exact-ID stopping.
- `bot/tool_registry.py`: the only natural-language executable whitelist; function
  schemas, code-owned risk levels, validators, and current domain adapters.
- `bot/openrouter_image.py::tool_chat`: transport for OpenRouter native function calls.
- `bot/dispatch.py`: Slack event ordering. Explicit bracket commands remain a direct,
  deterministic compatibility path.
- `bot/nl_router.py`: transitional context collector and legacy kill-switch code. Its
  `Route`/`ACTION_SPECS` execution path is no longer used for normal free-form messages.

`COWRITER_TOOL_ROUTER_ENABLED=0` restores the legacy free-form chain. For deployment
compatibility, the previous `COWRITER_ROUTER_ENABLED=0` switch has the same effect when
the new variable is unset.

## Hard rules

1. Never resolve a model-provided function name with `globals`, `getattr`, or imports.
2. Reject undeclared arguments and wrong JSON types before any handler is invoked.
3. Cross-check attachment IDs against the current Slack event, then pass only the
   selected attachment to the adapter.
4. Risk level is registry metadata. Ignore any model-provided confirmation flag.
5. Every valid natural-language execution plan starts immediately after full validation.
6. The progress card exposes only Stop. It carries an exact run ID; stale or mismatched IDs
   are rejected and can never stop a newer job.
7. Compound requests are represented as an ordered list of real whitelisted functions. Code
   validates every step before starting, then executes in that order under one Stop control.
8. Add a function only when its validator and offline boundary tests are added together.

Regression commands:

```bash
python3 -m tests.test_tool_registry
python3 -m tests.test_openrouter_tool_chat
python3 -m tests.test_tool_router_safety
python3 -m tests.test_tool_router_batch
```

For a large utterance set, call the pure router without Slack:

```bash
python3 scripts/test_tool_router_batch.py utterances.json --workers 5
```

The batch CLI accepts JSON, JSONL, and CSV, writes an atomic checkpoint after every
completed case, validates tool arguments, and reports immediate/stop-button policy. It
never invokes a registered executor.

## Migration direction

The registry adapters currently call established functions in `dispatch_cowriter.py` and
`dispatch_storyboard.py` directly. This keeps behavior stable while those large modules
are split. The next structural migration is:

```text
tool adapter -> application workflow -> domain service -> infrastructure adapter
```

Move one tool at a time. A tool is complete when it no longer constructs legacy command
text and its workflow has explicit typed inputs, state transitions, and tests.
