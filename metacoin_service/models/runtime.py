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
import hashlib
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

    # ---- static batched generation (Order 07 group B) ----------------------------------------------
    def continuous_supported(self):
        return hasattr(self.model, 'init_continuous_batching') and self.device == 'cuda' and getattr(self.tok, 'chat_template', None) is not None

    def generate_continuous(self, cmd, q):
        """A continuous-batching session (transformers ContinuousBatchingManager, paged attention): requests are admitted and
        removed while the session runs. Commands arrive on the child's queue during the session: cb_add {request_id, messages|
        prompt, max_new_tokens, stop}, cancel {request_id} (via the reader thread's cancel set) and cb_end. Per-request segments
        are streamed as they are produced (delta of the decoded text), each request reports its own usage on completion;
        stop strings are enforced by the session (cancel + truncation) because the manager has no stop-string support.
        Greedy only; sampled requests are refused (session-global random state)."""
        torch = self.torch
        sid = cmd['session_id']
        if not self.continuous_supported():
            emit({'event': 'session_done', 'session_id': sid, 'members': 0, 'ms': 0, 'error': {'code': 'CONTINUOUS_UNSUPPORTED', 'reason': 'runtime lacks init_continuous_batching, a cuda device or a chat template'}}); return
        from transformers import GenerationConfig
        eos = self.tok.eos_token_id
        gen_eos = getattr(self.model.generation_config, 'eos_token_id', None)
        eos_ids = set(eos if isinstance(eos, list) else [eos]) | set(gen_eos if isinstance(gen_eos, list) else ([gen_eos] if gen_eos is not None else []))
        eos_ids.discard(None)
        cap = int(self.limits['max_output_tokens'])
        gc = GenerationConfig(max_new_tokens=cap, eos_token_id=sorted(eos_ids), do_sample=False, num_return_sequences=1, pad_token_id=self.tok.pad_token_id if self.tok.pad_token_id is not None else self.tok.eos_token_id)
        t0 = time.time()
        if self.device == 'cuda':
            torch.cuda.reset_peak_memory_stats(); before = int(torch.cuda.memory_allocated())
        orig_attn = getattr(self.model.config, '_attn_implementation', None)
        try:
            manager = self.model.init_continuous_batching(generation_config=gc)
            manager.start()
        except Exception as exc:
            emit({'event': 'session_done', 'session_id': sid, 'members': 0, 'ms': int((time.time() - t0) * 1000), 'error': {'code': 'CONTINUOUS_START_FAILED', 'reason': type(exc).__name__ + ': ' + str(exc)[:160]}}); return
        rows = {}
        ended = False; admitted = 0; steps = 0
        def finish(r, reason, gen_ids, text=None):
            if r['done']:
                return
            text = r['emitted'] if text is None else text
            r['done'] = True
            emit({'event': 'done', 'request_id': r['rid'], 'finish_reason': reason, 'usage': {'input_tokens': r['n_in'], 'output_tokens': len(gen_ids), 'total_tokens': r['n_in'] + len(gen_ids)}, 'ms': int((time.time() - r['t0']) * 1000),
                  'tokens_per_second': round(len(gen_ids) / max(time.time() - r['t0'], 1e-6), 2), 'text_sha256': hashlib.sha256(text.encode()).hexdigest(), 'config': {'do_sample': False, 'max_new_tokens': r['max_new'], 'template': r['template'], 'mode': 'continuous'}, 'stopped_on': r.get('stopped_on')})
        try:
            while not (ended and all(r['done'] for r in rows.values())):
                # 1. commands admitted during the session
                try:
                    c = q.get(timeout=0.02)
                except queue.Empty:
                    c = None
                if c is not None:
                    op = c.get('op')
                    if op == 'cb_add':
                        rid = c['request_id']
                        try:
                            ids, n_in, template = self.build_prompt(c)
                            if int(c.get('temperature_percent') or 0) > 0:
                                raise ValueError('sampled requests are not batched (session-global random state)')
                        except ValueError as exc:
                            emit({'event': 'error', 'request_id': rid, 'code': 'INPUT_INVALID', 'reason': str(exc)[:200]}); continue
                        max_new = min(int(c['max_new_tokens']), cap)
                        rows[rid] = {'rid': rid, 'n_in': n_in, 'max_new': max_new, 'stop': [x for x in (c.get('stop') or []) if x][:4], 'template': template, 'emitted': '', 'seq': 0, 'done': False, 't0': time.time(), 'stopped_on': None, 'gen': []}
                        manager.add_request(input_ids=[int(x) for x in ids['input_ids'][0]], request_id=rid, max_new_tokens=max_new, streaming=True)
                        admitted += 1
                        emit({'event': 'admitted', 'request_id': rid, 'session_id': sid, 'position': admitted - 1, 'input_tokens': n_in})
                    elif op == 'cb_end':
                        ended = True
                    elif op == 'unload':
                        ended = True; q.put(c)
                    else:
                        emit({'event': 'error', 'request_id': c.get('request_id'), 'code': 'UNSUPPORTED_OPERATION', 'reason': 'op %r not accepted during a continuous session' % op})
                # 2. cancellations from the reader thread: the member is removed now (its partial output stands); later manager outputs for it are ignored
                for r in rows.values():
                    if not r['done'] and r['rid'] in self.cancel:
                        try:
                            manager.cancel_request(r['rid'])
                        except Exception:
                            pass
                        r['cancel_sent'] = True; finish(r, 'cancelled', r['gen'])
                # 3. outputs
                out = manager.get_result(timeout=0.02)
                while out is not None:
                    r = rows.get(out.request_id)
                    if r is not None and not r['done']:
                        gen_ids = list(out.generated_tokens)
                        if len(gen_ids) > len(r['gen']):
                            steps += 1
                        r['gen'] = gen_ids
                        text = self.tok.decode(gen_ids, skip_special_tokens=True)
                        if r['stop'] and any(s in text for s in r['stop']):
                            s = next(s for s in r['stop'] if s in text); r['stopped_on'] = s
                            text = text[:text.index(s)]
                            delta = text[len(r['emitted']):]
                            if delta:
                                emit({'event': 'segment', 'request_id': r['rid'], 'seq': r['seq'], 'text': delta}); r['emitted'] = text; r['seq'] += 1
                            manager.cancel_request(r['rid']); finish(r, 'stop', gen_ids, text)
                        else:
                            if not text.endswith('\ufffd'):
                                delta = text[len(r['emitted']):]
                                if delta:
                                    emit({'event': 'segment', 'request_id': r['rid'], 'seq': r['seq'], 'text': delta}); r['emitted'] = text; r['seq'] += 1
                            if out.is_finished():
                                if getattr(out, 'error', None):
                                    r['done'] = True; emit({'event': 'error', 'request_id': r['rid'], 'code': 'RUNTIME_ERROR', 'reason': str(out.error)[:160], 'partial_segments': r['seq']})
                                else:
                                    last = gen_ids[-1] if gen_ids else None
                                    reason = 'cancelled' if r.get('cancel_sent') or r['rid'] in self.cancel else ('eos' if last in eos_ids else ('length' if len(gen_ids) >= r['max_new'] else 'eos'))
                                    finish(r, reason, gen_ids, text)
                    out = manager.get_result(timeout=0.0)
                if not manager.is_running():
                    for r in rows.values():
                        if not r['done']:
                            r['done'] = True; emit({'event': 'error', 'request_id': r['rid'], 'code': 'RUNTIME_ERROR', 'reason': 'continuous batching thread stopped', 'partial_segments': r['seq']})
                    ended = True
        finally:
            try:
                manager.stop(block=True, timeout=30)
            except Exception:
                pass
            try:
                cur = getattr(self.model.config, '_attn_implementation', None)
                if orig_attn and cur != orig_attn:
                    self.model.set_attn_implementation(orig_attn)          # the session's paged attention never leaks into later singleton or static generation
            except Exception:
                pass
            for r in rows.values():
                self.cancel.discard(r['rid'])
        mem = {'cuda_peak_delta_bytes': int(torch.cuda.max_memory_allocated() - before)} if self.device == 'cuda' else {}
        emit({'event': 'session_done', 'session_id': sid, 'members': admitted, 'steps': steps, 'ms': int((time.time() - t0) * 1000), 'memory': mem, 'mode': 'continuous', 'note': 'paged-attention continuous batching (transformers ContinuousBatchingManager); admission and removal during the session; outputs may differ from padded static batching at the token level'})

    def generate_batch(self, cmd):
        """One padded forward pass for several greedy requests. Left padding + attention mask (positions derive from the
        mask); a per-row stopping criterion returns a bool per sequence so each row stops on ITS OWN end-of-sequence,
        stop string, token ceiling or cancellation while the others continue; finished rows receive pad tokens which are
        never counted or delivered. Streams per-row segments; reports per-row usage and a batch summary."""
        torch = self.torch
        bid = cmd['batch_id']
        rows = []
        for r in cmd['requests']:
            rid = r['request_id']
            try:
                ids, n_in, template = self.build_prompt(r)
            except ValueError as exc:
                emit({'event': 'error', 'request_id': rid, 'code': 'INPUT_INVALID', 'reason': str(exc)[:200]}); continue
            if int(r.get('temperature_percent') or 0) > 0:
                emit({'event': 'error', 'request_id': rid, 'code': 'INPUT_INVALID', 'reason': 'sampled requests are not batched (batch-global random state); run as a singleton'}); continue
            rows.append({'rid': rid, 'ids': ids['input_ids'][0], 'n_in': n_in, 'max_new': min(int(r['max_new_tokens']), int(self.limits['max_output_tokens'])), 'stop': [x for x in (r.get('stop') or []) if x][:4],
                         'template': template, 'emitted': '', 'seq': 0, 'done': False, 'finish': None, 'n_out': 0, 'stopped_on': None})
        if not rows:
            emit({'event': 'batch_done', 'batch_id': bid, 'members': 0, 'steps': 0, 'ms': 0}); return
        pad = self.tok.pad_token_id if self.tok.pad_token_id is not None else self.tok.eos_token_id
        n, L = len(rows), max(int(r['ids'].shape[0]) for r in rows)
        input_ids = torch.full((n, L), pad, dtype=torch.long); attn = torch.zeros((n, L), dtype=torch.long)
        for i, r in enumerate(rows):
            k = int(r['ids'].shape[0]); input_ids[i, L - k:] = r['ids']; attn[i, L - k:] = 1
        eos = self.tok.eos_token_id
        gen_eos = getattr(self.model.generation_config, 'eos_token_id', None)
        eos_ids = set(eos if isinstance(eos, list) else [eos]) | set(gen_eos if isinstance(gen_eos, list) else ([gen_eos] if gen_eos is not None else []))
        runtime, tok = self, self.tok
        from transformers import StoppingCriteria, StoppingCriteriaList

        class RowStop(StoppingCriteria):
            def __call__(self, all_ids, scores, **kw):
                gen = all_ids[:, L:]
                t = int(gen.shape[1])
                out = torch.zeros(all_ids.shape[0], dtype=torch.bool, device=all_ids.device)
                for i, r in enumerate(rows):
                    if r['done']:
                        out[i] = True; continue
                    last = int(gen[i, t - 1])
                    r['n_out'] = t
                    text = tok.decode(gen[i, :t], skip_special_tokens=True)
                    if not text.endswith('\ufffd'):
                        delta = text[len(r['emitted']):]
                        if delta:
                            emit({'event': 'segment', 'request_id': r['rid'], 'seq': r['seq'], 'text': delta}); r['emitted'] = text; r['seq'] += 1
                    finish = None
                    if last in eos_ids:
                        finish = 'eos'
                    elif r['rid'] in runtime.cancel:
                        finish = 'cancelled'
                    elif r['stop'] and any(s in text for s in r['stop']):
                        finish = 'stop'; r['stopped_on'] = next(s for s in r['stop'] if s in text)
                    elif t >= r['max_new']:
                        finish = 'length'
                    if finish:
                        r['done'] = True; r['finish'] = finish; out[i] = True
                return out
        if self.device == 'cuda':
            torch.cuda.reset_peak_memory_stats(); before = int(torch.cuda.memory_allocated())
        t0 = time.time()
        try:
            with torch.no_grad():
                out = self.model.generate(input_ids=input_ids.to(self.device), attention_mask=attn.to(self.device), max_new_tokens=max(r['max_new'] for r in rows), do_sample=False,
                                          stopping_criteria=StoppingCriteriaList([RowStop()]), pad_token_id=pad, temperature=None, top_p=None, top_k=None)
        except Exception as exc:
            for r in rows:
                emit({'event': 'error', 'request_id': r['rid'], 'code': 'RUNTIME_ERROR', 'reason': type(exc).__name__ + ': ' + str(exc)[:160], 'partial_segments': r['seq']}); runtime.cancel.discard(r['rid'])
            emit({'event': 'batch_done', 'batch_id': bid, 'members': n, 'steps': 0, 'ms': int((time.time() - t0) * 1000), 'error': type(exc).__name__}); return
        if self.device == 'cuda':
            torch.cuda.synchronize()
        ms = int((time.time() - t0) * 1000)
        steps = int(out.shape[1]) - L
        for i, r in enumerate(rows):
            n_out = r['n_out'] if r['done'] else steps
            ids_out = out[i, L:L + n_out]
            text = tok.decode(ids_out, skip_special_tokens=True)
            finish = r['finish'] or ('length' if n_out >= r['max_new'] else 'eos')
            runtime.cancel.discard(r['rid'])
            emit({'event': 'done', 'request_id': r['rid'], 'finish_reason': finish, 'stop_sequence': r['stopped_on'], 'segments': r['seq'], 'text_sha256': __import__('hashlib').sha256(text.encode()).hexdigest(), 'text': text,
                  'usage': {'input_tokens': r['n_in'], 'output_tokens': n_out, 'counting': 'runtime tokenizer; input = templated prompt tokens of THIS request; output = ids generated for THIS row until its own stop (pad tokens of finished rows are never counted)'},
                  'config': {'max_new_tokens': r['max_new'], 'do_sample': False, 'temperature': None, 'top_p': None, 'seed': None, 'stop': r['stop'], 'template': r['template'],
                             'batch': {'id': bid, 'mode': 'static', 'members': n, 'position': i, 'padded_prompt_length': L},
                             'determinism': 'greedy; static batching pads prompts on the left with an attention mask, so bf16 numerics can diverge from a singleton run late in a sequence; measured on the pinned model, not promised bitwise'},
                  'ms': ms, 'tokens_per_second': round(n_out / max(ms / 1000.0, 1e-6), 2)})
        mem = memory_report(torch, self.device)
        if self.device == 'cuda':
            mem['cuda_peak_delta_bytes'] = int(torch.cuda.max_memory_allocated()) - before
        emit({'event': 'batch_done', 'batch_id': bid, 'members': n, 'steps': steps, 'ms': ms, 'padded_prompt_length': L, 'prompt_tokens': sum(r['n_in'] for r in rows), 'output_tokens': sum((r['n_out'] if r['done'] else steps) for r in rows),
              'memory': mem, 'note': 'one forward pass per decode step for all members; a member that finishes early keeps its slot (padded) until the last member stops; cancellation stops delivery and counting for that member immediately, its slot computation until the batch ends'})

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
    emit({'event': 'ready', 'revision_id': spec['revision_id'], 'load_ms': int(rt.load_seconds * 1000), 'versions': dict(rt.versions(), continuous_batching=bool(spec['loader'] == 'causal_lm' and rt.continuous_supported())), 'memory': memory_report(rt.torch, rt.device), 'pid': os.getpid()})
    while True:
        cmd = q.get()
        op = cmd.get('op')
        if op == 'unload':
            emit({'event': 'unloaded', 'reason': cmd.get('reason')}); return
        if op == 'ping':
            emit({'event': 'pong', 'memory': memory_report(rt.torch, rt.device), 'versions': rt.versions()}); continue
        if op == 'generate' and spec['loader'] == 'causal_lm':
            rt.generate(cmd); continue
        if op == 'generate_batch' and spec['loader'] == 'causal_lm':
            rt.generate_batch(cmd); continue
        if op == 'generate_continuous' and spec['loader'] == 'causal_lm':
            rt.generate_continuous(cmd, q); continue
        if op == 'embed' and spec['loader'] == 'encoder':
            rt.embed(cmd); continue
        emit({'event': 'error', 'request_id': cmd.get('request_id'), 'code': 'UNSUPPORTED_OPERATION', 'reason': 'op %r not supported by loader %s' % (op, spec['loader'])})


if __name__ == '__main__':
    main()
