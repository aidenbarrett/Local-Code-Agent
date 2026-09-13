from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import psutil
import pytest
from local_agent.config import MODEL_PRESETS
from measurement import serve


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def test_every_preset_has_identity_and_headroom():
    for name, config in MODEL_PRESETS.items():
        assert all((config.runtime, config.quant, config.device, config.tier)), name
        assert 0 < config.context_budget_tokens < config.server_max_prompt_length, name
    c = MODEL_PRESETS['ptl-npu-8b']
    with pytest.raises(serve.Refusal, match='context budget'):
        serve.validate(replace(c, context_budget_tokens=c.server_max_prompt_length))


def test_five_profiles_and_exact_npu_arguments(tmp_path):
    names = ['ptl-npu-8b', 'ptl-npu-8b-16k', 'ptl-gpu-30b', 'ptl-cpu-30b', 'ceiling-27b-dense']
    ports = set()
    for name in names:
        c = MODEL_PRESETS[name]
        p = serve.make_plan(name, c, tmp_path, windows=True)
        assert p['argv'][0] == 'ovms.exe'
        assert p['args'][p['args'].index('--target_device')+1] == c.device
        assert p['args'][p['args'].index('--rest_port')+1] == str(p['port'])
        assert '--source_model' not in p['args']  # no download between check and inference
        ports.add(p['port'])
        if c.device == 'NPU':
            assert p['args'][p['args'].index('--max_prompt_len')+1] == str(c.server_max_prompt_length)
            assert json.loads(p['args'][-1]) == {'NPUW_LLM_PREFILL_ATTENTION_HINT': 'PYRAMID'}
        else:
            assert '--max_prompt_len' not in p['args']  # this is an NPU-only property
    assert len(ports) == 5


def test_llama_native_and_openvino_devices(tmp_path):
    c = MODEL_PRESETS['nuc-llama-8b']
    p = serve.make_plan('native', c, tmp_path, gguf=tmp_path/'with spaces.gguf')
    assert p['args'][-2:] == ['-ngl', '0']
    assert p['env'] == {}
    for device in ['CPU', 'GPU', 'GPU.0', 'GPU.1', 'NPU']:
        cfg = replace(c, llama_backend='openvino', device=device, quant='Q4_0')
        p = serve.make_plan('ov', cfg, tmp_path)
        assert p['env']['GGML_OPENVINO_DEVICE'] == device
        assert p['env']['GGML_OPENVINO_STATEFUL_EXECUTION'] == '0'
        assert p['args'][p['args'].index('-np')+1] == '1'
        assert p['args'][p['args'].index('-c')+1] == str(cfg.server_max_prompt_length)
    with pytest.raises(serve.Refusal, match='Q4_0'):
        serve.make_plan('ov', replace(c, llama_backend='openvino', device='NPU'), tmp_path)


def ir(path, mode='int4_sym', group='-1', ratio='1.0'):
    path.mkdir(parents=True, exist_ok=True)
    (path/'openvino_model.xml').write_text(f'<net><rt_info><nncf><weight_compression><mode value="{mode}"/><group_size value="{group}"/><ratio value="{ratio}"/></weight_compression></nncf></rt_info></net>')


def test_npu_reads_ir_and_refuses_actual_asym_before_launch(tmp_path, monkeypatch):
    c = replace(MODEL_PRESETS['ptl-gpu-30b'], device='NPU')
    p = serve.make_plan('wrong', c, tmp_path)
    ir(Path(p['model_dir']), 'int4_asym', '128')
    monkeypatch.setattr(serve, 'launch', lambda p: pytest.fail('must not launch'))
    monkeypatch.setattr(serve, 'available_devices', lambda: pytest.fail('precision first'))
    with pytest.raises(serve.Refusal, match='INT4_ASYM'):
        serve.start(p, c)
    assert not Path(p['state_file']).exists()
    assert not Path(p['spec_file']).exists()


def test_precision_unknown_int8_ratio_and_valid_artifact(tmp_path):
    for mode, group, ratio in [('int8_asym','128','1.0'), ('unknown','-1','1.0'), ('int4_sym','-1','0.8')]:
        ir(tmp_path, mode, group, ratio)
        with pytest.raises(serve.Refusal, match='NPU requires'):
            serve.check_precision(tmp_path, 'NPU')
    ir(tmp_path)
    (tmp_path/'openvino_config.json').write_text('{"quantization_config":{"default_config":{"quant_method":"default"}}}')
    assert serve.check_precision(tmp_path, 'NPU')['mode'] == 'INT4_SYM'


def test_preflight_device_disk_port_and_experimental(tmp_path, monkeypatch):
    serve.check_device('GPU', ['CPU', 'GPU.0'])
    with pytest.raises(serve.Refusal, match='GPU.1'):
        serve.check_device('GPU.1', ['CPU', 'GPU.0'])
    with pytest.raises(serve.Refusal, match='requires'):
        serve.check_disk(tmp_path, 10**9)
    c = MODEL_PRESETS['ptl-npu-8b-16k']
    p = serve.make_plan('experimental', c, tmp_path)
    with pytest.raises(serve.Refusal, match='unproven'):
        serve.preflight(p, c)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); sock.listen()
        p['port'] = sock.getsockname()[1]
        with pytest.raises(serve.Refusal, match='occupied'):
            serve.check_port(p)


# Real HTTP children exercise persistence, independent ports, launcher quoting,
# stale PIDs, idempotence and stop on both Windows and Linux. No model required.
SERVER = '''import http.server,json,sys
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.end_headers()
  self.wfile.write(json.dumps({'data':[{'id':sys.argv[2]}]} if self.path=='/v1/models' else {}).encode())
 def log_message(self,*args):pass
http.server.HTTPServer(('127.0.0.1',int(sys.argv[1])),Handler).serve_forever()
'''


def require_process_inventory():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        observed = psutil.Process(child.pid)
        assert observed.ppid() == os.getpid()
        observed.exe()
    except (psutil.AccessDenied, psutil.NoSuchProcess, AssertionError):
        if os.environ.get("CI") or os.name == "nt":
            raise
        pytest.skip("sandbox PID namespace differs from /proc; native CI required")
    finally:
        child.terminate()
        child.wait(timeout=5)


def mock_plan(tmp_path, name):
    c = replace(MODEL_PRESETS['nuc-llama-8b'], base_url=f'http://127.0.0.1:{free_port()}/v1')
    p = serve.make_plan(name, c, tmp_path, executable=sys.executable)
    p['args'] = ['-u', '-c', SERVER, str(p['port']), c.model]
    p['argv'] = [sys.executable, *p['args']]
    return p, c


def test_two_real_servers_idempotence_abrupt_death_and_stop(tmp_path, monkeypatch):
    require_process_inventory()
    monkeypatch.setattr(serve, 'preflight', lambda *a: {})
    first, c1 = mock_plan(tmp_path, 'first')
    second, c2 = mock_plan(tmp_path, 'second')
    try:
        a = serve.start(first, c1, wait_seconds=10)
        b = serve.start(second, c2, wait_seconds=10)
        assert a['healthy'] and b['healthy'] and a['pid'] != b['pid']
        assert serve.start(first, c1, wait_seconds=1)['pid'] == a['pid']
        assert serve.status(second)['healthy']
        psutil.Process(a['pid']).kill()
        deadline = time.monotonic() + 5
        while serve.status(first)['process_alive'] and time.monotonic() < deadline:
            time.sleep(.05)
        assert not serve.status(first)['healthy']
        assert serve.status(second)['healthy']
        assert serve.stop(second)['stopped']
        assert not Path(second['state_file']).exists()
    finally:
        serve.stop(first, grace_seconds=.2)
        serve.stop(second, grace_seconds=.2)


def test_live_unhealthy_refuses_without_killing(tmp_path, monkeypatch):
    require_process_inventory()
    monkeypatch.setattr(serve, 'preflight', lambda *a: {})
    p, c = mock_plan(tmp_path, 'unhealthy')
    p['args'] = ['-c', 'import time; time.sleep(60)']
    p['argv'] = [sys.executable, *p['args']]
    try:
        with pytest.raises(serve.Refusal, match='not ready'):
            serve.start(p, c, wait_seconds=0)
        record = serve.read_record(p)
        assert serve.owned_process(record)
        with pytest.raises(serve.Refusal, match='live but unhealthy'):
            serve.start(p, c, wait_seconds=0)
        assert serve.owned_process(record)
    finally:
        serve.stop(p, grace_seconds=.1)


def test_pid_reuse_refuses_to_stop_other_process(tmp_path):
    require_process_inventory()
    p, c = mock_plan(tmp_path, 'reuse')
    me = psutil.Process()
    serve.write_json(p['state_file'], {'pid':me.pid, 'create_time':me.create_time()-1, 'process_exe':me.exe(), 'plan':p})
    with pytest.raises(serve.Refusal, match='another process'):
        serve.stop(p)
    assert Path(p['state_file']).exists()


def test_dry_run_creates_no_state(tmp_path):
    assert serve.main(['start','--profile','ptl-npu-8b','--dry-run','--runtime-root',str(tmp_path)]) == 0
    assert not list(tmp_path.iterdir())


def test_status_does_not_adopt_foreign_http_or_wrong_model(tmp_path, monkeypatch):
    p,c=mock_plan(tmp_path,'state')
    monkeypatch.setattr(serve,'get_json',lambda url:(200,{'data':[{'id':c.model}]}))
    assert not serve.status(p)['healthy']  # HTTP success alone does not establish ownership.
    serve.write_json(p['state_file'],{'pid':123,'create_time':time.time(),'plan':p})
    monkeypatch.setattr(serve,'owned_process',lambda record:object())
    monkeypatch.setattr(serve,'get_json',lambda url:(200,{'data':[{'id':'wrong-model'}]}))
    assert not serve.status(p)['healthy']
    monkeypatch.setattr(serve,'get_json',lambda url:(404,{}))
    assert not serve.status(p)['healthy']
    monkeypatch.setattr(serve,'get_json',lambda url:(200,{'data':[{'id':c.model}]}))
    assert serve.status(p)['healthy']
    assert serve.status(p)['server_observed_device'] is None


def test_status_all_lists_presets(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(serve,'get_json',lambda url:(None,{'error':'offline'}))
    assert serve.main(['status','--all','--runtime-root',str(tmp_path)]) == 0
    output=json.loads(capsys.readouterr().out)
    assert {'ptl-npu-8b','ptl-gpu-30b'} <= {row['profile'] for row in output}
    assert not any(row['healthy'] for row in output)
