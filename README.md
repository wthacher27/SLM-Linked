# SLM-Linked

A small language model that writes LinkedIn posts from a prompt, trained from scratch.

No pretrained weights and no pretrained tokenizer: a randomly initialized Llama
(~10M params, 6 layers, 512-token context) reading raw UTF-8 bytes, so it learns
everything — spelling included — from the posts you give it.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Use

```bash
.venv/bin/python slm.py prep                            # data/raw/* -> data/posts.jsonl
.venv/bin/python slm.py train data/posts.jsonl 5000     # ~70 min for 5k iters on an M-series Mac
.venv/bin/python slm.py generate "Write a short post about finishing a bootcamp"
.venv/bin/python slm.py test                            # self-check
```

`train` also takes a HuggingFace dataset name directly, e.g.
`.venv/bin/python slm.py train sanjayram-a/linkedin_posts`.

Best-validation checkpoint lands in `ckpt/`.

## Data

`slm.py prep` folds every `.csv`, `.json`, `.jsonl`, `.tsv` and `.parquet` under
`data/raw/` into one file. It picks each file's free-text column automatically
(the one with the longest average string), strips links and "…see more", drops
posts under 100 or over 6000 characters, drops non-English and near-duplicate
posts, and uses each post's first line as its prompt.

Training data format, one JSON object per line:

```json
{"prompt": "finishing a bootcamp", "post": "Some personal news 🎉\n\nAfter 6 months..."}
```

Sources surveyed (only the first is in use so far):

| rows | size | source |
|---|---|---|
| 2,560 | 2.7MB | `sanjayram-a/linkedin_posts` (HF) — **in use**; templated prompts, mostly certificate/internship posts |
| 1,035 | 1.1MB | `BrianClone/linkedin_posts` (HF) |
| 156 | 0.6MB | `Greich/linkedin_posts` (HF) |

Skipped: `LakshayRahal/linkedin-llama2-dataset` — 992 posts generated from only
93 unique sentences, so it teaches memorization rather than style.

**Data is the bottleneck.** ~4MB is enough to learn LinkedIn cadence and real
words, not enough for meaning. 20MB+ is where a model this size starts making
sense. Do not scrape LinkedIn directly — it breaks their ToS. Use published
dumps (Kaggle, HF) or a licensed API.

## Notes

- Byte-level is the right call under ~5MB. Past that, train a byte-level BPE
  tokenizer (~8k vocab) with HF `tokenizers`: ~4x more text fits the context
  window and the model stops learning spelling from scratch.
- Posts longer than the 512-byte context get truncated during generation.
