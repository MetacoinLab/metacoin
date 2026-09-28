"""Task-owned model runtime child (transformers + torch under the trusted compute interpreter).

    python -m metacoin_service.models.runtime WORKDIR

Reads WORKDIR/spec.json {revision_id, local_dir, loader, device, precision, limits, pooling, normalize}, loads the
pinned artifact from the local directory only (offline, no remote code, safetensors only) and then serves JSON-line
commands on stdin, answering with JSON-line events on stdout:

    <- {"op":"generate","request_id","messages"|"prompt","max_new_tokens","temperature_percent","top_p_percent","seed","stop"}
    -> {"event":"segment","request_id","seq","text"} ...  {"event":"done","request_id","finish_reason","usage","ms"}
    <- {"op":"embed","request_id","texts","truncate"}
    -> {"event":"embedding","request_id","dim","vectors","tokens","truncated","ms"}
    <- {"op":"cancel","request_id"}         (takes effect at the next generated token)
    <- {"op":"ping"}  -> {"event":"pong", memory}
    <- {"op":"unload"} -> exits 0

One request runs at a time. Errors for one request are reported as {"event":"error","request_id",code,reason} and the
child keeps serving; a fatal load error exits with code 3. Nothing here reads anything outside the artifact directory."""
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

EXIT_SPEC, EXIT_LOAD = 2, 3


def emit(obj):
    sys.stdout.write(json.dumps(obj, separators=(',', ':')) + '\n'); sys.stdout.flush()


def memory_report(torch, device):
    out = {'rss_bytes': None, 'cuda_allocated_bytes': None, 'cuda_reserved_bytes': None, 'scope': 'process RSS from /proc; torch allocator counters (unified memory: not additive with RSS)'}
    try:
        for line in open('/proc/self/status'):
            if line.startswith('VmRSS:'):
                out['rss_bytes'] = int(line.split()[1]) * 1024
    except OSError:
        pass
    if device == 'cuda':
        try:
            out['cuda_allocated_bytes'] = int(torch.cuda.memory_allocated()); out['cuda_reserved_bytes'] = int(torch.cuda.memory_reserved())
        except Exception:
            pass
    return out


class Runtime:
    def __init__(self, spec):
        self.spec = spec
        self.limits = spec['limits']
        self.cancel = set()
        self.lock = threading.Lock()
        os.environ.setdefault('HF_HUB_OFFLINE', '1'); os.environ.setdefault('TRANSFORMERS_OFFLINE', '1'); os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
        os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')
        import torch
        self.torch = torch
        torch.set_num_threads(int(self.limits.get('threads') or 4))
        self.device = spec['device']
        if self.device == 'cuda' and not torch.cuda.is_available():
            raise RuntimeError('cuda requested but unavailable')
        prec = spec.get('precision', 'auto')
        if prec == 'auto':
            prec = 'bfloat16' if self.device == 'cuda' else 'float32'
        self.dtype = {'float32': torch.float32, 'bfloat16': torch.bfloat16, 'float16': torch.float16}[prec]
        self.dtype_name = prec
        d = Path(spec['local_dir'])
        if not (d / 'model.safetensors').is_file():
            raise RuntimeError('model.safetensors missing')
        import transformers
        from transformers import AutoTokenizer
        self.transformers = transformers
        t0 = time.time()
        self.tok = AutoTokenizer.from_pretrained(str(d), local_files_only=True, trust_remote_code=False)
        if spec['loader'] == 'causal_lm':
            from transformers import AutoModelForCausalLM
            self.model = AutoModelForCausalLM.from_pretrained(str(d), dtype=self.dtype, local_files_only=True, trust_remote_code=False, use_safetensors=True).to(self.device).eval()
        elif spec['loader'] == 'encoder':
            from transformers import AutoModel
            self.model = AutoModel.from_pretrained(str(d), dtype=self.dtype, local_files_only=True, trust_remote_code=False, use_safetensors=True, add_pooling_layer=False).to(self.device).eval()
        else:
            raise RuntimeError('unknown loader')
        self.load_seconds = time.time() - t0
        self.param_bytes = sum(p.numel() * p.element_size() for p in self.model.parameters())
        self.pooling = spec.get('pooling') or 'mean'
        self.max_seq = int(spec.get('max_seq_length') or self.limits.get('max_input_tokens') or 512)

    def versions(self):
        return {'torch': self.torch.__version__, 'transformers': self.transformers.__version__, 'python': sys.version.split()[0], 'device': self.device,
                'device_name': self.torch.cuda.get_device_name(0) if self.device == 'cuda' else 'cpu', 'dtype': self.dtype_name, 'threads': self.torch.get_num_threads(),
                'tokenizer_class': type(self.tok).__name__, 'model_class': type(self.model).__name__, 'param_bytes': self.param_bytes}

    # ---- generation ------------------------------------------------------------------------------
    def build_prompt(self, req):
        if 'messages' in req and req['messages'] is not None:
            if getattr(self.tok, 'chat_template', None) is None:
                raise ValueError('tokenizer has no chat template; use prompt')
            text = self.tok.apply_chat_template(req['messages'], add_generation_prompt=True, tokenize=False)
            template = 'tokenizer chat_template with add_generation_prompt (system/user/assistant roles as given; no hidden system text added by the runtime)'
        else:
            text = req['prompt']; template = 'raw prompt (no template)'
        ids = self.tok(text, return_tensors='pt', add_special_tokens=False)
        n = int(ids['input_ids'].shape[1])
        if n > int(self.limits['max_input_tokens']):
            raise ValueError('input_too_long: %d tokens > limit %d (no silent truncation)' % (n, int(self.limits['max_input_tokens'])))
        return ids, n, template

    def generate(self, req):
        torch = self.torch
        rid = req['request_id']
        try:
            ids, n_in, template = self.build_prompt(req)
        except ValueError as exc:
            emit({'event': 'error', 'request_id': rid, 'code': 'INPUT_INVALID', 'reason': str(exc)[:200]}); return
        max_new = min(int(req['max_new_tokens']), int(self.limits['max_output_tokens']))
        temp = int(req.get('temperature_percent') or 0) / 100.0
        top_p = int(req.get('top_p_percent') or 100) / 100.0
        seed = req.get('seed')
        stop = [s for s in (req.get('stop') or []) if s][:4]
        do_sample = temp > 0
        if seed is not None:
            torch.manual_seed(int(seed))
            if self.device == 'cuda':
                torch.cuda.manual_seed_all(int(seed))
        from transformers import TextIteratorStreamer, StoppingCriteria, StoppingCriteriaList
        runtime = self

        class Cancel(StoppingCriteria):
            def __call__(self, input_ids, scores, **kw):
                return rid in runtime.cancel
        streamer = TextIteratorStreamer(self.tok, skip_prompt=True, skip_special_tokens=True, timeout=float(self.limits.get('token_timeout_seconds') or 120))
        gen_kwargs = dict(ids.to(self.device), max_new_tokens=max_new, do_sample=do_sample, streamer=streamer, stopping_criteria=StoppingCriteriaList([Cancel()]),
                          pad_token_id=self.tok.pad_token_id if self.tok.pad_token_id is not None else self.tok.eos_token_id)
        if do_sample:
            gen_kwargs.update(temperature=temp, top_p=top_p)
        else:
            gen_kwargs.update(temperature=None, top_p=None, top_k=None)
        result = {}

        def run():
            try:
                with torch.no_grad():
                    out = self.model.generate(**gen_kwargs)
                result['ids'] = out[0][ids['input_ids'].shape[1]:]
            except Exception as exc:
                result['error'] = type(exc).__name__ + ': ' + str(exc)[:160]
                try:
                    streamer.end()
                except Exception:
                    pass
        t0 = time.time()
        th = threading.Thread(target=run, daemon=True); th.start()
        seq, text_all, stopped_on = 0, '', None
        try:
            for piece in streamer:
                if not piece:
                    continue
                text_all += piece
                emit({'event': 'segment', 'request_id': rid, 'seq': seq, 'text': piece}); seq += 1
                if stop and any(s in text_all for s in stop):
                    stopped_on = next(s for s in stop if s in text_all); self.cancel.add(rid)
        except Exception as exc:
            result.setdefault('error', 'stream: ' + type(exc).__name__)
        th.join(timeout=float(self.limits.get('token_timeout_seconds') or 120))
        if self.device == 'cuda':
            torch.cuda.synchronize()
        ms = int((time.time() - t0) * 1000)
        if 'error' in result:
            emit({'event': 'error', 'request_id': rid, 'code': 'RUNTIME_ERROR', 'reason': result['error'], 'partial_segments': seq, 'ms': ms}); self.cancel.discard(rid); return
        out_ids = result.get('ids')
        n_out = int(out_ids.shape[0]) if out_ids is not None else 0
        eos = self.tok.eos_token_id
        eos_ids = set(eos if isinstance(eos, list) else [eos]) | set(getattr(self.model.generation_config, 'eos_token_id', None) or [] if isinstance(getattr(self.model.generation_config, 'eos_token_id', None), list) else [getattr(self.model.generation_config, 'eos_token_id', None)])
        ended_with_eos = n_out > 0 and int(out_ids[-1]) in eos_ids
        if stopped_on is not None:
            finish = 'stop'
        elif rid in self.cancel:
            finish = 'cancelled'
        elif ended_with_eos:
            finish = 'eos'
        elif n_out >= max_new:
            finish = 'length'
        else:
            finish = 'eos'
        self.cancel.discard(rid)
        emit({'event': 'done', 'request_id': rid, 'finish_reason': finish, 'stop_sequence': stopped_on, 'segments': seq, 'text_sha256': __import__('hashlib').sha256(text_all.encode()).hexdigest(),
              'usage': {'input_tokens': n_in, 'output_tokens': n_out, 'counting': 'runtime tokenizer; input = templated prompt tokens; output = generated ids including a terminal end-of-sequence token when emitted'},
              'config': {'max_new_tokens': max_new, 'do_sample': do_sample, 'temperature': temp if do_sample else None, 'top_p': top_p if do_sample else None, 'seed': seed, 'stop': stop, 'template': template,
                         'determinism': 'greedy decoding is repeatable on this runtime for the same device/dtype/versions; sampled decoding repeats only for the same seed on the same device and versions; no cross-device or cross-version bitwise promise'},
              'ms': ms, 'tokens_per_second': round(n_out / max(ms / 1000.0, 1e-6), 2)})

    # ---- embeddings ------------------------------------------------------------------------------
    def embed(self, req):
        torch = self.torch
        rid = req['request_id']
        texts = req['texts']
        if type(texts) is not list or not texts or len(texts) > int(self.limits['max_items']):
            emit({'event': 'error', 'request_id': rid, 'code': 'INPUT_INVALID', 'reason': 'texts: 1..%d items' % int(self.limits['max_items'])}); return
        truncate = bool(req.get('truncate'))
        t0 = time.time()
        enc_full = self.tok(texts, add_special_tokens=True, padding=False, truncation=False)
        lengths = [len(x) for x in enc_full['input_ids']]
        truncated = [n > self.max_seq for n in lengths]
        if any(truncated) and not truncate:
            emit({'event': 'error', 'request_id': rid, 'code': 'INPUT_TOO_LONG', 'reason': 'items exceed the model sequence limit %d tokens: %s (truncate=false refuses silent truncation)' % (self.max_seq, [i for i, t in enumerate(truncated) if t][:20])}); return
        vectors = []
        bs = int(self.limits.get('batch_size') or 32)
        with torch.no_grad():
            for i in range(0, len(texts), bs):
                enc = self.tok(texts[i:i + bs], add_special_tokens=True, padding=True, truncation=True, max_length=self.max_seq, return_tensors='pt').to(self.device)
                h = self.model(**enc).last_hidden_state
                if self.pooling.startswith('cls'):
                    v = h[:, 0]
                else:
                    mask = enc['attention_mask'].unsqueeze(-1).to(h.dtype)
                    v = (h * mask).sum(1) / mask.sum(1).clamp(min=1)
                v = v.float()
                if self.spec.get('normalize', True):
                    v = torch.nn.functional.normalize(v, p=2, dim=1)
                vectors.extend(v.cpu().tolist())
        if self.device == 'cuda':
            torch.cuda.synchronize()
        ms = int((time.time() - t0) * 1000)
        emit({'event': 'embedding', 'request_id': rid, 'dim': len(vectors[0]) if vectors else 0, 'vectors': vectors, 'tokens': [min(n, self.max_seq) for n in lengths], 'tokens_before_truncation': lengths,
              'truncated': truncated, 'pooling': self.pooling, 'normalized': bool(self.spec.get('normalize', True)), 'max_seq_length': self.max_seq, 'ms': ms})


def reader(q, runtime_ref):
    """stdin reader thread: cancel commands are applied immediately; everything else is queued in order."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except ValueError:
            continue
        if cmd.get('op') == 'cancel' and runtime_ref:
            runtime_ref[0].cancel.add(cmd.get('request_id'))
            continue
        q.put(cmd)
    q.put({'op': 'unload', 'reason': 'stdin closed'})


def main():
    if len(sys.argv) != 2:
        sys.exit(EXIT_SPEC)
    try:
        spec = json.loads((Path(sys.argv[1]) / 'spec.json').read_bytes())
    except (OSError, ValueError):
        sys.exit(EXIT_SPEC)
    ref = []
    q = queue.Queue()
    threading.Thread(target=reader, args=(q, ref), daemon=True).start()
    try:
        rt = Runtime(spec)
    except Exception as exc:
        emit({'event': 'error', 'code': 'MODEL_LOAD_FAILED', 'reason': type(exc).__name__ + ': ' + str(exc)[:200]})
        sys.exit(EXIT_LOAD)
    ref.append(rt)
    emit({'event': 'ready', 'revision_id': spec['revision_id'], 'load_ms': int(rt.load_seconds * 1000), 'versions': rt.versions(), 'memory': memory_report(rt.torch, rt.device), 'pid': os.getpid()})
    while True:
        cmd = q.get()
        op = cmd.get('op')
        if op == 'unload':
            emit({'event': 'unloaded', 'reason': cmd.get('reason')}); return
        if op == 'ping':
            emit({'event': 'pong', 'memory': memory_report(rt.torch, rt.device), 'versions': rt.versions()}); continue
        if op == 'generate' and spec['loader'] == 'causal_lm':
            rt.generate(cmd); continue
        if op == 'embed' and spec['loader'] == 'encoder':
            rt.embed(cmd); continue
        emit({'event': 'error', 'request_id': cmd.get('request_id'), 'code': 'UNSUPPORTED_OPERATION', 'reason': 'op %r not supported by loader %s' % (op, spec['loader'])})


if __name__ == '__main__':
    main()
