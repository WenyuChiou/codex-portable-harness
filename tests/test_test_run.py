import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import test_run as runner
import install_maintenance as installer


class TestRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "home"
        self.home.mkdir()

    def cli(self, code, timeout=15):
        return subprocess.run([sys.executable, str(Path(runner.__file__)), "--timeout", str(timeout), "--", sys.executable, "-c", code],
                              env={**os.environ, "CODEX_HOME":str(self.home)}, capture_output=True, timeout=30)

    def test_success_cleans_owned_scratch_and_retains_complete_log(self):
        result = self.cli("import os,tempfile;from pathlib import Path;p=Path(tempfile.gettempdir());assert p==Path(os.environ['CODEX_TEST_SCRATCH']);(p/'probe').write_text('scratch');print('complete output')")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"complete output", result.stdout)
        self.assertEqual(list((self.home/"maintenance/test-runs").iterdir()), [])
        logs = list((self.home/"maintenance/test-logs").glob("*.log"))
        self.assertIn(b"complete output", logs[0].read_bytes())

    def test_failure_keeps_diagnostics_until_expiry(self):
        result = self.cli("import os,sys;from pathlib import Path;(Path(os.environ['CODEX_TEST_SCRATCH'])/'failure').write_text('diagnostic');sys.exit(7)")
        self.assertEqual(result.returncode, 7, result.stderr)
        path = next((self.home/"maintenance/test-runs").iterdir())
        self.assertEqual((path/"scratch/failure").read_text(), "diagnostic")
        self.assertEqual(runner.sweep(self.home, True), 0)
        self.assertTrue(path.exists())
        self.assertEqual(runner.sweep(self.home, True, time.time()+8*runner.DAY), 0)
        self.assertFalse(path.exists())

    def test_active_old_run_survives_sweep_and_foreign_data_survives(self):
        root, _ = runner.areas(self.home)
        path = root/("a"*32)
        path.mkdir()
        (path/"run.json").write_text(json.dumps({"owner":runner.OWNER,"id":path.name,"started":0,"state":"running"}))
        foreign = root/"unmanaged"
        foreign.mkdir()
        with runner.lease(path/"run.lock"):
            self.assertEqual(runner.sweep(self.home, True, time.time()+8*runner.DAY), 0)
            self.assertTrue(path.exists())
        self.assertEqual(runner.sweep(self.home, True, time.time()+8*runner.DAY), 0)
        self.assertFalse(path.exists())
        self.assertTrue(foreign.exists())

    def test_macos_blocking_io_error_is_an_active_lease(self):
        lock=self.home/'test.lock'
        operation='msvcrt.locking' if os.name=='nt' else 'fcntl.flock'
        with patch(operation,side_effect=BlockingIOError(35,'resource temporarily unavailable')):
            with self.assertRaises(runner.ActiveRun):
                with runner.lease(lock):
                    self.fail('busy lease must not be acquired')

    def test_timeout_terminates_the_spawned_process_tree(self):
        pidfile = self.home/"child.pid"
        code = "import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);Path(sys.argv[1]).write_text(str(p.pid));time.sleep(60)"
        result = subprocess.run([sys.executable, runner.__file__, "--timeout", "1", "--", sys.executable, "-c", code, str(pidfile)],
                                env={**os.environ,"CODEX_HOME":str(self.home)},capture_output=True,timeout=15)
        self.assertEqual(result.returncode,124,result.stderr)
        pid=int(pidfile.read_text())
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes
            api=ctypes.WinDLL('kernel32',use_last_error=True)
            api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
            api.OpenProcess.restype=wintypes.HANDLE
            api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
            api.CloseHandle.argtypes=[wintypes.HANDLE]
            handle=api.OpenProcess(0x100000,False,pid)
            if handle:
                try:self.assertEqual(api.WaitForSingleObject(handle,5000),0)
                finally:api.CloseHandle(handle)
        else:
            try:os.kill(pid,0)
            except ProcessLookupError:pass
            else:
                status=Path('/proc')/str(pid)/'stat'
                self.assertTrue(status.exists() and status.read_text().split()[2]=='Z')

    def test_pytest_override_is_rejected_before_scratch_creation(self):
        with self.assertRaises(ValueError):
            runner.execute(self.home,[sys.executable,'-m','pytest','--basetemp=/outside'],10)
        self.assertFalse((self.home/'maintenance/test-runs').exists())

    def test_changed_log_is_retained_and_reported(self):
        self.assertEqual(self.cli("print('evidence')").returncode,0)
        log=next((self.home/'maintenance/test-logs').glob('*.log'))
        log.write_text('user changed evidence')
        self.assertEqual(runner.sweep(self.home,True,time.time()+31*runner.DAY),1)
        self.assertTrue(log.exists())

    def test_expired_log_and_receipt_are_removed(self):
        self.assertEqual(self.cli("print('evidence')").returncode,0)
        self.assertEqual(runner.sweep(self.home,True,time.time()+31*runner.DAY),0)
        self.assertEqual(list((self.home/'maintenance/test-logs').iterdir()),[])

    def test_parallel_runs_have_distinct_scratch_and_both_clean(self):
        code="import os,time;from pathlib import Path;(Path(os.environ['CODEX_TEST_SCRATCH'])/'probe').write_text('own');time.sleep(.3)"
        command=[sys.executable,runner.__file__,'--timeout','10','--',sys.executable,'-c',code]
        processes=[subprocess.Popen(command,env={**os.environ,'CODEX_HOME':str(self.home)},stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
        outputs=[]
        results=[process.communicate(timeout=20) for process in processes]
        for process,(out,err) in zip(processes,results):
            self.assertEqual(process.returncode,0,err)
            outputs.append(json.loads(out.splitlines()[0])['scratch'])
        self.assertNotEqual(*outputs)
        self.assertEqual(list((self.home/'maintenance/test-runs').iterdir()),[])

    def test_nested_link_blocks_cleanup_and_external_payload_survives(self):
        root,_=runner.areas(self.home)
        path=root/('b'*32);path.mkdir()
        (path/'run.json').write_text(json.dumps({'owner':runner.OWNER,'id':path.name,'started':0,'state':'finished','exit_code':0}))
        outside=self.home/'outside';outside.mkdir()
        (outside/'keep').write_text('original')
        link=path/'redirect'
        if os.name=='nt':
            command='New-Item -ItemType Junction -Path $env:TEST_JUNCTION_LINK -Target $env:TEST_JUNCTION_TARGET -ErrorAction Stop | Out-Null'
            subprocess.run(['powershell','-NoProfile','-Command',command],check=True,capture_output=True,timeout=15,
                           env={**os.environ,'TEST_JUNCTION_LINK':str(link),'TEST_JUNCTION_TARGET':str(outside)})
        else:link.symlink_to(outside,target_is_directory=True)
        self.assertEqual(runner.sweep(self.home,True),1)
        self.assertTrue(path.exists())
        self.assertEqual((outside/'keep').read_text(),'original')

    def test_missing_executable_has_consistent_error_receipt_and_log(self):
        result=subprocess.run([sys.executable,runner.__file__,'--timeout','5','--',str(self.home/'missing-executable')],
                              env={**os.environ,'CODEX_HOME':str(self.home)},capture_output=True,timeout=15)
        self.assertEqual(result.returncode,125,result.stderr)
        receipt=json.loads(next((self.home/'maintenance/test-logs').glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(receipt['exit_code'],125)
        self.assertIn('error',receipt)
        self.assertIn(b'HARNESS_ERROR',next((self.home/'maintenance/test-logs').glob('*.log')).read_bytes())

    def test_already_swept_success_is_successful_cleanup(self):
        native=runner.atomic_write_json
        def publish_then_sweep(path,value):
            native(path,value)
            if path.parent.name=='test-logs':
                self.assertEqual(runner.sweep(self.home,True),0)
        with patch.object(runner,'atomic_write_json',side_effect=publish_then_sweep):
            self.assertEqual(runner.execute(self.home,[sys.executable,'-c','print("successful child")'],5),0)
        self.assertEqual(list((self.home/'maintenance/test-runs').iterdir()),[])

    def test_abandoned_run_log_gets_an_expiring_receipt(self):
        root,logs=runner.areas(self.home)
        path=root/('d'*32);path.mkdir()
        (path/'run.json').write_text(json.dumps({'owner':runner.OWNER,'id':path.name,'started':0,'state':'running'}))
        log=logs/(path.name+'.log');log.write_text('interrupted output')
        now=time.time()+8*runner.DAY
        self.assertEqual(runner.sweep(self.home,True,now),0)
        receipt=json.loads(log.with_suffix('.json').read_text())
        self.assertEqual(receipt['state'],'abandoned')
        self.assertEqual(runner.sweep(self.home,True,now+31*runner.DAY),0)
        self.assertFalse(log.exists())

    def test_interpreter_change_replaces_owned_hook_and_preserves_other_hooks(self):
        (self.home/'hooks.json').write_text(json.dumps({'hooks':{'Stop':[{'hooks':[{'command':'custom'}]}]}}))
        installer.deploy(self.home,True,sys.executable)
        alternate=getattr(sys,'_base_executable',sys.executable)
        if alternate==sys.executable:
            alternate=str(Path(sys.executable).parent)+os.sep+'.'+os.sep+Path(sys.executable).name
        self.assertTrue(Path(alternate).is_file())
        installer.deploy(self.home,True,alternate)
        installer.deploy(self.home,True,alternate)
        data=json.loads((self.home/'hooks.json').read_text())
        for event in ('Stop','SessionEnd'):
            hooks=[h for group in data['hooks'][event] for h in group['hooks']]
            cleanup=[h for h in hooks if 'test_run.py' in h.get('command','')]
            self.assertEqual(len(cleanup),1)
            self.assertIn(alternate,cleanup[0]['command'])
        self.assertEqual(data['hooks']['Stop'][0]['hooks'][0]['command'],'custom')


if __name__ == '__main__':
    unittest.main()
