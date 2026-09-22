# Recorded model responses

Real `qwen3.5:9b` output for the pull requests in `../prs/`, captured from the
`pr.reviews` topic. Thinking off, `num_ctx` 8192, `num_predict` 3000,
`temperature` 0, `presence_penalty` 0.

These exist so the verification and rubric tests run with no Ollama, no GPU and no
network. Those two are the riskiest logic in the service and the part a reviewer is
most likely to poke at, so they must be testable on a machine that cannot run a model.

Re-record by running the stack and re-reading the topic. The model is deterministic at
`temperature` 0, but a different model version will produce different findings, which
is the point: these capture what this model actually said, not what it ought to say.
