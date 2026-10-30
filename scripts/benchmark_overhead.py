"""Offline timing/byte comparison. No model calls or token-saving claim."""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from benchmark_autonomy import _source_fingerprint
from common import active_codex_home, ensure_within
import policy_engine


def legacy_fingerprint(root: Path) -> str:
    """Pre-fix implementation, kept local for a controlled comparison."""
    digest = hashlib.sha256()
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        if Path(directory) == root:
            names[:] = [name for name in names if name != '.git']
        names.sort()
        for name in sorted(names + files):
            path = Path(directory)/name; info = os.lstat(path)
            digest.update(path.relative_to(root).as_posix().encode('utf-8'))
            digest.update(stat.S_IFMT(info.st_mode).to_bytes(4,'big'))
            if stat.S_ISLNK(info.st_mode): digest.update(os.readlink(path).encode('utf-8'))
            elif stat.S_ISREG(info.st_mode):
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024*1024), b''): digest.update(chunk)
    return digest.hexdigest()


def median_seconds(function, repetitions):
    values=[]
    for _ in range(repetitions):
        start=time.perf_counter(); function(); values.append(time.perf_counter()-start)
    return statistics.median(values), values


def run(*, repetitions=3, runtime_mib=64, checkpoint=None):
    if repetitions not in range(1,11) or runtime_mib not in range(1,257):
        raise ValueError('Use1..10 repetitions and1..256 MiB')
    home=active_codex_home(); temporary_root=home/'harness-runs'
    ensure_within(temporary_root,home)
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='overhead-',dir=temporary_root) as temporary:
        root=Path(temporary); ensure_within(root,home)
        source=root/'source'; source.mkdir()
        subprocess.run(['git','init','-q',str(source)],check=True,capture_output=True)
        (source/'.gitignore').write_text('runs/\n',encoding='utf-8')
        (source/'code.py').write_text('value = 1\n',encoding='utf-8')
        subprocess.run(['git','-C',str(source),'add','.gitignore','code.py'],check=True,capture_output=True)
        runtime=source/'runs'; runtime.mkdir()
        with (runtime/'checkpoint.bin').open('wb') as stream:
            for _ in range(runtime_mib): stream.write(b'x'*(1024*1024))
        old, old_samples=median_seconds(lambda: legacy_fingerprint(source),repetitions)
        new, new_samples=median_seconds(lambda: _source_fingerprint(source),repetitions)
        source_bytes=sum((source/name).stat().st_size for name in ['.gitignore','code.py'])
        result={'schema_version':1,'scope':'Offline fixture; no provider requests',
                'fingerprint':{'runtime_mib':runtime_mib,'repetitions':repetitions,
                    'before_seconds':old_samples,'after_seconds':new_samples,
                    'before_median_seconds':old,'after_median_seconds':new,
                    'before_python_hashed_file_bytes':runtime_mib*1024*1024+source_bytes,
                    'after_python_hashed_file_bytes':source_bytes,
                    'note':'Git index/metadata I/O is included in time, not in Python hashed-file byte count.'},
                'actual_model_tokens':None,'actual_model_cost':None,
                'token_note':'Output-byte reduction is not a measured model-token or monetary saving.'}
        metrics={key:0 for key in ['cycles','tool_calls','elapsed_seconds','transcript_bytes',
                 'last_checkpoint_bytes','same_failure_retries','no_evidence_cycles','children_spawned',
                 'active_children','task_packet_tokens','parent_summary_tokens','memory_digest_tokens','child_result_words']}
        fixture={'schema_version':1,'task_id':'fixture','parent_id':None,'status':'running',
                 'evidence_refs':[],'metrics':metrics,'stop_reason':None}
        cp=root/'checkpoint.json'; cp.write_text(json.dumps(fixture),encoding='utf-8')
        summaries={}
        for compact in (False,True):
            output=io.StringIO(); argv=['policy_engine.py','evaluate',str(cp)]+(['--compact'] if compact else [])
            with patch.object(sys,'argv',argv),contextlib.redirect_stdout(output): code=policy_engine.main()
            summaries['compact' if compact else 'full']={'exit_code':code,'utf8_bytes':len(output.getvalue().encode('utf-8')),
                                                        'decision':json.loads(output.getvalue())['decision']}
        result['decision_output']=summaries
        if checkpoint is not None:
            checkpoint=Path(checkpoint); ensure_within(checkpoint,home)
            raw=checkpoint.read_bytes(); before=hashlib.sha256(raw).hexdigest()
            seconds, samples=median_seconds(lambda: json.loads(raw),repetitions)
            if hashlib.sha256(checkpoint.read_bytes()).hexdigest()!=before:
                raise RuntimeError('Checkpoint changed during readonly measurement; preserve evidence and repeat only after a stable boundary')
            result['actual_checkpoint_parse']={'bytes':len(raw),'sha256':before,'seconds':samples,'median_seconds':seconds,
                'mutated':False,'policy_decisions_performed':False}
        return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--checkpoint',type=Path); parser.add_argument('--runtime-mib',type=int,default=64)
    parser.add_argument('--repetitions',type=int,default=3); args=parser.parse_args()
    ensure_within(args.output,active_codex_home())
    if args.output.exists(): raise FileExistsError('Choose a new output; do not overwrite measurements')
    result=run(repetitions=args.repetitions,runtime_mib=args.runtime_mib,checkpoint=args.checkpoint)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as stream: json.dump(result,stream,indent=2,allow_nan=False); stream.write('\n')
    print(json.dumps(result,sort_keys=True))
