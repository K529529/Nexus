# Offline tokenizer resources

These assets are loaded locally. Nexus never downloads tokenizer assets at runtime.
No model weights, transformers, torch, prediction service or tokenizer API is used.
`tokenizers` 0.23.3 performs BPE; Jinja2 3.1.6 renders the pinned Qwen template.

| Resource | Upstream revision | Original SHA256 |
| --- | --- | --- |
| DeepSeek tokenizer.json | `deepseek-ai/DeepSeek-V4-Flash` `60d8d70770c6776ff598c94bb586a859a38244f1` | `8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf` |
| DeepSeek encoding/encoding_dsv4.py | same | `bdbd57c132a1b3725042323d02b98b9d1df28e5f388f134399555d041f5055e0` |
| Qwen tokenizer.json | `Qwen/Qwen3.5-35B-A3B` `59d61f3ce65a6d9863b86d2e96597125219dc754` | `5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42` |
| Qwen chat_template.jinja | same | `a4aee8afcf2e0711942cf848899be66016f8d14a889ff9ede07bca099c28f715` |

Sources: [DeepSeek](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/tree/60d8d70770c6776ff598c94bb586a859a38244f1),
[Qwen](https://huggingface.co/Qwen/Qwen3.5-35B-A3B/tree/59d61f3ce65a6d9863b86d2e96597125219dc754).

JSON assets are losslessly gzip-compressed (mtime=0). DeepSeek's encoding module is
vendored unchanged in `nexus/_vendor/encoding_dsv4.py`; lint/type overrides apply
only to that upstream namespace. Adjacent license files preserve the upstream MIT
and Apache-2.0 notices. The encoder renders protocol envelopes, **not** the Nexus
Compaction Prompt. Nexus prompts and summary schemas are unchanged.

Selection is restricted to the measured provider/model pairs, not a name substring
or a generic OpenAI tokenizer assumption. See `docs/next/final-p1-hardening.md` for
measured residuals, scope, fallback and limitations.
