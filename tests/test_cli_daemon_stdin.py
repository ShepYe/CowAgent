import subprocess
import pytest
from types import SimpleNamespace
from click.testing import CliRunner
from cli.commands import process

@pytest.mark.parametrize("is_windows", [False, True])
def test_background_launch_detaches_stdin(tmp_path, monkeypatch, is_windows):
    (tmp_path / 'app.py').write_text('pass')
    monkeypatch.setattr(process, 'get_project_root', lambda: str(tmp_path))
    monkeypatch.setattr(process, '_read_pid', lambda: None)
    monkeypatch.setattr(process, 'load_config_json', lambda: {'channel_type': 'feishu'})
    monkeypatch.setattr(process, '_write_pid', lambda pid: None)
    monkeypatch.setattr(process, '_IS_WIN', is_windows)
    monkeypatch.setattr(process.subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200, raising=False)
    launches = []
    def launch(*args, **kwargs):
        launches.append(kwargs)
        return SimpleNamespace(pid=1234)
    monkeypatch.setattr(process.subprocess, 'Popen', launch)
    result = CliRunner().invoke(process.start, ['--no-logs'])
    assert result.exit_code == 0, result.output
    assert launches[0]['stdin'] == subprocess.DEVNULL
    if is_windows:
        assert launches[0]['creationflags'] == 0x200 | 0x08000000
        assert 'start_new_session' not in launches[0]
    else:
        assert launches[0]['start_new_session'] is True
        assert 'creationflags' not in launches[0]
    assert launches[0]['cwd'] == str(tmp_path)

def test_daemon_cannot_consume_callers_input_pipe(tmp_path):
    import os
    import sys
    from pathlib import Path
    checkout = Path(__file__).resolve().parents[1]
    (tmp_path / 'app.py').write_text('pass')
    script = '''
import subprocess, sys
from cli.commands import process
process.get_project_root = lambda: sys.argv[1]
process._read_pid = lambda: None
process.load_config_json = lambda: {'channel_type': 'feishu'}
process._write_pid = lambda pid: None
popen = subprocess.Popen
def launch(args, **kwargs):
    child = popen([sys.executable, '-c', 'import sys; print(repr(sys.stdin.read()))'], **kwargs)
    child.wait(timeout=5)
    return child
process.subprocess.Popen = launch
process.start.main(['--no-logs'], standalone_mode=False)
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            input='shell-input-must-stay-private', text=True,
                            cwd=checkout, env={**os.environ, 'PYTHONPATH': str(checkout),
                                               'PYTHONIOENCODING': 'utf-8'},
                            capture_output=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'nohup.out').read_text().strip() == "''"


@pytest.mark.parametrize("channels", ["terminal,feishu", "feishu, terminal", ["feishu", "terminal"], "terminal", "feishu"])
def test_configured_terminal_keeps_native_stdin(tmp_path, channels):
    import os
    import sys
    from pathlib import Path
    import json
    checkout = Path(__file__).resolve().parents[1]
    (tmp_path / "app.py").write_text(
        "import pathlib,sys; pathlib.Path('received').write_text(sys.stdin.read())"
    )
    script = r'''import json,subprocess,sys
from cli.commands import process
process.get_project_root = lambda: sys.argv[1]
process.load_config_json = lambda: {"channel_type": json.loads(sys.argv[2])}
process._read_pid = lambda: None
process._write_pid = lambda pid: None
popen = subprocess.Popen
def launch(args, **kwargs):
    child = popen(args, **kwargs)
    child.wait(timeout=5)
    return child
process.subprocess.Popen = launch
process.start.main(["--no-logs"], standalone_mode=False)
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), json.dumps(channels)],
        input="terminal-input", text=True, cwd=checkout,
        env={**os.environ, "PYTHONPATH": str(checkout), "PYTHONIOENCODING": "utf-8"},
        capture_output=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "received").read_text() == (
        "terminal-input" if "terminal" in channels else ""
    )
