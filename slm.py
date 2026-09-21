"""Small language model for LinkedIn posts, trained from scratch (random init, byte-level, no pretrained weights).

  python slm.py fetch                            # download the curated HF sources -> data/raw/
  python slm.py prep                             # data/raw/* -> data/posts.jsonl
  python slm.py train data/posts.jsonl [iters]   # lines of {"prompt": "...", "post": "..."}
  python slm.py generate "Just got promoted to Senior Engineer"
  python slm.py test                             # self-check
"""
import collections, json, pathlib, random, re, shutil, subprocess, sys, unicodedata
import torch
from transformers import LlamaConfig, LlamaForCausalLM, get_cosine_schedule_with_warmup

END, PROMPT, POST = 0, 1, 2  # special token ids, fixed first in the vocab
VOCAB, TOK_PATH = 8000, 'data/tokenizer.json'
BLOCK = 512  # BPE tokens ~ 2000 chars: nearly every post fits whole
CFG = dict(vocab_size=VOCAB, hidden_size=384, intermediate_size=1024, num_hidden_layers=6,
           num_attention_heads=6, max_position_embeddings=BLOCK, tie_word_embeddings=True,
           bos_token_id=PROMPT, eos_token_id=END, pad_token_id=END,
           attention_dropout=0.1)  # ~13M params; dropout won the size sweep
DEV = 'cuda' if torch.cuda.is_available() else 'mps' if torch.backends.mps.is_available() else 'cpu'
_tok = None


def tokenizer(path=None):
    """Our own byte-level BPE, trained on our own posts -- nothing pretrained."""
    global _tok
    if _tok is None:
        from tokenizers import Tokenizer
        ck = pathlib.Path('ckpt/tokenizer.json')          # a checkpoint carries its own tokenizer
        _tok = Tokenizer.from_file(path or (str(ck) if ck.exists() else TOK_PATH))
    return _tok


def train_tokenizer(rows, path=TOK_PATH, vocab=VOCAB):
    from tokenizers import ByteLevelBPETokenizer
    global _tok
    t = ByteLevelBPETokenizer()
    t.train_from_iterator((s for r in rows for s in (r['post'], r.get('prompt', ''))),
                          vocab_size=vocab, min_frequency=2,
                          special_tokens=['<|end|>', '<|prompt|>', '<|post|>'])  # ids 0,1,2
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    t.save(path)
    _tok = None
    print(f'tokenizer: {t.get_vocab_size()} tokens -> {path}')
    return t.get_vocab_size()


def encode(prompt, post=None):
    t = tokenizer()
    ids = [PROMPT] + t.encode(prompt).ids + [POST]
    return ids + t.encode(post).ids + [END] if post is not None else ids


def decode(ids):
    return tokenizer().decode([i for i in ids if i > POST])


def generate(model, prompt, max_new=BLOCK, **kw):
    ids = torch.tensor([encode(prompt)], device=model.device)
    max_new = min(max_new, BLOCK - ids.shape[1])  # no positions past the context window
    # byte models repeat; the penalty and top_p matter more than temperature here
    sample = kw or dict(do_sample=True, temperature=0.9, top_p=0.9, repetition_penalty=1.1)
    out = model.generate(ids, max_new_tokens=max_new, eos_token_id=END, pad_token_id=END, **sample)
    return decode(out[0, ids.shape[1]:].tolist())


SOURCES = [  # HF datasets that really do hold English LinkedIn post text (surveyed 2026-09)
    'sanjayram-a/linkedin_posts',                    # 2560 templated certificate/internship posts
    'BrianClone/linkedin_posts',                     # 1035 real posts w/ engagement metrics
    'ShayanShamsi/prompt_to_linkedin_post',          # 447 prompt -> post pairs
    'ro-anderson/linkedin-top-voices-market-alpaca',  # 300 LinkedIn Top Voices posts
    'Ayushfj/LinkedInPostDataset',                   # 190 real posts
    'NeuML/neuml-linkedin-202501',                   # 258 company posts
    'logiover/linkedin-top-content-scraper-sample-data',  # 20 high-quality scraped posts
    # raw URLs work too:
    'https://raw.githubusercontent.com/harsh-jos/gemma-SFT-linkedin/main/data/final_dataset.jsonl',  # ~150 posts
]

KAGGLE_SOURCES = [  # needs ~/.kaggle/kaggle.json (kaggle.com -> Settings -> API); this is most of the corpus
    'shreyasajal/linkedin-influencers-data',           # ~24k real influencer posts, 91MB
    'mozharovartem/english-linkedin-posts',            # ~170k english<->linkedin-speak pairs, 82MB
    'moeinaminifard/1600-posts-on-linkedin',
    'themeghnasahu/linkedin-company-posts',
    'logiover/linkedin-top-content-scraper-data',
    'sreevaatsavbavana/layoffs-linkedin-posts',
]


def fetch(dst='data/raw'):
    """Pull SOURCES + KAGGLE_SOURCES into data/raw/; prep turns them into training data."""
    from datasets import load_dataset
    pathlib.Path(dst).mkdir(parents=True, exist_ok=True)
    for name in SOURCES:
        f = pathlib.Path(dst, re.sub(r'\W+', '_', name.split('://')[-1])[:80] + '.jsonl')
        if f.exists():
            print(f'have {f.name}'); continue
        try:
            if name.startswith('http'):
                import requests
                f.write_bytes(requests.get(name, timeout=60).content)
                print(f'{f.name}: {f.stat().st_size / 1e3:.0f} KB')
                continue
            df = load_dataset(name)['train'].to_pandas()
            df.to_json(f, orient='records', lines=True)
            print(f'{f.name}: {len(df)} rows')
        except Exception as e:
            print(f'skip {name}: {type(e).__name__} {e}')

    if not pathlib.Path('~/.kaggle/kaggle.json').expanduser().exists():
        print('no ~/.kaggle/kaggle.json: skipping KAGGLE_SOURCES (kaggle.com -> Settings -> API -> Create New Token)')
        return
    for ref in KAGGLE_SOURCES:
        marker = pathlib.Path(dst, '.kg_' + ref.replace('/', '_'))
        if marker.exists():
            print(f'have {ref}'); continue
        r = subprocess.run(['kaggle', 'datasets', 'download', '-d', ref, '-p', dst, '--unzip', '-q'],
                           capture_output=True, text=True, timeout=900)
        if r.returncode == 0:
            marker.touch()
            print(f'{ref}: downloaded')
        else:
            print(f'skip {ref}: {(r.stderr or r.stdout).strip()[:150]}')


def clean(t):
    t = unicodedata.normalize('NFKC', str(t))                 # 𝗳𝗮𝗻𝗰𝘆 LinkedIn text -> plain ascii
    t = re.sub(r'https?://\S+|www\.\S+', '', t)               # links teach nothing
    t = re.sub(r'\s*[…\.]{3}\s*see more\s*$', '', t, flags=re.I)
    t = re.sub(r'[​-‏﻿]', '', t)   # zero-width junk from scrapers
    t = re.sub(r'(?<=[a-z])([.!?])(?=[A-Z])', r'\1 ', t)      # scraper joins: "Mode.I tried" -> "Mode. I tried"
    t = re.sub(r'\n{3,}', '\n\n', t.replace('\r\n', '\n'))
    return re.sub(r'[ \t]+', ' ', t).strip()


def english(t):
    letters = [c for c in t if c.isalpha()]
    return len(letters) > 20 and sum(c.isascii() for c in letters) / len(letters) > 0.9


COLS = {  # files whose real (post, prompt) columns the name heuristic cannot guess
    'english-linkedin-posts-v2.csv': ('linkedin', 'english'),  # plain english <-> linkedin-speak pairs
}


def prep(src='data/raw', out='data/posts.jsonl', min_chars=100, max_chars=6000):
    """Fold every csv/json/jsonl/parquet under src into one deduped posts.jsonl."""
    import pandas as pd
    reader = {'.csv': pd.read_csv, '.tsv': lambda p: pd.read_csv(p, sep='\t'),
              '.json': pd.read_json, '.jsonl': lambda p: pd.read_json(p, lines=True),
              '.parquet': pd.read_parquet}
    seen, kept = set(), []
    for f in sorted(p for p in pathlib.Path(src).rglob('*') if p.suffix.lower() in reader):
        try:
            df = reader[f.suffix.lower()](f)
        except Exception as e:
            print(f'skip {f.name}: {type(e).__name__} {e}'); continue
        # the post column is the free-text one: longest average string, but skip JSON/URL
        # columns and prefer a post-shaped name -- a profile 'about' blurb is longer than the post
        text = {}
        for c in df.columns:
            if pd.api.types.is_numeric_dtype(df[c]) or pd.api.types.is_datetime64_any_dtype(df[c]):
                continue
            s = df[c].dropna().astype(str)
            if not len(s) or s.head(200).str.match(r'\s*[\[{]|https?://').mean() > 0.5:
                continue
            text[c] = s.str.len().mean()
        named = {c: v for c, v in text.items() if re.search(r'post|content|text|body|message|caption', c, re.I)}
        col = max(named or text, key=(named or text).get) if text else None
        if f.name in COLS:
            col = COLS[f.name][0]
        if not col or text.get(col, 0) < min_chars:
            print(f'skip {f.name}: no free-text column (best {col!r} avg {text.get(col, 0):.0f} chars)'); continue
        # use a real prompt column when the file has one, else the post's own hook
        cand = {c: v for c, v in text.items()
                if c != col and v < text[col] and re.search(r'prompt|instruction|input|topic', c, re.I)}
        pcol = COLS[f.name][1] if f.name in COLS else (max(cand, key=cand.get) if cand else None)
        dupes = collections.Counter(df[pcol].astype(str)) if pcol else {}
        n = 0
        for raw, praw in zip(df[col], df[pcol] if pcol else df[col]):
            post = clean(raw)
            key = re.sub(r'\W+', '', post.lower())[:300]
            if min_chars <= len(post) <= max_chars and english(post) and key not in seen:
                seen.add(key)
                p = clean(praw) if pcol else ''
                # boilerplate, JSON config blobs and "unknown" placeholders teach nothing. Fall back to
                # the post's hashtags as a topic prompt, else its hook -- a prefix-only prompt would
                # teach the model to continue text rather than write a post from an instruction.
                if not p or p.startswith('{') or 'unknown' in p.lower() or dupes[str(praw)] > 3:
                    tags = re.findall(r'#(\w+)', post)[:3]
                    p = ('Write a LinkedIn post about ' + ', '.join(t.lower() for t in tags)
                         if tags else post.split('\n')[0][:80])
                kept.append({'prompt': p[:120], 'post': post})
                n += 1
        print(f'{f.name}: {n} posts from {col!r}' + (f', prompts from {pcol!r}' if pcol else ''))
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w') as fh:
        for r in kept:
            fh.write(json.dumps(r) + '\n')
    print(f'wrote {len(kept)} posts, {sum(len(r["post"].encode()) for r in kept) / 1e6:.1f} MB to {out}')


def load_rows(path):
    """Local .jsonl of {"prompt", "post"}, or a HuggingFace dataset name."""
    if path.endswith('.jsonl'):
        return [json.loads(l) for l in open(path) if l.strip()]
    from datasets import load_dataset
    d = load_dataset(path)['train']
    return [{'prompt': r['input'], 'post': r['output']} for r in d]


def batch_of(exs, batch):
    """One padded batch; labels are -100 everywhere but the post bytes, so the model is
    scored on writing the post, not on echoing the prompt."""
    picks = [exs[k] for k in torch.randint(len(exs), (batch,)).tolist()]
    need = max(len(ids) for ids, _ in picks)
    # bucket to a handful of fixed widths (not the exact max): a fresh shape almost every step
    # starves MPS's allocator of any chance to reuse buffers, which fills swap over a long run
    width = next(b for b in (64, 128, 256, 384, BLOCK) if b >= need)
    x = torch.full((batch, width), END)
    y = torch.full((batch, width), -100)
    for row, (ids, plen) in enumerate(picks):
        x[row, :len(ids)] = torch.tensor(ids)
        y[row, plen:len(ids)] = torch.tensor(ids[plen:])
    return x.to(DEV), y.to(DEV)


def train(path, iters=5000, cfg=CFG, batch=16, lr=3e-4, out='ckpt'):
    rows = load_rows(path)
    random.Random(0).shuffle(rows)  # else the val split is whichever source landed last
    cfg = cfg | dict(vocab_size=train_tokenizer(rows))  # fresh tokenizer for this corpus
    pathlib.Path(out).mkdir(parents=True, exist_ok=True)
    shutil.copy(TOK_PATH, f'{out}/tokenizer.json')      # keep them together, they must match
    exs = [(ids, len(encode(r.get('prompt', ''))))
           for r in rows for ids in [encode(r.get('prompt', ''), r['post'])[:BLOCK]]]
    exs = [e for e in exs if e[1] < len(e[0])]  # keep only examples with post bytes left after truncation
    n = int(len(exs) * 0.9)
    tr, va = exs[:n], exs[n:]
    assert len(va) > batch, f'need more data: val split has {len(va)} examples'

    def get(split):
        return batch_of(split, batch)

    model = LlamaForCausalLM(LlamaConfig(**cfg)).to(DEV)  # random init: nothing pretrained
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.1)
    sched = get_cosine_schedule_with_warmup(opt, 100, iters)
    print(f'{len(exs)} examples ({sum(len(i) for i, _ in exs):,} bytes), '
          f'{model.num_parameters() / 1e6:.1f}M params on {DEV}')
    best, stale = float('inf'), 0
    for it in range(iters + 1):
        if it % 250 == 0:
            model.eval()
            with torch.no_grad():
                val = sum(model(input_ids=x, labels=y).loss.item() for x, y in (get(va) for _ in range(20))) / 20
            model.train()
            print(f'iter {it}  val loss {val:.3f}')
            if val < best:  # ponytail: best-val checkpoint only, no resume; add optimizer state if runs get long
                best, stale = val, 0
                model.save_pretrained(out)
            else:
                stale += 1
                if stale == 3:  # 3 evals without improvement: it is overfitting, stop
                    print(f'early stop at {it}, best val {best:.3f}')
                    break
        x, y = get(tr)
        loss = model(input_ids=x, labels=y).loss  # HF shifts labels internally
        opt.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        sched.step()
    return best


def test():
    # prep: picks the text column, cleans, drops dupes / shorts / non-English
    import tempfile, pandas as pd
    long = 'Thrilled to share that I completed my certification in cloud engineering ' * 2
    with tempfile.TemporaryDirectory() as d:
        pd.DataFrame({'author': ['a', 'b', 'c', 'd', 'e'],
                      'about': [long * 3] * 5,          # longer than the post, but not a post
                      'links': ['["http://x.com"]'] * 5,  # JSON blob column
                      'prompt': ['Topic: cloud certification', 'x', 'y', 'z', '{"tone": "proud"}'],
                      'content': [long + ' https://lnkd.in/xyz ...see more', long, 'hi',
                                  'Профессиональный рост и новые возможности в этом году ' * 3,
                                  long.replace('cloud', 'data') + ' #cloud #data']}).to_csv(f'{d}/raw.csv', index=False)
        prep(d, f'{d}/posts.jsonl')
        rows = load_rows(f'{d}/posts.jsonl')
    assert len(rows) == 2, rows                      # dupe, short and Russian dropped
    assert 'http' not in rows[0]['post'] and 'see more' not in rows[0]['post'], rows[0]
    assert rows[0]['prompt'] == 'Topic: cloud certification', rows[0]
    assert rows[1]['prompt'] == 'Write a LinkedIn post about cloud, data', rows[1]  # JSON blob -> hashtag topic

    with tempfile.TemporaryDirectory() as d:
        # a tokenizer trained here, on these posts: round-trips text and keeps the special ids
        vocab = train_tokenizer(rows * 20, f'{d}/tok.json', vocab=400)
        global _tok
        _tok = None
        tokenizer(f'{d}/tok.json')
        txt = 'I am thrilled to announce my cloud certification!'
        ids = encode('promo', txt)
        assert ids[0] == PROMPT and ids[-1] == END and POST in ids, ids
        assert decode(ids[ids.index(POST) + 1:]) == txt, decode(ids[ids.index(POST) + 1:])
        assert len(ids) < len(txt.encode()), 'BPE must beat raw bytes'

        # batches: loss is scored on post tokens only, never on the prompt or the padding
        ids, plen = encode('promo', 'hello'), len(encode('promo'))
        x, y = batch_of([(ids, plen)], 1)
        assert (y[0, :plen] == -100).all() and (y[0, len(ids):] == -100).all(), y
        assert y[0, plen:len(ids)].tolist() == ids[plen:], y
        assert x[0, :len(ids)].tolist() == ids, x

        # overfit a tiny model on one post; it must reproduce it from the prompt
        torch.manual_seed(0)
        cfg = CFG | dict(vocab_size=vocab, hidden_size=64, intermediate_size=128,
                         num_hidden_layers=2, num_attention_heads=2)
        model = LlamaForCausalLM(LlamaConfig(**cfg))
        x = torch.tensor([encode('promo', 'I am thrilled to announce...') * 3])
        opt = torch.optim.AdamW(model.parameters(), lr=3e-3)
        for _ in range(300):
            loss = model(input_ids=x, labels=x).loss
            opt.zero_grad(); loss.backward(); opt.step()
        out = generate(model, 'promo', max_new=60, do_sample=False)
        assert out == 'I am thrilled to announce...', repr(out)
    _tok = None
    print('ok')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'train':
        train(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 5000)
    elif cmd == 'generate':
        print(generate(LlamaForCausalLM.from_pretrained('ckpt').to(DEV), ' '.join(sys.argv[2:])))
    elif cmd == 'fetch':
        fetch(*sys.argv[2:])
    elif cmd == 'prep':
        prep(*sys.argv[2:])
    elif cmd == 'test':
        test()
    else:
        print(__doc__)
