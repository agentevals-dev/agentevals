# TODOs

## Eval set schema migration
- **What:** Update `examples/langchain_agent/eval_set.json` and `examples/strands_agent/eval_set.json` to use documented schema (`eval_id`/`final_response`) instead of stale format (`case_id`/`agent_content`).
- **Why:** The new openai-agents eval set uses the correct format. Without this fix, the langchain/strands examples become the odd ones out and teach new contributors the wrong pattern.
- **Context:** Discovered during openai-agents zero-code integration review (2026-03-29). Schema loader likely accepts both formats so no behavior change. Reference: `docs/eval-set-format.md`, `samples/eval_set_helm.json` (already correct).
- **Blocked by:** Nothing. Two-line JSON key rename per file.
