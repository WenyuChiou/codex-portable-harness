from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import benchmark_overhead


class OverheadBenchmarkTests(unittest.TestCase):
    def test_resolved_escape_is_rejected_without_symlink_privilege(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve(); home=root/'home'; home.mkdir()
            outside=root/'outside'; outside.mkdir(); target=home/'harness-runs'
            original=Path.resolve
            def redirected(path,*args,**kwargs):
                return outside if path==target else original(path,*args,**kwargs)
            with patch.object(benchmark_overhead,'active_codex_home',return_value=home), patch.object(Path,'resolve',redirected), patch.object(benchmark_overhead.subprocess,'run',side_effect=AssertionError('No process before containment')):
                with self.assertRaisesRegex(ValueError,'escapes allowed root'):
                    benchmark_overhead.run(repetitions=1,runtime_mib=1)
            self.assertFalse(target.exists())

    def test_redirected_temporary_root_is_rejected_before_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve(); home=root/'home'; home.mkdir()
            outside=root/'outside'; outside.mkdir()
            target=home/'harness-runs'
            try:
                target.symlink_to(outside,target_is_directory=True)
            except OSError:
                self.skipTest('Directory symlinks unavailable')
            with patch.object(benchmark_overhead,'active_codex_home',return_value=home):
                with self.assertRaisesRegex(ValueError,'escapes allowed root'):
                    benchmark_overhead.run(repetitions=1,runtime_mib=1)
            self.assertEqual(list(outside.iterdir()),[])

    def test_invalid_workload_does_not_create_files(self):
        with self.assertRaises(ValueError):
            benchmark_overhead.run(repetitions=0,runtime_mib=1)
