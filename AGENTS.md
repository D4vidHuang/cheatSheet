# Repository conventions

- Preserve `data/SWE-Refactor/` originals; never edit upstream data or scripts in place.
- Keep raw benchmark files, reference answers, model weights, caches, and run outputs out of Git. Commit compact hashes, aggregate reports, configs, and our source code.
- Do not print credentials from upstream configuration. Use environment variables for credentials.
- Solver inputs may contain only explicitly allowed BEFORE code and the task instruction. AFTER code, diffs, author results, coverage, and evaluation verdicts belong to the evaluator.
- Use immutable model revisions and record data/config/source/environment hashes. Do not silently replace Qwen3.5-8B with another model.
- Treat a model output as correct only after independent intended-refactoring detection AND compilation AND tests with a verified baseline. Format/exact-file scores are diagnostics.
- Never infer successful execution from Slurm submission or completion alone. Retain raw outputs, status denominators, receipts, and verification logs.
- Keep one clean checkout per evaluation task and version; never reset a user's existing repository.
- Before publishing benchmark scores, run the meaningful unit tests and independently audit denominators, missing outputs, timeouts, tool errors, and gold/no-op controls.
- Explain research decisions and limitations in Chinese in `docs/`; use Python 3.11+ and the standard library for the core CLI.
