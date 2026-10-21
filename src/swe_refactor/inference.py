"""Optional vLLM backend. Importing the CLI never requires GPU packages."""

from __future__ import annotations

import time


class VLLMBackend:
    def __init__(self, protocol: dict):
        from transformers import AutoTokenizer

        self.protocol = protocol
        model = protocol["model"]
        self.tokenizer = AutoTokenizer.from_pretrained(
            model["model_id"], revision=model["revision"], trust_remote_code=False)
        self.engine = None
        self.engine_load_seconds = None
        self.engine_error = None

    def encode(self, messages: list[dict]) -> tuple[str, list[int]]:
        prompt = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        # The chat template already supplies its special tokens.
        ids = self.tokenizer.encode(prompt, add_special_tokens=False)
        return prompt, ids

    def complete(self, token_ids: list[int]) -> dict:
        from vllm import LLM, SamplingParams

        initialized_seconds = 0.0
        if self.engine_error is not None:
            raise RuntimeError(f"engine initialization previously failed: {self.engine_error}")
        if self.engine is None:
            started = time.perf_counter()
            model = self.protocol["model"]
            try:
                self.engine = LLM(
                    model=model["model_id"], revision=model["revision"],
                    tokenizer_revision=model["revision"], trust_remote_code=False,
                    dtype="bfloat16", quantization=None, max_model_len=32768,
                    language_model_only=self.protocol.get("engine_language_model_only", True),
                    tensor_parallel_size=self.protocol["tensor_parallel_size"],
                    gpu_memory_utilization=self.protocol["gpu_memory_utilization"],
                    seed=self.protocol["experiment"]["seed"], max_num_seqs=1,
                    enable_prefix_caching=False)
            except Exception as exc:
                self.engine_error = f"{type(exc).__name__}: {exc}"
                raise
            self.engine_load_seconds = time.perf_counter() - started
            initialized_seconds = self.engine_load_seconds
        result = self.engine.generate(
            [{"prompt_token_ids": token_ids}],
            SamplingParams(n=1, temperature=0.0, top_p=1.0, max_tokens=8192,
                           seed=self.protocol["experiment"]["seed"]),
            use_tqdm=False)[0].outputs[0]
        return {"text": result.text, "token_ids": result.token_ids,
                "finish_reason": result.finish_reason, "stop_reason": result.stop_reason,
                "engine_initialization_seconds": initialized_seconds}
