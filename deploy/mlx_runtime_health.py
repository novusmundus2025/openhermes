"""Backup-first MLX runtime health and conservative admission defaults."""

def patch_source(source):
    """Patch the installed MLX server; node-agent regenerates its startup wrapper."""
    if '# OpenHermes runtime admission defaults' in source:return patch_stall_detection(source)
    for flag,old,new in [('--decode-concurrency','32','1'),('--prompt-concurrency','8','1'),('--prefill-step-size','2048','512')]:
        marker='"'+flag+'",\n        type=int,\n        default='+old+','
        if marker not in source:raise ValueError('Unsupported MLX CLI layout: '+flag)
        source=source.replace(marker,marker.replace('default='+old,'default='+new),1)
    health='        # OpenHermes: models discovery must not report a dead generation thread as healthy.\n        if self.path.startswith("/v1/models") and not self.response_generator.is_healthy:\n            self._set_completion_headers(503)\n            self.end_headers()\n            self.wfile.write(b\'{"error":"MLX generation worker unavailable"}\')\n            return\n'
    marker='        if self.path.startswith("/v1/models"):'
    if marker not in source:raise ValueError('Unsupported MLX health layout')
    source=source.replace(marker,health+marker,1)
    marker='def main():\n'
    if marker not in source:raise ValueError('Unsupported MLX entrypoint')
    source=source.replace(marker,marker+'    # OpenHermes runtime admission defaults\n    mx.set_cache_limit(1024**3)\n',1)
    compile(source,'mlx_lm/server.py','exec')
    return patch_stall_detection(source)


def patch_stall_detection(source):
    if '# OpenHermes stalled generation detection' in source:return source
    if '    def do_POST(self):' not in source:return source
    imports = 'import time as _oh_time\nimport threading as _oh_threading\n_oh_requests = {}\n_oh_lock = _oh_threading.Lock()\n'
    # Insert after future imports and module documentation.
    import ast
    tree=ast.parse(source)
    insert=0
    for node in tree.body:
        if isinstance(node,ast.Expr) and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str):insert=node.end_lineno
        elif isinstance(node,ast.ImportFrom) and node.module=='__future__':insert=node.end_lineno
        else:break
    lines=source.splitlines(keepends=True);lines.insert(insert,imports);source=''.join(lines)
    wrapper='''    # OpenHermes stalled generation detection
    def do_POST(self):
        key = id(self)
        with _oh_lock:
            _oh_requests[key] = _oh_time.monotonic()
        try:
            return self._openhermes_do_POST()
        finally:
            with _oh_lock:
                _oh_requests.pop(key, None)

    def _openhermes_do_POST(self):'''
    source=source.replace('    def do_POST(self):',wrapper,1)
    check='''        if self.path.startswith("/v1/models"):
            with _oh_lock:
                oldest = min(_oh_requests.values(), default=_oh_time.monotonic())
            busy = getattr(self.response_generator, '_oh_batch_started', None)
            if (_oh_time.monotonic()-oldest > 240 or
                    (busy is not None and _oh_time.monotonic()-busy > 120)):
                self._set_completion_headers(503)
                self.end_headers()
                self.wfile.write(b'{"error":"MLX generation worker stalled"}')
                return
'''
    marker='        # OpenHermes: models discovery'
    if marker not in source:raise ValueError('Missing health patch')
    source=source.replace(marker,check+marker,1)
    marker='                    prompt_responses, gen_responses = batch_generator.next()'
    if marker not in source:raise ValueError('Unsupported generation loop')
    source=source.replace(marker,'                    self._oh_batch_started = _oh_time.monotonic()\n'+marker+'\n                    self._oh_batch_started = None',1)
    compile(source,'mlx_lm/server.py','exec')
    return source


if __name__=='__main__':
    import argparse,shutil,time
    from pathlib import Path
    parser=argparse.ArgumentParser();parser.add_argument('server_source',type=Path);args=parser.parse_args()
    original=args.server_source.read_text();updated=patch_source(original)
    if updated!=original:
        shutil.copy2(args.server_source,str(args.server_source)+'.before-openhermes-'+str(time.time_ns()))
        args.server_source.write_text(updated)
    print('MLX health and admission patch installed; restart runtime to apply')
