from datetime import datetime
import json
import math
from pathlib import Path
import sys

import pytest
from measurement import energy


def test_trapezoids_clip_window_and_report_hz():
    joules, hz = energy.integrate([(0,10),(2,30),(4,10)],1,3)
    assert joules == 50
    assert hz == .5


def test_missing_edges_gaps_bad_values_and_reversed_samples_refuse():
    for samples in [[(1,10),(2,10)],[(0,10),(10,10)],[(0,10),(2,math.nan)],[(2,10),(0,10)],[(0,10),(0,10)],[(0,-1),(2,10)]]:
        with pytest.raises(ValueError):
            energy.integrate(samples,0,2)


def test_hwinfo_csv_and_missing_sampler(tmp_path):
    p = tmp_path/'power.csv'
    p.write_text('Date,Time,CPU Package Power [W]\n13.09.2026,12:00:00.000,10\n13.09.2026,12:00:02.000,30\n13.09.2026,12:00:04.000,10\n')
    t = datetime(2026,9,13,12).timestamp()
    measured = energy.measure(p,'CPU Package Power [W]','%d.%m.%Y %H:%M:%S.%f',t+1,t+3)
    assert measured['energy_joules'] == 50
    assert measured['sampler'] == 'HWiNFO64 CSV'
    assert measured['measurement_quality'] == 'sampled'
    missing = energy.measure(None,'unused','unused',t,t+2)
    assert missing['energy_joules'] is None
    assert missing['measurement_quality'] == 'unobserved'


def test_no_sampler_command_runs_and_existing_manifest_is_unchanged(tmp_path):
    original = tmp_path/'pre-run.json'
    original.write_text('{"identity":"original"}')
    output = tmp_path/'complete.json'
    rc = energy.main(['--out',str(output),'--run-manifest',str(original),'--',sys.executable,'-c','print(1+1)'])
    assert rc == 0
    data = json.loads(output.read_text())
    assert data['energy_joules'] is None and data['measurement_quality'] == 'unobserved'
    assert data['links']['run_manifest']['sha256']
    assert original.read_text() == '{"identity":"original"}'
    with pytest.raises(FileExistsError):
        energy.main(['--out',str(output),'--',sys.executable,'-c','raise RuntimeError()'])


def test_duplicate_sensor_name_refuses(tmp_path):
    p=tmp_path/'ambiguous.csv'
    p.write_text('Date,Time,CPU Package Power [W],CPU Package Power [W]\n')
    data=energy.measure(p,'CPU Package Power [W]','%d.%m.%Y %H:%M:%S.%f',0,1)
    assert data['measurement_quality']=='unobserved'
    assert 'found 2' in data['reason']


def test_command_manifest_with_synthetic_csv_sampler(tmp_path):
    # Synthetic watts test wiring and integration only, not a hardware energy claim.
    import threading
    import time
    csv_path = tmp_path/'live.csv'
    done = threading.Event()
    ready = threading.Event()
    def logger():
        with csv_path.open('w') as fh:
            fh.write('Date,Time,CPU Package Power [W]\n')
            while not done.is_set():
                stamp = datetime.now().strftime('%d.%m.%Y,%H:%M:%S.%f')
                fh.write(stamp+',10\n'); fh.flush(); ready.set()
                done.wait(.02)
    thread = threading.Thread(target=logger)
    thread.start(); ready.wait(timeout=3)
    try:
        out = tmp_path/'measured.json'
        assert energy.main(['--out',str(out),'--hwinfo-csv',str(csv_path),
                            '--',sys.executable,'-c','sum(i*i for i in range(1000000))']) == 0
        data=json.loads(out.read_text())
        assert math.isclose(data['energy_joules'], 10*(data['end_unix']-data['start_unix']), rel_tol=1e-6)
        assert data['sampler']=='HWiNFO64 CSV'
    finally:
        done.set(); thread.join(timeout=3)
