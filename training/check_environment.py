"""Bounded real GPU and loopback HTTP checks; this is not a trained model."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from uuid import uuid4


def check_stack(scratch: Path) -> dict:
    import numpy as np
    import torch
    import torch.nn.functional as F
    import yaml
    from safetensors.torch import load_file, save_file
    from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers
    from torch.utils.checkpoint import checkpoint
    from torch.utils.tensorboard import SummaryWriter

    if not torch.cuda.is_available():
        raise RuntimeError('CUDA is unavailable; refusing to pass with a CPU fallback')
    torch.manual_seed(1234)
    torch.cuda.manual_seed_all(1234)
    torch.set_num_threads(4)
    torch.cuda.reset_peak_memory_stats()
    bf16 = torch.cuda.is_bf16_supported()
    dtype = torch.bfloat16 if bf16 else torch.float32
    result = {
        'torch': torch.__version__, 'cuda_runtime': torch.version.cuda,
        'device': torch.cuda.get_device_name(0),
        'compute_capability': list(torch.cuda.get_device_capability(0)),
        'bf16_supported': bf16, 'training_dtype': str(dtype),
    }

    def make_model():
        return torch.nn.Sequential(
            torch.nn.Linear(64, 128), torch.nn.GELU(), torch.nn.Linear(128, 16),
        ).cuda()

    model = make_model()
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.01)
    inputs = torch.randn(32, 64, device='cuda')
    targets = inputs[:, :16] * 0.5
    losses = []
    torch.cuda.synchronize()
    started = time.perf_counter()
    for _ in range(24):
        optimizer.zero_grad(set_to_none=True)
        with torch.autocast('cuda', dtype=dtype, enabled=bf16):
            prediction = checkpoint(model, inputs, use_reentrant=False)
            loss = F.mse_loss(prediction.float(), targets)
        loss.backward()
        if not torch.isfinite(loss) or not all(
            p.grad is not None and bool(torch.isfinite(p.grad).all()) for p in model.parameters()
        ):
            raise RuntimeError('Non-finite loss or invalid gradients')
        optimizer.step()
        losses.append(float(loss.detach()))
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - started
    if losses[-1] >= losses[0]:
        raise RuntimeError('Small fixed-batch optimization did not reduce loss')
    result['gpu_training'] = {
        'steps': len(losses), 'first_loss': losses[0], 'last_loss': losses[-1],
        'seconds': elapsed, 'activation_checkpointing': True,
        'note': 'Tiny MLP environment check; not language-model throughput or quality',
    }

    q = torch.randn(1, 6, 16, 64, device='cuda', dtype=dtype, requires_grad=True)
    k = torch.randn(1, 2, 16, 64, device='cuda', dtype=dtype, requires_grad=True)
    v = torch.randn(1, 2, 16, 64, device='cuda', dtype=dtype, requires_grad=True)
    attention = F.scaled_dot_product_attention(q, k, v, is_causal=True, enable_gqa=True)
    attention.float().square().mean().backward()
    if attention.shape != q.shape or not all(
        tensor.grad is not None and bool(torch.isfinite(tensor.grad).all()) for tensor in (q, k, v)
    ):
        raise RuntimeError('GQA SDPA forward/backward check failed')
    result['causal_gqa_sdpa'] = 'forward and backward passed'

    model.eval()
    with torch.no_grad():
        expected = model(inputs)
    weights_path = scratch / 'smoke.safetensors'
    save_file({name: value.detach().cpu().contiguous() for name, value in model.state_dict().items()}, str(weights_path))
    restored = make_model().eval()
    restored.load_state_dict(load_file(str(weights_path)))
    with torch.no_grad():
        torch.testing.assert_close(restored(inputs), expected)
    state_path = scratch / 'training-state.pt'
    torch.save({'optimizer': optimizer.state_dict(), 'step': len(losses),
                'rng': torch.get_rng_state(), 'cuda_rng': torch.cuda.get_rng_state_all()}, state_path)
    state = torch.load(state_path, map_location='cpu', weights_only=True)
    restored_optimizer = torch.optim.AdamW(restored.parameters(), lr=0.01)
    restored_optimizer.load_state_dict(state['optimizer'])
    if state['step'] != len(losses) or not restored_optimizer.state:
        raise RuntimeError('Training state serialization failed')
    result['serialization'] = 'safetensors roundtrip and optimizer state load passed'

    samples = ['int add(int a, int b) {\n    return a + b;\n}\n', '你好，测试自己的分词器。', 'C++ Qt signal slot']
    tokenizer = Tokenizer(models.BPE(unk_token='<unk>'))
    tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tokenizer.decoder = decoders.ByteLevel()
    tokenizer.train_from_iterator(samples, trainers.BpeTrainer(
        vocab_size=384, min_frequency=1, show_progress=False,
        initial_alphabet=pre_tokenizers.ByteLevel.alphabet(),
        special_tokens=['<unk>', '<pad>', '<bos>', '<eos>'],
    ))
    tokenizer_path = scratch / 'tokenizer.json'
    tokenizer.save(str(tokenizer_path))
    loaded_tokenizer = Tokenizer.from_file(str(tokenizer_path))
    for sample in samples:
        if loaded_tokenizer.decode(loaded_tokenizer.encode(sample).ids) != sample:
            raise RuntimeError('Tokenizer text roundtrip failed')
    ids = np.asarray(loaded_tokenizer.encode(samples[0]).ids, dtype=np.uint32)
    token_file = scratch / 'tokens.bin'
    ids.tofile(token_file)
    mapped = np.memmap(token_file, dtype=np.uint32, mode='r')
    np.testing.assert_array_equal(mapped, ids)
    del mapped
    config = {'sequence_length': 512, 'dtype': str(dtype)}
    if yaml.safe_load(yaml.safe_dump(config)) != config:
        raise RuntimeError('YAML roundtrip failed')
    result['data_stack'] = 'BPE Chinese/code roundtrip, numpy mmap and YAML passed'

    log_directory = scratch / 'tensorboard'
    with SummaryWriter(log_dir=str(log_directory)) as writer:
        writer.add_scalar('smoke/loss', losses[-1], len(losses))
        writer.flush()
    if not list(log_directory.glob('events.out.tfevents.*')):
        raise RuntimeError('TensorBoard event file missing')
    result['tensorboard'] = 'event written'
    result['peak_allocated_mib'] = round(torch.cuda.max_memory_allocated() / 2**20, 2)
    result['peak_reserved_mib'] = round(torch.cuda.max_memory_reserved() / 2**20, 2)
    return result


def check_http() -> dict:
    from fastapi import FastAPI
    import uvicorn

    app = FastAPI()

    @app.get('/health')
    def health():
        return {'status': 'ok', 'environment_smoke_only': True}

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False, timeout_graceful_shutdown=2))
    worker = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not worker.is_alive() or time.monotonic() > deadline:
                raise RuntimeError('Loopback HTTP service failed to start')
            time.sleep(0.05)
        with opener.open(f'http://127.0.0.1:{port}/health', timeout=3) as response:
            payload = json.load(response)
        if payload != {'status': 'ok', 'environment_smoke_only': True}:
            raise RuntimeError('Unexpected loopback service response')
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        listener.close()
    if worker.is_alive():
        raise RuntimeError('Loopback server did not stop')
    return {'result': 'real loopback HTTP request passed; service stopped',
            'note': 'Health endpoint only, not a trained-model inference service'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='JSON report path')
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    root = Path(__file__).resolve().parents[1]
    report_path = (args.output or root / 'logs/training-environment' / f'check-{stamp}-{uuid4().hex[:8]}.json').resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'python': sys.version,
              'executable': sys.executable, 'passed': False}
    try:
        names = ['torch', 'numpy', 'tokenizers', 'safetensors', 'PyYAML', 'tqdm',
                 'fastapi', 'uvicorn', 'pytest', 'tensorboard']
        report['packages'] = {name: version(name) for name in names}
        print('Checking real CUDA training, serialization and data stack...', flush=True)
        with tempfile.TemporaryDirectory(prefix='smoke-', dir=report_path.parent) as temporary:
            scratch = Path(temporary).resolve()
            if not scratch.is_relative_to(report_path.parent):
                raise RuntimeError('Unexpected temporary directory location')
            report['stack'] = check_stack(scratch)
        print('Checking temporary loopback HTTP service...', flush=True)
        report['http'] = check_http()
        report['passed'] = True
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f'Report: {report_path}')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
